from __future__ import annotations

import html as html_module
import http.cookiejar
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings


class ChannelsParseError(RuntimeError):
    pass


class ChannelsSessionError(ChannelsParseError):
    pass


def is_channels_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"channels.weixin.qq.com", "weixin.qq.com"}


def _first_line(value: str, fallback: str) -> str:
    cleaned = value.replace("\ufeff", "").strip()
    line = next((item.strip().strip('"“”') for item in cleaned.splitlines() if item.strip()), "")
    return line[:120] or fallback


def parse_channels_feed(payload: dict[str, Any], webpage_url: str) -> dict[str, Any]:
    if int(payload.get("errCode") or 0) != 0:
        raise ChannelsSessionError(f"视频号详情接口拒绝请求：{payload.get('errMsg') or '未知错误'}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    feed = data.get("feedInfo") if isinstance(data.get("feedInfo"), dict) else {}
    author = data.get("authorInfo") if isinstance(data.get("authorInfo"), dict) else {}
    description = str(feed.get("description") or "").strip()

    video_urls: list[str] = []
    image_urls: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and value.startswith(("http://", "https://")):
            if "video" in key or re.search(r"\.(?:mp4|mov)(?:\?|$)", value, re.I):
                video_urls.append(value)
            elif any(token in key for token in ("pic", "image", "cover", "thumb")):
                image_urls.append(value)

    visit(feed)
    if not video_urls and feed.get("coverUrl"):
        image_urls.append(str(feed["coverUrl"]))
    video_urls = list(dict.fromkeys(video_urls))
    image_urls = list(dict.fromkeys(image_urls))
    if not description and not video_urls and not image_urls:
        raise ChannelsParseError("视频号详情没有返回正文或媒体")
    short_id = parse_qs(urlparse(webpage_url).query).get("id", [""])[0]
    formats = [{"url": value, "ext": "mp4", "vcodec": "h264", "acodec": "aac", "format_id": f"channels-{index}"}
        for index, value in enumerate(video_urls, 1)]
    images = [{"url": value, "fallback_urls": [value], "width": 0, "height": 0}
        for value in image_urls]
    return {
        "id": short_id or str((data.get("sceneInfo") or {}).get("dynamicExportId") or webpage_url),
        "title": _first_line(description, "视频号内容"),
        "description": description,
        "uploader": str(author.get("nickname") or "").strip() or None,
        "duration": None,
        "thumbnail": image_urls[0] if image_urls else None,
        "webpage_url": webpage_url,
        "extractor": "WechatChannels",
        "extractor_key": "WechatChannels",
        "source_subtype": "wechat_channels_video" if formats else "wechat_channels_image_post",
        "formats": formats,
        "note_images": [] if formats else images,
        "source_context": {"scene": data.get("sceneInfo") or {}},
        "_browser_native": bool(formats),
    }


def parse_channels_html(document: str, webpage_url: str) -> dict[str, Any]:
    lowered = document.lower()
    if "support.weixin.qq.com/update" in lowered:
        raise ChannelsSessionError("视频号分享链接已失效或 exportkey 已过期，请重新复制分享链接")
    soup = BeautifulSoup(document, "html.parser")

    def meta(*names: str) -> str | None:
        for name in names:
            node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if node and node.get("content"):
                return str(node["content"]).strip()
        return None

    candidates = [meta("og:video", "og:video:url", "twitter:player:stream")]
    patterns = (
        r'https?:\\?/\\?/[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+\.mp4(?:\?[^"\'<>\\\s]*)?',
        r'"(?:url|videoUrl|video_url)"\s*:\s*"(https?:\\?/\\?/[^"<>]+)"',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, document, re.IGNORECASE):
            value = match.group(1) if match.lastindex else match.group(0)
            candidates.append(html_module.unescape(value).replace("\\/", "/"))
    media = next((item for item in candidates if isinstance(item, str) and item.startswith(("http://", "https://"))), None)
    if not media:
        raise ChannelsSessionError(
            "视频号页面未公开媒体流；请提供新鲜分享链接并配置同设备低权限 Cookie，或上传原视频"
        )
    title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "视频号视频")
    return {
        "id": urlparse(webpage_url).query or webpage_url,
        "title": title,
        "description": meta("og:description", "description") or "",
        "uploader": meta("author"),
        "duration": None,
        "thumbnail": meta("og:image", "twitter:image"),
        "webpage_url": webpage_url,
        "extractor": "WechatChannels",
        "extractor_key": "WechatChannels",
        "source_subtype": "wechat_channels_video",
        "formats": [{"url": media, "ext": "mp4", "vcodec": "h264", "acodec": "aac", "format_id": "page"}],
        "_browser_native": True,
    }


def _cookies(path: str) -> dict[str, str]:
    if not path:
        return {}
    cookie_path = Path(path)
    if not cookie_path.is_file():
        raise ChannelsSessionError(f"视频号 Cookie 文件不存在：{cookie_path}")
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise ChannelsSessionError(f"视频号 Cookie 文件无法读取：{exc}") from exc
    return {cookie.name: cookie.value for cookie in jar}


def extract_channels_info(url: str) -> dict[str, Any]:
    user_agent = settings.channels_user_agent or settings.ytdlp_user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
    cookie_file = settings.channels_cookies_file or settings.ytdlp_cookies_file
    try:
        headers = {"User-Agent": user_agent, "Referer": "https://channels.weixin.qq.com/"}
        with httpx.Client(headers=headers, cookies=_cookies(cookie_file), follow_redirects=True, timeout=30) as client:
            response = client.get(url)
            response.raise_for_status()
            final_url = str(response.url)
            if "support.weixin.qq.com/update" in final_url:
                raise ChannelsSessionError("视频号分享链接已失效或 exportkey 已过期，请重新复制分享链接")
            parsed = urlparse(final_url)
            short_uri = parse_qs(parsed.query).get("id", [""])[0]
            if parsed.path == "/finder-preview/pages/sph" and short_uri:
                api_response = client.post(
                    "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info",
                    params={"_pageUrl": "https://channels.weixin.qq.com/finder-preview/pages/sph"},
                    headers={"Referer": final_url, "Origin": "https://channels.weixin.qq.com"},
                    json={"baseReq": {"generalToken": ""}, "shortUri": short_uri},
                )
                api_response.raise_for_status()
                info = parse_channels_feed(api_response.json(), final_url)
            else:
                info = parse_channels_html(response.text, final_url)
            info["http_headers"] = {"User-Agent": user_agent, "Referer": final_url}
            return info
    except ChannelsParseError:
        raise
    except httpx.HTTPError as exc:
        raise ChannelsSessionError(f"视频号页面请求失败：{exc}") from exc
