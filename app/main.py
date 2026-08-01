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
    StoredVideoList,
    StructuredInformation,
    VerifyRequest,
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
from app.trust.service import verify_structured_information


app = FastAPI(
    title="MiMo Trust Multimodal Source Verification",
    version="0.5.0",
    docs_url="/api/docs",
)
cache = ResultCache(settings.cache_ttl_seconds)
static_dir = Path(__file__).parent / "static"
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
    try:
        url = await asyncio.to_thread(
            resolve_content_input,
            request.url,
            platform_only=request.input_kind == "platform",
        )
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cache_key = cache.key(f"{request.input_kind}:{url}", request.mode)
    if not request.refresh:
        cached = cache.get(cache_key)
        if cached:
            cached["cached"] = True
            _ensure_cleaned_article(cached)
            cached_result = AnalyzeResponse.model_validate(cached)
            thumbnail_changed = await _stabilize_result_thumbnail(cached_result)
            if request.verify and not cached_result.verification:
                try:
                    cached_result.verification = await verify_structured_information(
                        cached_result.structured_data
                    )
                    cache.set(cache_key, cached_result.model_dump(mode="json"))
                except Exception as exc:
                    cached_result.verification = {
                        "status": "failed",
                        "message": str(exc),
                    }
            if not cached_result.extraction_milliseconds:
                cached_result.extraction_milliseconds = sum(
                    item.milliseconds for item in cached_result.timings
                )
            verification_seconds = float(
                ((cached_result.verification or {}).get("timings") or {}).get(
                    "total_seconds", 0
                )
            )
            if not cached_result.full_pipeline_milliseconds:
                cached_result.full_pipeline_milliseconds = (
                    cached_result.extraction_milliseconds
                    + round(verification_seconds * 1000)
                )
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

    await _stabilize_result_thumbnail(result)

    if request.verify:
        try:
            result.verification = await verify_structured_information(
                result.structured_data
            )
        except Exception as exc:
            result.verification = {
                "status": "failed",
                "message": str(exc),
            }
    result.full_pipeline_milliseconds = round(
        (time.perf_counter() - request_started) * 1000
    )
    cache.set(cache_key, result.model_dump(mode="json"))
    return result


@app.post("/api/analyze/upload", response_model=AnalyzeResponse)
async def analyze_uploaded_content(
    title: str = Form(default="多模态组合核验", max_length=200),
    text: str = Form(default="", max_length=50_000),
    files: list[UploadFile] = File(default=[]),
    verify: bool = Form(default=True),
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
    await _stabilize_result_thumbnail(result)
    if verify:
        try:
            result.verification = await verify_structured_information(
                result.structured_data
            )
        except Exception as exc:
            result.verification = {"status": "failed", "message": str(exc)}
    result.full_pipeline_milliseconds = round(
        (time.perf_counter() - request_started) * 1000
    )
    return result


@app.post("/api/verify")
async def verify_claims(request: VerifyRequest) -> dict[str, object]:
    try:
        result = await verify_structured_information(request.structured_data)
        if request.cache_key:
            cached = cache.get(request.cache_key)
            if not cached:
                raise HTTPException(status_code=404, detail="缓存记录不存在或已过期")
            cached_structured = StructuredInformation.model_validate(
                cached.get("structured_data", {})
            )
            if cached_structured.case_id != request.structured_data.case_id:
                raise HTTPException(status_code=409, detail="核验案例与缓存记录不匹配")
            cached["verification"] = result
            extraction_milliseconds = int(
                cached.get("extraction_milliseconds")
                or sum(
                    int(item.get("milliseconds") or 0)
                    for item in cached.get("timings", [])
                    if isinstance(item, dict)
                )
            )
            verification_milliseconds = round(
                float((result.get("timings") or {}).get("total_seconds") or 0)
                * 1000
            )
            cached["extraction_milliseconds"] = extraction_milliseconds
            cached["full_pipeline_milliseconds"] = (
                extraction_milliseconds + verification_milliseconds
            )
            cache.set(request.cache_key, cached)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/videos", response_model=StoredVideoList)
async def list_videos(limit: int = 100) -> StoredVideoList:
    safe_limit = min(max(limit, 1), 500)
    items = cache.list(safe_limit)
    for item in items:
        result = item.get("result")
        if isinstance(result, dict):
            _ensure_cleaned_article(result)
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
