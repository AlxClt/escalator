"""Execution of submitted SQL for the simulation, through the sandbox, with a persistent
outcome cache.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Protocol

from escalator.datasets.config import REPO_ROOT, load_config
from escalator.env.sandbox import ErrorKind, Result, Sandbox, SqlError, SqlValue
from escalator.util import canon

EXEC_CACHE_SCHEMA = 1
DEFAULT_EXEC_CACHE = REPO_ROOT / ".cache" / "exec.sqlite"
_ERROR_KINDS: frozenset[str] = frozenset({"syntax", "runtime", "timeout", "denied", "multi_statement", "unknown_db"})


class Executor(Protocol):
    def execute(self, db_id: str, sql: str) -> Result | SqlError: ...


def sandbox_salt(sandbox: Sandbox, sources_lock: Path | None = None) -> str:
    """What an outcome depends on besides (db, sql): the pinned data, the engine and the time limit."""
    lock = sources_lock or load_config().lock
    return canon.sha256({"sources_lock": hashlib.sha256(lock.read_bytes()).hexdigest(),
                         "sqlite": sqlite3.sqlite_version, "timeout_s": sandbox.timeout_s})


def _untag(v: object) -> SqlValue:
    if isinstance(v, dict):
        if set(v) == {"$hex"} and isinstance(v["$hex"], str):
            return bytes.fromhex(v["$hex"])
        if set(v) == {"$float"} and isinstance(v["$float"], str):
            return float(v["$float"])
        raise ValueError(f"unexpected tagged value {v!r}")
    if v is None or isinstance(v, (int, float, str)) and not isinstance(v, bool):
        return v
    raise ValueError(f"unexpected cell {v!r}")


def encode(out: Result | SqlError) -> bytes:
    if isinstance(out, SqlError):
        return canon.dumps({"error": out.kind, "message": out.message})
    return canon.dumps({"columns": out.columns, "rows": out.rows, "truncated": out.truncated})


def decode(data: bytes) -> Result | SqlError:
    obj = json.loads(data)
    if not isinstance(obj, dict):
        raise ValueError("malformed execution cache entry")
    if "error" in obj:
        kind = obj["error"]
        if kind not in _ERROR_KINDS:
            raise ValueError(f"unknown error kind {kind!r}")
        error_kind: ErrorKind = kind
        return SqlError(error_kind, str(obj["message"]))
    rows = [[_untag(v) for v in row] for row in obj["rows"]]
    return Result(columns=[str(c) for c in obj["columns"]], rows=rows, truncated=bool(obj["truncated"]))


class CachedExecutor:
    """sandbox.execute(db_id, sql, row_cap=None), memoized in memory and, when `path` is given, on disk."""

    def __init__(self, sandbox: Sandbox, path: Path | None = DEFAULT_EXEC_CACHE, *, salt: str | None = None) -> None:
        self.sandbox = sandbox
        self.salt = salt if salt is not None else sandbox_salt(sandbox)
        self._memo: dict[str, Result | SqlError] = {}
        self._conn: sqlite3.Connection | None = None
        self.executed = 0  # sandbox calls made (misses), for reporting
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path)
            self._conn.execute("CREATE TABLE IF NOT EXISTS outcomes (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
            self._conn.commit()

    def key(self, db_id: str, sql: str) -> str:
        return canon.sha256({"v": EXEC_CACHE_SCHEMA, "salt": self.salt, "db_id": db_id, "sql": sql})

    def execute(self, db_id: str, sql: str) -> Result | SqlError:
        k = self.key(db_id, sql)
        hit = self._memo.get(k)
        if hit is not None:
            return hit
        out: Result | SqlError | None = None
        if self._conn is not None:
            row = self._conn.execute("SELECT value FROM outcomes WHERE key = ?", (k,)).fetchone()
            if row is not None:
                out = decode(bytes(row[0]))
        if out is None:
            out = self.sandbox.execute(db_id, sql, row_cap=None)
            self.executed += 1
            if self._conn is not None:
                self._conn.execute("INSERT OR IGNORE INTO outcomes (key, value) VALUES (?, ?)", (k, encode(out)))
                self._conn.commit()
        self._memo[k] = out
        return out

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
