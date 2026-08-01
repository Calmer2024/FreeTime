from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
from app.security import UnsafeUrlError, resolve_video_input
from app.trust.service import verify_structured_information


app = FastAPI(
    title="MiMo Trust Video Information Extraction Demo",
    version="0.3.0",
    docs_url="/api/docs",
)
cache = ResultCache(settings.cache_ttl_seconds)
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _ensure_cleaned_article(payload: dict[str, object]) -> dict[str, object]:
    metadata = payload.get("metadata")
    metadata_dict = metadata if isinstance(metadata, dict) else {}
    if not payload.get("cleaned_article"):
        payload["cleaned_article"] = _clean_source_article(
            str(payload.get("full_source_text") or ""),
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
        "supported_platforms": ["抖音", "哔哩哔哩", "YouTube"],
        "accepted_inputs": ["完整 URL", "平台短链", "手机分享文本"],
        "extraction_protocol": "structured-information-v3",
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_video(request: AnalyzeRequest) -> AnalyzeResponse:
    try:
        url = await asyncio.to_thread(resolve_video_input, request.url)
    except UnsafeUrlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cache_key = cache.key(url, request.mode)
    if not request.refresh:
        cached = cache.get(cache_key)
        if cached:
            cached["cached"] = True
            _ensure_cleaned_article(cached)
            cached_result = AnalyzeResponse.model_validate(cached)
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
            return cached_result

    try:
        result = await analyze(url, request.mode)
    except PipelineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

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
    cache.set(cache_key, result.model_dump(mode="json"))
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
