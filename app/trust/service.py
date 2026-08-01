from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from app.models import StructuredInformation
from app.trust.pipeline import run_pipeline


_verification_lock = asyncio.Lock()
_data_root = Path("data") / "trust" / "cases"
_incoming_root = Path(".cache") / "trust-inputs"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _client_result(workspace: Any) -> dict[str, Any]:
    report = _read_json(workspace.run_dir / "04_report.json")
    timings = _read_json(workspace.run_dir / "05_timings.json")
    search_plan = _read_json(workspace.run_dir / "01_search_plan.json")
    structured_report = report.get("structured_report", {})
    return {
        "status": "completed",
        "case_id": workspace.case_id,
        "run_id": workspace.run_id,
        "overall_verdict": structured_report.get("overall_verdict", "证据不足"),
        "conclusion": structured_report.get("conclusion", ""),
        "claim_checks": structured_report.get("claim_checks", []),
        "uncertainties": structured_report.get("uncertainties", []),
        "source_ids": structured_report.get("source_ids", []),
        "evidence_used": report.get("evidence_used", []),
        "evidence_counts": report.get("evidence_counts", {}),
        "evidence_reviewed_count": report.get("evidence_reviewed_count", 0),
        "evidence_selected_count": report.get("evidence_selected_count", 0),
        "search_plan": search_plan,
        "timings": timings,
        "report_markdown": report.get("final_report", ""),
    }


async def verify_structured_information(
    structured: StructuredInformation,
) -> dict[str, Any]:
    """Run the audited downstream pipeline and return its web-facing result."""
    if not (
        structured.atomic_claims
        or structured.news_facts
        or structured.implicit_opinions
    ):
        return {
            "status": "skipped",
            "case_id": structured.case_id,
            "message": "当前内容没有可核验的主张。",
        }

    _incoming_root.mkdir(parents=True, exist_ok=True)
    payload = structured.model_dump(mode="json", by_alias=True)
    input_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            prefix=f"{structured.case_id}-",
            dir=_incoming_root,
            encoding="utf-8",
            delete=False,
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            input_path = Path(handle.name)

        # The imported demo keeps per-run LLM timings in a process global. Serialising
        # runs preserves the original audit semantics until that module is redesigned.
        async with _verification_lock:
            workspace = await run_pipeline(input_path, data_root=_data_root)
        return _client_result(workspace)
    finally:
        if input_path is not None:
            input_path.unlink(missing_ok=True)
