from __future__ import annotations

import os
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


def data_root() -> Path:
    configured = os.getenv("FREETIME_DATA_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.cwd()
    root.mkdir(parents=True, exist_ok=True)
    return root


def cache_root() -> Path:
    root = data_root() / ".cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def copy_missing(source: Path, destination: Path) -> int:
    """Copy legacy user data without replacing anything already migrated."""
    if not source.exists():
        return 0
    if source.is_file():
        if destination.exists():
            return 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return 1

    copied = 0
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    return copied


def _merge_summary_database(source: Path, destination: Path) -> int:
    if not source.is_file():
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copy2(source, destination)
        with sqlite3.connect(destination) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM summaries").fetchone()[0])

    with sqlite3.connect(source) as source_connection:
        rows = source_connection.execute(
            "SELECT cache_key, created_at, payload FROM summaries"
        ).fetchall()
    merged = 0
    with sqlite3.connect(destination) as destination_connection:
        for row in rows:
            cursor = destination_connection.execute(
                """
                INSERT INTO summaries(cache_key, created_at, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    created_at = excluded.created_at,
                    payload = excluded.payload
                WHERE excluded.created_at > summaries.created_at
                """,
                row,
            )
            merged += max(0, cursor.rowcount)
    return merged


def _read_task_jobs(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _merge_task_jobs(source: Path, destination: Path) -> int:
    source_jobs = _read_task_jobs(source)
    if not source_jobs:
        return 0
    destination_jobs = _read_task_jobs(destination)
    merged = 0
    for task_id, source_job in source_jobs.items():
        current = destination_jobs.get(task_id)
        if current is None or float(source_job.get("updated_at") or 0) > float(current.get("updated_at") or 0):
            destination_jobs[task_id] = source_job
            merged += 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(destination_jobs, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
    return merged


def merge_data_roots(source: Path, destination: Path) -> dict[str, int]:
    """Merge user-owned data without replacing newer destination records."""
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or not source.exists():
        return {"summaries_merged": 0, "tasks_merged": 0, "files_copied": 0}
    destination.mkdir(parents=True, exist_ok=True)
    summaries_merged = _merge_summary_database(
        source / ".cache" / "video_summary.sqlite3",
        destination / ".cache" / "video_summary.sqlite3",
    )
    tasks_merged = _merge_task_jobs(
        source / "task-jobs.json",
        destination / "task-jobs.json",
    )
    files_copied = copy_missing(source / ".cache", destination / ".cache")
    files_copied += copy_missing(source / "settings.json", destination / "settings.json")
    files_copied += copy_missing(
        source / "download-settings.json", destination / "download-settings.json"
    )
    files_copied += copy_missing(
        source / "chaoxing" / "config.json",
        destination / "chaoxing" / "config.json",
    )
    return {
        "summaries_merged": summaries_merged,
        "tasks_merged": tasks_merged,
        "files_copied": files_copied,
    }


def migrate_legacy_data(data_dir: Path, legacy_roots: list[Path]) -> int:
    data_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0
    for root in legacy_roots:
        result = merge_data_roots(root, data_dir)
        migrated += sum(result.values())
        migrated += copy_missing(
            root / "apps" / "chaoxing-auto" / "config.json",
            data_dir / "chaoxing" / "config.json",
        )
    return migrated
