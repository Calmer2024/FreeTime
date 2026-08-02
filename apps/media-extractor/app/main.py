from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
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


app = FastAPI(
    title="FreeTime Media Extractor",
    version="1.0.0",
    docs_url="/api/docs",
)
cache = ResultCache(settings.cache_ttl_seconds)
static_dir = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "mimo_configured": bool(settings.mimo_api_key),
        "supported_platforms": [
            "抖音", "哔哩哔哩", "YouTube", "快手", "微博", "小红书", "视频号"
        ],
        "accepted_inputs": [
            "文章 URL", "平台链接/分享文本", "无有效链接时自动组合文本+图片+音频+视频"
        ],
        "extraction_protocol": "structured-information-v4",
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
