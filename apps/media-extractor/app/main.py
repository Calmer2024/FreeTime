"""FreeTime - 统一应用工具箱"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, UploadFile, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.cache import ResultCache
from app.config import settings
from app.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    DeleteResponse,
    StageTiming,
    StoredVideoList,
)
from app.pipeline import (
    PipelineError,
    _clean_source_article,
    analyze,
)
from app.content import analyze_article_url, analyze_upload_bundle
from app.security import (
    ALLOWED_HOST_SUFFIXES,
    BROWSER_USER_AGENT,
    UnsafeUrlError,
    resolve_content_input,
    validate_public_url,
)
from app.thumbnails import thumbnail_store
from app.storage import data_root


# ========== FreeTime 主应用 ==========

app = FastAPI(
    title="FreeTime",
    version="1.0.0",
    docs_url="/api/docs",
)

# 目录配置
if getattr(sys, "frozen", False):
    # PyInstaller 打包模式: _internal/ 下有 portal/, static/, apps/
    # 注意: --contents-directory . 会把数据文件放在 _internal/ 而不是 exe 同级目录
    BASE_DIR = Path(sys._MEIPASS)
else:
    BASE_DIR = Path(__file__).parent.parent.parent.parent  # FreeTime 根目录

PORTAL_DIR = BASE_DIR / "portal"
# extractor 前端文件 (index.html, app.js, app.css)
STATIC_DIR = BASE_DIR / "apps" / "media-extractor" / "static"
ROOT_STATIC_DIR = BASE_DIR / "static"
CHAOXING_DIR = BASE_DIR / "apps" / "chaoxing-auto"
DATA_DIR = data_root()
TASKS_FILE = DATA_DIR / "task-jobs.json"
TASKS_LOCK = threading.RLock()
DOWNLOAD_SETTINGS_FILE = DATA_DIR / "download-settings.json"
REMOTE_IMAGE_CACHE = DATA_DIR / ".cache" / "remote-images"
REMOTE_IMAGE_CACHE.mkdir(parents=True, exist_ok=True)


def _load_task_jobs() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


TASK_JOBS: dict[str, dict[str, Any]] = _load_task_jobs()


def _write_task_jobs() -> None:
    with TASKS_LOCK:
        trimmed = dict(sorted(
            TASK_JOBS.items(),
            key=lambda item: float(item[1].get("updated_at") or 0),
            reverse=True,
        )[:200])
        TASK_JOBS.clear()
        TASK_JOBS.update(trimmed)
        temp = TASKS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(TASK_JOBS, ensure_ascii=False), encoding="utf-8")
        temp.replace(TASKS_FILE)


def _update_task_job(task_id: str | None, **values: Any) -> None:
    if not task_id:
        return
    with TASKS_LOCK:
        current = TASK_JOBS.setdefault(task_id, {
            "id": task_id,
            "status": "pending",
            "progress": 0,
            "started_at": time.time(),
            "updated_at": time.time(),
        })
        current.update(values)
        current["updated_at"] = time.time()
        _write_task_jobs()


def _download_directory() -> Path:
    default = Path.home() / "Downloads" / "FreeTime"
    try:
        payload = json.loads(DOWNLOAD_SETTINGS_FILE.read_text(encoding="utf-8"))
        configured = str(payload.get("directory") or "").strip()
        target = Path(configured).expanduser() if configured else default
    except (OSError, json.JSONDecodeError):
        target = default
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def _project_download_directory(project_name: str | None = None) -> Path:
    root = _download_directory()
    label = re.sub(
        r'[<>:"：/\\|?*\x00-\x1f]+',
        "_",
        str(project_name or "未命名项目"),
    ).strip(" .")
    label = re.sub(r"\s+", " ", label).strip()[:80] or "未命名项目"
    target = root / label
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def _safe_download_name(filename: str, fallback: str = "resource") -> str:
    cleaned = re.sub(
        r'[<>:"：/\\|?*\x00-\x1f]+', "_", str(filename)
    ).strip(" .")
    return cleaned or fallback


def _resource_headers(url: str) -> dict[str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    referer = (
        "https://www.xiaohongshu.com/"
        if host.endswith("xhscdn.com")
        else "https://www.douyin.com/"
        if host.endswith(("douyinpic.com", "douyinvod.com"))
        else f"{parsed.scheme}://{parsed.netloc}/"
    )
    return {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        "Referer": referer,
    }

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
        config_path = DATA_DIR / "chaoxing" / "config.json"
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
        return {
            "course": {"url": "", "name": ""},
            "playback": {"speed": 2.0, "poll_interval_seconds": 3},
        }

    def save_config(self, config: dict) -> None:
        config_path = DATA_DIR / "chaoxing" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
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
    if not payload.get("cleaned_article"):
        payload["cleaned_article"] = _clean_source_article(
            full_source_text,
            str(metadata_dict.get("title") or ""),
        )
    return payload


# ========== 页面路由 ==========

SETTINGS_FILE = DATA_DIR / "settings.json"


def load_settings() -> dict:
    """加载全局设置"""
    if SETTINGS_FILE.exists():
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    return {}


def save_settings(settings: dict) -> None:
    """保存全局设置"""
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/", include_in_schema=False)
async def portal() -> HTMLResponse:
    """FreeTime 主门户入口"""
    content = (PORTAL_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})


@app.get("/api/settings")
async def get_settings():
    """获取全局设置"""
    return load_settings()


@app.post("/api/settings")
async def update_settings(data: dict):
    """更新全局设置"""
    save_settings(data)
    return {"status": "ok"}


@app.get("/extractor", include_in_schema=False)
async def extractor_index() -> HTMLResponse:
    """流媒体内容提取器入口"""
    content = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(content=content, headers={"Cache-Control": "no-store"})


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
  <link rel="icon" type="image/png" href="/static/icon_freetime.png">
  <style>
    :root {{
      color-scheme: light;
      font-size: 14px;
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
      --scrollbar-thumb: rgba(82, 84, 88, .24);
      --scrollbar-thumb-hover: rgba(52, 54, 58, .42);
      --radius-sm: 9px;
      --radius-md: 12px;
      --radius-lg: 18px;
      --font-sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "PingFang SC", "Microsoft YaHei UI", sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    * {{ scrollbar-width: thin; scrollbar-color: var(--scrollbar-thumb) transparent; }}
    *::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    *::-webkit-scrollbar-track {{ background: transparent; }}
    *::-webkit-scrollbar-thumb {{
      min-height: 44px; border: 3px solid transparent; border-radius: 999px;
      background: var(--scrollbar-thumb); background-clip: content-box;
    }}
    *::-webkit-scrollbar-thumb:hover {{ background-color: var(--scrollbar-thumb-hover); }}
    *::-webkit-scrollbar-corner {{ background: transparent; }}
    html, body {{ width: 100%; height: 100%; overflow: hidden; }}
    body {{
      display: flex; flex-direction: column;
      font-family: var(--font-sans); background: var(--shell); color: var(--ink);
    }}

    .cx-header {{
      flex: 0 0 auto;
      display: flex; align-items: center; gap: 16px;
      padding: 10px 20px;
      background: rgba(255,255,255,0.85);
      backdrop-filter: saturate(180%) blur(20px);
    }}
    .cx-back {{
      display: inline-flex; align-items: center; gap: 6px;
      padding: 8px 12px; color: var(--ink-secondary);
      text-decoration: none; font-size: 14px; font-weight: 500;
      border-radius: var(--radius-sm); transition: background 0.2s;
    }}
    .cx-back:hover {{ color: var(--ink); background: var(--surface-muted); }}
    .cx-back svg {{ width: 18px; height: 18px; }}
    .cx-logo {{
      min-width: 0; display: flex; align-items: center; gap: 10px;
      padding-right: 16px; border-right: 1px solid var(--line);
    }}
    .cx-logo-icon {{ width: 32px; height: 32px; object-fit: contain; }}
    .cx-title {{
      min-width: 0; overflow: hidden; font-size: 16px; font-weight: 600;
      color: var(--ink); text-overflow: ellipsis; white-space: nowrap;
    }}

    .cx-content {{
      flex: 1 1 auto; min-height: 0;
      width: min(900px, 100% - 40px);
      margin: 0 auto; padding: 20px;
      overflow-y: auto;
    }}

    .cx-card {{
      background: var(--surface); border-radius: var(--radius-lg);
      box-shadow: 0 1px 3px rgba(0,0,0,.04), 0 4px 12px rgba(0,0,0,.04);
      margin-bottom: 16px; overflow: hidden;
    }}
    .cx-card-header {{
      padding: 16px 20px; border-bottom: 0.5px solid var(--line);
      font-size: 14px; font-weight: 600; color: var(--ink);
      display: flex; align-items: center; gap: 8px;
    }}
    .cx-card-body {{ padding: 20px; }}

    .cx-status {{
      display: flex; align-items: center; gap: 12px;
      padding: 12px 16px; background: var(--surface-muted);
      border-radius: var(--radius-sm); margin-bottom: 16px;
    }}
    .cx-status-dot {{
      width: 10px; height: 10px; border-radius: 50%;
      background: var(--ink-tertiary); flex-shrink: 0;
    }}
    .cx-status-dot.active {{ background: #34c759; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1; }} 50% {{ opacity:.5; }} }}
    .cx-status-text {{ font-size: 14px; color: var(--ink-secondary); }}

    .cx-btn-group {{ display: flex; gap: 12px; }}
    .cx-btn {{
      flex: 1; display: inline-flex; align-items: center; justify-content: center;
      gap: 8px; padding: 10px 16px; border: none;
      border-radius: var(--radius-sm); font-size: 14px;
      font-weight: 500; cursor: pointer; transition: all 0.16s;
    }}
    .cx-btn svg {{ width: 16px; height: 16px; }}
    .cx-btn-primary {{ background: var(--action); color: white; }}
    .cx-btn-primary:hover {{ background: var(--action-hover); }}
    .cx-btn-danger {{ background: var(--danger); color: white; }}
    .cx-btn-danger:hover {{ background: #8a4a43; }}
    .cx-btn:disabled {{ opacity: .45; cursor: not-allowed; }}

    .cx-form-group {{ margin-bottom: 16px; }}
    .cx-form-group:last-child {{ margin-bottom: 0; }}
    .cx-label {{ display: block; font-size: 13px; color: var(--ink-secondary); margin-bottom: 6px; }}
    .cx-input {{
      min-width: 0; width: 100%; padding: 10px 14px; font-size: 14px;
      background: var(--surface-muted); border: 1px solid var(--line);
      border-radius: var(--radius-sm); color: var(--ink);
    }}
    .cx-input:focus {{ outline: none; border-color: var(--action); }}
    .cx-select {{
      width: 100%; padding: 10px 14px; font-size: 14px;
      background: var(--surface-muted); border: 1px solid var(--line);
      border-radius: var(--radius-sm); color: var(--ink); cursor: pointer;
    }}
    .cx-form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}

    .cx-log {{
      background: #1d1d1f; color: #e5e5ea;
      border-radius: var(--radius-md); padding: 16px;
      max-height: 400px; overflow-y: auto;
      font-family: "SF Mono", Menlo, monospace;
      font-size: 12px; line-height: 1.6;
      scrollbar-color: rgba(255,255,255,.28) transparent;
    }}
    .cx-log::-webkit-scrollbar-thumb {{ background-color: rgba(255,255,255,.28); }}
    .cx-log::-webkit-scrollbar-thumb:hover {{ background-color: rgba(255,255,255,.46); }}
    .cx-log-line {{ padding: 3px 0; border-bottom: 1px solid rgba(255,255,255,.06); }}

    @media (min-width: 1800px) and (min-height: 900px) {{
      .cx-content {{ width: min(1120px, 100% - 96px); padding-top: 32px; }}
      .cx-card-body {{ padding: 26px; }}
      .cx-log {{ max-height: min(46vh, 520px); }}
    }}

    @media (max-width: 800px) {{
      .cx-header {{ gap: 8px; padding: 8px 14px; }}
      .cx-back {{ padding: 6px 8px; font-size: 13px; }}
      .cx-logo {{ gap: 6px; padding-right: 10px; }}
      .cx-logo-icon {{ width: 24px; height: 24px; }}
      .cx-title {{ font-size: 14px; }}
      .cx-content {{ width: min(100% - 24px, 900px); padding: 12px 0 20px; }}
      .cx-card-body {{ padding: 14px; }}
      .cx-form-row {{ grid-template-columns: 1fr; gap: 12px; }}
      .cx-btn-group {{ flex-direction: column; }}
    }}

    @media (max-height: 620px) {{
      .cx-content {{ padding-top: 10px; padding-bottom: 10px; }}
      .cx-card {{ margin-bottom: 12px; }}
      .cx-card-header {{ padding-top: 12px; padding-bottom: 12px; }}
      .cx-log {{ max-height: 220px; }}
    }}
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
      <img src="/static/icon_chaoxing.png" alt="超星刷课助手" class="cx-logo-icon" width="32" height="32">
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


def _find_python_for_subprocess() -> str:
    """在打包模式下找到可用的 Python 解释器用于子进程"""
    import shutil
    import platform

    # 优先使用系统 PATH 中的 python
    system_python = shutil.which("python") or shutil.which("python3")
    if system_python:
        return system_python

    # Windows: 尝试常见安装路径
    if platform.system() == "Windows":
        for candidate in [
            r"C:\Python312\python.exe",
            r"C:\Python311\python.exe",
            r"C:\Python310\python.exe",
        ]:
            if os.path.exists(candidate):
                return candidate

    return "python"


@chaoxing_router.post("/start")
async def chaoxing_start():
    if chaoxing.is_running:
        raise HTTPException(status_code=400, detail="任务已在运行中")
    chaoxing.log_lines = []
    chaoxing.is_running = True

    def run_task():
        try:
            script_path = CHAOXING_DIR / "main.py"
            # 打包后 sys.executable 是 freetime-backend.exe，不是 Python
            # 需要找到可用的 Python 解释器
            if getattr(sys, "frozen", False):
                python_exe = _find_python_for_subprocess()
            else:
                python_exe = sys.executable
            chaoxing.process = subprocess.Popen(
                [python_exe, str(script_path)],
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
        "accepted_inputs": ["文章 URL", "视频 URL", "分享文本", "直接输入文字", "本地上传"],
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


@app.get("/api/download/thumbnail/{key}", include_in_schema=False)
async def download_thumbnail(
    key: str,
    filename: str = Query(default="thumbnail.jpg"),
) -> FileResponse:
    path = thumbnail_store.get_path(key)
    if not path:
        raise HTTPException(status_code=404, detail="封面不存在或已过期")
    safe_name = Path(filename).name or f"thumbnail{path.suffix or '.jpg'}"
    if not Path(safe_name).suffix:
        safe_name += path.suffix or ".jpg"
    return FileResponse(
        path,
        filename=safe_name,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/download/save-thumbnail/{key}", include_in_schema=False)
async def save_thumbnail_to_project(
    key: str, payload: dict[str, Any]
) -> dict[str, Any]:
    source = thumbnail_store.get_path(key)
    if not source:
        raise HTTPException(status_code=404, detail="封面不存在或已过期")
    filename = _safe_download_name(
        str(payload.get("filename") or f"thumbnail{source.suffix or '.jpg'}"),
        fallback=f"thumbnail{source.suffix or '.jpg'}",
    )
    if not Path(filename).suffix:
        filename += source.suffix or ".jpg"
    target_dir = _project_download_directory(
        str(payload.get("project_name") or "未命名项目")
    )
    target = target_dir / filename
    index = 2
    while target.exists():
        target = target_dir / f"{Path(filename).stem}-{index}{Path(filename).suffix}"
        index += 1
    target.write_bytes(source.read_bytes())
    return {"saved": 1, "path": str(target), "directory": str(target_dir)}


@app.get("/api/remote-image", include_in_schema=False)
async def remote_image(url: str = Query(..., description="远程图片 URL")):
    """通过后端携带站点 Referer 代理图片，供浏览器预览。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="仅支持 http/https 协议")
    try:
        validate_public_url(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"资源地址不可访问: {exc}") from exc
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_path = REMOTE_IMAGE_CACHE / f"{cache_key}.img"
    meta_path = REMOTE_IMAGE_CACHE / f"{cache_key}.json"
    if cache_path.is_file() and meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            return FileResponse(
                cache_path,
                media_type=str(meta.get("content_type") or "image/jpeg"),
                headers={"Cache-Control": "public, max-age=86400, immutable"},
            )
        except (OSError, json.JSONDecodeError):
            pass
    try:
        async with httpx.AsyncClient(
            timeout=45.0, follow_redirects=True, headers=_resource_headers(url)
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=415, detail="远程资源不是图片")
        if len(response.content) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="图片过大")
        temp_path = cache_path.with_suffix(".tmp")
        temp_path.write_bytes(response.content)
        temp_path.replace(cache_path)
        meta_path.write_text(
            json.dumps({"content_type": content_type}), encoding="utf-8"
        )
        return FileResponse(
            cache_path,
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"图片加载失败: {exc}") from exc


@app.get("/api/download-settings", include_in_schema=False)
async def get_download_settings() -> dict[str, str]:
    return {"directory": str(_download_directory())}


@app.put("/api/download-settings", include_in_schema=False)
async def set_download_settings(payload: dict[str, Any]) -> dict[str, str]:
    raw = str(payload.get("directory") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="下载目录不能为空")
    target = Path(raw).expanduser()
    try:
        target.mkdir(parents=True, exist_ok=True)
        target = target.resolve()
        probe = target / ".freetime-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"目录不可写: {exc}") from exc
    DOWNLOAD_SETTINGS_FILE.write_text(
        json.dumps({"directory": str(target)}, ensure_ascii=False), encoding="utf-8"
    )
    return {"directory": str(target)}


async def _save_remote_resource(
    url: str, filename: str, project_name: str | None = None
) -> Path:
    validate_public_url(url)
    target_dir = _project_download_directory(project_name)
    safe_name = _safe_download_name(filename)
    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cached_path = REMOTE_IMAGE_CACHE / f"{cache_key}.img"
    cached_meta = REMOTE_IMAGE_CACHE / f"{cache_key}.json"
    content_type = ""
    content: bytes
    if cached_path.is_file():
        content = cached_path.read_bytes()
        try:
            content_type = str(
                json.loads(cached_meta.read_text(encoding="utf-8")).get("content_type") or ""
            )
        except (OSError, json.JSONDecodeError):
            content_type = ""
    else:
        async with httpx.AsyncClient(
            timeout=90.0, follow_redirects=True, headers=_resource_headers(url)
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
        content = response.content
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
    if "." not in safe_name:
        extension = {
            "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
            "image/gif": ".gif", "video/mp4": ".mp4", "application/pdf": ".pdf",
        }.get(content_type, ".bin")
        safe_name += extension
    target = target_dir / safe_name
    stem, suffix = target.stem, target.suffix
    index = 2
    while target.exists():
        target = target_dir / f"{stem}-{index}{suffix}"
        index += 1
    target.write_bytes(content)
    return target


@app.post("/api/download/save", include_in_schema=False)
async def save_resource(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        path = await _save_remote_resource(
            str(payload.get("url") or ""),
            str(payload.get("filename") or "resource"),
            str(payload.get("project_name") or ""),
        )
        return {"saved": 1, "path": str(path), "directory": str(path.parent)}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"保存资源失败: {exc}") from exc


@app.post("/api/download/save-text", include_in_schema=False)
async def save_text_resource(payload: dict[str, Any]) -> dict[str, Any]:
    content = str(payload.get("content") or "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="没有可导出的全文")
    if len(content.encode("utf-8")) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="导出全文超过 10 MB")
    filename = _safe_download_name(
        str(payload.get("filename") or "完整全文.md"),
        fallback="完整全文.md",
    )
    if Path(filename).suffix.lower() != ".md":
        filename = f"{Path(filename).stem or '完整全文'}.md"
    target_dir = _project_download_directory(
        str(payload.get("project_name") or "未命名项目")
    )
    target = target_dir / filename
    index = 2
    while target.exists():
        target = target_dir / f"{Path(filename).stem}-{index}.md"
        index += 1
    target.write_text(content, encoding="utf-8")
    return {"saved": 1, "path": str(target), "directory": str(target_dir)}


@app.post("/api/download/batch", include_in_schema=False)
async def save_resource_batch(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="没有可下载资源")
    semaphore = asyncio.Semaphore(4)
    async def save_one(item: dict[str, Any]):
        async with semaphore:
            return await _save_remote_resource(
                str(item.get("url") or ""),
                str(item.get("filename") or "resource"),
                str(payload.get("project_name") or ""),
            )
    outputs = await asyncio.gather(
        *(save_one(item) for item in items[:100]), return_exceptions=True
    )
    paths = [str(item) for item in outputs if isinstance(item, Path)]
    errors = [str(item) for item in outputs if isinstance(item, BaseException)]
    return {
        "saved": len(paths), "failed": len(errors), "paths": paths,
        "directory": str(_project_download_directory(str(payload.get("project_name") or ""))),
        "errors": errors[:5],
    }


@app.post("/api/download/open-folder", include_in_schema=False)
async def open_download_folder() -> dict[str, str]:
    directory = _download_directory()
    if os.name == "nt":
        os.startfile(directory)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(directory)])
    return {"directory": str(directory)}


@app.get("/api/download", include_in_schema=False)
async def download_resource(
    url: str = Query(..., description="资源 URL"),
    filename: str = Query(default="download", description="下载文件名"),
):
    """代理下载外部资源，支持视频、图片等文件"""
    # 安全验证：只允许 http/https URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="仅支持 http/https 协议")
    try:
        validate_public_url(url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"资源地址不可下载: {exc}") from exc

    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                "Referer": (
                    "https://www.xiaohongshu.com/"
                    if (parsed.hostname or "").lower().endswith("xhscdn.com")
                    else f"{parsed.scheme}://{parsed.netloc}/"
                ),
            },
        ) as client:
            response = await client.get(url)
            response.raise_for_status()

            # 检测 Content-Type 以确定文件扩展名
            content_type = response.headers.get("content-type", "")
            if "." not in filename:
                ext_map = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                    "video/mp4": ".mp4",
                    "video/webm": ".webm",
                }
                for mime, ext in ext_map.items():
                    if mime in content_type:
                        filename += ext
                        break
                else:
                    filename += ".bin"

            ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "download"
            disposition = (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename)}"
            )
            return StreamingResponse(
                iter([response.content]),
                media_type=content_type or "application/octet-stream",
                headers={
                    "Content-Disposition": disposition,
                    "Content-Length": str(len(response.content)),
                },
            )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"远程服务器返回错误: {exc.response.status_code}"
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"下载资源失败: {str(exc)}"
        ) from exc


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_content(request: AnalyzeRequest) -> AnalyzeResponse:
    request_started = time.perf_counter()
    _update_task_job(
        request.task_id,
        status="running",
        progress=8,
        route={"kind": "url", "url": request.url},
        error="",
    )
    input_started = time.perf_counter()
    try:
        url = await asyncio.to_thread(
            resolve_content_input,
            request.url,
            platform_only=request.input_kind == "platform",
        )
    except UnsafeUrlError as exc:
        _update_task_job(request.task_id, status="error", progress=100, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    input_milliseconds = round((time.perf_counter() - input_started) * 1000)
    _update_task_job(request.task_id, progress=20)

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
            _update_task_job(
                request.task_id,
                status="success",
                progress=100,
                result=cached_result.model_dump(mode="json"),
            )
            return cached_result

    try:
        _update_task_job(request.task_id, progress=32)
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
        _update_task_job(request.task_id, status="error", progress=100, error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        _update_task_job(request.task_id, status="error", progress=100, error=str(exc))
        raise

    _update_task_job(request.task_id, progress=86)
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
    _update_task_job(
        request.task_id,
        status="success",
        progress=100,
        result=result.model_dump(mode="json"),
    )
    return result


@app.post("/api/analyze/upload", response_model=AnalyzeResponse)
async def analyze_uploaded_content(
    title: str = Form(default="多模态内容提取", max_length=200),
    text: str = Form(default="", max_length=50_000),
    files: list[UploadFile] = File(default=[]),
    task_id: str = Form(default="", max_length=100),
) -> AnalyzeResponse:
    request_started = time.perf_counter()
    _update_task_job(
        task_id or None,
        status="running",
        progress=15,
        route={"kind": "text", "text": text[:500]},
        error="",
    )
    try:
        result = await analyze_upload_bundle(title.strip(), text, files)
    except PipelineError as exc:
        _update_task_job(task_id or None, status="error", progress=100, error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        _update_task_job(task_id or None, status="error", progress=100, error=str(exc))
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
    _update_task_job(
        task_id or None,
        status="success",
        progress=100,
        result=result.model_dump(mode="json"),
    )
    return result


@app.get("/api/tasks", include_in_schema=False)
async def list_task_jobs() -> dict[str, Any]:
    with TASKS_LOCK:
        return {"items": list(TASK_JOBS.values())}


@app.get("/api/tasks/{task_id}", include_in_schema=False)
async def get_task_job(task_id: str) -> dict[str, Any]:
    with TASKS_LOCK:
        item = TASK_JOBS.get(task_id)
        if not item:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(item)


@app.delete("/api/tasks/completed", include_in_schema=False)
async def delete_completed_task_jobs() -> dict[str, Any]:
    with TASKS_LOCK:
        task_ids = sorted(
            task_id
            for task_id, item in TASK_JOBS.items()
            if item.get("status") in {"success", "error"}
        )
        for task_id in task_ids:
            del TASK_JOBS[task_id]
        if task_ids:
            _write_task_jobs()
    return {"status": "deleted", "deleted": len(task_ids), "ids": task_ids}


@app.delete("/api/tasks/{task_id}", include_in_schema=False)
async def delete_task_job(task_id: str) -> dict[str, str]:
    with TASKS_LOCK:
        item = TASK_JOBS.get(task_id)
        if not item:
            raise HTTPException(status_code=404, detail="任务不存在")
        if item.get("status") not in {"success", "error"}:
            raise HTTPException(status_code=409, detail="运行中的任务不能删除")
        del TASK_JOBS[task_id]
        _write_task_jobs()
    return {"status": "deleted", "id": task_id}


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
