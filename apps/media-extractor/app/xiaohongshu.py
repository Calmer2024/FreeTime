from __future__ import annotations

import http.cookiejar
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from yt_dlp.utils import js_to_json

from app.config import settings
from app.security import BROWSER_USER_AGENT, REDIRECT_LIMIT, canonicalize_video_url, validate_video_url


class XiaohongshuParseError(RuntimeError):
    pass


def is_xiaohongshu_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")


def _assigned_object(source: str, marker_pattern: str) -> str:
    marker = re.search(marker_pattern, source)
    if not marker:
        raise XiaohongshuParseError("页面缺少小红书笔记状态数据")
    start = source.find("{", marker.end())
    if start < 0:
        raise XiaohongshuParseError("小红书笔记状态数据格式无效")
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise XiaohongshuParseError("小红书笔记状态数据不完整")


def _unique_urls(value: Any) -> list[str]:
    urls: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and item.startswith(("http://", "https://")):
            urls.append(item.replace("\\u002F", "/"))

    visit(value)
    return list(dict.fromkeys(urls))


def _named_values(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"name", "title", "displayname", "goodsname", "poiname"}:
                text = str(child or "").strip()
                if text:
                    names.append(text)
            else:
                names.extend(_named_values(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_named_values(child))
    return list(dict.fromkeys(names))


def _note_from_state(state: dict[str, Any], note_id: str) -> dict[str, Any]:
    detail_map = ((state.get("note") or {}).get("noteDetailMap") or {})
    detail = detail_map.get(note_id)
    if not isinstance(detail, dict) and detail_map:
        detail = next((item for item in detail_map.values() if isinstance(item, dict)), None)
    note = (detail or {}).get("note") if isinstance(detail, dict) else None
    if not isinstance(note, dict):
        raise XiaohongshuParseError(
            "页面没有返回公开笔记详情；笔记可能已删除、设为私密或要求登录验证"
        )
    return note


def _static_image(image: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        str(image.get(key) or "")
        for key in ("urlDefault", "urlPre", "url", "traceId")
        if str(image.get(key) or "").startswith(("http://", "https://"))
    ]
    if not candidates:
        candidates = [
            url for url in _unique_urls(image)
            if not re.search(r"(?:video|stream|live-photo)", url, re.IGNORECASE)
        ][:2]
    if not candidates:
        return None
    return {
        "url": candidates[0],
        "fallback_urls": candidates,
        "width": int(image.get("width") or 0),
        "height": int(image.get("height") or 0),
    }


def _live_motion_urls(image: dict[str, Any], static_urls: set[str]) -> list[str]:
    motion_roots = []
    for key, value in image.items():
        lowered = key.lower()
        if any(token in lowered for token in ("live", "motion", "stream", "video")):
            motion_roots.append(value)
    candidates = [url for root in motion_roots for url in _unique_urls(root)]
    return list(dict.fromkeys(url for url in candidates if url not in static_urls))


def _video_formats(note: dict[str, Any]) -> list[dict[str, Any]]:
    video = note.get("video") or {}
    urls = _unique_urls(video)
    formats = []
    for index, url in enumerate(urls, 1):
        if re.search(r"(?:cover|image|webp|jpe?g|png)", url, re.IGNORECASE):
            continue
        formats.append({"format_id": f"xhs-{index}", "url": url, "ext": "mp4"})
    return formats


def parse_xiaohongshu_note_html(html: str, webpage_url: str) -> dict[str, Any]:
    match = re.search(r"/(?:explore|discovery/item)/([\da-z]+)", urlparse(webpage_url).path, re.IGNORECASE)
    if not match:
        raise XiaohongshuParseError("无法从小红书 URL 识别笔记 ID")
    note_id = match.group(1)
    raw_state = _assigned_object(html, r"window\.__INITIAL_STATE__\s*=")
    try:
        state = json.loads(js_to_json(raw_state))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise XiaohongshuParseError("小红书笔记状态数据无法解析") from exc
    note = _note_from_state(state, note_id)
    raw_type = str(note.get("type") or "normal").lower()
    formats = _video_formats(note)
    is_video = raw_type in {"video", "vedio"} or bool(formats)

    note_images: list[dict[str, Any]] = []
    live_videos: list[str] = []
    for raw_image in note.get("imageList") or []:
        if not isinstance(raw_image, dict):
            continue
        image = _static_image(raw_image)
        if image and not is_video:
            note_images.append(image)
        static_urls = set((image or {}).get("fallback_urls") or [])
        live_videos.extend(_live_motion_urls(raw_image, static_urls))
    live_videos = list(dict.fromkeys(live_videos))

    if raw_type in {"multi", "multi_note", "long", "long_note"}:
        subtype = "xiaohongshu_long_note"
    elif is_video:
        subtype = "xiaohongshu_video_note"
        note_images = []
        live_videos = []
    elif live_videos:
        subtype = "xiaohongshu_live_photo_note"
    else:
        subtype = "xiaohongshu_image_note"

    user = note.get("user") or {}
    tags = [
        str(item.get("name") or "").strip()
        for item in note.get("tagList") or []
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    ]
    attachment_roots = [
        note.get(key) for key in (
            "poi", "poiInfo", "goodsInfo", "goodsList", "productInfo",
            "brandInfo", "cooperateInfo", "atUserList",
        ) if note.get(key)
    ]
    attachments = list(dict.fromkeys(
        name for root in attachment_roots for name in _named_values(root)
    ))
    canonical = canonicalize_video_url(webpage_url)
    thumbnail = None
    if note.get("imageList"):
        first_image = _static_image(note["imageList"][0])
        thumbnail = (first_image or {}).get("url")
    description = str(note.get("desc") or "").strip()
    explicit_title = str(note.get("title") or "").strip()
    derived_title = next(
        (line.strip() for line in description.splitlines() if line.strip()), ""
    )
    return {
        "id": str(note.get("noteId") or note_id),
        "title": explicit_title or derived_title[:120] or "小红书笔记",
        "description": description,
        "uploader": str(user.get("nickname") or user.get("nickName") or "").strip() or None,
        "uploader_id": str(user.get("userId") or "").strip() or None,
        "duration": None,
        "thumbnail": thumbnail,
        "webpage_url": canonical,
        "extractor": "XiaoHongShu",
        "extractor_key": "XiaoHongShu",
        "formats": formats,
        "note_images": note_images,
        "live_photo_videos": live_videos,
        "source_subtype": subtype,
        "source_context": {"topics": tags, "attachments": attachments},
        "http_headers": {
            "User-Agent": settings.ytdlp_user_agent or BROWSER_USER_AGENT,
            "Referer": "https://www.xiaohongshu.com/",
        },
    }


def _cookie_dict() -> dict[str, str]:
    if not settings.ytdlp_cookies_file:
        return {}
    cookie_path = Path(settings.ytdlp_cookies_file).expanduser()
    if not cookie_path.is_file():
        return {}
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return {}
    return {
        cookie.name: cookie.value
        for cookie in jar
        if cookie.domain.lstrip(".").endswith("xiaohongshu.com")
    }


def extract_xiaohongshu_info(url: str) -> dict[str, Any]:
    current = canonicalize_video_url(url)
    headers = {
        "User-Agent": settings.ytdlp_user_agent or BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Referer": "https://www.xiaohongshu.com/",
    }
    with httpx.Client(
        headers=headers, cookies=_cookie_dict(), follow_redirects=False, timeout=30
    ) as client:
        for _ in range(REDIRECT_LIMIT + 1):
            validate_video_url(current)
            try:
                response = client.get(current)
            except httpx.HTTPError as exc:
                raise XiaohongshuParseError(f"小红书页面请求失败：{exc}") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise XiaohongshuParseError("小红书页面跳转缺少目标地址")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise XiaohongshuParseError(f"小红书页面返回 HTTP {response.status_code}")
            return parse_xiaohongshu_note_html(response.text, current)
    raise XiaohongshuParseError(f"小红书链接跳转超过 {REDIRECT_LIMIT} 次")
