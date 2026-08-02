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
CHAOXING_DIR = BASE_DIR / "apps" / "chaoxing-auto"

# 挂载静态文件
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/portal", StaticFiles(directory=PORTAL_DIR), name="portal")

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
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f8fafc; color: #1e293b; min-height: 100vh; }}
    .app-header {{ background: white; border-bottom: 1px solid #e2e8f0; padding: 16px 24px; display: flex; align-items: center; gap: 16px; }}
    .back-link {{ color: #64748b; text-decoration: none; display: flex; align-items: center; gap: 8px; font-size: 14px; }}
    .back-link:hover {{ color: #1e293b; }}
    .app-title {{ display: flex; align-items: center; gap: 12px; }}
    .app-title h1 {{ font-size: 20px; font-weight: 600; }}
    .container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
    .card {{ background: white; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
    .card h2 {{ font-size: 18px; font-weight: 600; margin-bottom: 16px; }}
    .form-group {{ margin-bottom: 16px; }}
    .form-group label {{ display: block; font-size: 14px; color: #64748b; margin-bottom: 8px; }}
    .form-group input, .form-group select {{ width: 100%; padding: 10px 14px; background: white; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 14px; }}
    .form-group input:focus, .form-group select:focus {{ outline: none; border-color: #10b981; }}
    .form-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
    .button-group {{ display: flex; gap: 12px; margin-top: 20px; }}
    .btn {{ padding: 10px 24px; border: none; border-radius: 8px; font-size: 14px; font-weight: 500; cursor: pointer; }}
    .btn-primary {{ background: #10b981; color: white; }}
    .btn-primary:hover {{ background: #059669; }}
    .btn-danger {{ background: #ef4444; color: white; }}
    .btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
    .status-bar {{ display: flex; align-items: center; gap: 12px; padding: 12px 16px; background: #f8fafc; border-radius: 8px; margin-bottom: 16px; }}
    .status-dot {{ width: 10px; height: 10px; border-radius: 50%; background: #e2e8f0; }}
    .status-dot.running {{ background: #10b981; animation: pulse 2s infinite; }}
    @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} }}
    .log-container {{ background: #1e293b; color: #e2e8f0; border-radius: 8px; padding: 16px; max-height: 400px; overflow-y: auto; font-family: monospace; font-size: 13px; }}
    .log-line {{ padding: 4px 0; border-bottom: 1px solid #334155; }}
  </style>
</head>
<body>
  <header class="app-header">
    <a href="/" class="back-link">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
      返回 FreeTime
    </a>
    <div class="app-title">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>
      <h1>超星刷课助手</h1>
    </div>
  </header>
  <div class="container">
    <div class="card">
      <h2>运行状态</h2>
      <div class="status-bar">
        <div class="status-dot" id="status-dot"></div>
        <span id="status-text">未运行</span>
      </div>
      <div class="button-group">
        <button class="btn btn-primary" id="start-btn" onclick="startTask()">启动刷课</button>
        <button class="btn btn-danger" id="stop-btn" onclick="stopTask()" disabled>停止</button>
      </div>
    </div>
    <div class="card">
      <h2>课程配置</h2>
      <div class="form-group">
        <label>课程链接或名称</label>
        <input type="text" id="course-input" placeholder="粘贴课程链接或输入课程名称" value="{course_value}">
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>播放倍速</label>
          <select id="speed-select">{speed_options}</select>
        </div>
        <div class="form-group">
          <label>轮询间隔 (秒)</label>
          <input type="number" id="interval-input" min="1" max="30" value="{interval_value}">
        </div>
      </div>
      <div class="button-group">
        <button class="btn btn-primary" onclick="saveConfig()">保存配置</button>
      </div>
    </div>
    <div class="card">
      <h2>运行日志</h2>
      <div class="log-container" id="log-container">
        <div class="log-line">等待启动...</div>
      </div>
    </div>
  </div>
  <script>
    let isRunning = false;
    let pollTimer = null;
    function startTask() {{ fetch('/chaoxing/api/start', {{ method: 'POST' }}).then(r => r.json()).then(d => {{ if(d.status==='ok'){{ updateStatus(true); startPolling(); }} }}); }}
    function stopTask() {{ fetch('/chaoxing/api/stop', {{ method: 'POST' }}).then(r => r.json()).then(d => {{ if(d.status==='ok'){{ updateStatus(false); stopPolling(); }} }}); }}
    function saveConfig() {{
      const v = document.getElementById('course-input').value;
      const d = {{ speed: parseFloat(document.getElementById('speed-select').value), poll_interval: parseInt(document.getElementById('interval-input').value) }};
      if(v.startsWith('http')) d.course_url = v; else d.course_name = v;
      fetch('/chaoxing/api/config', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify(d) }}).then(() => alert('配置已保存'));
    }}
    function updateStatus(r) {{ isRunning=r; document.getElementById('status-dot').className='status-dot'+(r?' running':''); document.getElementById('status-text').textContent=r?'运行中...':'未运行'; document.getElementById('start-btn').disabled=r; document.getElementById('stop-btn').disabled=!r; }}
    function startPolling() {{ if(pollTimer) clearInterval(pollTimer); pollTimer=setInterval(fetchStatus,2000); }}
    function stopPolling() {{ if(pollTimer){{clearInterval(pollTimer);pollTimer=null;}} }}
    function fetchStatus() {{ fetch('/chaoxing/api/status').then(r=>r.json()).then(d=>{{ updateStatus(d.is_running); document.getElementById('log-container').innerHTML=d.log_lines.map(l=>'<div class="log-line">'+l+'</div>').join(''); }}); }}
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
