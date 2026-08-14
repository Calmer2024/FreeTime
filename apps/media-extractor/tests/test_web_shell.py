from __future__ import annotations

from pathlib import Path
import re

from fastapi.testclient import TestClient

from app.main import app
import app.main as main_module


def test_index_prevents_stale_frontend_bundle() -> None:
    client = TestClient(app)
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "/portal/style.css" in response.text

    extractor = client.get("/extractor")
    assert extractor.status_code == 200
    assert extractor.headers["cache-control"] == "no-store"
    assert "/extractor-static/app.css?v=" in extractor.text
    assert "/extractor-static/app.js?v=" in extractor.text
    assert "/extractor-static/task-ui.js?v=" in extractor.text
    versions = re.findall(r"/extractor-static/[^?]+\?v=([a-f0-9]{12})", extractor.text)
    assert len(versions) == 3
    assert len(set(versions)) == 1


def test_saved_mimo_key_updates_runtime_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "SETTINGS_FILE", tmp_path / "settings.json")
    previous_key = main_module.settings.mimo_api_key
    previous_model = main_module.settings.summary_model
    try:
        response = TestClient(app).post(
            "/api/settings",
            json={
                "lastProvider": "mimo",
                "mimo": {"apiKey": "saved-mimo-key", "model": "mimo-2.5"},
            },
        )

        assert response.status_code == 200
        assert main_module.settings.mimo_api_key == "saved-mimo-key"
        assert main_module.settings.summary_model == "mimo-v2.5"
    finally:
        object.__setattr__(main_module.settings, "mimo_api_key", previous_key)
        object.__setattr__(main_module.settings, "summary_model", previous_model)


def test_unexpected_analysis_failure_is_returned_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    monkeypatch.setattr(
        main_module,
        "resolve_content_input",
        lambda *_args, **_kwargs: "https://www.douyin.com/video/1",
    )

    async def fail_analysis(*_args, **_kwargs):
        raise RuntimeError("unexpected extractor failure")

    monkeypatch.setattr(main_module, "analyze", fail_analysis)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/analyze",
        json={
            "url": "https://www.douyin.com/video/1",
            "input_kind": "platform",
            "refresh": True,
            "task_id": "json-error-test",
        },
    )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert "unexpected extractor failure" in response.json()["detail"]


def test_platform_failure_is_not_masked_by_article_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    monkeypatch.setattr(
        main_module,
        "resolve_content_input",
        lambda *_args, **_kwargs: "https://www.douyin.com/video/1",
    )

    async def fail_platform(*_args, **_kwargs):
        raise main_module.PipelineError("视频时长超过平台处理限制")

    async def fail_article(*_args, **_kwargs):
        raise main_module.PipelineError("页面未提取到足够的文章正文")

    monkeypatch.setattr(main_module, "analyze", fail_platform)
    monkeypatch.setattr(main_module, "analyze_article_url", fail_article)
    response = TestClient(app).post(
        "/api/analyze",
        json={"url": "https://v.douyin.com/example/", "refresh": True},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "视频时长超过平台处理限制"


def test_completed_verification_is_part_of_video_response(monkeypatch) -> None:
    payload = {
        "protocol_version": "structured-information-v4",
        "request_id": "request-one",
        "cached": True,
        "strategy": "metadata",
        "metadata": {
            "platform": "抖音",
            "title": "测试视频",
            "webpage_url": "https://www.douyin.com/video/7655319255663070499",
        },
        "summary": "测试摘要",
        "coverage_note": "测试覆盖",
        "structured_data": {
            "case_id": "test-case",
            "content_topic": "测试主题",
            "atomic_claims": ["这是一条用于回归测试的完整中文主张"],
            "implicit_opinions": [],
        },
        "verification": {
            "status": "completed",
            "overall_verdict": "属实",
            "claim_checks": [{"claim_id": "A1", "verdict": "属实"}],
        },
    }
    monkeypatch.setattr(
        "app.main.cache.list",
        lambda _limit: [
            {
                "cache_key": "a" * 64,
                "created_at": "2026-08-01T12:00:00",
                "expired": False,
                "result": payload,
            }
        ],
    )

    response = TestClient(app).get("/api/videos")

    assert response.status_code == 200
    verification = response.json()["items"][0]["result"]["verification"]
    assert verification["status"] == "completed"
    assert verification["claim_checks"][0]["verdict"] == "属实"


def test_full_pipeline_details_have_one_unified_process_view() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    script = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="full-pipeline-summary"' in html
    assert 'id="process-trace"' in html
    assert html.index('id="process-trace"') > html.index('id="view-process"')
    assert "fullPipelineMilliseconds" in script
    assert "orchestrationTraceItems" in script
    assert "输入解析与安全展开" in script
    assert "封面获取与转存" in script
    assert "其他编排开销" in script
    assert "data.full_source_text" in script
    assert 'id="thumbnail-placeholder"' in html
    assert "showThumbnail" in script
    assert "thumbnail.onerror" in script
    css = Path("static/app.css").read_text(encoding="utf-8")
    assert "object-fit: contain" in css
    assert "filter: saturate" not in css


def test_web_shell_uses_one_input_for_links_and_text() -> None:
    html = Path("static/index.html").read_text(encoding="utf-8")
    script = Path("static/app.js").read_text(encoding="utf-8")

    assert 'id="url" type="text"' in html
    assert 'autocomplete="off"' in html
    assert "支持直接输入文字" in html
    assert 'id="upload-fields"' not in html
    assert 'id="upload-files"' not in html
    assert 'id="upload-title"' not in html
    assert 'id="upload-text"' not in html
    assert 'fetch("/api/analyze/upload"' in script
    assert '<option value="upload">' not in html
    assert "selectInputRoutes" in script
    assert 'route.kind === "text"' in script
    assert '$("upload-' not in script
