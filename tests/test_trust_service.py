from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.main as main_module
from app.models import StructuredInformation
from app.trust.service import _client_result, verify_structured_information


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_client_result_projects_audited_artifacts(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "04_report.json",
        {
            "structured_report": {
                "overall_verdict": "属实",
                "conclusion": "核验完成",
                "claim_checks": [{"claim_id": "A1"}],
                "uncertainties": [],
                "source_ids": ["E001"],
            },
            "evidence_used": [{"id": "E001"}],
            "evidence_reviewed_count": 5,
            "evidence_selected_count": 1,
            "final_report": "# 报告",
        },
    )
    _write_json(tmp_path / "05_timings.json", {"total_seconds": 1.2})
    _write_json(tmp_path / "01_search_plan.json", {"web_queries": ["查询"]})

    result = _client_result(
        SimpleNamespace(run_dir=tmp_path, case_id="case-one", run_id="run-one")
    )

    assert result["status"] == "completed"
    assert result["overall_verdict"] == "属实"
    assert result["evidence_used"] == [{"id": "E001"}]
    assert result["search_plan"]["web_queries"] == ["查询"]


def test_empty_structured_information_skips_verification() -> None:
    result = asyncio.run(
        verify_structured_information(
            StructuredInformation(
                case_id="empty-case",
                内容主题="没有可核验主张",
                原子主张=[],
                隐性观点=[],
            )
        )
    )

    assert result == {
        "status": "skipped",
        "case_id": "empty-case",
        "message": "当前内容没有可核验的主张。",
    }


def test_verify_endpoint_persists_result_to_requested_cache(monkeypatch) -> None:
    structured = {
        "case_id": "cached-case",
        "内容主题": "缓存核验测试",
        "原子主张": ["这是一条用于验证缓存写回行为的完整中文主张"],
        "隐性观点": [],
    }
    stored = {
        "structured_data": {
            "case_id": "cached-case",
            "content_topic": "缓存核验测试",
            "atomic_claims": ["这是一条用于验证缓存写回行为的完整中文主张"],
            "implicit_opinions": [],
        }
    }
    writes: list[tuple[str, dict]] = []

    async def fake_verify(_structured):
        return {"status": "completed", "overall_verdict": "属实"}

    monkeypatch.setattr(main_module, "verify_structured_information", fake_verify)
    monkeypatch.setattr(main_module.cache, "get", lambda _key: dict(stored))
    monkeypatch.setattr(
        main_module.cache,
        "set",
        lambda key, value: writes.append((key, value)),
    )

    key = "a" * 64
    response = TestClient(main_module.app).post(
        "/api/verify",
        json={"structured_data": structured, "cache_key": key},
    )

    assert response.status_code == 200
    assert writes[0][0] == key
    assert writes[0][1]["verification"]["overall_verdict"] == "属实"
