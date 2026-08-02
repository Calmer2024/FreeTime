from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import json
import math
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import imageio_ffmpeg
import yt_dlp

from app.channels import ChannelsParseError, extract_channels_info, is_channels_url
from app.config import settings
from app.douyin_cookies import (
    DouyinCookieError,
    extract_douyin_browser_info,
    get_dev_cookie_file,
)
from app.mimo import (
    MimoError,
    analyze_keyframes,
    structure_information,
    summarize_video,
    transcribe_audio,
)
from app.models import (
    AnalyzeResponse,
    CostStep,
    CoverageInfo,
    ExtractionPlan,
    KeyframeEvidence,
    StageTiming,
    StructuredInformation,
    VideoMetadata,
)
from app.kuaishou import KuaishouParseError, extract_kuaishou_info, is_kuaishou_url
from app.transcript import (
    choose_subtitle,
    local_extractive_summary,
    parse_subtitle_document,
)
from app.weibo import WeiboParseError, extract_weibo_post_info, is_weibo_status_url
from app.xiaohongshu import (
    XiaohongshuParseError,
    extract_xiaohongshu_info,
    is_xiaohongshu_url,
)


class PipelineError(RuntimeError):
    pass


@dataclass
class AudioChunk:
    path: Path
    start: float
    end: float


@dataclass
class AdaptiveFrame:
    index: int
    timestamp: float
    path: Path
    frame_type: str


def _structured_information_from_model_result(
    result: dict[str, Any],
) -> StructuredInformation:
    """Validate protocol fields without treating transport metadata as payload."""
    protocol_fields = {
        "case_id",
        "内容主题",
        "原子主张",
        "隐性观点",
    }
    return StructuredInformation.model_validate(
        {key: value for key, value in result.items() if key in protocol_fields}
    )


def _has_usable_spoken_text(transcript: str) -> bool:
    """Return whether ASR/subtitles contain enough linguistic content to short-circuit OCR."""
    meaningful = re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", transcript)
    return len(meaningful) >= 12


def _needs_visual_fallback(
    *, transcript: str, is_image_carousel: bool, mode: str
) -> bool:
    return (
        is_image_carousel
        or mode == "visual"
        or not _has_usable_spoken_text(transcript)
    )


def _has_unfilled_event_video_gap(
    *,
    is_image_carousel: bool,
    visual_fallback_required: bool,
    plan: ExtractionPlan,
    full_visual_executed: bool,
) -> bool:
    return (
        not is_image_carousel
        and visual_fallback_required
        and plan.video_type in {"event_footage", "low_information"}
        and not full_visual_executed
    )


def _platform(info: dict[str, Any]) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown")
    labels = {
        "Youtube": "YouTube",
        "BiliBili": "哔哩哔哩",
        "Douyin": "抖音",
        "DouyinNote": "抖音",
        "DouyinBrowser": "抖音",
        "XiaoHongShu": "小红书",
        "Kuaishou": "快手",
        "WechatChannels": "视频号",
        "Weibo": "微博",
        "WeiboVideo": "微博",
        "WeiboPost": "微博",
    }
    return labels.get(extractor, extractor)


def _metadata(info: dict[str, Any], original_url: str) -> VideoMetadata:
    return VideoMetadata(
        platform=_platform(info),
        content_type=(
            "image_carousel" if info.get("note_images") else "video"
        ),
        image_count=len(info.get("note_images") or []),
        source_subtype=info.get("source_subtype"),
        source_context=(
            info.get("source_context")
            if isinstance(info.get("source_context"), dict)
            else {}
        ),
        title=str(info.get("title") or "未命名内容"),
        uploader=info.get("uploader") or info.get("channel"),
        duration_seconds=info.get("duration"),
        thumbnail=info.get("thumbnail"),
        webpage_url=str(info.get("webpage_url") or original_url),
    )


def _is_douyin(url: str) -> bool:
    return (urlparse(url).hostname or "").lower().endswith("douyin.com")


def _is_douyin_note(url: str) -> bool:
    return _is_douyin(url) and bool(re.search(r"/(?:share/)?note/\d+", urlparse(url).path))


def _parse_douyin_note_html(html: str, webpage_url: str) -> dict[str, Any]:
    pattern = re.compile(
        r'self\.__pace_f\.push\(\[1,("(?:\\.|[^"\\])*")\]\)'
    )
    detail: dict[str, Any] | None = None
    for match in pattern.finditer(html):
        try:
            chunk = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        marker = re.search(r'"aweme"\s*:\s*', chunk)
        if not marker:
            continue
        try:
            aweme, _ = json.JSONDecoder().raw_decode(
                chunk, marker.end()
            )
        except (json.JSONDecodeError, TypeError):
            continue
        candidate = aweme.get("detail") if isinstance(aweme, dict) else None
        if isinstance(candidate, dict) and candidate.get("images"):
            detail = candidate
            break
    if not detail:
        raise PipelineError("抖音图文页面没有返回可解析的作品数据")
    if int(detail.get("awemeType") or 0) != 68:
        raise PipelineError("抖音作品不是受支持的图文轮播类型")

    images: list[dict[str, Any]] = []
    for raw in detail.get("images") or []:
        if not isinstance(raw, dict):
            continue
        urls = [
            str(value)
            for value in raw.get("urlList") or raw.get("downloadUrlList") or []
            if str(value).startswith(("http://", "https://"))
        ]
        if not urls:
            continue
        preferred = next(
            (value for value in urls if ".jpeg" in value or ".jpg" in value),
            urls[0],
        )
        images.append(
            {
                "url": preferred,
                "fallback_urls": urls,
                "width": int(raw.get("width") or 0),
                "height": int(raw.get("height") or 0),
            }
        )
    if not images:
        raise PipelineError("抖音图文作品没有返回可访问的图片")

    author = detail.get("authorInfo") or {}
    description = str(
        detail.get("desc") or detail.get("itemTitle") or detail.get("caption") or ""
    ).strip()
    item_id = str(
        detail.get("awemeId")
        or detail.get("groupId")
        or re.search(r"/note/(\d+)", webpage_url).group(1)
    )
    canonical_url = f"https://www.douyin.com/note/{item_id}"
    return {
        "id": item_id,
        "title": description or "抖音图文作品",
        "description": description,
        "uploader": str(author.get("nickname") or "").strip() or None,
        "duration": None,
        "thumbnail": images[0]["url"],
        "webpage_url": canonical_url,
        "extractor": "DouyinNote",
        "extractor_key": "DouyinNote",
        "note_images": images,
        "formats": [],
    }


def _extract_douyin_note_info(url: str) -> dict[str, Any]:
    return _extract_douyin_note_info_once(url, force_douyin_refresh=False)


def _extract_douyin_note_info_once(
    url: str, *, force_douyin_refresh: bool
) -> dict[str, Any]:
    cookie_options = _cookie_options(url, force_douyin_refresh)
    headers = {
        "User-Agent": (
            (cookie_options.get("http_headers") or {}).get("User-Agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
        )
    }
    cookies: dict[str, str] = {}
    cookie_file = cookie_options.get("cookiefile")
    if cookie_file:
        jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
            cookies = {cookie.name: cookie.value for cookie in jar}
        except (OSError, http.cookiejar.LoadError):
            cookies = {}
    try:
        response = httpx.get(
            url,
            headers=headers,
            cookies=cookies,
            follow_redirects=True,
            timeout=30,
        )
        response.raise_for_status()
        info = _parse_douyin_note_html(response.text, str(response.url))
        info["http_headers"] = {"User-Agent": headers["User-Agent"], "Referer": url}
        return info
    except Exception as exc:
        if _can_refresh_douyin_session(url, exc) and not force_douyin_refresh:
            return _extract_douyin_note_info_once(
                url, force_douyin_refresh=True
            )
        raise


def _cookie_options(url: str, force_douyin_refresh: bool = False) -> dict[str, Any]:
    if settings.ytdlp_cookies_file:
        cookie_path = Path(settings.ytdlp_cookies_file)
        if not cookie_path.is_file():
            raise PipelineError(
                f"YTDLP_COOKIES_FILE 指向的文件不存在：{cookie_path}"
            )
        result: dict[str, Any] = {"cookiefile": str(cookie_path)}
        if settings.ytdlp_user_agent:
            result["http_headers"] = {"User-Agent": settings.ytdlp_user_agent}
        return result
    if _is_douyin(url) and settings.douyin_auto_cookies:
        try:
            cookie_path, user_agent = get_dev_cookie_file(
                url, force=force_douyin_refresh
            )
        except DouyinCookieError as exc:
            raise PipelineError(f"抖音开发 Cookie 自动刷新失败：{exc}") from exc
        return {
            "cookiefile": str(cookie_path),
            "http_headers": {"User-Agent": user_agent},
        }
    return {}


_DOUYIN_SESSION_ERROR = re.compile(
    r"fresh cookies?|cookies? (?:are )?needed|sign[- ]?in|login|required|"
    r"forbidden|http error 40[13]|verify|captcha|风控|验证|登录|"
    r"invalid (?:response|json)|unable to extract|没有返回可解析|没有返回.*作品数据",
    re.IGNORECASE,
)


def _is_douyin_session_error(error: BaseException) -> bool:
    return bool(_DOUYIN_SESSION_ERROR.search(str(error)))


def _can_refresh_douyin_session(url: str, error: BaseException) -> bool:
    return (
        _is_douyin(url)
        and settings.douyin_auto_cookies
        and not settings.ytdlp_cookies_file
        and _is_douyin_session_error(error)
    )


def _douyin_failure(error: BaseException) -> PipelineError:
    if settings.ytdlp_cookies_file and _is_douyin_session_error(error):
        return PipelineError(
            "抖音专用服务会话已失效或与当前 User-Agent/IP 不匹配；"
            "请重新导出 Cookie，并同步配置 YTDLP_USER_AGENT"
        )
    return PipelineError(f"抖音作品解析失败：{error}")


def _base_ydl_options(url: str, force_douyin_refresh: bool = False) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "extractor_args": {"youtube": {"player_client": ["android_vr", "web"]}},
        **_cookie_options(url, force_douyin_refresh),
    }


def _extract_info(url: str) -> dict[str, Any]:
    # Image carousels expose their full image list in the page payload and do
    # not consistently emit the video-detail XHR observed by the adapter.
    if _is_douyin_note(url):
        return _extract_douyin_note_info(url)
    if is_kuaishou_url(url):
        try:
            return extract_kuaishou_info(url)
        except KuaishouParseError as exc:
            raise PipelineError(f"快手作品解析失败：{exc}") from exc
    if is_channels_url(url):
        try:
            return extract_channels_info(url)
        except ChannelsParseError as exc:
            raise PipelineError(f"视频号作品解析失败：{exc}") from exc
    if is_weibo_status_url(url):
        try:
            info = extract_weibo_post_info(url, _base_ydl_options(url))
            if info.get("_type") == "playlist" and info.get("entries"):
                info = next(iter(info["entries"]))
            return info
        except WeiboParseError as exc:
            raise PipelineError(f"微博帖子解析失败：{exc}") from exc
    xiaohongshu_error: XiaohongshuParseError | None = None
    if is_xiaohongshu_url(url):
        try:
            return extract_xiaohongshu_info(url)
        except XiaohongshuParseError as exc:
            xiaohongshu_error = exc
    if (
        _is_douyin(url)
        and settings.douyin_auto_cookies
    ):
        try:
            return extract_douyin_browser_info(url)
        except DouyinCookieError:
            # Keep yt-dlp and the existing note parser as compatibility
            # fallbacks when the browser cannot observe a public detail call.
            pass
    options = {
        **_base_ydl_options(url),
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as first_error:
        if _can_refresh_douyin_session(url, first_error):
            retry_options = {
                **_base_ydl_options(url, force_douyin_refresh=True),
                "skip_download": True,
            }
            try:
                with yt_dlp.YoutubeDL(retry_options) as downloader:
                    info = downloader.extract_info(url, download=False)
            except Exception as retry_error:
                raise _douyin_failure(retry_error) from retry_error
        elif _is_douyin(url):
            raise _douyin_failure(first_error) from first_error
        elif is_xiaohongshu_url(url):
            raise PipelineError(
                f"小红书笔记解析失败：{xiaohongshu_error or first_error}"
            ) from first_error
        else:
            raise
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next(iter(info["entries"]))
    if is_xiaohongshu_url(url) and not info.get("source_subtype"):
        if info.get("formats"):
            info["source_subtype"] = "xiaohongshu_video_note"
            info["note_images"] = []
        else:
            thumbnail_urls = list(dict.fromkeys(
                str(item.get("url") or "")
                for item in info.get("thumbnails") or []
                if isinstance(item, dict) and str(item.get("url") or "")
            ))
            info["source_subtype"] = "xiaohongshu_image_note"
            info["note_images"] = [
                {"url": image_url, "fallback_urls": [image_url]}
                for image_url in thumbnail_urls
            ]
        info["source_context"] = {
            "topics": [str(item) for item in info.get("tags") or []],
            "attachments": [],
        }
    return info


def _download_note_images(
    info: dict[str, Any], target_dir: str
) -> list[AdaptiveFrame]:
    image_dir = Path(target_dir) / "note-images"
    image_dir.mkdir(exist_ok=True)
    headers = info.get("http_headers") or {}
    frames: list[AdaptiveFrame] = []
    with httpx.Client(headers=headers, follow_redirects=True, timeout=30) as client:
        for offset, image in enumerate(info.get("note_images") or []):
            candidates = [
                image.get("url"),
                *(image.get("fallback_urls") or []),
            ]
            response: httpx.Response | None = None
            for candidate in dict.fromkeys(
                str(value) for value in candidates if value
            ):
                try:
                    attempt = client.get(candidate)
                    attempt.raise_for_status()
                    if attempt.content:
                        response = attempt
                        break
                except httpx.HTTPError:
                    continue
            if response is None:
                continue
            content_type = response.headers.get("content-type", "").lower()
            suffix = (
                ".png"
                if "png" in content_type
                else ".webp"
                if "webp" in content_type
                else ".jpg"
            )
            path = image_dir / f"slide-{offset + 1:03d}{suffix}"
            path.write_bytes(response.content)
            frames.append(
                AdaptiveFrame(
                    index=offset + 1,
                    timestamp=float(offset),
                    path=path,
                    frame_type="image_slide",
                )
            )
    if not frames:
        raise PipelineError("抖音图文作品的图片全部下载失败")
    return frames


async def _download_subtitle(
    selected: dict[str, Any], info: dict[str, Any]
) -> tuple[str, float]:
    headers = info.get("http_headers") or {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(selected["url"], headers=headers)
        response.raise_for_status()
    return parse_subtitle_document(response.text, selected.get("ext", "vtt"))


def _select_progressive_format(info: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        item
        for item in (info.get("formats") or [])
        if item.get("url")
        and item.get("vcodec") not in {None, "none"}
        and item.get("acodec") not in {None, "none"}
    ]
    if not candidates and info.get("url"):
        return {"url": str(info["url"])}
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            (item.get("height") or 9999) > 480,
            abs((item.get("height") or 480) - 360),
            item.get("filesize") or item.get("filesize_approx") or 0,
        )
    )
    return candidates[0]


def _select_progressive_media_url(info: dict[str, Any]) -> str | None:
    selected = _select_progressive_format(info)
    return str(selected["url"]) if selected else None


def _download_browser_media(info: dict[str, Any], target_dir: str) -> Path:
    selected = _select_progressive_format(info)
    if not selected:
        raise PipelineError("平台适配器未返回完整 MP4 媒体流")
    candidates = list(
        dict.fromkeys(
            str(value)
            for value in [selected.get("url"), *(selected.get("fallback_urls") or [])]
            if value
        )
    )
    headers = dict(info.get("http_headers") or {})
    destination = Path(target_dir) / "video.mp4"
    last_error: Exception | None = None
    with httpx.Client(headers=headers, follow_redirects=True, timeout=120) as client:
        for candidate in candidates:
            try:
                with client.stream("GET", candidate) as response:
                    response.raise_for_status()
                    with destination.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            output.write(chunk)
                if destination.stat().st_size > 0:
                    # Browser adapter deliberately selects H.264 + AAC MP4,
                    # already compatible with audio extraction and MiMo input.
                    return destination
            except (OSError, httpx.HTTPError) as exc:
                last_error = exc
                destination.unlink(missing_ok=True)
    raise PipelineError(f"平台媒体流下载失败：{last_error}")


def _download_merged_video(
    url: str, target_dir: str, info: dict[str, Any] | None = None
) -> Path:
    if info and info.get("_browser_native"):
        try:
            return _download_browser_media(info, target_dir)
        except PipelineError:
            if not _is_douyin(url):
                raise
            try:
                refreshed = extract_douyin_browser_info(url)
                return _download_browser_media(refreshed, target_dir)
            except DouyinCookieError as exc:
                raise PipelineError(f"抖音浏览器媒体会话刷新失败：{exc}") from exc
    template = str(Path(target_dir) / "video.%(ext)s")
    download_options = {
        "format": (
            "bestvideo[height<=480][ext=mp4]+worstaudio[ext=m4a]/"
            "bestvideo[height<=480]+worstaudio/best[height<=480]/best"
        ),
        "outtmpl": template,
        "merge_output_format": "mp4",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "socket_timeout": 120,
        "retries": 5,
        "fragment_retries": 5,
    }
    last_error: Exception | None = None
    session_refresh_used = False
    refresh_on_next_attempt = False
    # Re-extracting rotates Bilibili's short-lived/CDN-specific media URLs.
    # Downloader-level retries alone keep retrying the same unhealthy node.
    for attempt in range(3):
        options = {
            **_base_ydl_options(url, force_douyin_refresh=refresh_on_next_attempt),
            **download_options,
        }
        refresh_on_next_attempt = False
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
                if isinstance(info, dict):
                    _prefer_bilibili_backup_urls(info, url)
                    downloader.process_ie_result(info, download=True)
                else:
                    downloader.download([url])
            last_error = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if _can_refresh_douyin_session(url, exc) and not session_refresh_used:
                session_refresh_used = True
                refresh_on_next_attempt = True
                continue
            if attempt == 2:
                break
    if last_error is not None:
        if _is_douyin(url) and _is_douyin_session_error(last_error):
            raise _douyin_failure(last_error) from last_error
        raise PipelineError(f"视频音视频流下载失败：{last_error}") from last_error
    candidates = sorted(Path(target_dir).glob("video.*"))
    if not candidates:
        raise PipelineError("视频下载或 DASH 音视频合并失败")
    video = next((path for path in candidates if path.suffix.lower() == ".mp4"), candidates[0])
    return _compress_video_for_mimo(video)


def _download_visual_track(url: str, target_dir: str) -> Path:
    """Download only a low-resolution visual track when audio is already covered."""
    template = str(Path(target_dir) / "visual.%(ext)s")
    download_options = {
        "format": "bestvideo[height<=480][ext=mp4]/best[height<=480]/worst",
        "outtmpl": template,
        "socket_timeout": 120,
        "retries": 5,
    }
    last_error: Exception | None = None
    session_refresh_used = False
    refresh_on_next_attempt = False
    for attempt in range(3):
        options = {
            **_base_ydl_options(url, force_douyin_refresh=refresh_on_next_attempt),
            **download_options,
        }
        refresh_on_next_attempt = False
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                info = downloader.extract_info(url, download=False)
                if isinstance(info, dict):
                    _prefer_bilibili_backup_urls(info, url)
                    downloader.process_ie_result(info, download=True)
                else:
                    downloader.download([url])
            last_error = None
            break
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            if _can_refresh_douyin_session(url, exc) and not session_refresh_used:
                session_refresh_used = True
                refresh_on_next_attempt = True
                continue
            if attempt == 2:
                break
    if last_error is not None:
        if _is_douyin(url) and _is_douyin_session_error(last_error):
            raise _douyin_failure(last_error) from last_error
        raise PipelineError(f"视觉轨下载失败：{last_error}") from last_error
    candidates = sorted(Path(target_dir).glob("visual.*"))
    if not candidates:
        raise PipelineError("平台未返回可用的视觉轨")
    return candidates[0]


def _prefer_bilibili_backup_urls(info: dict[str, Any], url: str) -> None:
    """Replace throttled mcdn URLs with Bilibili's own backup CDN URLs."""
    if "bilibili.com" not in (urlparse(url).hostname or ""):
        return
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/136.0 Safari/537.36"
            ),
            "Referer": url,
        }
        page = httpx.get(url, headers=headers, timeout=15).text
        match = re.search(r"window\.__INITIAL_STATE__=({.+?});\(function\(\)", page)
        if not match:
            return
        state = json.loads(match.group(1))
        video_data = state.get("videoData") or {}
        bvid = str(video_data.get("bvid") or info.get("id") or "")
        cid = video_data.get("cid")
        if not bvid or not cid:
            return
        play_url = (
            "https://api.bilibili.com/x/player/playurl"
            f"?bvid={bvid}&cid={cid}&fnval=4048&fourk=1"
        )
        payload = httpx.get(play_url, headers=headers, timeout=15).json()
        dash = ((payload.get("data") or {}).get("dash") or {})
        backups: dict[str, str] = {}
        for stream in [*(dash.get("video") or []), *(dash.get("audio") or [])]:
            urls = stream.get("backupUrl") or stream.get("backup_url") or []
            if urls:
                # The last URL is normally Bilibili's stable upos mirror rather
                # than the frequently throttled mcdn edge hostname.
                backups[str(stream.get("id"))] = str(urls[-1])
        for media_format in info.get("formats") or []:
            replacement = backups.get(str(media_format.get("format_id")))
            if replacement:
                media_format["url"] = replacement
    except (httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # This is an optimization. yt-dlp's original URLs remain a valid fallback.
        return


def _run_ffmpeg(arguments: list[str]) -> None:
    command = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error"]
    completed = subprocess.run(
        [*command, *arguments],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise PipelineError(f"FFmpeg 处理失败：{completed.stderr[-500:]}")


def _compress_video_for_mimo(video: Path) -> Path:
    # A .mp4 extension does not guarantee a model-compatible codec. Normalize
    # platform-specific AV1/HEVC/DASH output to H.264 + AAC before Base64 upload.
    output = video.with_name("video-mimo.mp4")
    _run_ffmpeg(
        [
            "-i",
            str(video),
            "-vf",
            "scale=-2:256",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-b:v",
            "140k",
            "-maxrate",
            "180k",
            "-bufsize",
            "360k",
            "-c:a",
            "aac",
            "-b:a",
            "24k",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ]
    )
    if output.stat().st_size > 36 * 1024 * 1024:
        raise PipelineError("压缩后视频仍超过 MiMo Base64 传入限制")
    return output


def _extract_audio(media: Path) -> Path:
    output = media.with_name("audio.mp3")
    _run_ffmpeg(
        [
            "-i",
            str(media),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "40k",
            "-y",
            str(output),
        ]
    )
    return output


def _audio_activity_percent(audio: Path, duration: float) -> float:
    """Cheap non-silence probe used to avoid ASR on silent videos."""
    if duration <= 0:
        return 100.0
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-i",
            str(audio),
            "-af",
            "silencedetect=noise=-35dB:d=0.8",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    silence = sum(
        float(value)
        for value in re.findall(r"silence_duration:\s*([0-9.]+)", completed.stderr)
    )
    return round(max(0.0, min(100.0, (duration - silence) / duration * 100)), 1)


def _extract_adaptive_keyframes(
    media: Path, target_dir: str
) -> list[AdaptiveFrame]:
    frame_dir = Path(target_dir) / "keyframes"
    frame_dir.mkdir(exist_ok=True)
    output = str(frame_dir / "frame-%03d.jpg")
    threshold = max(0.05, min(0.95, settings.keyframe_scene_threshold))
    period = max(5, settings.keyframe_period_seconds)
    maximum = max(1, settings.keyframe_max_frames)
    select = (
        f"select='eq(n\\,0)+gt(scene\\,{threshold})+"
        f"gte(t-prev_selected_t\\,{period})',"
        "scale='min(768,iw)':-2,showinfo"
    )
    completed = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(media),
            "-vf",
            select,
            "-fps_mode",
            "vfr",
            "-frames:v",
            str(maximum),
            "-q:v",
            "4",
            "-y",
            output,
        ],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise PipelineError(f"自适应关键帧提取失败：{completed.stderr[-500:]}")
    paths = sorted(frame_dir.glob("frame-*.jpg"))
    timestamps = [
        float(value)
        for value in re.findall(r"pts_time:([0-9.]+)", completed.stderr)
    ]
    frames: list[AdaptiveFrame] = []
    for offset, path in enumerate(paths):
        timestamp = timestamps[offset] if offset < len(timestamps) else offset * period
        if offset == 0:
            frame_type = "first_frame"
        elif frames and timestamp - frames[-1].timestamp >= period - 1:
            frame_type = "periodic"
        else:
            frame_type = "scene_change"
        frames.append(
            AdaptiveFrame(
                index=offset + 1,
                timestamp=timestamp,
                path=path,
                frame_type=frame_type,
            )
        )
    return frames


def _split_audio(audio: Path, duration: float) -> list[AudioChunk]:
    chunk_dir = audio.parent / "chunks"
    chunk_dir.mkdir(exist_ok=True)
    pattern = str(chunk_dir / "chunk-%03d.mp3")
    _run_ffmpeg(
        [
            "-i",
            str(audio),
            "-f",
            "segment",
            "-segment_time",
            str(settings.asr_chunk_seconds),
            "-reset_timestamps",
            "1",
            "-c",
            "copy",
            "-y",
            pattern,
        ]
    )
    paths = sorted(chunk_dir.glob("chunk-*.mp3"))
    chunks: list[AudioChunk] = []
    for index, path in enumerate(paths):
        start = index * settings.asr_chunk_seconds
        end = min(duration, start + settings.asr_chunk_seconds) if duration else (
            start + settings.asr_chunk_seconds
        )
        chunks.append(AudioChunk(path=path, start=start, end=end))
    if not chunks:
        raise PipelineError("音频分段失败")
    return chunks


def _timecode(seconds: float) -> str:
    minutes, second = divmod(max(0, int(seconds)), 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02d}:{minute:02d}:{second:02d}"


async def _transcribe_chunks(
    chunks: list[AudioChunk], duration: float
) -> tuple[str, float, list[str], float]:
    semaphore = asyncio.Semaphore(max(1, settings.asr_concurrency))

    async def transcribe_one(chunk: AudioChunk) -> dict[str, Any]:
        async with semaphore:
            try:
                result = await transcribe_audio(chunk.path)
                return {"chunk": chunk, "result": result, "error": None}
            except Exception as exc:
                return {"chunk": chunk, "result": None, "error": str(exc)}

    rows = await asyncio.gather(*(transcribe_one(chunk) for chunk in chunks))
    lines: list[str] = []
    missing_ranges: list[str] = []
    processed_seconds = 0.0
    for row in rows:
        chunk: AudioChunk = row["chunk"]
        label = f"{_timecode(chunk.start)}-{_timecode(chunk.end)}"
        if row["error"]:
            missing_ranges.append(label)
            continue
        result = row["result"]
        text = str(result.get("text") or "").strip()
        expected = max(0.0, chunk.end - chunk.start)
        reported = float(result.get("processed_seconds") or 0)
        accepted = min(expected, reported if reported > 0 else expected)
        if result.get("finish_reason") == "length":
            missing_ranges.append(label)
        elif text:
            processed_seconds += accepted
            lines.append(f"[{label}] {text}")
        else:
            missing_ranges.append(label)
    coverage = min(100.0, processed_seconds / duration * 100) if duration else (
        100.0 if lines else 0.0
    )
    return "\n".join(lines), coverage, missing_ranges, processed_seconds


def _estimate_visual_cost_cny(
    duration: float, width: int, height: int, fps: float = 0.2
) -> float:
    if duration <= 0:
        return 0.0
    temporal_patch, spatial = 2, 32
    pixels_per_token = spatial**2
    frames = min(math.ceil(duration * fps), 2048)
    frames = max(math.ceil(frames / temporal_patch) * temporal_patch, temporal_patch)
    max_pixels = min(
        131072 * pixels_per_token * temporal_patch // frames,
        300 * pixels_per_token,
    )
    max_pixels = max(8192, min(max_pixels, 8388608))
    height, width = max(height, 32), max(width, 32)
    scaled_h = round(height / spatial) * spatial
    scaled_w = round(width / spatial) * spatial
    if scaled_h * scaled_w > max_pixels:
        beta = math.sqrt(height * width / max_pixels)
        scaled_h = max(spatial, math.floor(height / beta / spatial) * spatial)
        scaled_w = max(spatial, math.floor(width / beta / spatial) * spatial)
    grids = frames // temporal_patch
    visual_tokens = grids * (scaled_h // 16) * (scaled_w // 16) // 4
    audio_tokens = math.ceil((((int(duration * 24000) // 240) // 4) / 4)) + 2
    input_tokens = visual_tokens + grids * 5 + 2 + audio_tokens
    return input_tokens / 1_000_000 + 700 / 1_000_000 * 2


def _token_usage_cost_cny(result: dict[str, Any] | None) -> float:
    usage = (result or {}).get("_usage") or {}
    prompt_tokens = float(
        usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    )
    completion_tokens = float(
        usage.get("completion_tokens") or usage.get("output_tokens") or 0
    )
    return prompt_tokens / 1_000_000 + completion_tokens / 1_000_000 * 2


def _visual_context(result: dict[str, Any] | None) -> tuple[str, list[str]]:
    if not result:
        return "", []
    notes = [
        *[str(item) for item in result.get("key_points", [])],
        *[f"屏幕文字：{item}" for item in result.get("on_screen_text", [])],
    ]
    summary = str(result.get("summary") or "")
    text = "\n".join([summary, *notes]).strip()
    return text, notes


def _normalize_keyframe_result(
    frames: list[AdaptiveFrame], result: dict[str, Any] | None
) -> list[KeyframeEvidence]:
    returned = {
        int(item.get("frame_index") or 0): item
        for item in (result or {}).get("frames", [])
        if isinstance(item, dict)
    }
    normalized: list[KeyframeEvidence] = []
    for frame in frames:
        item = returned.get(frame.index, {})
        normalized.append(
            KeyframeEvidence(
                frame_index=frame.index,
                # Local FFmpeg time is authoritative; do not trust a generated time.
                timestamp_seconds=round(frame.timestamp, 3),
                ocr_text=[
                    str(text).strip()
                    for text in item.get("ocr_text", [])
                    if str(text).strip()
                ][:20],
                visual_observations=[
                    str(text).strip()
                    for text in item.get("visual_observations", [])
                    if str(text).strip()
                ][:12],
                frame_type=frame.frame_type,
            )
        )
    return normalized


async def _analyze_frame_batches(
    frames: list[AdaptiveFrame],
    metadata: dict[str, Any],
    batch_size: int = 8,
) -> dict[str, Any]:
    """Analyze every frame while keeping individual multimodal requests bounded."""
    batches = [
        frames[offset : offset + batch_size]
        for offset in range(0, len(frames), batch_size)
    ]
    semaphore = asyncio.Semaphore(2)

    async def analyze_batch(batch: list[AdaptiveFrame]) -> dict[str, Any]:
        async with semaphore:
            return await analyze_keyframes(
                [(frame.index, frame.timestamp, frame.path) for frame in batch],
                metadata,
            )

    outputs = await asyncio.gather(
        *(analyze_batch(batch) for batch in batches), return_exceptions=True
    )
    merged_frames: list[dict[str, Any]] = []
    prompt_tokens = 0
    completion_tokens = 0
    failures = 0
    for output in outputs:
        if isinstance(output, BaseException):
            failures += 1
            continue
        merged_frames.extend(
            item for item in output.get("frames", []) if isinstance(item, dict)
        )
        usage = output.get("_usage") or {}
        prompt_tokens += int(
            usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        )
        completion_tokens += int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
    if not merged_frames:
        raise MimoError("所有图片/关键帧 OCR 批次均失败")
    return {
        "frames": merged_frames,
        "_failed_batches": failures,
        "_usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


def _keyframe_context(keyframes: list[KeyframeEvidence]) -> tuple[str, list[str]]:
    lines: list[str] = []
    notes: list[str] = []
    for frame in keyframes:
        label = (
            f"图片 {frame.frame_index}"
            if frame.frame_type == "image_slide"
            else _timecode(frame.timestamp_seconds)
        )
        for text in frame.ocr_text:
            lines.append(f"[{label}][屏幕文字] {text}")
            notes.append(f"{label} 屏幕文字：{text}")
        for observation in frame.visual_observations:
            lines.append(f"[{label}][画面观察] {observation}")
            notes.append(f"{label} 画面：{observation}")
    return "\n".join(lines), notes


def _classify_video(
    transcript: str,
    keyframes: list[KeyframeEvidence],
    duration: float,
    audio_activity: float,
) -> ExtractionPlan:
    minutes = max(duration / 60, 1)
    speech_density = len(transcript) / minutes
    ocr_chars = sum(len(text) for frame in keyframes for text in frame.ocr_text)
    observations = sum(len(frame.visual_observations) for frame in keyframes)
    if speech_density >= 80 and ocr_chars < max(80, len(transcript) * 0.15):
        video_type = "speech_dominant"
    elif ocr_chars >= 60 and speech_density < 80:
        video_type = "text_dominant"
    elif speech_density < 25 and observations > 0:
        video_type = "event_footage"
    elif speech_density >= 25 and (ocr_chars > 0 or observations > 0):
        video_type = "mixed"
    elif speech_density < 10 and ocr_chars == 0 and observations == 0:
        video_type = "low_information"
    else:
        video_type = "unknown"

    modalities: list[str] = ["post_context"]
    if transcript:
        modalities.append("speech")
    if ocr_chars:
        modalities.append("screen_text")
    if observations:
        modalities.append("visual")
    reasons = [
        f"语音文本密度约 {speech_density:.0f} 字/分钟",
        f"关键帧 OCR 共 {ocr_chars} 字",
        f"分析 {len(keyframes)} 个自适应关键帧",
    ]
    return ExtractionPlan(
        video_type=video_type,
        active_modalities=modalities,
        highest_cost_level="L2",
        reasons=reasons,
    )


def _local_structured_information(
    source_text: str,
    title: str,
    webpage_url: str,
) -> StructuredInformation:
    """Conservative schema-valid fallback without attempting semantic judgment."""
    case_hash = hashlib.sha256(webpage_url.encode("utf-8")).hexdigest()[:12]
    return StructuredInformation(
        case_id=f"case-{case_hash}",
        content_topic=title.strip() or "未识别内容主题",
        atomic_claims=[],
        implicit_opinions=[],
    )


def _split_readable_paragraphs(text: str, max_chars: int = 420) -> list[str]:
    """Split continuous Chinese prose without dropping any source characters."""
    if not text:
        return []
    sentence_parts = [
        part for part in re.split(r"(?<=[。！？!?])", text) if part
    ]
    paragraphs: list[str] = []
    current = ""
    for sentence in sentence_parts:
        remaining = sentence
        while len(remaining) > max_chars:
            if current:
                paragraphs.append(current)
                current = ""
            paragraphs.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
        if not remaining:
            continue
        if current and len(current) + len(remaining) > max_chars:
            paragraphs.append(current)
            current = remaining
        else:
            current += remaining
    if current:
        paragraphs.append(current)
    return [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]


def _transcript_retention_percent(transcript: str, article: str) -> float:
    """Measure whether readable-article reflow retained the acquired transcript."""
    expected = re.sub(
        r"\[\d{1,2}:\d{2}:\d{2}-\d{1,2}:\d{2}:\d{2}\]\s*",
        "",
        transcript,
    )
    expected = re.sub(r"\s+", "", expected)
    observed = re.sub(r"\s+", "", article)
    if not expected:
        return 100.0
    chunk_size = 80
    chunks = [
        expected[index : index + chunk_size]
        for index in range(0, len(expected), chunk_size)
    ]
    retained = sum(len(chunk) for chunk in chunks if chunk in observed)
    return min(100.0, retained / len(expected) * 100)


def _clean_source_article(source_text: str, title: str) -> str:
    """Turn acquired source layers into readable, deduplicated article text."""
    if not source_text.strip():
        return title.strip()

    section_names = {
        "发布上下文": "发布内容",
        "语音/字幕": "口播与字幕",
        "自适应关键帧 OCR 与观察": "画面文字与观察",
        "全视频多模态补充": "画面补充",
    }
    parts = re.split(r"(?m)^\[([^\]]+)\]\s*$", source_text)
    sections: list[tuple[str, str]] = []
    if len(parts) == 1:
        sections = [("正文", source_text)]
    else:
        prefix = parts[0].strip()
        if prefix:
            sections.append(("正文", prefix))
        for index in range(1, len(parts), 2):
            body = parts[index + 1] if index + 1 < len(parts) else ""
            sections.append((section_names.get(parts[index], parts[index]), body))

    seen: set[str] = set()
    article_sections: list[str] = []
    for section_name, body in sections:
        cleaned_lines: list[str] = []
        section_identities: list[str] = []
        for raw_line in body.splitlines():
            line = raw_line.strip()
            line = re.sub(r"^(?:\[[^\]]+\])+\s*", "", line)
            line = re.sub(
                r"^\[?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\]?\s*",
                "",
                line,
            )
            line = re.sub(
                r"^(?:图片|关键帧)\s*\d+(?:\s*[·,，]\s*[^：:]+)?[：:]\s*",
                "",
                line,
            )
            line = re.sub(
                r"^(?:OCR|屏幕文字|画面|观察|视觉观察)[：:]\s*", "", line
            )
            if line.startswith(("标题：", "作者：")):
                continue
            line = re.sub(r"^简介：\s*", "", line)
            line = re.sub(r"\s+", " ", line).strip(" ·")
            identity = re.sub(r"[\s，。！？、；：,.!?;:]+", "", line).lower()
            if (
                len(identity) < 4
                or identity in seen
                or line.endswith(("...", "…"))
                or re.fullmatch(r"\d+(?:\.\d+)?", identity)
                or identity
                in {
                    "快速",
                    "打电话",
                    "ai创作",
                    "发消息或按住说话",
                    "app内打开",
                }
            ):
                continue
            if any(
                identity in existing and len(existing) >= len(identity) + 8
                for existing in section_identities
            ):
                continue
            contained_indices = [
                index
                for index, existing in enumerate(section_identities)
                if existing in identity and len(identity) >= len(existing) + 8
            ]
            for index in reversed(contained_indices):
                seen.discard(section_identities[index])
                del section_identities[index]
                del cleaned_lines[index]
            seen.add(identity)
            section_identities.append(identity)
            cleaned_lines.append(line)

        if not cleaned_lines:
            continue
        text = " ".join(cleaned_lines)
        text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
        paragraphs = _split_readable_paragraphs(text)
        article_sections.append(
            f"{section_name}\n" + "\n\n".join(paragraphs or [text])
        )

    return "\n\n".join(article_sections).strip() or title.strip()


def _structured_reading_result(
    structured: StructuredInformation,
) -> tuple[str, list[str], list[str]]:
    sections = [
        *structured.atomic_claims,
        *structured.implicit_opinions,
    ]
    topic = re.sub(r"\s+", " ", structured.content_topic).strip()
    topic = re.sub(
        r"[。！？!?；;，,、：:\s]+(?=[”’）】》」』]*$)", "", topic
    )
    summary = f"{topic}。" if topic else ""
    claims = []
    for claim in structured.atomic_claims[:3]:
        normalized = re.sub(r"\s+", " ", claim).strip()
        normalized = re.sub(
            r"[。！？!?；;，,、：:\s]+(?=[”’）】》」』]*$)",
            "",
            normalized,
        )
        if normalized:
            claims.append(normalized)
    if claims:
        summary += "核心主张：" + "；".join(claims) + "。"
    return summary, sections[:8], [structured.content_topic]


async def _analyze_visual(
    url: str,
    info: dict[str, Any],
    metadata: dict[str, Any],
    local_media: Path | None = None,
) -> tuple[dict[str, Any], Path | None]:
    fps = max(0.1, min(10.0, settings.video_visual_fps))
    if local_media is not None:
        return await summarize_video(local_media, metadata, fps=fps), local_media
    progressive = _select_progressive_media_url(info)
    if progressive:
        try:
            return await summarize_video(progressive, metadata, fps=fps), None
        except MimoError:
            pass
    with tempfile.TemporaryDirectory(prefix="mimo-visual-") as temp_dir:
        media = await asyncio.to_thread(_download_merged_video, url, temp_dir, info)
        return await summarize_video(media, metadata, fps=fps), None


async def _analyze_live_photo_videos(
    urls: list[str],
    metadata: dict[str, Any],
    http_headers: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Analyze every motion component while bounding concurrent MiMo requests."""
    semaphore = asyncio.Semaphore(3)

    async def analyze_one(
        index: int, url: str, temp_dir: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        async with semaphore:
            try:
                result = await summarize_video(url, metadata, fps=1.0)
                return {"url": url, **result}, None
            except Exception:
                destination = Path(temp_dir) / f"live-{index:02d}.mp4"
                try:
                    async with httpx.AsyncClient(
                        headers=http_headers or {}, follow_redirects=True, timeout=30
                    ) as client:
                        response = await client.get(url)
                        response.raise_for_status()
                    destination.write_bytes(response.content)
                    if not response.content:
                        return None, url
                    result = await summarize_video(destination, metadata, fps=1.0)
                    return {"url": url, **result}, None
                except Exception:
                    return None, url

    with tempfile.TemporaryDirectory(prefix="mimo-xhs-live-") as temp_dir:
        outcomes = await asyncio.gather(*(
            analyze_one(index, url, temp_dir)
            for index, url in enumerate(urls, 1)
        ))
    results = [result for result, _ in outcomes if result is not None]
    failures = [url for _, url in outcomes if url is not None]
    return results, failures


def _live_photo_context(
    results: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    lines: list[str] = []
    notes: list[str] = []
    for index, result in enumerate(results, 1):
        observations = [
            str(result.get("summary") or "").strip(),
            *(
                str(item).strip()
                for item in result.get("key_points") or []
            ),
            *(
                str(item).strip()
                for item in result.get("on_screen_text") or []
            ),
        ]
        observations = [item for item in observations if item]
        if observations:
            lines.append(f"[实况片段 {index}] " + "；".join(observations))
            notes.extend(observations)
    return "\n".join(lines), list(dict.fromkeys(notes))


def _timing(name: str, started: float) -> StageTiming:
    return StageTiming(
        name=name, milliseconds=round((time.perf_counter() - started) * 1000)
    )


async def analyze(url: str, mode: str) -> AnalyzeResponse:
    pipeline_started = time.perf_counter()
    timings: list[StageTiming] = []
    cost_trace = [
        CostStep(
            level="L0",
            name="缓存、URL 与平台元数据",
            executed=True,
            reason="所有请求必经的近零成本探测层",
        )
    ]
    stage = time.perf_counter()
    try:
        info = await asyncio.to_thread(_extract_info, url)
    except Exception as exc:
        raise PipelineError(f"视频网址解析失败：{exc}") from exc
    timings.append(_timing("解析视频网址", stage))

    metadata = _metadata(info, url)
    is_image_carousel = bool(info.get("note_images"))
    duration = float(metadata.duration_seconds or 0)
    if duration and duration > settings.max_duration_seconds:
        raise PipelineError(
            f"视频时长 {round(duration / 60, 1)} 分钟，超过 Demo 的 "
            f"{round(settings.max_duration_seconds / 60)} 分钟限制"
        )
    metadata_for_prompt = {
        **metadata.model_dump(),
        "description": str(info.get("description") or ""),
    }

    transcript = ""
    subtitle_end = 0.0
    selected = choose_subtitle(info)
    subtitle_source = "none"
    if selected:
        stage = time.perf_counter()
        try:
            transcript, subtitle_end = await _download_subtitle(selected, info)
        except Exception:
            transcript = ""
        timings.append(_timing("获取平台字幕", stage))
        if transcript:
            subtitle_source = "automatic" if selected["automatic"] else "human"

    if not settings.mimo_api_key:
        if mode == "visual":
            raise PipelineError("全模态模式需要先在 .env 中配置 MIMO_API_KEY")
        strategy = "subtitle" if transcript else "metadata"
        source_text = transcript or str(info.get("description") or "")
        result = local_extractive_summary(
            source_text,
            metadata.title,
            str(info.get("description") or ""),
        )
        structured = _local_structured_information(
            source_text, metadata.title, metadata.webpage_url
        )
        cleaned_article = _clean_source_article(source_text, metadata.title)
        text_retention = _transcript_retention_percent(
            transcript, cleaned_article
        )
        coverage_percent = (
            min(100.0, subtitle_end / duration * 100)
            if duration and subtitle_end
            else (100.0 if transcript else 0.0)
        )
        coverage = CoverageInfo(
            status="partial" if transcript else (
                "partial" if transcript else "metadata_only"
            ),
            audio_percent=round(coverage_percent, 1),
            speech_percent=round(coverage_percent, 1),
            text_retention_percent=round(text_retention, 1),
            visual_analyzed=False,
            post_context_captured=True,
            subtitle_source=subtitle_source,
            critical_gaps=["未执行 OCR、视觉分析和模型级结构化转换"],
        )
        cost_trace.extend(
            [
                CostStep(
                    level="L1",
                    name="字幕与本地轻量解析",
                    executed=bool(transcript),
                    reason="存在平台字幕" if transcript else "没有可解析字幕",
                ),
                CostStep(
                    level="L2",
                    name="ASR、OCR 与结构化信息转换",
                    executed=False,
                    reason="未配置 MIMO_API_KEY",
                ),
                CostStep(
                    level="L3",
                    name="全视频多模态升级",
                    executed=False,
                    reason="未配置 MIMO_API_KEY",
                ),
            ]
        )
        return AnalyzeResponse(
            request_id=uuid.uuid4().hex[:12],
            cached=False,
            strategy=strategy,
            metadata=metadata,
            summary=str(result.get("summary") or "").strip(),
            key_points=[str(item) for item in result.get("key_points", [])][:5],
            topics=[str(item) for item in result.get("topics", [])][:8],
            coverage_note="未配置 MiMo Key，当前结果不能作为信源核实输入。",
            transcript=transcript or None,
            transcript_excerpt=transcript[:1200] if transcript else None,
            transcript_chars=len(transcript),
            full_source_text=source_text or None,
            cleaned_article=cleaned_article,
            timings=timings,
            extraction_milliseconds=round(
                (time.perf_counter() - pipeline_started) * 1000
            ),
            coverage=coverage,
            structured_data=structured,
            extraction_plan=ExtractionPlan(
                video_type="unknown",
                active_modalities=(
                    ["speech", "post_context"] if transcript else ["post_context"]
                ),
                highest_cost_level="L1" if transcript else "L0",
                reasons=["本地预览不能作为自动信源核实输入"],
            ),
            cost_trace=cost_trace,
        )

    estimated_cost = 0.0
    missing_ranges: list[str] = []
    audio_coverage = 0.0
    audio_activity = 100.0 if transcript else 0.0
    keyframes: list[KeyframeEvidence] = []
    keyframe_success = False
    full_visual_result: dict[str, Any] | None = None
    full_visual_executed = False
    structured_conversion_degraded = False
    structured_conversion_error = ""
    plan = ExtractionPlan()
    raw_frames: list[AdaptiveFrame] = []
    keyframe_output: dict[str, Any] | BaseException | None = None
    live_photo_results: list[dict[str, Any]] = []
    live_photo_failures: list[str] = []
    visual_fallback_required = _needs_visual_fallback(
        transcript=transcript,
        is_image_carousel=is_image_carousel,
        mode=mode,
    )

    with tempfile.TemporaryDirectory(prefix="mimo-extract-") as temp_dir:
        if is_image_carousel:
            stage = time.perf_counter()
            local_media = None
            raw_frames = await asyncio.to_thread(
                _download_note_images, info, temp_dir
            )
            download_label = f"下载图文全部 {len(raw_frames)} 张图片"
            timings.append(_timing(download_label, stage))
        elif transcript and not visual_fallback_required:
            local_media = None
        else:
            stage = time.perf_counter()
            local_media = await asyncio.to_thread(
                _download_merged_video, url, temp_dir, info
            )
            download_label = "下载并标准化低清音视频"
            timings.append(_timing(download_label, stage))

        if transcript:
            audio_coverage = (
                min(100.0, subtitle_end / duration * 100)
                if duration and subtitle_end
                else 100.0
            )
            processed_seconds = 0.0
        elif is_image_carousel:
            audio_activity = 0.0
            audio_coverage = 0.0
            processed_seconds = 0.0
        else:
            try:
                audio_path = await asyncio.to_thread(_extract_audio, local_media)
                audio_activity = await asyncio.to_thread(
                    _audio_activity_percent, audio_path, duration
                )
            except PipelineError:
                audio_path = None
                audio_activity = 0.0
            if audio_path is not None and audio_activity >= 8:
                chunks = await asyncio.to_thread(
                    _split_audio, audio_path, duration
                )
                stage = time.perf_counter()
                try:
                    transcript, audio_coverage, missing_ranges, processed_seconds = (
                        await _transcribe_chunks(chunks, duration)
                    )
                except Exception:
                    transcript = ""
                    audio_coverage = 0.0
                    missing_ranges = ["ASR 转写失败，已降级为画面文字提取"]
                    processed_seconds = 0.0
                timings.append(_timing("完整音频 ASR", stage))
                estimated_cost += processed_seconds / 3600 * 0.5
            else:
                transcript = ""
                audio_coverage = 0.0
                processed_seconds = 0.0

        visual_fallback_required = _needs_visual_fallback(
            transcript=transcript,
            is_image_carousel=is_image_carousel,
            mode=mode,
        )
        if visual_fallback_required and not is_image_carousel:
            stage = time.perf_counter()
            try:
                raw_frames = await asyncio.to_thread(
                    _extract_adaptive_keyframes, local_media, temp_dir
                )
            except Exception:
                raw_frames = []
            timings.append(_timing("场景变化与周期关键帧选择", stage))

        if visual_fallback_required and raw_frames:
            stage = time.perf_counter()
            try:
                keyframe_output = await _analyze_frame_batches(
                    raw_frames, metadata_for_prompt
                )
            except Exception:
                keyframe_output = None
            timings.append(
                _timing(
                    "逐图 OCR 与视觉理解"
                    if is_image_carousel
                    else "关键帧 OCR 与画面观察",
                    stage,
                )
            )

        if isinstance(keyframe_output, BaseException):
            keyframe_output = None
        keyframe_success = isinstance(keyframe_output, dict)
        if keyframe_success:
            estimated_cost += _token_usage_cost_cny(keyframe_output)
        keyframes = _normalize_keyframe_result(
            raw_frames, keyframe_output if keyframe_success else None
        )
        live_photo_urls = [
            str(value) for value in info.get("live_photo_videos") or [] if value
        ]
        if live_photo_urls:
            stage = time.perf_counter()
            live_photo_results, live_photo_failures = (
                await _analyze_live_photo_videos(
                    live_photo_urls,
                    metadata_for_prompt,
                    info.get("http_headers") or {},
                )
            )
            timings.append(_timing("逐段理解实况照片动态轨", stage))
            estimated_cost += sum(
                _token_usage_cost_cny(result)
                for result in live_photo_results
            )
        plan = _classify_video(
            transcript, keyframes, duration, audio_activity
        )

        should_escalate = (
            not is_image_carousel
            and visual_fallback_required
            and (
                mode == "visual"
                or not keyframe_success
                or (
                    settings.full_visual_escalation
                    and plan.video_type in {"event_footage", "low_information"}
                )
            )
        )
        if should_escalate:
            stage = time.perf_counter()
            try:
                normalized_media = (
                    local_media
                    if local_media is not None
                    and local_media.name == "video-mimo.mp4"
                    else await asyncio.to_thread(
                        _compress_video_for_mimo, local_media
                    )
                )
                full_visual_result = await summarize_video(
                    normalized_media,
                    metadata_for_prompt,
                    fps=max(0.1, settings.video_visual_fps),
                )
                full_visual_executed = True
                estimated_cost += (
                    _token_usage_cost_cny(full_visual_result)
                    or _estimate_visual_cost_cny(
                        duration,
                        int(info.get("width") or 1280),
                        int(info.get("height") or 720),
                        max(0.1, settings.video_visual_fps),
                    )
                )
            except Exception:
                full_visual_result = None
            timings.append(_timing("L3 全视频多模态升级", stage))
            plan.highest_cost_level = "L3"
            plan.reasons.append(
                "用户指定全模态"
                if mode == "visual"
                else "关键帧不足或视频以无口播事件为主"
            )
        else:
            plan.highest_cost_level = "L2"

        keyframe_text, keyframe_notes = _keyframe_context(keyframes)
        full_visual_text, full_visual_notes = _visual_context(full_visual_result)
        live_photo_text, live_photo_notes = _live_photo_context(
            live_photo_results
        )
        source_context = metadata.source_context or {}
        context_lines = [
            f"话题：{'、'.join(map(str, source_context.get('topics') or []))}"
            if source_context.get("topics") else "",
            f"附加卡片：{'、'.join(map(str, source_context.get('attachments') or []))}"
            if source_context.get("attachments") else "",
        ]
        context_text = "\n".join(item for item in context_lines if item)
        source_sections = [
            (
                "[发布上下文]\n"
                f"标题：{metadata.title}\n"
                f"作者：{metadata.uploader or ''}\n"
                f"帖子类型：{metadata.source_subtype or metadata.content_type}\n"
                f"{context_text}\n"
                f"正文：{metadata_for_prompt['description'][:settings.max_transcript_chars]}"
            ),
            f"[语音/字幕]\n{transcript}" if transcript else "",
            f"[自适应关键帧 OCR 与观察]\n{keyframe_text}" if keyframe_text else "",
            (
                f"[实况照片动态轨]\n{live_photo_text}"
                if live_photo_text else ""
            ),
            (
                f"[全视频多模态补充]\n{full_visual_text}"
                if full_visual_text
                else ""
            ),
        ]
        combined_input = "\n\n".join(
            section for section in source_sections if section
        )

        stage = time.perf_counter()
        try:
            structured_result = await structure_information(
                combined_input, metadata_for_prompt
            )
            structured = _structured_information_from_model_result(structured_result)
        except MimoError as exc:
            structured_conversion_degraded = True
            structured_conversion_error = str(exc)
            structured_result = {}
            structured = _local_structured_information(
                combined_input, metadata.title, metadata.webpage_url
            )
        timings.append(_timing("标准结构化信息转换", stage))
        estimated_cost += _token_usage_cost_cny(structured_result) or (
            min(len(combined_input), settings.max_transcript_chars) / 1_000_000
            + 5000 / 1_000_000 * 2
        )
        summary, key_points, topics = _structured_reading_result(structured)

    cleaned_article = _clean_source_article(combined_input, metadata.title)
    text_retention = _transcript_retention_percent(
        transcript, cleaned_article
    )
    visual_analyzed = (
        keyframe_success or full_visual_executed or bool(live_photo_results)
    )
    visual_notes = list(
        dict.fromkeys([
            *keyframe_notes, *live_photo_notes, *full_visual_notes
        ])
    )[:40]
    critical_gaps: list[str] = []
    speech_active = "speech" in plan.active_modalities
    if speech_active and audio_coverage < 95:
        critical_gaps.append("语音覆盖不足 95%")
    if missing_ranges and "语音覆盖不足 95%" not in critical_gaps:
        critical_gaps.append("部分语音分段处理失败")
    if transcript and text_retention < 99:
        critical_gaps.append("完整全文重组保留率不足 99%")
    if visual_fallback_required and not keyframe_success:
        critical_gaps.append("自适应关键帧 OCR 未完成")
    returned_frame_count = len(
        {
            int(item.get("frame_index") or 0)
            for item in (keyframe_output or {}).get("frames", [])
            if isinstance(item, dict)
        }
    )
    frame_coverage = (
        min(100.0, returned_frame_count / len(keyframes) * 100)
        if keyframes
        else 0.0
    )
    if keyframe_success and frame_coverage < 100:
        critical_gaps.append("部分图片/关键帧 OCR 未完成")
    if live_photo_failures:
        critical_gaps.append(
            f"{len(live_photo_failures)} 段实况照片动态轨分析失败"
        )
    if structured_conversion_degraded:
        critical_gaps.append(
            "模型级结构化转换失败，已保留完整原文并执行本地降级"
        )
    if _has_unfilled_event_video_gap(
        is_image_carousel=is_image_carousel,
        visual_fallback_required=visual_fallback_required,
        plan=plan,
        full_visual_executed=full_visual_executed,
    ):
        critical_gaps.append("事件型视频的全视频多模态补充未完成")
    structured_count = (
        len(structured.atomic_claims)
        + len(structured.implicit_opinions)
    )
    if structured_conversion_degraded:
        coverage_status = "needs_review"
    elif critical_gaps:
        coverage_status = "partial"
    elif structured_count:
        coverage_status = "structured_ready"
    else:
        coverage_status = "no_structured_information"

    scene_percent = frame_coverage
    screen_text_percent = frame_coverage
    visual_coverage_note = (
        f"已分析 {returned_frame_count}/{len(keyframes)} "
        f"{'张图文图片' if is_image_carousel else '个场景变化/周期关键帧'}"
        if visual_fallback_required
        else "检测到有效口播文本，已按短路策略跳过关键帧 OCR"
    )
    if info.get("live_photo_videos"):
        visual_coverage_note += (
            f"；已分析 {len(live_photo_results)}/"
            f"{len(info.get('live_photo_videos') or [])} 段实况动态轨"
        )
    coverage_note = (
        f"语音覆盖 {audio_coverage:.1f}%；"
        f"全文重组保留 {text_retention:.1f}%；{visual_coverage_note}；"
        f"结构化输出包含 {len(structured.atomic_claims)} 条原子主张和"
        f"{len(structured.implicit_opinions)} 条隐性观点。"
    )
    if structured_conversion_degraded:
        coverage_note += (
            f" 结构化转换已降级：{structured_conversion_error}；"
            "下游应结合完整原文复核。"
        )
    if critical_gaps:
        coverage_note += f" 关键缺口：{'；'.join(critical_gaps)}。"
    if missing_ranges:
        coverage_note += f" 未覆盖区间：{', '.join(missing_ranges)}。"

    cost_trace.extend(
        [
            CostStep(
                level="L1",
                name="平台字幕与音频活动探测",
                executed=True,
                reason="优先判断是否已有足够口播文本",
            ),
            CostStep(
                level="L2",
                name="完整 ASR 或关键帧 OCR 与结构化转换",
                executed=True,
                reason=(
                    "复用完整平台字幕，跳过 ASR 与视觉提取"
                    if subtitle_source != "none"
                    else "下载全部图文图片并逐图执行 OCR/视觉理解"
                    if is_image_carousel
                    else (
                        "完整 ASR 已获得有效口播文本，跳过关键帧 OCR"
                        if _has_usable_spoken_text(transcript)
                        else "无有效口播文本，降级执行关键帧 OCR"
                    )
                ),
            ),
            CostStep(
                level="L3",
                name="全视频多模态升级",
                executed=full_visual_executed,
                reason=(
                    "用户指定或事件型/低信息视频触发"
                    if full_visual_executed
                    else "上层信息提取已完成，无需升级"
                ),
            ),
        ]
    )
    strategy = (
        "hybrid"
        if transcript and visual_analyzed
        else "visual"
        if visual_analyzed
        else "subtitle"
        if subtitle_source != "none"
        else "asr"
    )

    return AnalyzeResponse(
        request_id=uuid.uuid4().hex[:12],
        cached=False,
        strategy=strategy,
        metadata=metadata,
        summary=summary,
        key_points=key_points,
        topics=topics,
        coverage_note=coverage_note,
        transcript=transcript or None,
        transcript_excerpt=transcript[:5000] if transcript else None,
        transcript_chars=len(transcript),
        full_source_text=combined_input,
        structured_input_text=combined_input[: settings.max_transcript_chars],
        structured_input_chars=min(
            len(combined_input), settings.max_transcript_chars
        ),
        structured_input_truncated=(
            len(combined_input) > settings.max_transcript_chars
        ),
        cleaned_article=cleaned_article,
        timings=timings,
        extraction_milliseconds=round(
            (time.perf_counter() - pipeline_started) * 1000
        ),
        estimated_cost_cny=round(estimated_cost, 5),
        coverage=CoverageInfo(
            status=coverage_status,
            audio_percent=round(audio_coverage, 1),
            speech_percent=round(audio_coverage, 1),
            text_retention_percent=round(text_retention, 1),
            screen_text_percent=screen_text_percent,
            scene_percent=scene_percent,
            visual_analyzed=visual_analyzed,
            post_context_captured=True,
            provenance_checked=False,
            subtitle_source=subtitle_source,
            missing_ranges=missing_ranges,
            critical_gaps=critical_gaps,
        ),
        visual_notes=visual_notes,
        keyframes=keyframes,
        structured_data=structured,
        extraction_plan=plan,
        cost_trace=cost_trace,
    )
