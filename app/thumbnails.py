from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx

from app.security import BROWSER_USER_AGENT, REDIRECT_LIMIT, validate_public_url


MAX_THUMBNAIL_BYTES = 10 * 1024 * 1024
KEY_PATTERN = re.compile(r"^[a-f0-9]{64}$")


class ThumbnailError(RuntimeError):
    pass


def normalize_thumbnail_url(url: str) -> str:
    value = str(url or "").strip()
    if value.startswith("http://"):
        return "https://" + value[len("http://"):]
    return value


def _referer(webpage_url: str) -> str:
    parsed = urlparse(webpage_url)
    if parsed.scheme in {"http", "https"} and parsed.hostname:
        return f"{parsed.scheme}://{parsed.hostname}/"
    return ""


def _image_extension(data: bytes, content_type: str) -> str | None:
    mime = content_type.split(";", 1)[0].strip().lower()
    if data.startswith(b"\xff\xd8\xff") and mime in {"image/jpeg", "image/jpg", "application/octet-stream"}:
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n") and mime in {"image/png", "application/octet-stream"}:
        return "png"
    if data[:6] in {b"GIF87a", b"GIF89a"} and mime in {"image/gif", "application/octet-stream"}:
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and mime in {"image/webp", "application/octet-stream"}:
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and b"avif" in data[8:16] and mime in {"image/avif", "application/octet-stream"}:
        return "avif"
    return None


def fetch_thumbnail(url: str, webpage_url: str) -> tuple[bytes, str]:
    current = normalize_thumbnail_url(url)
    headers = {
        "User-Agent": BROWSER_USER_AGENT,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    referer = _referer(webpage_url)
    if referer:
        headers["Referer"] = referer
    with httpx.Client(headers=headers, follow_redirects=False, timeout=30) as client:
        for _ in range(REDIRECT_LIMIT + 1):
            validate_public_url(current)
            try:
                with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise ThumbnailError("封面重定向缺少目标地址")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > MAX_THUMBNAIL_BYTES:
                            raise ThumbnailError("封面文件超过 10MB 安全限制")
                        chunks.append(chunk)
                    return b"".join(chunks), content_type
            except httpx.HTTPError as exc:
                raise ThumbnailError(f"封面请求失败：{exc}") from exc
    raise ThumbnailError(f"封面重定向超过 {REDIRECT_LIMIT} 次")


class ThumbnailStore:
    def __init__(
        self,
        root: Path,
        *,
        fetcher: Callable[[str, str], tuple[bytes, str]] = fetch_thumbnail,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.fetcher = fetcher

    def materialize(self, url: str | None, webpage_url: str) -> str | None:
        if not url:
            return None
        value = str(url).strip()
        if value.startswith("/api/thumbnails/"):
            return value
        remote_url = normalize_thumbnail_url(value)
        if not remote_url.startswith(("http://", "https://")):
            return None
        key = hashlib.sha256(remote_url.encode("utf-8")).hexdigest()
        existing = self.get_path(key)
        if existing:
            return f"/api/thumbnails/{key}"
        try:
            data, content_type = self.fetcher(remote_url, webpage_url)
            extension = _image_extension(data, content_type)
            if not extension:
                raise ThumbnailError("封面响应不是受支持的图片")
            destination = self.root / f"{key}.{extension}"
            temporary = self.root / f"{key}.tmp"
            temporary.write_bytes(data)
            temporary.replace(destination)
            return f"/api/thumbnails/{key}"
        except (OSError, ThumbnailError, httpx.HTTPError):
            return None

    def materialize_bytes(self, data: bytes, content_type: str) -> str | None:
        extension = _image_extension(data, content_type)
        if not extension or len(data) > MAX_THUMBNAIL_BYTES:
            return None
        key = hashlib.sha256(data).hexdigest()
        existing = self.get_path(key)
        if existing:
            return f"/api/thumbnails/{key}"
        try:
            destination = self.root / f"{key}.{extension}"
            temporary = self.root / f"{key}.tmp"
            temporary.write_bytes(data)
            temporary.replace(destination)
            return f"/api/thumbnails/{key}"
        except OSError:
            return None

    def get_path(self, key: str) -> Path | None:
        if not KEY_PATTERN.fullmatch(key):
            return None
        return next(
            (path for path in self.root.glob(f"{key}.*") if path.suffix != ".tmp"),
            None,
        )


thumbnail_store = ThumbnailStore(Path(".cache") / "thumbnails")
