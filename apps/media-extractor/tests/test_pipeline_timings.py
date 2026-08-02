import asyncio
import time

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import _ensure_request_timings, _finalize_request_timings
from app.models import StageTiming
from app.models import AnalyzeResponse


def _result() -> AnalyzeResponse:
    return AnalyzeResponse.model_validate({
        "request_id": "timing-test",
        "cached": False,
        "strategy": "metadata",
        "metadata": {
            "platform": "测试",
            "title": "计时测试",
            "webpage_url": "https://example.com/item",
        },
        "summary": "测试摘要",
        "coverage_note": "测试覆盖",
        "timings": [
            {"name": "解析", "milliseconds": 1000},
            {"name": "转换", "milliseconds": 2000},
        ],
        "extraction_milliseconds": 3200,
        "structured_data": {
            "case_id": "timing-test",
            "内容主题": "测试计时完整性",
            "原子主张": [],
            "隐性观点": [],
        },
        "verification": {
            "timings": {
                "total_seconds": 2.6,
                "stages": {"retrieval": 1.0, "report_generation": 1.5},
            }
        },
    })


def test_visible_stage_sum_equals_wall_clock_total() -> None:
    result = _result()

    _finalize_request_timings(
        result,
        full_milliseconds=10_000,
        input_milliseconds=1_200,
        thumbnail_milliseconds=300,
    )

    orchestration = {
        item.name: item.milliseconds for item in result.orchestration_timings
    }
    visible_extraction = sum(item.milliseconds for item in result.timings)
    visible_verification = round(sum(
        result.verification["timings"]["stages"].values()
    ) * 1000)
    assert orchestration == {
        "输入解析与安全展开": 1200,
        "封面获取与转存": 300,
        "其他编排开销": 3000,
    }
    assert (
        visible_extraction
        + visible_verification
        + sum(orchestration.values())
        == result.full_pipeline_milliseconds
        == 10_000
    )


def test_legacy_result_assigns_unattributed_time_to_other_orchestration() -> None:
    result = _result()
    result.full_pipeline_milliseconds = 10_000

    _ensure_request_timings(result)

    orchestration = {
        item.name: item.milliseconds for item in result.orchestration_timings
    }
    assert orchestration == {
        "输入解析与安全展开": 0,
        "封面获取与转存": 0,
        "其他编排开销": 4500,
    }


def test_fresh_analyze_request_records_real_entry_and_thumbnail_stages(
    monkeypatch,
) -> None:
    result = _result()
    result.timings = [StageTiming(name="解析", milliseconds=2)]
    result.extraction_milliseconds = 2
    result.verification = None

    def fake_resolve(*_args, **_kwargs):
        time.sleep(0.01)
        return "https://www.douyin.com/video/1"

    async def fake_analyze(*_args, **_kwargs):
        return result

    async def fake_thumbnail(*_args, **_kwargs):
        await asyncio.sleep(0.006)
        return True

    async def fake_verify(*_args, **_kwargs):
        return {
            "status": "completed",
            "timings": {
                "total_seconds": 0.002,
                "stages": {"retrieval": 0.001, "report_generation": 0.001},
            },
        }

    monkeypatch.setattr(main_module, "resolve_content_input", fake_resolve)
    monkeypatch.setattr(main_module, "analyze", fake_analyze)
    monkeypatch.setattr(main_module, "_stabilize_result_thumbnail", fake_thumbnail)
    monkeypatch.setattr(main_module, "verify_structured_information", fake_verify)
    monkeypatch.setattr(main_module.cache, "get", lambda *_args: None)
    monkeypatch.setattr(main_module.cache, "set", lambda *_args: None)

    response = TestClient(main_module.app).post(
        "/api/analyze",
        json={"url": "https://example.test/item", "refresh": True, "verify": True},
    )

    assert response.status_code == 200
    payload = response.json()
    orchestration = {
        item["name"]: item["milliseconds"]
        for item in payload["orchestration_timings"]
    }
    visible = (
        sum(item["milliseconds"] for item in payload["timings"])
        + round(sum(payload["verification"]["timings"]["stages"].values()) * 1000)
        + sum(orchestration.values())
    )
    assert orchestration["输入解析与安全展开"] >= 8
    assert orchestration["封面获取与转存"] >= 4
    assert visible == payload["full_pipeline_milliseconds"]
