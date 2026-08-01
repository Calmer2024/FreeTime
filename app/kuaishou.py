from __future__ import annotations

import http.cookiejar
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.security import BROWSER_USER_AGENT


class KuaishouParseError(RuntimeError):
    pass


class KuaishouSessionError(KuaishouParseError):
    pass


_QUERY = """
query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) {
  visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) {
    status type
    author { id name }
    photo {
      id duration caption coverUrl photoUrl photoH265Url croppedPhotoUrl
      timestamp videoRatio
      manifest { adaptationSet { representation { id url backupUrl codecs width height frameRate qualityType } } }
    }
    tags { name }
  }
}
"""


def is_kuaishou_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "kuaishou.com" or host.endswith(".kuaishou.com")


def _photo_id(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[-2] == "short-video":
        return parts[-1]
    raise KuaishouParseError("快手链接不是受支持的 /short-video/{作品ID} 作品页")


def parse_kuaishou_html(document: str, webpage_url: str) -> dict[str, Any]:
    marker = "window.__APOLLO_STATE__="
    start = document.find(marker)
    if start < 0:
        raise KuaishouParseError("快手页面没有返回 Apollo 作品数据")
    try:
        state, _ = json.JSONDecoder().raw_decode(document[start + len(marker):].lstrip())
    except (json.JSONDecodeError, TypeError) as exc:
        raise KuaishouParseError("快手页面 Apollo 作品数据格式无效") from exc
    client = state.get("defaultClient") if isinstance(state, dict) else None
    if not isinstance(client, dict):
        raise KuaishouParseError("快手页面 Apollo 缓存为空")
    photo_id = _photo_id(webpage_url)
    detail = next((value for key, value in client.items()
        if key.startswith("$ROOT_QUERY.visionVideoDetail") and photo_id in key
        and isinstance(value, dict)), None)
    if not detail:
        raise KuaishouParseError("快手页面没有当前作品详情")

    def dereference(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        reference = value.get("id")
        entity = client.get(reference) if isinstance(reference, str) else None
        return entity if isinstance(entity, dict) else value

    payload = {"data": {"visionVideoDetail": {
        "status": detail.get("status"),
        "type": detail.get("type"),
        "author": dereference(detail.get("author")),
        "photo": dereference(detail.get("photo")),
        "tags": detail.get("tags") or [],
    }}}
    return parse_kuaishou_detail(payload, webpage_url)


def _duration_seconds(value: Any) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration / 1000 if duration > 1000 else duration


def parse_kuaishou_detail(payload: dict[str, Any], webpage_url: str) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    if int(data.get("result") or 0) == 400002 or data.get("captcha"):
        raise KuaishouSessionError(
            "快手要求完成验证码；请由人工刷新低权限浏览会话后重新导出 Cookie，系统不会绕过验证码"
        )
    detail = data.get("visionVideoDetail")
    if not isinstance(detail, dict):
        raise KuaishouParseError("快手接口没有返回作品详情，分享可能已删除或会话已失效")
    photo = detail.get("photo") if isinstance(detail.get("photo"), dict) else {}
    author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
    media: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(url: Any, **fields: Any) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")) or url in seen:
            return
        seen.add(url)
        media.append(
            {
                "url": url,
                "ext": "mp4",
                "protocol": "https",
                "vcodec": fields.pop("vcodec", None) or "h264",
                "acodec": "aac",
                **fields,
            }
        )

    add(photo.get("photoUrl"), format_id="photoUrl")
    add(photo.get("croppedPhotoUrl"), format_id="croppedPhotoUrl")
    add(photo.get("photoH265Url"), format_id="photoH265Url", vcodec="hevc")
    manifest = photo.get("manifest") if isinstance(photo.get("manifest"), dict) else {}
    for adaptation in manifest.get("adaptationSet") or []:
        if not isinstance(adaptation, dict):
            continue
        for representation in adaptation.get("representation") or []:
            if not isinstance(representation, dict):
                continue
            backups = representation.get("backupUrl") or []
            if isinstance(backups, str):
                backups = [backups]
            add(
                representation.get("url"),
                format_id=str(representation.get("id") or representation.get("qualityType") or "manifest"),
                width=representation.get("width"),
                height=representation.get("height"),
                fps=representation.get("frameRate"),
                vcodec=representation.get("codecs"),
                fallback_urls=[item for item in backups if isinstance(item, str)],
            )
    if not media:
        raise KuaishouParseError("快手作品详情没有返回可访问的视频流")
    caption = str(photo.get("caption") or "").strip()
    tags = [str(item.get("name")) for item in detail.get("tags") or [] if isinstance(item, dict) and item.get("name")]
    return {
        "id": str(photo.get("id") or _photo_id(webpage_url)),
        "title": caption or "快手视频",
        "description": caption,
        "uploader": str(author.get("name") or "").strip() or None,
        "uploader_id": author.get("id"),
        "duration": _duration_seconds(photo.get("duration")),
        "thumbnail": photo.get("coverUrl"),
        "timestamp": photo.get("timestamp"),
        "webpage_url": webpage_url,
        "extractor": "Kuaishou",
        "extractor_key": "Kuaishou",
        "source_subtype": "kuaishou_video",
        "source_context": {"tags": tags},
        "formats": media,
        "_browser_native": True,
    }


def _load_cookie_file(path: str) -> dict[str, str]:
    if not path:
        return {}
    cookie_path = Path(path)
    if not cookie_path.is_file():
        raise KuaishouSessionError(f"快手 Cookie 文件不存在：{cookie_path}")
    jar = http.cookiejar.MozillaCookieJar(str(cookie_path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError) as exc:
        raise KuaishouSessionError(f"快手 Cookie 文件无法读取：{exc}") from exc
    return {cookie.name: cookie.value for cookie in jar}


def extract_kuaishou_info(url: str) -> dict[str, Any]:
    user_agent = settings.kuaishou_user_agent or settings.ytdlp_user_agent or BROWSER_USER_AGENT
    cookie_file = settings.kuaishou_cookies_file or settings.ytdlp_cookies_file
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    try:
        with httpx.Client(headers=headers, cookies=_load_cookie_file(cookie_file), follow_redirects=True, timeout=30) as client:
            page = client.get(url)
            page.raise_for_status()
            final_url = str(page.url)
            photo_id = _photo_id(final_url)
            try:
                info = parse_kuaishou_html(page.text, final_url)
                info["http_headers"] = {"User-Agent": user_agent, "Referer": final_url}
                return info
            except KuaishouParseError:
                pass
            response = client.post(
                "https://www.kuaishou.com/graphql",
                headers={"Referer": final_url, "Origin": "https://www.kuaishou.com"},
                json={
                    "operationName": "visionVideoDetail",
                    "variables": {"photoId": photo_id, "page": "detail", "webPageArea": "brilliantxxcarefully"},
                    "query": _QUERY,
                },
            )
            response.raise_for_status()
            info = parse_kuaishou_detail(response.json(), final_url)
            info["http_headers"] = {"User-Agent": user_agent, "Referer": final_url}
            return info
    except KuaishouParseError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise KuaishouSessionError(f"快手作品请求失败：{exc}") from exc
