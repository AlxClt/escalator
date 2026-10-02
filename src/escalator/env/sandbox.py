"""SQLite sandbox: execute(db_id, sql) -> Result | SqlError, deterministic and stateless.

One fresh read-only, immutable connection per call. Enforcement is an authorizer allowlist (not
string matching), one statement per call, a wall-clock deadline via the progress handler that stays
active through fetch, and a row cap. No timings are returned: elapsed time never reaches the model.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from escalator.datasets.config import EXPECTED_DB_IDS, load_config
from escalator.datasets.lock import iter_files, load_env_lock, load_lock

TIMEOUT_S = 30.0  # X1: wall clock only
ROW_CAP = 1000
PROGRESS_OPS = 1000

ErrorKind = Literal["syntax", "runtime", "timeout", "denied", "multi_statement", "unknown_db"]

_ALLOWED = frozenset({sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE})

# SQLite values as returned, never coerced: type handling is the scorer's job.
SqlValue = int | float | str | bytes | None


@dataclass(frozen=True)
class Result:
    columns: list[str]  # a list: SELECT a.id, b.id duplicates names
    rows: list[list[SqlValue]]
    truncated: bool


@dataclass(frozen=True)
class SqlError:
    kind: ErrorKind
    message: str  # SQLite's own message: deterministic, shown to the model as-is


def _authorize(action: int, *_: object) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def _text(b: bytes) -> str:
    # The default text_factory raises mid-fetch on a non-UTF-8 TEXT value.
    return b.decode("utf-8", errors="backslashreplace")


class Sandbox:
    def __init__(
        self,
        db_root: Path,
        db_ids: Iterable[str],
        *,
        expected_sqlite: str,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        if sqlite3.sqlite_version != expected_sqlite:
            raise RuntimeError(f"SQLite {sqlite3.sqlite_version} != env.lock {expected_sqlite}")
        self.db_root = db_root
        self.db_ids = frozenset(db_ids)
        self.timeout_s = timeout_s

    @classmethod
    def from_config(cls, *, timeout_s: float = TIMEOUT_S) -> Sandbox:
        """The 11 Mini-Dev databases pinned in sources.lock, under db_root from configs/data.yaml."""
        cfg = load_config()
        ids = {PurePosixPath(f.path).parent.name for f in iter_files(load_lock(cfg.lock))
               if f.path.endswith(".sqlite")}
        if ids != set(EXPECTED_DB_IDS):
            raise RuntimeError(f"sources.lock databases {sorted(ids)} != expected {list(EXPECTED_DB_IDS)}")
        return cls(cfg.data_root, ids, expected_sqlite=load_env_lock(cfg.env_lock).sqlite_version,
                   timeout_s=timeout_s)

    def path(self, db_id: str) -> Path:
        return self.db_root / "bird" / db_id / f"{db_id}.sqlite"

    def execute(self, db_id: str, sql: str, *, row_cap: int | None = ROW_CAP) -> Result | SqlError:
        """`row_cap=None` returns every row (the scorer compares full result sets)."""
        # The allowlist check also closes path traversal: db_id never reaches the filesystem unchecked.
        if db_id not in self.db_ids:
            return SqlError("unknown_db", f"unknown database: {db_id}")
        path = self.path(db_id)
        if not path.is_file():
            return SqlError("unknown_db", f"database file missing: {db_id}")
        # as_uri() percent-encodes; concatenating a raw path breaks on '?', '#' or spaces.
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        deadline = time.monotonic() + self.timeout_s
        try:
            conn.text_factory = _text
            conn.set_authorizer(_authorize)
            # Active through fetch: rows are produced lazily.
            conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_OPS)
            try:
                cur = conn.execute(sql)
                rows = cur.fetchall() if row_cap is None else cur.fetchmany(row_cap + 1)
                columns = [d[0] for d in cur.description or ()]
            except sqlite3.ProgrammingError as exc:
                if "one statement" in str(exc):
                    return SqlError("multi_statement", str(exc))
                return SqlError("runtime", str(exc))
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if msg == "interrupted" and time.monotonic() > deadline:
                    return SqlError("timeout", f"query exceeded {self.timeout_s:g} s")
                if "syntax error" in msg or msg == "incomplete input":
                    return SqlError("syntax", msg)
                if "not authorized" in msg:
                    return SqlError("denied", msg)
                return SqlError("runtime", msg)
            except sqlite3.DatabaseError as exc:
                msg = str(exc)
                return SqlError("denied" if "not authorized" in msg else "runtime", msg)
        finally:
            conn.close()
        truncated = row_cap is not None and len(rows) > row_cap
        if truncated:
            rows = rows[:row_cap]
        return Result(columns=columns, rows=[list(r) for r in rows], truncated=truncated)
