"""FreeTime - 统一应用工具箱"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.cache import ResultCache
from app.config import settings
from app.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    DeleteResponse,
    StageTiming,
    StoredVideoList,
    StructuredInformation,
)
from app.pipeline import (
    PipelineError,
    _clean_source_article,
    _structured_reading_result,
    analyze,
)
from app.content import analyze_article_url, analyze_upload_bundle
from app.security import ALLOWED_HOST_SUFFIXES, UnsafeUrlError, resolve_content_input
from app.thumbnails import thumbnail_store


# ========== FreeTime 主应用 ==========

app = FastAPI(
    title="FreeTime",
    version="1.0.0",
    docs_url="/api/docs",
)

# 目录配置
BASE_DIR = Path(__file__).parent.parent.parent.parent  # FreeTime 根目录
PORTAL_DIR = BASE_DIR / "portal"
STATIC_DIR = Path(__file__).parent.parent / "static"
ROOT_STATIC_DIR = BASE_DIR / "static"
CHAOXING_DIR = BASE_DIR / "apps" / "chaoxing-auto"

# 挂载静态文件
app.mount("/static", StaticFiles(directory=ROOT_STATIC_DIR), name="root-static")
app.mount("/portal", StaticFiles(directory=PORTAL_DIR), name="portal")
app.mount("/extractor-static", StaticFiles(directory=STATIC_DIR), name="extractor-static")

# 超星 API 路由
chaoxing_router = APIRouter(prefix="/chaoxing/api", tags=["chaoxing"])


# ========== 缓存实例 ==========

cache = ResultCache(settings.cache_ttl_seconds)


# ========== 超星刷课助手状态 ==========

class ChaoxingState:
    def __init__(self):
        self.process: subprocess.Popen | None = None
        self.is_running = False
        self.log_lines: list[str] = []
        self.config = self._load_config()

    def _load_config(self) -> dict:
        config_path = CHAOXING_DIR / "config.json"
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
        return {
            "course": {"url": "", "name": ""},
            "playback": {"speed": 2.0, "poll_interval_seconds": 3},
        }

    def save_config(self, config: dict) -> None:
        config_path = CHAOXING_DIR / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config = config


chaoxing = ChaoxingState()


# ========== 工具函数 ==========

async def _stabilize_result_thumbnail(result: AnalyzeResponse) -> bool:
    original = result.metadata.thumbnail
    if not original or original.startswith("/api/thumbnails/"):
        return False
    result.metadata.thumbnail = await asyncio.to_thread(
        thumbnail_store.materialize,
        original,
        result.metadata.webpage_url,
    )
    return result.metadata.thumbnail != original


async def _stabilize_payload_thumbnail(payload: dict[str, object]) -> None:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return
    original = metadata.get("thumbnail")
    if not isinstance(original, str) or not original or original.startswith(
        "/api/thumbnails/"
    ):
        return
    metadata["thumbnail"] = await asyncio.to_thread(
        thumbnail_store.materialize,
        original,
        str(metadata.get("webpage_url") or ""),
    )


def _visible_extraction_milliseconds(result: AnalyzeResponse) -> int:
    return sum(max(0, int(item.milliseconds)) for item in result.timings)


def _finalize_request_timings(
    result: AnalyzeResponse,
    *,
    full_milliseconds: int,
    input_milliseconds: int,
    thumbnail_milliseconds: int,
) -> None:
    visible_core = _visible_extraction_milliseconds(result)
    full = max(0, int(full_milliseconds), visible_core)
    remaining = full - visible_core
    input_time = min(max(0, int(input_milliseconds)), remaining)
    remaining -= input_time
    thumbnail_time = min(max(0, int(thumbnail_milliseconds)), remaining)
    remaining -= thumbnail_time
    result.orchestration_timings = [
        StageTiming(name="输入解析与安全展开", milliseconds=input_time),
        StageTiming(name="封面获取与转存", milliseconds=thumbnail_time),
        StageTiming(name="其他编排开销", milliseconds=remaining),
    ]
    result.full_pipeline_milliseconds = full


def _ensure_request_timings(result: AnalyzeResponse) -> None:
    if result.orchestration_timings:
        return
    historical_full = result.full_pipeline_milliseconds or result.extraction_milliseconds
    _finalize_request_timings(
        result,
        full_milliseconds=historical_full,
        input_milliseconds=0,
        thumbnail_milliseconds=0,
    )


def _ensure_payload_request_timings(payload: dict[str, object]) -> None:
    try:
        result = AnalyzeResponse.model_validate(payload)
    except Exception:
        return
    _ensure_request_timings(result)
    payload.clear()
    payload.update(result.model_dump(mode="json"))


def _ensure_cleaned_article(payload: dict[str, object]) -> dict[str, object]:
    metadata = payload.get("metadata")
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    full_source_text = str(payload.get("full_source_text") or "")
    if not payload.get("structured_input_text") and full_source_text:
        structured_input = full_source_text[: settings.max_transcript_chars]
        payload["structured_input_text"] = structured_input
        payload["structured_input_chars"] = len(structured_input)
        payload["structured_input_truncated"] = (
            len(full_source_text) > settings.max_transcript_chars
        )
    if not payload.get("cleaned_article"):
        payload["cleaned_article"] = _clean_source_article(
            full_source_text,
            str(metadata_dict.get("title") or ""),
        )
    structured_payload = payload.get("structured_data")
    if isinstance(structured_payload, dict):
        structured = StructuredInformation.model_validate(structured_payload)
        summary, _, _ = _structured_reading_result(structured)
        payload["summary"] = summary
    return payload


# ========== 页面路由 ==========

@app.get("/", include_in_schema=False)
async def portal() -> HTMLResponse:
    """FreeTime 主门户入口"""
    content = (PORTAL_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/extractor", include_in_schema=False)
async def extractor_index() -> HTMLResponse:
    """流媒体内容提取器入口"""
    content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.get("/chaoxing", include_in_schema=False)
async def chaoxing_index() -> HTMLResponse:
    """超星刷课助手入口"""
    config = chaoxing.config
    speed_options = ""
    for s in [1, 1.5, 2, 3]:
        selected = "selected" if config["playback"]["speed"] == s else ""
        speed_options += f'<option value="{s}" {selected}>{s}x</option>'

    course_value = config['course']['url'] or config['course']['name']
    interval_value = config['playback']['poll_interval_seconds']

    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>超星刷课助手 - FreeTime</title>
  <style>
    :root {{
      color-scheme: light;
      --shell: #ffffff;
      --surface: #ffffff;
      --surface-muted: #f6f6f7;
      --ink: #1d1d1f;
      --ink-secondary: #5f5f63;
      --ink-tertiary: #8e8e93;
      --line: rgba(29, 29, 31, .09);
      --action: #1d1d1f;
      --action-hover: #000000;
      --danger: #9a5a53;
      --danger-soft: #f3e8e6;
      --radius-sm: 9px;
      --radius-md: 12px;
      --radius-lg: 18px;
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei UI", sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    html, body {{ width: 100%; height: 100%; overflow: hidden; }}
    body {{ font-family: var(--font-sans); background: var(--shell); color: var(--ink); }}

    /* 头部 */
    .cx-header {{
      display: flex;
      align-items: center;
      gap: 16px;
      padding: 10px 20px;
      background: rgba(255, 255, 255, 0.85);
      backdrop-filter: saturate(180%) blur(20px);
      border-bottom: 0.5px solid var(--line);
    }}
    .cx-back {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 12px;
      color: var(--ink-secondary);
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      border-radius: var(--radius-sm);
      transition: background 0.2s;
    }}
    .cx-back:hover {{ color: var(--ink); background: var(--surface-muted); }}
    .cx-back svg {{ width: 18px; height: 18px; }}
    .cx-logo {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding-right: 16px;
      border-right: 1px solid var(--line);
    }}
    .cx-logo-icon {{
      width: 32px; height: 32px;
      display: flex; align-items: center; justify-content: center;
      background: var(--action);
      border-radius: var(--radius-sm);
    }}
    .cx-logo-icon svg {{ width: 18px; height: 18px; color: white; }}
    .cx-title {{ font-size: 16px; font-weight: 600; color: var(--ink); }}

    /* 内容 */
    .cx-content {{
      max-width: 720px;
      margin: 0 auto;
      padding: 20px;
      height: calc(100vh - 53px);
      overflow-y: auto;
    }}

    /* 卡片 */
    .cx-card {{
      background: var(--surface);
      border-radius: var(--radius-lg);
      box-shadow: 0 1px 3px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04);
      margin-bottom: 16px;
      overflow: hidden;
    }}
    .cx-card-header {{
      padding: 16px 20px;
      border-bottom: 0.5px solid var(--line);
      font-size: 14px;
      font-weight: 600;
      color: var(--ink);
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .cx-card-body {{ padding: 20px; }}

    /* 状态 */
    .cx-status {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 16px;
      background: var(--surface-muted);
      border-radius: var(--radius-sm);
      margin-bottom: 16px;
    }}
    .cx-status-dot {{
      width: 10px; height: 10px;
      border-radius: 50%;
      background: var(--ink-tertiary);
    }}
    .cx-status-dot.active {{
      background: #34c759;
      animation: pulse 2s infinite;
    }}
    @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.5; }} }}
    .cx-status-text {{ font-size: 14px; color: var(--ink-secondary); }}

    /* 按钮 */
    .cx-btn-group {{ display: flex; gap: 12px; }}
    .cx-btn {{
      flex: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 10px 16px;
      border: none;
      border-radius: var(--radius-sm);
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.16s;
    }}
    .cx-btn svg {{ width: 16px; height: 16px; }}
    .cx-btn-primary {{ background: var(--action); color: white; }}
    .cx-btn-primary:hover {{ background: var(--action-hover); }}
    .cx-btn-danger {{ background: var(--danger); color: white; }}
    .cx-btn-danger:hover {{ background: #8a4a43; }}
    .cx-btn:disabled {{ opacity: .45; cursor: not-allowed; }}

    /* 表单 */
    .cx-form-group {{ margin-bottom: 16px; }}
    .cx-form-group:last-child {{ margin-bottom: 0; }}
    .cx-label {{ display: block; font-size: 13px; color: var(--ink-secondary); margin-bottom: 6px; }}
    .cx-input {{
      width: 100%;
      padding: 10px 14px;
      font-size: 14px;
      background: var(--surface-muted);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      color: var(--ink);
    }}
    .cx-input:focus {{ outline: none; border-color: var(--action); }}
    .cx-select {{
      width: 100%;
      padding: 10px 14px;
      font-size: 14px;
      background: var(--surface-muted);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      color: var(--ink);
      cursor: pointer;
    }}
    .cx-form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}

    /* 日志 */
    .cx-log {{
      background: #1d1d1f;
      color: #e5e5ea;
      border-radius: var(--radius-md);
      padding: 16px;
      max-height: 400px;
      overflow-y: auto;
      font-family: "SF Mono", Menlo, monospace;
      font-size: 12px;
      line-height: 1.6;
    }}
    .cx-log-line {{ padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,.06); }}
  </style>
</head>
<body>
  <header class="cx-header">
    <a href="/" class="cx-back">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M19 12H5M12 19l-7-7 7-7"/>
      </svg>
      <span>FreeTime</span>
    </a>
    <div class="cx-logo">
      <div class="cx-logo-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/>
          <path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>
        </svg>
      </div>
      <span class="cx-title">超星刷课助手</span>
    </div>
  </header>

  <div class="cx-content">
    <!-- 运行状态 -->
    <div class="cx-card">
      <div class="cx-card-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"/>
          <polyline points="12 6 12 12 16 14"/>
        </svg>
        运行状态
      </div>
      <div class="cx-card-body">
        <div class="cx-status">
          <div class="cx-status-dot" id="status-dot"></div>
          <span class="cx-status-text" id="status-text">未运行</span>
        </div>
        <div class="cx-btn-group">
          <button class="cx-btn cx-btn-primary" id="start-btn" onclick="startTask()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polygon points="5 3 19 12 5 21 5 3"/>
            </svg>
            启动刷课
          </button>
          <button class="cx-btn cx-btn-danger" id="stop-btn" onclick="stopTask()" disabled>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="6" y="4" width="4" height="16"/>
              <rect x="14" y="4" width="4" height="16"/>
            </svg>
            停止
          </button>
        </div>
      </div>
    </div>

    <!-- 课程配置 -->
    <div class="cx-card">
      <div class="cx-card-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="3"/>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/>
        </svg>
        课程配置
      </div>
      <div class="cx-card-body">
        <div class="cx-form-group">
          <label class="cx-label">课程链接或名称</label>
          <input type="text" class="cx-input" id="course-input" placeholder="粘贴课程链接或输入课程名称" value="{course_value}">
        </div>
        <div class="cx-form-row">
          <div class="cx-form-group">
            <label class="cx-label">播放倍速</label>
            <select class="cx-select" id="speed-select">{speed_options}</select>
          </div>
          <div class="cx-form-group">
            <label class="cx-label">轮询间隔 (秒)</label>
            <input type="number" class="cx-input" id="interval-input" min="1" max="30" value="{interval_value}">
          </div>
        </div>
        <div class="cx-btn-group" style="margin-top: 16px;">
          <button class="cx-btn cx-btn-primary" onclick="saveConfig()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
              <polyline points="17 21 17 13 7 13 7 21"/>
              <polyline points="7 3 7 8 15 8"/>
            </svg>
            保存配置
          </button>
        </div>
      </div>
    </div>

    <!-- 运行日志 -->
    <div class="cx-card">
      <div class="cx-card-header">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
          <line x1="16" y1="13" x2="8" y2="13"/>
          <line x1="16" y1="17" x2="8" y2="17"/>
        </svg>
        运行日志
      </div>
      <div class="cx-card-body" style="padding: 0;">
        <div class="cx-log" id="log-container">
          <div class="cx-log-line">等待启动...</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    let isRunning = false;
    let pollTimer = null;

    function startTask() {{
      fetch('/chaoxing/api/start', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
          if (d.status === 'ok') {{
            updateStatus(true);
            startPolling();
          }}
        }});
    }}

    function stopTask() {{
      fetch('/chaoxing/api/stop', {{ method: 'POST' }})
        .then(r => r.json())
        .then(d => {{
          if (d.status === 'ok') {{
            updateStatus(false);
            stopPolling();
          }}
        }});
    }}

    function saveConfig() {{
      const v = document.getElementById('course-input').value;
      const d = {{
        speed: parseFloat(document.getElementById('speed-select').value),
        poll_interval: parseInt(document.getElementById('interval-input').value)
      }};
      if (v.startsWith('http')) d.course_url = v; else d.course_name = v;
      fetch('/chaoxing/api/config', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(d)
      }}).then(() => alert('配置已保存'));
    }}

    function updateStatus(r) {{
      isRunning = r;
      document.getElementById('status-dot').className = 'cx-status-dot' + (r ? ' active' : '');
      document.getElementById('status-text').textContent = r ? '运行中...' : '未运行';
      document.getElementById('start-btn').disabled = r;
      document.getElementById('stop-btn').disabled = !r;
    }}

    function startPolling() {{
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = setInterval(fetchStatus, 2000);
    }}

    function stopPolling() {{
      if (pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
    }}

    function fetchStatus() {{
      fetch('/chaoxing/api/status')
        .then(r => r.json())
        .then(d => {{
          updateStatus(d.is_running);
          document.getElementById('log-container').innerHTML =
            d.log_lines.map(l => '<div class="cx-log-line">' + l + '</div>').join('');
        }});
    }}

    fetchStatus();
  </script>
</body>
</html>"""
    return HTMLResponse(content=html)


# ========== 超星刷课助手 API ==========

@chaoxing_router.get("/config")
async def chaoxing_config_get():
    return chaoxing.config


@chaoxing_router.post("/config")
async def chaoxing_config_update(data: dict[str, Any]):
    config = chaoxing.config
    if "course_url" in data:
        config["course"]["url"] = data["course_url"]
    if "course_name" in data:
        config["course"]["name"] = data["course_name"]
    if "speed" in data:
        config["playback"]["speed"] = float(data["speed"])
    if "poll_interval" in data:
        config["playback"]["poll_interval_seconds"] = int(data["poll_interval"])
    chaoxing.save_config(config)
    return {"status": "ok"}


@chaoxing_router.post("/start")
async def chaoxing_start():
    if chaoxing.is_running:
        raise HTTPException(status_code=400, detail="任务已在运行中")
    chaoxing.log_lines = []
    chaoxing.is_running = True

    def run_task():
        try:
            script_path = CHAOXING_DIR / "main.py"
            chaoxing.process = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for line in chaoxing.process.stdout:
                chaoxing.log_lines.append(line.strip())
                if len(chaoxing.log_lines) > 1000:
                    chaoxing.log_lines.pop(0)
            chaoxing.process.wait()
        except Exception as e:
            chaoxing.log_lines.append(f"错误: {e}")
        finally:
            chaoxing.is_running = False
            chaoxing.process = None

    thread = threading.Thread(target=run_task, daemon=True)
    thread.start()
    return {"status": "ok", "message": "任务已启动"}


@chaoxing_router.post("/stop")
async def chaoxing_stop():
    if not chaoxing.is_running or chaoxing.process is None:
        raise HTTPException(status_code=400, detail="没有运行中的任务")
    try:
        chaoxing.process.terminate()
        chaoxing.process.wait(timeout=5)
    except Exception:
        chaoxing.process.kill()
    chaoxing.is_running = False
    chaoxing.process = None
    chaoxing.log_lines.append("任务已手动停止")
    return {"status": "ok", "message": "任务已停止"}


@chaoxing_router.get("/status")
async def chaoxing_status():
    return {
        "is_running": chaoxing.is_running,
        "log_lines": chaoxing.log_lines[-100:],
    }


# 注册超星路由
app.include_router(chaoxing_router)


# ========== 流媒体内容提取器 API ==========

@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "apps": ["extractor", "chaoxing"],
        "mimo_configured": bool(settings.mimo_api_key),
        "supported_platforms": [
            "抖音", "哔哩哔哩", "YouTube", "快手", "微博", "小红书", "视频号"
        ],
    }


@app.get("/api/thumbnails/{key}", include_in_schema=False)
async def thumbnail(key: str) -> FileResponse:
    path = thumbnail_store.get_path(key)
    if not path:
        raise HTTPException(status_code=404, detail="封面不存在或已过期")
    return FileResponse(
        path,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_content(request: AnalyzeRequest) -> AnalyzeResponse:
    request_started = time.perf_counter()
    input_started = time.perf_counter()
    try:
        url = await asyncio.to_thread(
            resolve_content_input,
            request.url,
            platform_only=request.input_kind == "platform",
        )
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    input_milliseconds = round((time.perf_counter() - input_started) * 1000)

    cache_key = cache.key(f"{request.input_kind}:{url}", request.mode)
    if not request.refresh:
        cached = cache.get(cache_key)
        if cached:
            cached["cached"] = True
            _ensure_cleaned_article(cached)
            cached_result = AnalyzeResponse.model_validate(cached)
            _ensure_request_timings(cached_result)
            thumbnail_changed = await _stabilize_result_thumbnail(cached_result)
            if not cached_result.extraction_milliseconds:
                cached_result.extraction_milliseconds = sum(
                    item.milliseconds for item in cached_result.timings
                )
            if not cached_result.full_pipeline_milliseconds:
                cached_result.full_pipeline_milliseconds = cached_result.extraction_milliseconds
            if thumbnail_changed:
                cache.set(cache_key, cached_result.model_dump(mode="json"))
            return cached_result

    try:
        hostname = (urlparse(url).hostname or "").lower()
        is_platform = any(
            hostname == suffix or hostname.endswith(f".{suffix}")
            for suffix in ALLOWED_HOST_SUFFIXES
        )
        if request.input_kind == "article" or not is_platform:
            result = await analyze_article_url(url)
        else:
            try:
                result = await analyze(url, request.mode)
            except PipelineError:
                if request.input_kind == "platform" or hostname.endswith(
                    ("kuaishou.com", "gifshow.com")
                ):
                    raise
                result = await analyze_article_url(url)
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    thumbnail_started = time.perf_counter()
    await _stabilize_result_thumbnail(result)
    thumbnail_milliseconds = round(
        (time.perf_counter() - thumbnail_started) * 1000
    )

    _finalize_request_timings(
        result,
        full_milliseconds=round(
            (time.perf_counter() - request_started) * 1000
        ),
        input_milliseconds=input_milliseconds,
        thumbnail_milliseconds=thumbnail_milliseconds,
    )
    cache.set(cache_key, result.model_dump(mode="json"))
    return result


@app.post("/api/analyze/upload", response_model=AnalyzeResponse)
async def analyze_uploaded_content(
    title: str = Form(default="多模态内容提取", max_length=200),
    text: str = Form(default="", max_length=50_000),
    files: list[UploadFile] = File(default=[]),
) -> AnalyzeResponse:
    request_started = time.perf_counter()
    try:
        result = await analyze_upload_bundle(title.strip(), text, files)
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"多模态材料解析失败：{exc}"
        ) from exc
    thumbnail_started = time.perf_counter()
    await _stabilize_result_thumbnail(result)
    thumbnail_milliseconds = round(
        (time.perf_counter() - thumbnail_started) * 1000
    )
    _finalize_request_timings(
        result,
        full_milliseconds=round(
            (time.perf_counter() - request_started) * 1000
        ),
        input_milliseconds=0,
        thumbnail_milliseconds=thumbnail_milliseconds,
    )
    return result


@app.get("/api/videos", response_model=StoredVideoList)
async def list_videos(limit: int = 100) -> StoredVideoList:
    safe_limit = min(max(limit, 1), 500)
    items = cache.list(safe_limit)
    for item in items:
        result = item.get("result")
        if isinstance(result, dict):
            _ensure_cleaned_article(result)
            _ensure_payload_request_timings(result)
    await asyncio.gather(*(
        _stabilize_payload_thumbnail(item["result"])
        for item in items
        if isinstance(item.get("result"), dict)
    ))
    return StoredVideoList.model_validate({"items": items, "total": len(items)})


@app.delete("/api/videos/{cache_key}", response_model=DeleteResponse)
async def delete_video(cache_key: str) -> DeleteResponse:
    deleted = cache.delete(cache_key)
    if not deleted:
        raise HTTPException(status_code=404, detail="缓存记录不存在")
    return DeleteResponse(deleted=deleted)


@app.delete("/api/videos", response_model=DeleteResponse)
async def clear_videos() -> DeleteResponse:
    return DeleteResponse(deleted=cache.clear())
