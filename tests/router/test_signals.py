"""router/signals.py: week-3 spec tests 5 (error), 6 (self-consistency), 7 (verifier), 8 (no label access)."""

from __future__ import annotations

import itertools
import sqlite3
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from router_helpers import FakeExecutor, ended, run_of, runset, submitted

from escalator.env.sandbox import Sandbox, SqlError
from escalator.router import inventory as inventory_mod
from escalator.router.execcache import CachedExecutor
from escalator.router.inventory import SmallRunSets
from escalator.router.signals import ans, compute_signals, error_signal, sc_score, submit_of, verifier_score
from escalator.trace.schema import End

FIXTURE_SQL = Path(__file__).resolve().parents[1] / "fixtures" / "agent_db.sql"
ERR = SqlError("runtime", "no such column: x")
TIMEOUT = SqlError("timeout", "query exceeded 30 s")


@pytest.fixture
def fixture_sandbox(tmp_path: Path) -> Sandbox:
    path = tmp_path / "bird" / "fixture" / "fixture.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return Sandbox(tmp_path, ["fixture"], expected_sqlite=sqlite3.sqlite_version, timeout_s=0.2)


# --- test 5: error -----------------------------------------------------------------------------


@pytest.mark.parametrize(("end", "expected"), [
    ("no_tool_call", 1), ("max_steps", 1), ("validation_exhausted", 1), ("context_overflow", 1),
])
def test_error_signal_per_failed_end(end: End, expected: int) -> None:
    assert error_signal(run_of(ended(end)), FakeExecutor({})) == expected


@pytest.mark.parametrize(("outcome", "expected"), [([[1]], 0), (ERR, 1), (TIMEOUT, 1), (SqlError("denied", "x"), 1)])
def test_error_signal_on_a_submitted_run(outcome: object, expected: int) -> None:
    ex = FakeExecutor({"SELECT q": outcome})  # type: ignore[dict-item]
    assert error_signal(run_of(submitted("SELECT q")), ex) == expected


def test_recovered_errors_do_not_count() -> None:
    recs = submitted("SELECT ok")
    first = recs[0].model_copy(update={"tool_status": ["execution_error"]})
    ex = FakeExecutor({"SELECT ok": [[1]]})
    assert error_signal(run_of([first, recs[1]]), ex) == 0


def test_provider_error_has_no_error_signal() -> None:
    with pytest.raises(ValueError):
        error_signal(run_of(ended("provider_error")), FakeExecutor({}))


def test_error_signal_through_the_real_sandbox(fixture_sandbox: Sandbox) -> None:
    ex = CachedExecutor(fixture_sandbox, path=None, salt="t")
    assert error_signal(run_of(submitted("SELECT name FROM player")), ex) == 0
    assert error_signal(run_of(submitted("SELECT nope FROM player")), ex) == 1
    slow = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 100000000) "
            "SELECT COUNT(*) FROM c")
    assert error_signal(run_of(submitted(slow)), ex) == 1
    out = ex.execute("fixture", slow)
    assert isinstance(out, SqlError) and out.kind == "timeout"


def test_submit_is_the_executed_call() -> None:
    recs = submitted("SELECT 1", explore=False)
    rec = recs[0].model_copy(update={
        "tool": ["submit_answer", "submit_answer", "submit_answer"],
        "args": [None, {"db_id": "fixture", "sql": "SELECT 2"}, {"db_id": "fixture", "sql": "SELECT 3"}],
        "args_raw": ["{", None, None], "n_tool_calls": 3, "tool_status": ["validation_error", "ok", "ignored"],
        "result_hash": [None, None, None], "result_preview": [None, None, None]})
    sub = submit_of(run_of([rec]))
    assert sub is not None and sub.sql == "SELECT 2"
    assert submit_of(run_of(ended("max_steps"))) is None


# --- test 6: self-consistency -------------------------------------------------------------------

A, B, C = [[1, "a"]], [[2, "b"]], [[3, "c"]]


@pytest.mark.parametrize(("answers", "s", "m"), [
    ([A, A, A], Fraction(0), 3),
    ([A, A, B], Fraction(1, 3), 2),
    ([A, B, C], Fraction(2, 3), 1),
    ([A, None, None], Fraction(2, 3), 1),
    ([None, None, None], Fraction(1), 0),
    ([[[1], [2]], [[2], [1]], [[1], [2]]], Fraction(0), 3),  # equal up to row order
    ([[[1.0]], [[1.0 + 1e-9]], [[1]]], Fraction(0), 3),  # equal up to float tolerance, int vs float
    ([[["x", 1]], [[1, "x"]], [["x", 1]]], Fraction(0), 3),  # column permutation, as the scorer
    ([[[None]], [[None]], [[0]]], Fraction(1, 3), 2),  # NULL equals NULL, not 0
    ([[], [], A], Fraction(1, 3), 2),  # two empty results agree
])
def test_sc_table(answers: list[object], s: Fraction, m: int) -> None:
    assert sc_score(answers) == (s, m)  # type: ignore[arg-type]


def test_sc_non_transitive_tolerance_is_well_defined_and_order_free() -> None:
    # x ~ y and y ~ z under rel_tol 1e-6, but x !~ z.
    x, y, z = [[1.0]], [[1.0 + 0.9e-6]], [[1.0 + 1.8e-6]]
    results = {sc_score(list(p)) for p in itertools.permutations([x, y, z])}  # type: ignore[arg-type]
    assert results == {(Fraction(0), 3)}  # y agrees with both: m = 3
    results = {sc_score(list(p)) for p in itertools.permutations([x, z, z])}  # type: ignore[arg-type]
    assert results == {(Fraction(1, 3), 2)}


def test_sc_samples_come_from_runs() -> None:
    ex = FakeExecutor({"SELECT a": A, "SELECT a2": [["a", 1]], "SELECT b": ERR})
    samples = [run_of(submitted("SELECT a", sample_idx=0)), run_of(submitted("SELECT a2", sample_idx=1)),
               run_of(submitted("SELECT b", sample_idx=2))]

    assert sc_score([ans(r, ex) for r in samples]) == (Fraction(1, 3), 2)


# --- test 7: verifier ---------------------------------------------------------------------------


@pytest.mark.parametrize(("base", "ver", "s"), [
    (A, A, 0), (A, [[1, "a"], [1, "a"]], 1), (A, B, 1), (None, A, 1), (A, None, 1), (None, None, 1),
])
def test_verifier_score(base: object, ver: object, s: int) -> None:
    assert verifier_score(base, ver) == s  # type: ignore[arg-type]


# --- test 8: no label access ----------------------------------------------------------------------


def _small(sc: bool, verifier: bool) -> SmallRunSets:
    base = runset("b", "m", submitted("SELECT a", run_id="b", task_id="1") + ended("max_steps", run_id="b", task_id="2"))
    sc_recs = []
    for j, sql in enumerate(["SELECT a", "SELECT a", "SELECT b"]):
        sc_recs += submitted(sql, run_id="s", task_id="1", sample_idx=j)
        sc_recs += ended("no_tool_call", run_id="s", task_id="2", sample_idx=j)
    ver_recs = submitted("SELECT b", run_id="v", task_id="1") + submitted("SELECT a", run_id="v", task_id="2")
    return SmallRunSets("m", base, runset("s", "m", sc_recs, run_kind="self_consistency") if sc else None,
                        runset("v", "m", ver_recs, run_kind="verifier") if verifier else None)


def test_signals_never_read_gold_or_frontier(monkeypatch: pytest.MonkeyPatch) -> None:
    import escalator.eval.tasks as tasks_mod

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("label access")

    for name in ("gold_result", "load_tasks", "score_sql", "load_ordered_tasks"):
        monkeypatch.setattr(tasks_mod, name, boom)
    monkeypatch.setattr(inventory_mod, "read_frontier_runs", boom)
    table = compute_signals(_small(True, True), ["1", "2"], FakeExecutor({"SELECT a": A, "SELECT b": B}))
    assert table.rows["1"].s_error == 0 and table.rows["2"].s_error == 1
    assert (table.rows["1"].s_sc, table.rows["1"].m_sc) == (Fraction(1, 3), 2)
    assert (table.rows["2"].s_sc, table.rows["2"].m_sc) == (Fraction(1), 0)
    assert table.rows["1"].s_verifier == 1 and table.rows["2"].s_verifier == 1


def test_signals_module_does_not_import_the_gold_side() -> None:
    code = ("import sys, escalator.router.signals; "
            "bad = [m for m in sys.modules if m in ('escalator.eval.tasks', 'escalator.datasets.gold', "
            "'escalator.eval.runner', 'escalator.trace.replay')]; print(bad); assert not bad")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_uncollected_signals_are_null_not_zero() -> None:
    table = compute_signals(_small(False, False), ["1", "2"], FakeExecutor({"SELECT a": A}))
    assert table.status == {"error": "ok", "self_consistency": "not_collected", "verifier": "not_collected"}
    assert all(r.s_sc is None and r.m_sc is None and r.s_verifier is None for r in table.rows.values())
