"""U4-U6: the sandbox denies every write and side channel, times out runaway queries, caps rows exactly."""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from escalator.env import sandbox as sandbox_mod
from escalator.env.sandbox import Result, Sandbox, SqlError

DB_ID = "fixture"


@pytest.fixture
def db_root(tmp_path: Path) -> Path:
    # A space and '#' in the path: the URI must be percent-encoded, not concatenated.
    root = tmp_path / "data root #1"
    path = root / "bird" / DB_ID / f"{DB_ID}.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
    conn.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"n{i}") for i in range(1, 1002)])  # 1001 rows
    conn.commit()
    conn.close()
    (tmp_path / "other.sqlite").write_bytes(b"")
    return root


def _sandbox(db_root: Path, timeout_s: float = 30.0) -> Sandbox:
    return Sandbox(db_root, {DB_ID}, expected_sqlite=sqlite3.sqlite_version, timeout_s=timeout_s)


def _digest(db_root: Path) -> str:
    return hashlib.sha256((db_root / "bird" / DB_ID / f"{DB_ID}.sqlite").read_bytes()).hexdigest()


# --- U4 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("INSERT INTO t VALUES (5000, 'x')", "denied"),
        ("DROP TABLE t", "denied"),
        ("ATTACH DATABASE '../other.sqlite' AS o", "denied"),
        ("PRAGMA table_info(t)", "denied"),
        ("pragma journal_mode = delete", "denied"),
        ("CREATE TEMP TABLE z (a)", "denied"),
        ("/* SELECT */ DELETE FROM t", "denied"),
        ("SELECT 1; DROP TABLE t", "multi_statement"),
    ],
)
def test_sandbox_denies(db_root: Path, sql: str, kind: str) -> None:
    before = _digest(db_root)
    out = _sandbox(db_root).execute(DB_ID, sql)
    assert isinstance(out, SqlError) and out.kind == kind, out
    assert _digest(db_root) == before


@pytest.mark.parametrize("db_id", ["nope", "../fixture", f"bird/{DB_ID}", ""])
def test_sandbox_unknown_db(db_root: Path, db_id: str) -> None:
    out = _sandbox(db_root).execute(db_id, "SELECT 1")
    assert out == SqlError("unknown_db", f"unknown database: {db_id}")


# --- U5 ----------------------------------------------------------------------


def test_sandbox_timeout(db_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def spy(*args: object, **kwargs: object) -> sqlite3.Connection:
        conn = real_connect(*args, **kwargs)  # type: ignore[call-overload]
        opened.append(conn)
        return conn

    monkeypatch.setattr(sandbox_mod.sqlite3, "connect", spy)
    sql = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT n FROM r"
    start = time.monotonic()
    out = _sandbox(db_root, timeout_s=0.2).execute(DB_ID, sql, row_cap=None)
    assert time.monotonic() - start < 5
    assert isinstance(out, SqlError) and out.kind == "timeout"
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):  # closed
        opened[0].execute("SELECT 1")


# --- U6 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "row_cap", "n_rows", "truncated"),
    [
        ("SELECT id, name FROM t ORDER BY id", 1000, 1000, True),
        ("SELECT id, name FROM t ORDER BY id LIMIT 1000", 1000, 1000, False),
        ("SELECT id, name FROM t ORDER BY id", None, 1001, False),
    ],
)
def test_sandbox_row_cap(db_root: Path, sql: str, row_cap: int | None, n_rows: int, truncated: bool) -> None:
    out = _sandbox(db_root).execute(DB_ID, sql, row_cap=row_cap)
    assert isinstance(out, Result)
    assert out.columns == ["id", "name"]
    assert len(out.rows) == n_rows and out.truncated is truncated
    assert out.rows[0] == [1, "n1"] and out.rows[-1] == [n_rows, f"n{n_rows}"]
