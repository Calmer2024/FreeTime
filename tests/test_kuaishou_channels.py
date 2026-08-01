from __future__ import annotations

import pytest

from app.channels import ChannelsSessionError, is_channels_url, parse_channels_feed, parse_channels_html
from app.kuaishou import KuaishouSessionError, parse_kuaishou_detail, parse_kuaishou_html


def test_kuaishou_graphql_video_detail_is_normalized() -> None:
    payload = {
        "data": {
            "visionVideoDetail": {
                "status": 1,
                "author": {"id": "author-1", "name": "快手作者"},
                "photo": {
                    "id": "3xabc",
                    "caption": "快手测试视频",
                    "duration": 12345,
                    "coverUrl": "https://img.example/cover.jpg",
                    "photoUrl": "https://media.example/video.mp4",
                    "timestamp": 1700000000000,
                    "videoRatio": 0.5625,
                },
                "tags": [{"name": "测试"}],
            }
        }
    }

    info = parse_kuaishou_detail(
        payload, "https://www.kuaishou.com/short-video/3xabc"
    )

    assert info["extractor_key"] == "Kuaishou"
    assert info["title"] == "快手测试视频"
    assert info["uploader"] == "快手作者"
    assert info["duration"] == pytest.approx(12.345)
    assert info["formats"][0]["url"] == "https://media.example/video.mp4"
    assert info["_browser_native"] is True


def test_kuaishou_captcha_is_reported_as_session_error() -> None:
    with pytest.raises(KuaishouSessionError, match="验证码"):
        parse_kuaishou_detail(
            {"data": {"result": 400002, "captcha": "https://captcha.example"}},
            "https://www.kuaishou.com/short-video/3xabc",
        )


def test_kuaishou_apollo_page_data_avoids_graphql_captcha() -> None:
    import json
    state = {"defaultClient": {
        '$ROOT_QUERY.visionVideoDetail({"page":"detail","photoId":"3xreal"})': {
            "status": 1, "author": {"id": "VisionVideoDetailAuthor:author"},
            "photo": {"id": "VisionVideoDetailPhoto:3xreal"}},
        "VisionVideoDetailAuthor:author": {"id": "author", "name": "真实作者"},
        "VisionVideoDetailPhoto:3xreal": {"id": "3xreal", "duration": 39000,
            "caption": "真实快手标题", "coverUrl": "https://img.example/ks.jpg",
            "photoUrl": "https://media.example/ks.mp4"}}}
    html = f"<script>window.__APOLLO_STATE__={json.dumps(state)};</script>"
    info = parse_kuaishou_html(html, "https://www.kuaishou.com/short-video/3xreal")
    assert info["title"] == "真实快手标题"
    assert info["uploader"] == "真实作者"
    assert info["formats"][0]["url"] == "https://media.example/ks.mp4"


def test_channels_public_page_video_is_normalized() -> None:
    html = """
    <html><head>
      <meta property="og:title" content="视频号测试视频">
      <meta property="og:description" content="测试说明">
      <meta property="og:image" content="https://img.example/channels.jpg">
      <meta property="og:video" content="https://finder.video.qq.com/video.mp4">
    </head></html>
    """

    info = parse_channels_html(
        html,
        "https://channels.weixin.qq.com/web/pages/feed?exportkey=fresh",
    )

    assert info["extractor_key"] == "WechatChannels"
    assert info["title"] == "视频号测试视频"
    assert info["formats"][0]["url"].startswith("https://finder.video.qq.com/")
    assert info["_browser_native"] is True


def test_channels_expired_share_is_reported() -> None:
    html = '<script>location.replace("https://support.weixin.qq.com/update/")</script>'
    with pytest.raises(ChannelsSessionError, match="失效|过期"):
        parse_channels_html(
            html,
            "https://channels.weixin.qq.com/web/pages/feed?exportkey=expired",
        )


def test_channels_sph_feed_image_post_is_normalized() -> None:
    payload = {"data": {"authorInfo": {"nickname": "为你写诗"}, "feedInfo": {
        "description": "我的思念，是一直醒着的窗。",
        "coverUrl": "https://finder.video.qq.com/cover.jpg", "picInfo": []},
        "sceneInfo": {"dynamicExportId": "export/test"}}, "errCode": 0}
    info = parse_channels_feed(payload, "https://channels.weixin.qq.com/finder-preview/pages/sph?id=exampleShortUri")
    assert info["source_subtype"] == "wechat_channels_image_post"
    assert info["uploader"] == "为你写诗"
    assert info["note_images"][0]["url"].endswith("cover.jpg")
    assert info["formats"] == []


def test_channels_short_and_preview_hosts_are_recognized() -> None:
    assert is_channels_url("https://weixin.qq.com/sph/exampleShortUri")
    assert is_channels_url("https://channels.weixin.qq.com/finder-preview/pages/sph?id=exampleShortUri")
