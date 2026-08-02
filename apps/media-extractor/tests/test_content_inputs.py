from app.content import extract_article
from app.main import app
from app.security import resolve_content_input, validate_video_url
from fastapi.testclient import TestClient


def test_extract_article_prefers_article_body_and_metadata() -> None:
    html = """
    <html><head><title>备用标题</title>
    <meta property="og:title" content="核验文章">
    <meta name="author" content="测试作者"></head>
    <body><nav>导航噪声</nav><article>
    <h1>核验文章</h1><p>这是第一段完整文章正文，用于核验来源。</p>
    <p>这是第二段完整文章正文，包含需要检查的事实主张。</p>
    </article><footer>页脚噪声</footer></body></html>
    """

    title, article, author, _ = extract_article(html, "https://news.example/article")

    assert title == "核验文章"
    assert author == "测试作者"
    assert "第一段完整文章正文" in article
    assert "导航噪声" not in article
    assert "页脚噪声" not in article


def test_article_input_accepts_public_non_platform_host(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )

    assert resolve_content_input("文章 https://news.example/story") == "https://news.example/story"


def test_new_platform_hosts_are_allowed(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.security.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )

    for url in (
        "https://www.kuaishou.com/short-video/abc",
        "https://weibo.com/123/abc",
        "https://www.xiaohongshu.com/explore/abc",
        "https://channels.weixin.qq.com/web/pages/feed?exportkey=abc",
    ):
        validate_video_url(url)


def test_upload_endpoint_requires_at_least_one_modality() -> None:
    response = TestClient(app).post(
        "/api/analyze/upload",
        data={"title": "空案例", "text": "", "verify": "false"},
    )

    assert response.status_code == 422
    assert "至少提供文本" in response.json()["detail"]


def test_health_reports_expanded_input_scope() -> None:
    payload = TestClient(app).get("/api/health").json()

    assert {"快手", "微博", "小红书", "视频号"} <= set(payload["supported_platforms"])
    assert "文章 URL" in payload["accepted_inputs"]
