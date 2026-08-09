import json
import sqlite3
from pathlib import Path

from app.storage import merge_data_roots


def _write_summary_db(path: Path, rows: list[tuple[str, int, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE summaries (cache_key TEXT PRIMARY KEY, created_at INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        connection.executemany("INSERT INTO summaries VALUES (?, ?, ?)", rows)


def test_merge_data_roots_preserves_both_histories_and_newest_duplicates(tmp_path: Path) -> None:
    source = tmp_path / "preview"
    destination = tmp_path / "installed"
    _write_summary_db(
        source / ".cache" / "video_summary.sqlite3",
        [("preview-only", 20, "preview"), ("shared", 30, "new")],
    )
    _write_summary_db(
        destination / ".cache" / "video_summary.sqlite3",
        [("installed-only", 10, "installed"), ("shared", 25, "old")],
    )

    result = merge_data_roots(source, destination)

    with sqlite3.connect(destination / ".cache" / "video_summary.sqlite3") as connection:
        rows = connection.execute(
            "SELECT cache_key, created_at, payload FROM summaries ORDER BY cache_key"
        ).fetchall()
    assert rows == [
        ("installed-only", 10, "installed"),
        ("preview-only", 20, "preview"),
        ("shared", 30, "new"),
    ]
    assert result["summaries_merged"] == 2


def test_merge_data_roots_merges_tasks_and_keeps_destination_settings(tmp_path: Path) -> None:
    source = tmp_path / "preview"
    destination = tmp_path / "installed"
    source.mkdir()
    destination.mkdir()
    (source / "task-jobs.json").write_text(
        json.dumps({"source": {"updated_at": 2}, "shared": {"updated_at": 5, "status": "success"}}),
        encoding="utf-8",
    )
    (destination / "task-jobs.json").write_text(
        json.dumps({"installed": {"updated_at": 3}, "shared": {"updated_at": 4, "status": "error"}}),
        encoding="utf-8",
    )
    (source / "settings.json").write_text('{"api_key":"preview"}', encoding="utf-8")
    (destination / "settings.json").write_text('{"api_key":"installed"}', encoding="utf-8")

    result = merge_data_roots(source, destination)

    tasks = json.loads((destination / "task-jobs.json").read_text(encoding="utf-8"))
    assert set(tasks) == {"source", "installed", "shared"}
    assert tasks["shared"]["status"] == "success"
    assert json.loads((destination / "settings.json").read_text(encoding="utf-8"))["api_key"] == "installed"
    assert result["tasks_merged"] == 2
