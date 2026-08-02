from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

from app import pipeline
from app.douyin_cookies import DouyinCookieError, _browser_aweme_info


@pytest.mark.parametrize(
    "detail",
    [
        "Fresh cookies (not necessarily logged in) are needed",
        "HTTP Error 403: Forbidden",
        "unable to extract aweme detail",
        "页面没有返回可解析的作品数据",
    ],
)
def test_douyin_session_error_recognizes_refreshable_failures(detail: str) -> None:
    assert pipeline._is_douyin_session_error(RuntimeError(detail))


def test_extract_info_refreshes_douyin_session_once(monkeypatch) -> None:
    forces: list[bool] = []

    def fake_options(url: str, force_douyin_refresh: bool = False):
        forces.append(force_douyin_refresh)
        return {}

    class FakeYoutubeDL:
        calls = 0

        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url: str, download: bool):
            type(self).calls += 1
            if type(self).calls == 1:
                raise yt_dlp.utils.DownloadError("Fresh cookies are needed")
            return {"id": "123", "title": "ok"}

    monkeypatch.setattr(pipeline, "_base_ydl_options", fake_options)
    monkeypatch.setattr(pipeline.yt_dlp, "YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr(
        pipeline,
        "extract_douyin_browser_info",
        lambda url: (_ for _ in ()).throw(DouyinCookieError("probe unavailable")),
    )
    monkeypatch.setattr(
        pipeline,
        "settings",
        SimpleNamespace(douyin_auto_cookies=True, ytdlp_cookies_file=""),
    )

    result = pipeline._extract_info("https://www.douyin.com/video/123")

    assert result["id"] == "123"
    assert forces == [False, True]


def test_external_cookie_file_uses_paired_user_agent(monkeypatch, tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(
        pipeline,
        "settings",
        SimpleNamespace(
            ytdlp_cookies_file=str(cookie_file),
            ytdlp_user_agent="service-browser-UA",
            douyin_auto_cookies=False,
        ),
    )

    options = pipeline._cookie_options("https://www.douyin.com/video/123")

    assert options["cookiefile"] == str(cookie_file)
    assert options["http_headers"]["User-Agent"] == "service-browser-UA"


def test_browser_aweme_info_selects_combined_h264_mp4() -> None:
    address = {
        "url_list": ["https://media.example/video.mp4"],
        "width": 576,
        "height": 1024,
        "data_size": 1234,
    }
    detail = {
        "aweme_id": "765",
        "aweme_type": 0,
        "desc": "测试视频",
        "author": {"nickname": "作者"},
        "video": {
            "duration": 250682,
            "bit_rate": [
                {"format": "dash", "is_h265": 0, "play_addr": address},
                {"format": "mp4", "is_h265": 1, "play_addr": address},
                {
                    "format": "mp4",
                    "is_h265": 0,
                    "bit_rate": 455124,
                    "play_addr": address,
                },
            ],
        },
    }

    info = _browser_aweme_info(detail, "https://www.douyin.com/video/765", "UA")

    assert info["duration"] == pytest.approx(250.682)
    assert len(info["formats"]) == 1
    assert info["formats"][0]["acodec"] == "aac"
    assert info["_browser_native"] is True


def test_browser_aweme_info_routes_image_carousel() -> None:
    detail = {
        "aweme_id": "768",
        "aweme_type": 68,
        "desc": "图文",
        "images": [
            {
                "url_list": ["https://media.example/slide.jpg"],
                "width": 1080,
                "height": 1440,
            }
        ],
    }

    info = _browser_aweme_info(detail, "https://www.douyin.com/note/768", "UA")

    assert info["duration"] is None
    assert info["webpage_url"].endswith("/note/768")
    assert len(info["note_images"]) == 1
