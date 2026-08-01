from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def test_index_prevents_stale_frontend_bundle() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "/static/app.css?v=" in response.text
    assert "/static/app.js?v=" in response.text


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
    html = Path("app/static/index.html").read_text(encoding="utf-8")
    script = Path("app/static/app.js").read_text(encoding="utf-8")

    assert 'id="full-pipeline-summary"' in html
    assert 'id="trust-audit-body"' in html
    assert html.index('id="trust-audit-body"') > html.index('id="view-process"')
    assert 'id="llm-structured-input"' in html
    assert "fullPipelineMilliseconds" in script
    assert "verificationTraceItems" in script
    assert "data.full_source_text" in script
