from pathlib import Path

from app.thumbnails import ThumbnailStore, normalize_thumbnail_url


def test_http_thumbnail_is_upgraded_and_materialized_as_same_origin(tmp_path: Path) -> None:
    requested: list[str] = []

    def fetcher(url: str, _webpage_url: str) -> tuple[bytes, str]:
        requested.append(url)
        return b"\xff\xd8\xff\xe0real-jpeg", "image/jpeg"

    store = ThumbnailStore(tmp_path, fetcher=fetcher)
    local_url = store.materialize(
        "http://sns-webpic-qc.xhscdn.com/cover.jpg",
        "https://www.xiaohongshu.com/explore/note",
    )

    assert requested == ["https://sns-webpic-qc.xhscdn.com/cover.jpg"]
    assert local_url and local_url.startswith("/api/thumbnails/")
    key = local_url.rsplit("/", 1)[-1]
    assert store.get_path(key).read_bytes().startswith(b"\xff\xd8\xff")


def test_non_image_response_never_becomes_a_thumbnail(tmp_path: Path) -> None:
    store = ThumbnailStore(
        tmp_path,
        fetcher=lambda *_args: (b"<html>forbidden</html>", "text/html"),
    )

    assert store.materialize(
        "https://wx4.sinaimg.cn/cover.jpg", "https://weibo.com/1/a"
    ) is None
    assert list(tmp_path.iterdir()) == []


def test_existing_same_origin_thumbnail_is_not_downloaded(tmp_path: Path) -> None:
    store = ThumbnailStore(tmp_path, fetcher=lambda *_args: (_ for _ in ()).throw(AssertionError()))

    value = "/api/thumbnails/" + "a" * 64

    assert store.materialize(value, "https://weibo.com/1/a") == value


def test_normalize_thumbnail_url_only_upgrades_http() -> None:
    assert normalize_thumbnail_url("http://example.com/a.jpg") == "https://example.com/a.jpg"
    assert normalize_thumbnail_url("https://example.com/a.jpg") == "https://example.com/a.jpg"


def test_uploaded_image_bytes_get_a_stable_thumbnail(tmp_path: Path) -> None:
    store = ThumbnailStore(tmp_path)
    value = store.materialize_bytes(b"\xff\xd8\xff\xe0uploaded-jpeg", "image/jpeg")

    assert value and value.startswith("/api/thumbnails/")
    assert store.get_path(value.rsplit("/", 1)[-1]).is_file()
