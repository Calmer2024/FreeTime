from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx


ALLOWED_HOST_SUFFIXES = (
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "gifshow.com",
    "weibo.com",
    "weibo.cn",
    "xiaohongshu.com",
    "xhslink.com",
    "channels.weixin.qq.com",
    "weixin.qq.com",
)
SHORT_LINK_HOSTS = {
    "b23.tv", "v.douyin.com", "v.kuaishou.com", "xhslink.com"
}
URL_PATTERN = re.compile(
    r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&()*+,;=%]+", re.IGNORECASE
)
TRAILING_SHARE_PUNCTUATION = ".,;:!?)]}，。；：！？）》】」』"
REDIRECT_LIMIT = 5
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


class UnsafeUrlError(ValueError):
    pass


def validate_video_url(url: str) -> None:
    validate_public_url(url)
    parsed = urlparse(url)
    hostname = parsed.hostname.lower().rstrip(".")
    if not any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ALLOWED_HOST_SUFFIXES
    ):
        raise UnsafeUrlError(
            "当前平台入口支持抖音、B站、YouTube、快手、微博、小红书和视频号"
        )


def validate_public_url(url: str) -> None:
    """Validate an arbitrary public article URL without weakening the SSRF boundary."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeUrlError("只接受有效的 HTTP(S) 网址")
    hostname = parsed.hostname.lower().rstrip(".")

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        }
    except socket.gaierror as exc:
        raise UnsafeUrlError("网址域名无法解析") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
        ):
            raise UnsafeUrlError("网址解析到了非公网地址，已拒绝访问")


def canonicalize_video_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if hostname.endswith("youtu.be"):
        video_id = path.strip("/").split("/")[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    if hostname.endswith("youtube.com"):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    if hostname.endswith("bilibili.com"):
        match = re.search(r"/video/([^/?]+)", path, re.IGNORECASE)
        if match:
            return f"https://www.bilibili.com/video/{match.group(1)}/"
    if hostname.endswith("douyin.com"):
        match = re.search(r"/video/(\d+)", path)
        if not match:
            match = re.search(r"/share/video/(\d+)", path)
        if match:
            return f"https://www.douyin.com/video/{match.group(1)}"
        note_match = re.search(r"/(?:share/)?note/(\d+)", path)
        if note_match:
            return f"https://www.douyin.com/note/{note_match.group(1)}"
        modal_id = parse_qs(parsed.query).get("modal_id", [""])[0]
        if re.fullmatch(r"\d+", modal_id):
            return f"https://www.douyin.com/video/{modal_id}"
    if hostname.endswith("xiaohongshu.com"):
        match = re.search(r"/(?:explore|discovery/item)/([\da-z]+)", path, re.IGNORECASE)
        if match:
            canonical = f"https://www.xiaohongshu.com/explore/{match.group(1)}"
            query = parse_qs(parsed.query)
            access_query = {
                key: query[key][0]
                for key in ("xsec_token", "xsec_source")
                if query.get(key) and query[key][0]
            }
            return f"{canonical}?{urlencode(access_query)}" if access_query else canonical
    return url


def extract_video_url(value: str) -> str:
    """Extract the first HTTP(S) URL from a URL or mobile share message."""
    match = URL_PATTERN.search(value.strip())
    if not match:
        raise UnsafeUrlError("输入内容中没有找到有效的 HTTP(S) 网址")
    return match.group(0).rstrip(TRAILING_SHARE_PUNCTUATION)


def _is_short_link(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (
        host in SHORT_LINK_HOSTS
        or (host.endswith("kuaishou.com") and parsed.path.startswith("/f/"))
        or (host == "weixin.qq.com" and parsed.path.startswith("/sph/"))
    )


def resolve_video_input(value: str) -> str:
    """Safely turn a full share message or short URL into a stable video URL."""
    candidate = extract_video_url(value)
    validate_video_url(candidate)

    canonical = canonicalize_video_url(candidate)
    if canonical != candidate or not _is_short_link(candidate):
        validate_video_url(canonical)
        return canonical

    current = candidate
    with httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(8),
        headers={"User-Agent": BROWSER_USER_AGENT},
    ) as client:
        for _ in range(REDIRECT_LIMIT):
            try:
                response = client.get(current)
            except httpx.HTTPError as exc:
                raise UnsafeUrlError("短链接展开失败，请稍后重试或粘贴完整链接") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise UnsafeUrlError("短链接未返回有效跳转，请确认分享链接仍然有效")
            location = response.headers.get("location")
            if not location:
                raise UnsafeUrlError("短链接缺少跳转地址")

            next_url = urljoin(current, location)
            # Validate every hop before it can be requested. This is the SSRF boundary.
            validate_video_url(next_url)
            canonical = canonicalize_video_url(next_url)
            validate_video_url(canonical)
            if canonical != next_url or not _is_short_link(next_url):
                return canonical
            current = next_url

    raise UnsafeUrlError(f"短链接跳转超过 {REDIRECT_LIMIT} 次，已停止解析")


def resolve_content_input(value: str, *, platform_only: bool = False) -> str:
    """Resolve share text for either a supported platform item or a public article."""
    candidate = extract_video_url(value)
    hostname = (urlparse(candidate).hostname or "").lower().rstrip(".")
    is_platform = any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in ALLOWED_HOST_SUFFIXES
    )
    if platform_only or is_platform:
        return resolve_video_input(value)
    validate_public_url(candidate)
    return candidate
