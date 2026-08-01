from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any


class ResultCache:
    SCHEMA_VERSION = "structured-information-v3"
    PIPELINE_VERSION = "lossless-fulltext-v1"

    def __init__(self, ttl_seconds: int) -> None:
        cache_dir = Path(".cache")
        cache_dir.mkdir(exist_ok=True)
        self.path = cache_dir / "video_summary.sqlite3"
        self.ttl_seconds = ttl_seconds
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    cache_key TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            rows = connection.execute(
                "SELECT cache_key, payload FROM summaries"
            ).fetchall()
            legacy_keys = []
            for cache_key, payload in rows:
                try:
                    decoded = json.loads(payload)
                except json.JSONDecodeError:
                    decoded = {}
                if decoded.get("protocol_version") != self.SCHEMA_VERSION:
                    legacy_keys.append((cache_key,))
            if legacy_keys:
                connection.executemany(
                    "DELETE FROM summaries WHERE cache_key = ?", legacy_keys
                )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    @staticmethod
    def key(url: str, mode: str) -> str:
        identity = (
            f"{ResultCache.SCHEMA_VERSION}:{ResultCache.PIPELINE_VERSION}:"
            f"{mode}:{url}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get(self, cache_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at, payload FROM summaries WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if not row:
            return None
        if int(time.time()) - row[0] > self.ttl_seconds:
            return None
        return json.loads(row[1])

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT cache_key, created_at, payload
                FROM summaries
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        now = int(time.time())
        return [
            {
                "cache_key": cache_key,
                "created_at": datetime.fromtimestamp(created_at).isoformat(),
                "expired": now - created_at > self.ttl_seconds,
                "result": json.loads(payload),
            }
            for cache_key, created_at, payload in rows
        ]

    def delete(self, cache_key: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM summaries WHERE cache_key = ?", (cache_key,)
            )
            return cursor.rowcount

    def clear(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM summaries")
            return cursor.rowcount

    def set(self, cache_key: str, payload: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO summaries(cache_key, created_at, payload)
                VALUES (?, ?, ?)
                """,
                (cache_key, int(time.time()), json.dumps(payload, ensure_ascii=False)),
            )
