"""Read-only SQLite access, gold execution with a per-query deadline, and the gold-check cache."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import quote

GoldStatus = Literal["ok", "empty", "error", "timeout"]
STATUSES: tuple[GoldStatus, ...] = ("ok", "empty", "error", "timeout")
PROGRESS_OPS = 10_000
CACHE_VERSION = 1


def readonly_uri(path: Path) -> str:
    posix = path.resolve().as_posix()
    if not posix.startswith("/"):  # Windows drive path: C:/x -> /C:/x
        posix = "/" + posix
    return f"file:{quote(posix)}?mode=ro&immutable=1"


def open_readonly(path: Path) -> sqlite3.Connection:
    """The only way this layer opens a database: read-only and immutable."""
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(readonly_uri(path), uri=True)


@dataclass(frozen=True)
class GoldOutcome:
    status: GoldStatus
    n_rows: int
    wall_s: float
    error: str | None = None


class GoldTimeout(RuntimeError):
    pass


class GoldError(RuntimeError):
    pass


def execute_with_deadline(conn: sqlite3.Connection, sql: str, timeout_s: float) -> list[tuple[object, ...]]:
    """Execute `sql` and fetch every row, interrupting once `timeout_s` has elapsed.

    The deadline is computed per query. An OperationalError raised after the deadline is a
    timeout (GoldTimeout); any other error is GoldError. The connection stays usable afterwards.
    """
    deadline = time.monotonic() + timeout_s
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, PROGRESS_OPS)
    try:
        rows: list[tuple[object, ...]] = conn.execute(sql).fetchall()
    except sqlite3.OperationalError as exc:
        if time.monotonic() > deadline:
            raise GoldTimeout(str(exc)) from exc
        raise GoldError(str(exc)) from exc
    except sqlite3.Error as exc:
        raise GoldError(str(exc)) from exc
    finally:
        conn.set_progress_handler(None, 0)
    return rows


def run_gold(conn: sqlite3.Connection, sql: str, timeout_s: float) -> GoldOutcome:
    """Gold outcome of `sql`: ok (>= 1 row), empty, error or timeout, with wall time."""
    start = time.monotonic()
    try:
        rows = execute_with_deadline(conn, sql, timeout_s)
    except GoldTimeout as exc:
        return GoldOutcome("timeout", 0, time.monotonic() - start, str(exc))
    except GoldError as exc:
        return GoldOutcome("error", 0, time.monotonic() - start, str(exc))
    return GoldOutcome("ok" if rows else "empty", len(rows), time.monotonic() - start)


# --- cache -----------------------------------------------------------------


def gold_source() -> bytes:
    """The source of this module: any change to how gold is executed invalidates the cache."""
    return Path(__file__).read_bytes()


def cache_key(lock: Path, env_lock: Path, allowlist: Path, timeout_s: float, source: bytes | None = None) -> str:
    h = hashlib.sha256()
    for part in (lock.read_bytes(), env_lock.read_bytes(), allowlist.read_bytes(), repr(float(timeout_s)).encode(),
                 gold_source() if source is None else source):
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()


def read_cache(path: Path, key: str) -> dict[str, GoldOutcome] | None:
    """Outcomes stored under exactly `key`, or None (missing, malformed or another key)."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or raw.get("key") != key:
        return None
    entries = raw.get("outcomes")
    if not isinstance(entries, dict):
        return None
    out: dict[str, GoldOutcome] = {}
    for qid, e in entries.items():
        if not isinstance(e, dict):
            return None
        status, n_rows, wall = e.get("status"), e.get("n_rows"), e.get("wall_s")
        if status not in STATUSES or not isinstance(n_rows, int) or not isinstance(wall, (int, float)):
            return None
        out[str(qid)] = GoldOutcome(status, n_rows, float(wall))
    return out


def write_cache(path: Path, key: str, outcomes: Mapping[str, GoldOutcome]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": CACHE_VERSION,
        "key": key,
        "outcomes": {qid: {"status": o.status, "n_rows": o.n_rows, "wall_s": o.wall_s} for qid, o in outcomes.items()},
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass(frozen=True)
class GoldCheck:
    outcomes: dict[str, GoldOutcome]
    from_cache: bool
    seconds: float


def check_gold(
    ids: list[str],
    execute: Callable[[str], GoldOutcome],
    cache_path: Path,
    key: str,
    use_cache: bool,
) -> GoldCheck:
    """Gold outcome per id. The cache is read only when `use_cache` (the caller's guarantee that
    the inputs are verified) and the key matches; otherwise every id executes and the cache is rewritten."""
    start = time.monotonic()
    if use_cache:
        cached = read_cache(cache_path, key)
        if cached is not None and set(cached) == set(ids):
            return GoldCheck(cached, True, time.monotonic() - start)
    outcomes = {qid: execute(qid) for qid in ids}
    write_cache(cache_path, key, outcomes)
    return GoldCheck(outcomes, False, time.monotonic() - start)
