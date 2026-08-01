from __future__ import annotations

import asyncio
import hashlib
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import UploadFile

from app.config import settings
from app.mimo import MimoError, analyze_keyframes, structure_information, summarize_video, transcribe_audio
from app.models import AnalyzeResponse, CostStep, CoverageInfo, ExtractionPlan, StageTiming, VideoMetadata
from app.pipeline import (
    PipelineError,
    _clean_source_article,
    _compress_video_for_mimo,
    _extract_audio,
    _local_structured_information,
    _structured_information_from_model_result,
    _structured_reading_result,
)
from app.security import BROWSER_USER_AGENT, REDIRECT_LIMIT, validate_public_url
from app.thumbnails import thumbnail_store


MAX_ARTICLE_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_TOTAL_BYTES = 150 * 1024 * 1024


def _article_platform(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(("weibo.com", "weibo.cn")):
        return "微博"
    if host.endswith(("xiaohongshu.com", "xhslink.com")):
        return "小红书"
    if host.endswith("weixin.qq.com"):
        return "微信公众平台"
    return host.removeprefix("www.") or "网页"


async def _fetch_public_html(url: str) -> tuple[str, str]:
    current = url
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    }
    async with httpx.AsyncClient(follow_redirects=False, timeout=20, headers=headers) as client:
        for _ in range(REDIRECT_LIMIT + 1):
            validate_public_url(current)
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                raise PipelineError(f"文章页面请求失败：{exc}") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise PipelineError("文章链接返回了缺少目标地址的跳转")
                current = urljoin(current, location)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise PipelineError(f"文章页面返回 HTTP {response.status_code}") from exc
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type:
                raise PipelineError("该 URL 不是可提取的 HTML 文章页面")
            if len(response.content) > MAX_ARTICLE_BYTES:
                raise PipelineError("文章页面超过 5 MB 提取上限")
            return response.text, str(response.url)
    raise PipelineError(f"文章链接跳转超过 {REDIRECT_LIMIT} 次")


def extract_article(html: str, url: str) -> tuple[str, str, str | None, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,noscript,nav,footer,aside,form"):
        node.decompose()
    title = ""
    for selector, attribute in (
        ('meta[property="og:title"]', "content"),
        ('meta[name="twitter:title"]', "content"),
    ):
        node = soup.select_one(selector)
        if node and node.get(attribute):
            title = str(node.get(attribute)).strip()
            break
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    author_node = soup.select_one('[rel="author"],meta[name="author"]')
    author = None
    if author_node:
        author = str(author_node.get("content") or author_node.get_text(" ", strip=True)).strip() or None
    image_node = soup.select_one('meta[property="og:image"]')
    thumbnail = str(image_node.get("content")).strip() if image_node and image_node.get("content") else None
    root = soup.select_one("article") or soup.select_one("main") or soup.body or soup
    blocks: list[str] = []
    seen: set[str] = set()
    for node in root.select("h1,h2,h3,p,li,blockquote"):
        text = " ".join(node.get_text(" ", strip=True).split())
        if len(text) < 2 or text in seen:
            continue
        seen.add(text)
        blocks.append(text)
    article = "\n".join(blocks)
    if len(article) < 40:
        article = " ".join(root.get_text(" ", strip=True).split())
    if len(article) < 20:
        raise PipelineError("页面未提取到足够的文章正文；可能需要登录或平台授权")
    return title or "未命名文章", article[: settings.max_transcript_chars], author, thumbnail


async def _structured_response(
    source_text: str,
    metadata: VideoMetadata,
    *,
    strategy: str = "metadata",
    visual_notes: list[str] | None = None,
    timings: list[StageTiming] | None = None,
    coverage_note: str,
) -> AnalyzeResponse:
    started = time.perf_counter()
    if settings.mimo_api_key:
        try:
            raw = await structure_information(source_text, metadata.model_dump())
            structured = _structured_information_from_model_result(raw)
            status = "structured_ready"
            gaps: list[str] = []
        except MimoError as exc:
            structured = _local_structured_information(source_text, metadata.title, metadata.webpage_url)
            status = "needs_review"
            gaps = [f"模型级结构化转换失败：{exc}"]
    else:
        structured = _local_structured_information(source_text, metadata.title, metadata.webpage_url)
        status = "partial"
        gaps = ["未配置 MIMO_API_KEY，当前为本地结构化预览"]
    summary, key_points, topics = _structured_reading_result(structured)
    all_timings = list(timings or [])
    all_timings.append(StageTiming(name="标准结构化信息转换", milliseconds=round((time.perf_counter() - started) * 1000)))
    return AnalyzeResponse(
        request_id=uuid.uuid4().hex[:12], cached=False, strategy=strategy,
        metadata=metadata, summary=summary, key_points=key_points, topics=topics,
        coverage_note=coverage_note + (f" 关键缺口：{'；'.join(gaps)}。" if gaps else ""),
        full_source_text=source_text,
        structured_input_text=source_text[: settings.max_transcript_chars],
        structured_input_chars=min(len(source_text), settings.max_transcript_chars),
        structured_input_truncated=len(source_text) > settings.max_transcript_chars,
        cleaned_article=_clean_source_article(source_text, metadata.title),
        timings=all_timings,
        extraction_milliseconds=sum(item.milliseconds for item in all_timings),
        coverage=CoverageInfo(
            status=status, text_retention_percent=100, visual_analyzed=bool(visual_notes),
            post_context_captured=True, critical_gaps=gaps,
        ),
        visual_notes=visual_notes or [], structured_data=structured,
        extraction_plan=ExtractionPlan(
            video_type="mixed" if visual_notes else "text_dominant",
            active_modalities=["post_context", *( ["visual"] if visual_notes else [])],
            highest_cost_level="L2", reasons=["统一内容入口完成正文与多模态信息合并"],
        ),
        cost_trace=[
            CostStep(level="L0", name="输入安全与类型识别", executed=True, reason="统一内容入口"),
            CostStep(level="L1", name="正文或上传文件提取", executed=True, reason="保留可回溯原文"),
            CostStep(level="L2", name="多模态理解与结构化转换", executed=True, reason="生成信源核实标准输入"),
            CostStep(level="L3", name="全视频多模态升级", executed=False, reason="当前输入无需额外升级"),
        ],
    )


async def analyze_article_url(url: str) -> AnalyzeResponse:
    started = time.perf_counter()
    html, final_url = await _fetch_public_html(url)
    title, article, author, thumbnail = extract_article(html, final_url)
    timing = StageTiming(name="提取文章正文", milliseconds=round((time.perf_counter() - started) * 1000))
    metadata = VideoMetadata(
        platform=_article_platform(final_url), content_type="article", title=title,
        uploader=author, thumbnail=thumbnail, webpage_url=final_url,
    )
    source = f"[发布上下文]\n标题：{title}\n作者：{author or ''}\n来源：{final_url}\n\n[文章正文]\n{article}"
    return await _structured_response(
        source, metadata, timings=[timing],
        coverage_note=f"已提取文章正文 {len(article)} 个字符，并保留最终来源 URL。",
    )


async def analyze_upload_bundle(title: str, text: str, files: list[UploadFile]) -> AnalyzeResponse:
    if not text.strip() and not files:
        raise PipelineError("请至少提供文本或一个图片、音频、视频文件")
    if len(files) > 12:
        raise PipelineError("单次最多上传 12 个文件")
    started = time.perf_counter()
    sections = [f"[用户说明]\n{text.strip()}"] if text.strip() else []
    visual_notes: list[str] = []
    upload_thumbnail: str | None = None
    total = 0
    with tempfile.TemporaryDirectory(prefix="mimo-upload-") as temp_dir:
        paths: list[tuple[UploadFile, Path]] = []
        for index, upload in enumerate(files, 1):
            data = await upload.read()
            total += len(data)
            if len(data) > MAX_UPLOAD_BYTES or total > MAX_UPLOAD_TOTAL_BYTES:
                raise PipelineError("上传文件超过单文件 50 MB 或合计 150 MB 限制")
            suffix = Path(upload.filename or "").suffix.lower()
            path = Path(temp_dir) / f"{index:02d}{suffix}"
            path.write_bytes(data)
            paths.append((upload, path))

        image_paths: list[Path] = []
        for upload, path in paths:
            mime = (upload.content_type or "").lower()
            if mime.startswith("text/") or path.suffix in {".txt", ".md", ".csv"}:
                sections.append(f"[上传文本：{upload.filename}]\n{path.read_text(encoding='utf-8', errors='replace')}")
            elif mime.startswith("image/"):
                image_paths.append(path)
                if upload_thumbnail is None:
                    upload_thumbnail = thumbnail_store.materialize_bytes(
                        path.read_bytes(), mime
                    )
            elif mime.startswith("audio/"):
                if not settings.mimo_api_key:
                    sections.append(f"[上传音频：{upload.filename}]\n未配置 MiMo，无法执行 ASR")
                    continue
                audio_path = path if path.suffix in {".mp3", ".wav"} else await asyncio.to_thread(_extract_audio, path)
                result = await transcribe_audio(audio_path)
                sections.append(f"[上传音频 ASR：{upload.filename}]\n{result.get('text', '')}")
            elif mime.startswith("video/"):
                if not settings.mimo_api_key:
                    sections.append(f"[上传视频：{upload.filename}]\n未配置 MiMo，无法执行全模态理解")
                    continue
                normalized_video = await asyncio.to_thread(_compress_video_for_mimo, path)
                audio_path = await asyncio.to_thread(_extract_audio, normalized_video)
                visual_result, audio_result = await asyncio.gather(
                    summarize_video(normalized_video, {"title": title}),
                    transcribe_audio(audio_path),
                )
                result = visual_result
                observations = [str(result.get("summary") or ""), *map(str, result.get("key_points") or []), *map(str, result.get("on_screen_text") or [])]
                observations = [item for item in observations if item]
                visual_notes.extend(observations)
                sections.append(f"[上传视频理解：{upload.filename}]\n" + "\n".join(observations))
                if str(audio_result.get("text") or "").strip():
                    sections.append(f"[上传视频 ASR：{upload.filename}]\n{audio_result['text']}")
            else:
                raise PipelineError(f"不支持的上传格式：{upload.filename or mime or '未知文件'}")

        if image_paths:
            if settings.mimo_api_key:
                frames = [(index, float(index - 1), path) for index, path in enumerate(image_paths, 1)]
                result = await analyze_keyframes(frames, {"title": title})
                for frame in result.get("frames") or []:
                    observations = [*map(str, frame.get("ocr_text") or []), *map(str, frame.get("visual_observations") or [])]
                    visual_notes.extend(observations)
                sections.append("[上传图片 OCR 与观察]\n" + "\n".join(visual_notes))
            else:
                sections.append("[上传图片]\n未配置 MiMo，无法执行 OCR 与视觉理解")

    source = "\n\n".join(section for section in sections if section.strip())
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    metadata = VideoMetadata(
        platform="用户手动上传", content_type="upload_bundle", title=title or "手动多模态核验",
        image_count=len([1 for upload in files if (upload.content_type or "").startswith("image/")]),
        thumbnail=upload_thumbnail,
        webpage_url=f"upload://{digest}",
    )
    timing = StageTiming(name="解析手动多模态组合", milliseconds=round((time.perf_counter() - started) * 1000))
    return await _structured_response(
        source, metadata, strategy="hybrid" if visual_notes else "metadata",
        visual_notes=visual_notes, timings=[timing],
        coverage_note=f"已合并用户说明与 {len(files)} 个上传文件，作为一个核验案例处理。",
    )
