"""Case input validation and durable per-run artifact storage."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CASE_LIST_FIELDS = ("原子主张", "隐性观点")


def validate_case_input(raw_data: Any) -> dict[str, Any]:
    if not isinstance(raw_data, dict):
        raise ValueError("案例输入必须是 JSON 对象")

    data = dict(raw_data)
    for field in CASE_LIST_FIELDS:
        value = data.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{field} 必须是字符串数组")
        data[field] = [item.strip() for item in value if item.strip()]

    if not any(data[field] for field in CASE_LIST_FIELDS):
        raise ValueError("原子主张、隐性观点至少需要提供一项")

    topic = data.get("内容主题", "")
    if topic is not None and not isinstance(topic, str):
        raise ValueError("内容主题必须是字符串")
    data["内容主题"] = (topic or "").strip()
    return data


def normalize_case_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    normalized = normalized.strip("-_")
    if not normalized:
        raise ValueError("case_id 或输入文件名必须包含英文字母、数字、连字符或下划线")
    return normalized


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


@dataclass(frozen=True)
class CaseRunWorkspace:
    case_id: str
    case_dir: Path
    run_id: str
    run_dir: Path

    @classmethod
    def create(
        cls,
        input_path: Path,
        data_root: Path = Path("data/cases"),
        run_id: str | None = None,
    ) -> tuple["CaseRunWorkspace", dict[str, Any]]:
        input_path = input_path.resolve()
        input_data = validate_case_input(
            json.loads(input_path.read_text(encoding="utf-8"))
        )
        inferred_case_id = (
            input_path.parent.name
            if input_path.stem.lower() in {"input", "case"}
            else input_path.stem
        )
        case_id = normalize_case_id(
            str(input_data.get("case_id") or inferred_case_id)
        )
        input_data["case_id"] = case_id

        resolved_run_id = run_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        case_dir = data_root.resolve() / case_id
        run_dir = case_dir / "runs" / resolved_run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        workspace = cls(
            case_id=case_id,
            case_dir=case_dir,
            run_id=resolved_run_id,
            run_dir=run_dir,
        )
        _write_json(case_dir / "input.json", input_data)
        workspace.write_json("00_input.json", input_data)
        return workspace, input_data

    def write_json(self, filename: str, value: Any) -> Path:
        path = self.run_dir / filename
        _write_json(path, value)
        return path

    def write_text(self, filename: str, value: str) -> Path:
        path = self.run_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = path.with_suffix(path.suffix + ".tmp")
        temporary_path.write_text(value, encoding="utf-8")
        temporary_path.replace(path)
        return path

    def finalize(self, summary: dict[str, Any]) -> Path:
        latest = {
            "case_id": self.case_id,
            "run_id": self.run_id,
            "run_directory": str(self.run_dir),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **summary,
        }
        return self.write_case_json("latest_run.json", latest)

    def write_case_json(self, filename: str, value: Any) -> Path:
        path = self.case_dir / filename
        _write_json(path, value)
        return path
