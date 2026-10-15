"""The three escalation signals (week-3 spec D3, D4): pure functions of small-tier runs plus label-free
execution of their submitted SQL. Larger s means "more likely to need escalation".

No label access: this module reads no gold SQL, no gold result and no frontier-tier trace. It imports
nothing from eval.tasks (the gold side) and receives only SmallRunSets; a test runs it with the gold
loader and the frontier reader patched to raise. eval.scorers.execution_match is a pure comparison of
two row lists, used here with row order always ignored.

- ans(r): the result rows of pred(r) when end(r) = submitted and its execution succeeds, else ⊥ (None).
- eq(u, v): both non-⊥ and equal under the scorer's normalisation (NULLs, float tolerance, column
  permutation), row order ignored. ⊥ equals nothing, itself included.
- error: 1 iff end ∈ FAILED_ENDS, or submitted and pred fails to execute (error or timeout).
- self_consistency: m = max_j Σ_j' eq(ans_j, ans_j'), s = 1 - m/k (k samples at T = 0.7).
- verifier: 0 iff eq(ans(baseline), ans(verifier)), else 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from escalator.agent.tools import SUBMIT_ANSWER
from escalator.env.sandbox import SqlError, SqlValue
from escalator.eval.scorers import execution_match
from escalator.router.execcache import Executor
from escalator.router.inventory import Run, SmallRunSets
from escalator.trace.schema import FAILED_ENDS

Rows = list[list[SqlValue]]
Answer = Rows | None  # None = ⊥
SignalName = Literal["error", "self_consistency", "verifier"]
SIGNALS: tuple[SignalName, ...] = ("error", "self_consistency", "verifier")


@dataclass(frozen=True)
class Submit:
    db_id: str
    sql: str


def submit_of(run: Run) -> Submit | None:
    """pred(r): the SQL of the executed submit_answer call, defined iff end(r) = submitted. The loop
    executes the first valid submit; calls after it are `ignored`, invalid ones `validation_error`."""
    if run.end != "submitted":
        return None
    for rec in run.records:
        for name, args, status in zip(rec.tool, rec.args, rec.tool_status, strict=True):
            if name == SUBMIT_ANSWER and status not in ("validation_error", "ignored") and args is not None:
                db_id, sql = args.get("db_id"), args.get("sql")
                if isinstance(db_id, str) and isinstance(sql, str):
                    return Submit(db_id, sql)
    raise ValueError(f"{run.run_id} task {run.task_id} sample {run.sample_idx}: submitted without an executed submit")


def ans(run: Run, ex: Executor) -> Answer:
    sub = submit_of(run)
    if sub is None:
        return None
    out = ex.execute(sub.db_id, sub.sql)
    return None if isinstance(out, SqlError) else out.rows


def eq(u: Answer, v: Answer) -> bool:
    if u is None or v is None:
        return False
    return execution_match(u, v, ordered=False)


def error_signal(run: Run, ex: Executor) -> int:
    """Errors the model hit and recovered from during the trajectory do not count."""
    if run.end in FAILED_ENDS:
        return 1
    if run.end != "submitted":
        raise ValueError(f"{run.run_id} task {run.task_id}: end {run.end} has no error-signal value")
    return 1 if ans(run, ex) is None else 0


def sc_score(answers: Sequence[Answer]) -> tuple[Fraction, int]:
    """(s, m) over k sample answers. The inner sum includes j' = j, so m >= 1 as soon as one answer is
    not ⊥, and m = 0 when all are ⊥. eq is a pairwise predicate (float tolerance is not transitive);
    m does not depend on the order of the samples."""
    k = len(answers)
    if k == 0:
        raise ValueError("self-consistency needs k >= 1 samples")
    m = max(sum(1 for b in answers if eq(a, b)) for a in answers)
    return 1 - Fraction(m, k), m


def verifier_score(base: Answer, verifier: Answer) -> int:
    return 0 if eq(base, verifier) else 1


@dataclass(frozen=True)
class SignalRow:
    task_id: str
    s_error: int
    s_sc: Fraction | None  # None: not collected
    m_sc: int | None
    s_verifier: int | None


@dataclass(frozen=True)
class SignalTable:
    model: str
    rows: dict[str, SignalRow]  # task_id -> row, for every task asked
    status: dict[SignalName, str]  # "ok" | "not_collected"


def compute_signals(small: SmallRunSets, task_ids: Sequence[str], ex: Executor) -> SignalTable:
    """One score per task and signal, from small-tier runs only. A signal whose run is not in the
    inventory is reported as not_collected, with null values, never as zeros."""
    status: dict[SignalName, str] = {
        "error": "ok",
        "self_consistency": "ok" if small.self_consistency is not None else "not_collected",
        "verifier": "ok" if small.verifier is not None else "not_collected",
    }
    rows: dict[str, SignalRow] = {}
    for t in task_ids:
        base = small.baseline.single(t)
        s_sc: Fraction | None = None
        m_sc: int | None = None
        if small.self_consistency is not None:
            s_sc, m_sc = sc_score([ans(r, ex) for r in small.self_consistency.runs[t]])
        s_ver: int | None = None
        if small.verifier is not None:
            s_ver = verifier_score(ans(base, ex), ans(small.verifier.single(t), ex))
        rows[t] = SignalRow(t, error_signal(base, ex), s_sc, m_sc, s_ver)
    return SignalTable(small.model, rows, status)
