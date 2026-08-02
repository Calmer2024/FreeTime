import json
import asyncio

from app.pipeline import (
    _analyze_live_photo_videos,
    _extract_info,
    _has_unfilled_event_video_gap,
    _metadata,
)
from app.models import ExtractionPlan
from app.security import canonicalize_video_url
from app.xiaohongshu import parse_xiaohongshu_note_html


def _page(note_id: str, note: dict) -> str:
    state = {"note": {"noteDetailMap": {note_id: {"note": note}}}}
    return (
        "<html><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";</script></html>"
    )


def test_parse_image_note_preserves_every_image_and_context() -> None:
    note_id = "abc123"
    html = _page(note_id, {
        "noteId": note_id,
        "type": "normal",
        "title": "两张图的图文笔记",
        "desc": "正文包含一条需要核验的事实。",
        "user": {"nickname": "作者甲", "userId": "u1"},
        "tagList": [{"name": "科普"}],
        "imageList": [
            {"urlDefault": "https://sns-img.example/1.jpg", "width": 1080, "height": 1440},
            {"urlPre": "https://sns-img.example/2.webp", "width": 1080, "height": 1440},
        ],
        "poi": {"name": "测试地点"},
        "goodsInfo": [{"name": "测试商品"}],
    })

    info = parse_xiaohongshu_note_html(html, f"https://www.xiaohongshu.com/explore/{note_id}")

    assert info["source_subtype"] == "xiaohongshu_image_note"
    assert len(info["note_images"]) == 2
    assert info["uploader"] == "作者甲"
    assert info["source_context"]["topics"] == ["科普"]
    assert "测试地点" in info["source_context"]["attachments"]
    assert "测试商品" in info["source_context"]["attachments"]


def test_empty_note_title_falls_back_to_description_first_line() -> None:
    note_id = "note123"
    html = _page(note_id, {
        "noteId": note_id,
        "type": "normal",
        "title": "",
        "desc": "女骑手的尽头，不是流量，是ICU\n后续正文",
        "imageList": [{"urlDefault": "https://sns-img.example/1.jpg"}],
    })

    info = parse_xiaohongshu_note_html(
        html, f"https://www.xiaohongshu.com/explore/{note_id}"
    )

    assert info["title"] == "女骑手的尽头，不是流量，是ICU"


def test_complete_image_carousel_never_requires_full_video_escalation() -> None:
    plan = ExtractionPlan(video_type="event_footage")

    assert not _has_unfilled_event_video_gap(
        is_image_carousel=True,
        visual_fallback_required=True,
        plan=plan,
        full_visual_executed=False,
    )


def test_parse_video_note_keeps_video_route() -> None:
    note_id = "def456"
    html = _page(note_id, {
        "noteId": note_id,
        "type": "video",
        "title": "视频笔记",
        "desc": "视频正文",
        "imageList": [{"urlDefault": "https://sns-img.example/cover.jpg"}],
        "video": {"media": {"stream": {"h264": [{
            "masterUrl": "https://sns-video.example/video.mp4",
            "duration": 3200,
        }]}}},
    })

    info = parse_xiaohongshu_note_html(html, f"https://www.xiaohongshu.com/explore/{note_id}")

    assert info["source_subtype"] == "xiaohongshu_video_note"
    assert info["note_images"] == []
    assert info["formats"][0]["url"] == "https://sns-video.example/video.mp4"


def test_parse_long_note_is_not_collapsed_into_ordinary_carousel() -> None:
    note_id = "fed321"
    html = _page(note_id, {
        "noteId": note_id,
        "type": "multi",
        "title": "长文笔记",
        "desc": "这是长文正文。" * 100,
        "imageList": [{"urlDefault": "https://sns-img.example/page-1.jpg"}],
    })

    info = parse_xiaohongshu_note_html(html, f"https://www.xiaohongshu.com/discovery/item/{note_id}")

    assert info["source_subtype"] == "xiaohongshu_long_note"
    assert len(info["description"]) > 500
    assert len(info["note_images"]) == 1


def test_parse_live_photo_keeps_still_and_motion_assets() -> None:
    note_id = "live789"
    html = _page(note_id, {
        "noteId": note_id,
        "type": "normal",
        "title": "实况照片笔记",
        "desc": "按住照片可以看到动态内容。",
        "imageList": [{
            "urlDefault": "https://sns-img.example/live.jpg",
            "livePhoto": True,
            "stream": {"h264": [{
                "masterUrl": "https://sns-video.example/live-motion.mp4"
            }]},
        }],
    })

    info = parse_xiaohongshu_note_html(html, f"https://www.xiaohongshu.com/explore/{note_id}")

    assert info["source_subtype"] == "xiaohongshu_live_photo_note"
    assert len(info["note_images"]) == 1
    assert info["live_photo_videos"] == ["https://sns-video.example/live-motion.mp4"]


def test_canonicalize_xiaohongshu_note_routes_and_tracking() -> None:
    expected = "https://www.xiaohongshu.com/explore/abc123"

    assert canonicalize_video_url(
        "https://www.xiaohongshu.com/discovery/item/abc123?xsec_token=secret&source=share"
    ) == f"{expected}?xsec_token=secret"
    assert canonicalize_video_url(
        "https://www.xiaohongshu.com/explore/abc123?app_platform=ios"
    ) == expected


def test_pipeline_uses_xiaohongshu_adapter_before_generic_video_extractor(monkeypatch) -> None:
    expected = {
        "extractor_key": "XiaoHongShu",
        "source_subtype": "xiaohongshu_image_note",
        "source_context": {"topics": ["科普"], "attachments": []},
        "title": "图文",
        "description": "正文",
        "webpage_url": "https://www.xiaohongshu.com/explore/abc123",
        "note_images": [{"url": "https://sns-img.example/1.jpg"}],
        "formats": [],
    }
    monkeypatch.setattr("app.pipeline.extract_xiaohongshu_info", lambda _url: expected)

    info = _extract_info("https://www.xiaohongshu.com/explore/abc123")
    metadata = _metadata(info, info["webpage_url"])

    assert info is expected
    assert metadata.content_type == "image_carousel"
    assert metadata.source_subtype == "xiaohongshu_image_note"
    assert metadata.source_context["topics"] == ["科普"]


def test_live_photo_motion_assets_are_individually_analyzed(monkeypatch) -> None:
    calls = []

    async def fake_summarize(url, _metadata, fps):
        calls.append((url, fps))
        return {"summary": f"动态内容 {url[-5:]}", "key_points": ["人物发生移动"]}

    monkeypatch.setattr("app.pipeline.summarize_video", fake_summarize)
    results, failures = asyncio.run(_analyze_live_photo_videos(
        ["https://video.example/a.mp4", "https://video.example/b.mp4"],
        {"title": "实况"},
    ))

    assert len(results) == 2
    assert failures == []
    assert calls == [
        ("https://video.example/a.mp4", 1.0),
        ("https://video.example/b.mp4", 1.0),
    ]
