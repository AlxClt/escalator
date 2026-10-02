from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from escalator.datasets import verify
from escalator.datasets.config import DataConfig
from escalator.datasets.gold import (
    GoldOutcome,
    cache_key,
    check_gold,
    open_readonly,
    read_cache,
    readonly_uri,
    run_gold,
    write_cache,
)


def _make_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    conn.commit()
    conn.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_uri_percent_encodes_path(tmp_path: Path) -> None:
    uri = readonly_uri(tmp_path / "a b#c%d.sqlite")
    assert uri.startswith("file:/") and uri.endswith("?mode=ro&immutable=1")
    assert " " not in uri and "#" not in uri.split("?")[0]


# U8
def test_u8_readonly_connection_rejects_writes_and_leaves_file_untouched(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "odd name #1.sqlite")
    before = _sha(db)
    conn = open_readonly(db)
    assert conn.execute("SELECT count(*) FROM t").fetchone() == (100,)
    for stmt in ("INSERT INTO t VALUES (1)", "CREATE TABLE u (y)", "DELETE FROM t"):
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(stmt)
    conn.close()
    assert _sha(db) == before
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["odd name #1.sqlite"]


# U11
def test_u11_slow_query_is_timeout_and_connection_stays_usable(tmp_path: Path) -> None:
    conn = open_readonly(_make_db(tmp_path / "t.sqlite"))
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT max(x) FROM c"
    outcome = run_gold(conn, slow, timeout_s=0.2)
    assert outcome.status == "timeout"
    assert run_gold(conn, "SELECT 1", timeout_s=5).status == "ok"
    assert run_gold(conn, "SELECT x FROM t WHERE x < 0", timeout_s=5).status == "empty"
    assert run_gold(conn, "SELECT nope FROM t", timeout_s=5).status == "error"
    conn.close()


# U12
def test_u12_deadline_is_per_query_not_per_connection(tmp_path: Path) -> None:
    conn = open_readonly(_make_db(tmp_path / "t.sqlite"))
    bounded = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 1500000) SELECT max(x) FROM c"
    unbounded = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT max(x) FROM c"
    runs = sorted(run_gold(conn, bounded, timeout_s=60).wall_s for _ in range(3))
    budget = runs[1] / 0.6  # the bounded query needs about 60% of the budget
    assert run_gold(conn, unbounded, timeout_s=budget).status == "timeout"
    assert run_gold(conn, bounded, timeout_s=budget).status == "ok"
    assert [run_gold(conn, bounded, timeout_s=budget).status for _ in range(2)] == ["ok", "ok"]
    conn.close()


# U13
def _key_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    paths = (tmp_path / "sources.lock", tmp_path / "env.lock", tmp_path / "gold_allowlist.json")
    for p in paths:
        p.write_text(p.name, encoding="utf-8")
    return paths


def test_u13_every_key_component_invalidates(tmp_path: Path) -> None:
    lock, env, allow = _key_inputs(tmp_path)
    base = cache_key(lock, env, allow, 60, b"src")
    assert cache_key(lock, env, allow, 60, b"src") == base
    keys = {cache_key(lock, env, allow, 61, b"src"), cache_key(lock, env, allow, 60, b"src2")}
    for p in (lock, env, allow):
        original = p.read_text(encoding="utf-8")
        p.write_text(original + " changed", encoding="utf-8")
        keys.add(cache_key(lock, env, allow, 60, b"src"))
        p.write_text(original, encoding="utf-8")
    assert base not in keys and len(keys) == 5


def _counting_executor(calls: list[str]) -> Callable[[str], GoldOutcome]:
    def execute(qid: str) -> GoldOutcome:
        calls.append(qid)
        return GoldOutcome("ok", 1, 0.01)

    return execute


def test_u13_cache_under_another_key_is_ignored_and_rewritten(tmp_path: Path) -> None:
    cache = tmp_path / "_cache" / "gold_check.json"
    write_cache(cache, "other-key", {"1": GoldOutcome("error", 0, 1.0)})
    calls: list[str] = []
    first = check_gold(["1", "2"], _counting_executor(calls), cache, "key", use_cache=True)
    assert not first.from_cache and calls == ["1", "2"]
    assert read_cache(cache, "key") == first.outcomes and read_cache(cache, "other-key") is None
    second = check_gold(["1", "2"], _counting_executor(calls), cache, "key", use_cache=True)
    assert second.from_cache and second.outcomes == first.outcomes and calls == ["1", "2"]


def test_u13_cache_never_read_when_inputs_unverified(tmp_path: Path) -> None:
    cache = tmp_path / "gold_check.json"
    write_cache(cache, "key", {"1": GoldOutcome("ok", 1, 0.01)})
    calls: list[str] = []
    check = check_gold(["1"], _counting_executor(calls), cache, "key", use_cache=False)
    assert not check.from_cache and calls == ["1"]


@pytest.mark.parametrize("failed_gate", ["V2", "V5"])
def test_u13_context_does_not_read_cache_after_v2_or_v5_failed(tmp_path: Path, failed_gate: str) -> None:
    raw = tmp_path / "raw"
    db = raw / "bird" / "superhero" / "superhero.sqlite"
    db.parent.mkdir(parents=True)
    _make_db(db)
    (raw / "arcwise").mkdir()
    (raw / "arcwise" / "arcwise_plat_full_with_diff.json").write_text(
        json.dumps([{"question_id": 1, "db_id": "superhero", "question": "q", "evidence": None, "SQL": "SELECT 1"}]),
        encoding="utf-8")
    lock, env, allow = _key_inputs(tmp_path)
    cfg = DataConfig(raw, lock, env, 60.0, 5.0)
    key = cache_key(lock, env, allow, 60.0)
    write_cache(cfg.gold_cache, key, {"1": GoldOutcome("error", 0, 9.0)})  # a poisoned entry

    ctx = verify.DataContext(cfg)
    ctx.evaluated = {"V2": True, "V5": True, failed_gate: False}
    check = ctx.gold()
    ctx.close()
    assert not check.from_cache and check.outcomes["1"].status == "ok"
