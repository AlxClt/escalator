"""Response cache: SQLite keyed by a hash of the entire request plus its unsent context.

Values are raw provider bodies, normalized on read, so a parser bug is fixed at $0.
Successful completions only (including max_tokens and refusals); transport errors are never cached.
Concurrency: WAL and INSERT OR IGNORE, first writer wins. Two concurrent misses on one key both
call the provider and both pay; this is accepted.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from escalator.datasets.config import REPO_ROOT
from escalator.llm.errors import CacheMiss
from escalator.llm.types import Request
from escalator.util import canon

CACHE_SCHEMA = 1  # bump when key semantics change
DEFAULT_CACHE = REPO_ROOT / ".cache" / "llm.sqlite"

CacheMode = Literal["readwrite", "readonly", "off"]


def key(req: Request, context: dict[str, str]) -> str:
    return canon.sha256({"v": CACHE_SCHEMA, "req": req.model_dump(mode="json"), "ctx": context})


@dataclass(frozen=True)
class Entry:
    provider: str
    raw: bytes
    latency_ms: int
    created_at: str


class Cache:
    def __init__(self, path: Path = DEFAULT_CACHE, mode: CacheMode = "readwrite") -> None:
        self.path = path
        self.mode: CacheMode = mode
        self._conn: sqlite3.Connection | None = None
        if mode == "readwrite":
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, provider TEXT NOT NULL,"
                " raw BLOB NOT NULL, latency_ms INTEGER NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
            self._conn = conn
        elif mode == "readonly":
            if not path.is_file():
                raise CacheMiss(f"readonly cache {path} does not exist")
            self._conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)

    def get(self, k: str) -> Entry | None:
        """The stored entry, or None on a miss. A readonly cache raises CacheMiss instead."""
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT provider, raw, latency_ms, created_at FROM responses WHERE key = ?", (k,)
        ).fetchone()
        if row is None:
            if self.mode == "readonly":
                raise CacheMiss(k)
            return None
        return Entry(str(row[0]), bytes(row[1]), int(row[2]), str(row[3]))

    def put(self, k: str, provider: str, raw: bytes, latency_ms: int) -> None:
        if self.mode != "readwrite" or self._conn is None:
            return
        created_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        self._conn.execute(
            "INSERT OR IGNORE INTO responses (key, provider, raw, latency_ms, created_at) VALUES (?, ?, ?, ?, ?)",
            (k, provider, raw, latency_ms, created_at),
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
