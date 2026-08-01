from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.extractor.weibo import WeiboIE


class WeiboParseError(RuntimeError):
    pass


def is_weibo_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "weibo.com" or host.endswith(".weibo.com") or host.endswith(".weibo.cn")


def is_weibo_status_url(url: str) -> bool:
    return is_weibo_url(url) and bool(
        re.fullmatch(r"/\d+/[A-Za-z0-9]+/?", urlparse(url).path)
    )


def _title(description: str) -> str:
    first = next((line.strip() for line in description.splitlines() if line.strip()), "")
    return first[:120] or "微博帖子"


def parse_weibo_status(meta: dict[str, Any], webpage_url: str) -> dict[str, Any]:
    description = str(meta.get("text_raw") or "").strip()
    user = meta.get("user") if isinstance(meta.get("user"), dict) else {}
    images: list[dict[str, Any]] = []
    pic_infos = meta.get("pic_infos") if isinstance(meta.get("pic_infos"), dict) else {}
    for pic_id in meta.get("pic_ids") or pic_infos:
        raw = pic_infos.get(pic_id) if isinstance(pic_infos.get(pic_id), dict) else {}
        versions = [raw.get(name) for name in ("original", "largest", "large", "bmiddle", "thumbnail")]
        versions = [item for item in versions if isinstance(item, dict) and str(item.get("url") or "").startswith(("http://", "https://"))]
        if not versions:
            continue
        preferred = versions[0]
        images.append({
            "url": str(preferred["url"]),
            "fallback_urls": list(dict.fromkeys(str(item["url"]) for item in versions)),
            "width": int(preferred.get("width") or 0),
            "height": int(preferred.get("height") or 0),
        })
    if not description and not images:
        raise WeiboParseError("微博帖子没有返回可解析正文或媒体")
    return {
        "id": str(meta.get("idstr") or meta.get("id") or meta.get("mid") or ""),
        "display_id": str(meta.get("mblogid") or ""),
        "title": _title(description),
        "description": description,
        "uploader": str(user.get("screen_name") or "").strip() or None,
        "uploader_id": str(user.get("idstr") or user.get("id") or "").strip() or None,
        "duration": None,
        "thumbnail": images[0]["url"] if images else None,
        "webpage_url": webpage_url,
        "extractor": "WeiboPost",
        "extractor_key": "WeiboPost",
        "formats": [],
        "note_images": images,
        "source_subtype": "weibo_image_post" if images else "weibo_text_post",
        "source_context": {"topics": [str(item.get("topic_title")) for item in meta.get("topic_struct") or [] if isinstance(item, dict) and item.get("topic_title")]},
        "http_headers": {"Referer": "https://weibo.com/"},
    }


def extract_weibo_post_info(url: str, ydl_options: dict[str, Any]) -> dict[str, Any]:
    display_id = urlparse(url).path.rstrip("/").split("/")[-1]
    try:
        with YoutubeDL({**ydl_options, "skip_download": True}) as downloader:
            extractor = WeiboIE(downloader)
            meta = extractor._weibo_download_json(
                "https://weibo.com/ajax/statuses/show",
                display_id,
                query={"id": display_id},
            )
            if meta.get("mix_media_info"):
                entries = list(extractor._entries(meta["mix_media_info"].get("items") or []))
                if entries:
                    return {"_type": "playlist", "id": str(meta.get("id") or display_id), "entries": entries}
            video_info = extractor._parse_video_info(meta)
            if video_info.get("formats"):
                video_info["source_subtype"] = "weibo_video"
                video_info["source_context"] = {"topics": video_info.get("tags") or []}
                return video_info
        return parse_weibo_status(meta, url)
    except WeiboParseError:
        raise
    except Exception as exc:
        raise WeiboParseError(f"微博帖子详情请求失败：{exc}") from exc
