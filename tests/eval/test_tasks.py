"""Gold side of scoring: gold re-executed through the sandbox and checked against gold_result_hash."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from escalator.datasets.config import REPO_ROOT
from escalator.datasets.manifest import result_hash
from escalator.env.sandbox import Sandbox
from escalator.eval.tasks import GoldMismatch, Task, gold_result, load_ordered_ids, load_tasks, score_sql

DB_ID = "fixture"
GOLD_SQL = "SELECT id, name FROM t WHERE id <= 3"
GOLD_ROWS: list[tuple[object, ...]] = [(1, "n1"), (2, "n2"), (3, "n3")]


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    path = tmp_path / "bird" / DB_ID / f"{DB_ID}.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score REAL)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, f"n{i}", i / 3) for i in range(1, 11)])
    conn.commit()
    conn.close()
    return Sandbox(tmp_path, {DB_ID}, expected_sqlite=sqlite3.sqlite_version)


def _task(sql: str = GOLD_SQL, gold_hash: str | None = None, ordered: bool = False) -> Task:
    return Task("1", DB_ID, "simple", sql, gold_hash or result_hash(GOLD_ROWS), ordered)


def test_load_ordered_ids(tmp_path: Path) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text("ordered_tasks:\n  - 824\n  - '1040'\n", encoding="utf-8")
    assert load_ordered_ids(path) == {"824", "1040"}


@pytest.mark.parametrize("text", ["ordered_tasks: 824\n", "other: []\n", "- 824\n", "ordered_tasks: [x]\n"])
def test_load_ordered_ids_rejects_malformed(tmp_path: Path, text: str) -> None:
    path = tmp_path / "scoring.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_ordered_ids(path)


def test_pinned_ordered_ids() -> None:
    assert load_ordered_ids() == {"824", "1040"}


def test_gold_result_matches_hash(sandbox: Sandbox) -> None:
    out = gold_result(sandbox, _task())
    assert out.rows == [list(r) for r in GOLD_ROWS] and not out.truncated


def test_gold_hash_drift_raises(sandbox: Sandbox) -> None:
    with pytest.raises(GoldMismatch, match="gold_result_hash"):
        gold_result(sandbox, _task(gold_hash=result_hash([(1, "n1")])))


def test_gold_sql_error_raises(sandbox: Sandbox) -> None:
    with pytest.raises(GoldMismatch, match="failed in the sandbox"):
        gold_result(sandbox, _task(sql="SELECT nope FROM t"))


def test_gold_is_never_row_capped(sandbox: Sandbox) -> None:
    rows: list[tuple[object, ...]] = [(i,) for i in range(1, 11)]
    task = Task("1", DB_ID, "simple", "SELECT id FROM t", result_hash(rows), False)
    assert len(gold_result(sandbox, task).rows) == 10


def test_score_sql_reordered_prediction(sandbox: Sandbox) -> None:
    out = score_sql(sandbox, _task(), "SELECT name, id FROM t WHERE id IN (3, 1, 2) ORDER BY id DESC")
    assert out.ex and out.soft_f1 == 1.0 and out.error is None


def test_score_sql_wrong_prediction(sandbox: Sandbox) -> None:
    out = score_sql(sandbox, _task(), "SELECT id, name FROM t WHERE id <= 2")
    assert not out.ex and 0.0 < out.soft_f1 < 1.0


def test_score_sql_ordered_task(sandbox: Sandbox) -> None:
    task = _task(GOLD_SQL + " ORDER BY id DESC", ordered=True)
    assert score_sql(sandbox, task, "SELECT id, name FROM t WHERE id <= 3 ORDER BY id DESC").ex
    assert not score_sql(sandbox, task, "SELECT id, name FROM t WHERE id <= 3 ORDER BY id").ex


def test_score_sql_gold_order_by_alone_is_unordered(sandbox: Sandbox) -> None:
    task = _task(GOLD_SQL + " ORDER BY id DESC")
    assert score_sql(sandbox, task, "SELECT id, name FROM t WHERE id <= 3 ORDER BY id").ex


def test_score_sql_prediction_error(sandbox: Sandbox) -> None:
    out = score_sql(sandbox, _task(), "SELEC 1")
    assert not out.ex and out.soft_f1 == 0.0 and out.error == "syntax"


# --- on the pinned data ------------------------------------------------------

BIRD_ROOT = REPO_ROOT / "data" / "raw" / "bird"


@pytest.mark.skipif(not any(BIRD_ROOT.glob("*/*.sqlite")), reason="BIRD databases not installed (run make data)")
def test_pinned_gold_scores_itself() -> None:
    tasks = load_tasks()
    assert len(tasks) == 200
    assert {t.question_id for t in tasks if t.ordered} == load_ordered_ids()
    sandbox = Sandbox.from_config()
    for task in tasks[::20]:
        out = score_sql(sandbox, task, task.gold_sql)
        assert out.ex and out.soft_f1 == 1.0, task.question_id
