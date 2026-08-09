import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main_module
import app.mimo as mimo_module


def test_task_status_is_persisted_and_queryable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()

    main_module._update_task_job(
        "task-test",
        status="success",
        progress=100,
        route={"kind": "url", "url": "https://example.com/item"},
        result={"request_id": "result-test"},
    )

    payload = TestClient(main_module.app).get("/api/tasks/task-test").json()
    assert payload["status"] == "success"
    assert payload["progress"] == 100
    assert payload["result"]["request_id"] == "result-test"
    assert (tmp_path / "tasks.json").is_file()


def test_completed_task_can_be_deleted_and_stays_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    main_module._update_task_job("done", status="success", progress=100)

    response = TestClient(main_module.app).delete("/api/tasks/done")

    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "id": "done"}
    assert "done" not in main_module.TASK_JOBS
    assert "done" not in (tmp_path / "tasks.json").read_text(encoding="utf-8")


def test_running_task_cannot_be_deleted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()
    main_module._update_task_job("running", status="running", progress=30)

    response = TestClient(main_module.app).delete("/api/tasks/running")

    assert response.status_code == 409
    assert "running" in main_module.TASK_JOBS


def test_missing_task_delete_returns_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "TASKS_FILE", tmp_path / "tasks.json")
    main_module.TASK_JOBS.clear()

    response = TestClient(main_module.app).delete("/api/tasks/missing")

    assert response.status_code == 404


def test_image_post_prompt_is_ocr_only(tmp_path: Path, monkeypatch) -> None:
    image = tmp_path / "image.jpg"
    image.write_bytes(b"fake-image")
    captured = {}

    async def fake_completion(payload, timeout):
        captured["payload"] = payload
        return {
            "choices": [{"message": {"content": '{"frames":[]}'}}],
            "usage": {},
        }

    monkeypatch.setattr(mimo_module, "_completion", fake_completion)
    asyncio.run(
        mimo_module.analyze_keyframes(
            [(1, 0.0, image)], {"title": "图片帖"}, ocr_only=True
        )
    )

    serialized = str(captured["payload"])
    assert "只执行 OCR" in serialized
    assert "禁止画面观察" in serialized


def test_project_downloads_use_a_safe_subdirectory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "_download_directory", lambda: tmp_path)

    target = main_module._project_download_directory(
        '抖音项目：咖啡店拍照 / 第一集'
    )

    assert target.parent == tmp_path
    assert target.name == "抖音项目_咖啡店拍照 _ 第一集"
    assert target.is_dir()


def test_markdown_export_uses_project_download_rules(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(main_module, "_download_directory", lambda: tmp_path)
    client = TestClient(main_module.app)
    payload = {
        "content": "# Harness\n\n## 核心机制\n正文内容。",
        "filename": "Harness：Agent / 实践.md",
        "project_name": "Harness：Agent / 实践",
    }

    first = client.post("/api/download/save-text", json=payload)
    second = client.post("/api/download/save-text", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    first_path = Path(first.json()["path"])
    second_path = Path(second.json()["path"])
    assert first_path.name == "Harness_Agent _ 实践.md"
    assert second_path.name == "Harness_Agent _ 实践-2.md"
    assert first_path.read_text(encoding="utf-8") == payload["content"]
    assert first_path.parent.name == "Harness_Agent _ 实践"


def test_article_quality_detects_summary_like_output() -> None:
    source = "这是需要完整保留的原文信息。" * 120
    issues = mimo_module._article_quality_issues(source, "简短摘要。")

    assert "正文相对原文过度压缩" in issues
    assert "缺少 Markdown 文章标题" in issues


def test_opinion_assessment_enables_native_web_search(monkeypatch) -> None:
    captured = {}

    async def fake_completion(payload, timeout):
        captured["payload"] = payload
        return {
            "choices": [{
                "message": {
                    "content": (
                        '{"verdict":"mixed","promotional_risk":"medium",'
                        '"usefulness":"有一定用途，但需要核验适用条件。",'
                        '"reasons":["公开资料与宣传口径存在差异"],'
                        '"advice":["先进行小范围试用"],'
                        '"sources":["https://example.com/evidence"],'
                        '"web_searched":true}'
                    )
                },
                "finish_reason": "stop",
            }],
            "usage": {},
        }

    monkeypatch.setattr(mimo_module, "_completion", fake_completion)
    result = asyncio.run(mimo_module.assess_opinion_with_web_search(
        "某产品宣称可以显著提升效率。",
        {"title": "产品测评", "webpage_url": "https://example.com/post"},
    ))

    assert captured["payload"]["tools"] == [{
        "type": "web_search", "max_keyword": 3, "force_search": False
    }]
    assert result["web_searched"] is True
    assert result["advice"] == ["先进行小范围试用"]
