"""Routing policies simulated over recorded outcomes (week-3 spec D5-D7). Pure, no I/O.

Everything works on per-task arrays over the analysis set A (sorted by task id) and on a sequence of
row positions into them: range(n) for the point estimate, a bootstrap row for a resample (positions
may repeat; each occurrence counts as a task).

Costs are exact Decimal sums; comparisons between policies use totals over the same rows, so matching
"at equal USD per task" never depends on a division.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

Tau = Fraction | None  # None = -inf (escalate all)


@dataclass(frozen=True)
class PairData:
    """One (small model, frontier) pair over A."""

    task_ids: tuple[str, ...]
    a_S: tuple[int, ...]
    a_L: tuple[int, ...]
    c_S: tuple[Decimal, ...]
    c_L: tuple[Decimal, ...]
    strata: tuple[str, ...]

    def __post_init__(self) -> None:
        n = len(self.task_ids)
        if not all(len(x) == n for x in (self.a_S, self.a_L, self.c_S, self.c_L, self.strata)):
            raise ValueError("pair arrays differ in length")

    @property
    def n(self) -> int:
        return len(self.task_ids)

    def delta(self, i: int) -> int:
        return self.a_L[i] - self.a_S[i]


@dataclass(frozen=True)
class Outcome:
    """A policy's per-task outcomes and costs summed over a sequence of rows."""

    n: int
    solved: int
    cost_total: Decimal
    escalated: int
    helpful: int  # escalated with delta = +1
    wasted: int  # escalated with delta = 0
    harmful: int  # escalated with delta = -1

    @property
    def acc(self) -> Fraction:
        return Fraction(self.solved, self.n)

    @property
    def cost(self) -> Decimal:
        """USD per task."""
        return self.cost_total / self.n

    @property
    def cps(self) -> Decimal | None:
        """USD per solved task: a ratio of sums; undefined when nothing is solved."""
        return self.cost_total / self.solved if self.solved else None

    @property
    def escalation_rate(self) -> Fraction:
        return Fraction(self.escalated, self.n)


def _outcome(data: PairData, rows: Sequence[int], escalate: Sequence[bool], extra: Sequence[Decimal] | None,
             *, pay_small: bool = True) -> Outcome:
    solved, total, esc, helpful, wasted, harmful = 0, Decimal(0), 0, 0, 0, 0
    for i in rows:
        e = escalate[i]
        solved += data.a_L[i] if e else data.a_S[i]
        if pay_small:
            total += data.c_S[i]
        if extra is not None:
            total += extra[i]
        if e:
            total += data.c_L[i]
            esc += 1
            d = data.delta(i)
            helpful += d == 1
            wasted += d == 0
            harmful += d == -1
    return Outcome(len(rows), solved, total, esc, helpful, wasted, harmful)


def small_only(data: PairData, rows: Sequence[int]) -> Outcome:
    return _outcome(data, rows, [False] * data.n, None)


def frontier_only(data: PairData, rows: Sequence[int]) -> Outcome:
    """Every task goes to the frontier, no small attempt: outcome a_L, cost c_L."""
    return _outcome(data, rows, [True] * data.n, None, pay_small=False)


def escalated(s: Sequence[Fraction], tau: Tau) -> list[bool]:
    return [True] * len(s) if tau is None else [x > tau for x in s]


def threshold(data: PairData, rows: Sequence[int], s: Sequence[Fraction], c_sigma: Sequence[Decimal],
              tau: Tau) -> Outcome:
    """pi(sigma, tau): E = {s > tau}; per task c_S + c_sigma + 1[E] c_L (small attempt and signal always paid)."""
    return _outcome(data, rows, escalated(s, tau), c_sigma)


def threshold_grid(s: Sequence[Fraction]) -> list[Tau]:
    """{-inf} ∪ distinct observed values, increasing; tau = max s escalates none."""
    return [None, *sorted(set(s))]


def is_nondegenerate(data: PairData, s: Sequence[Fraction], tau: Tau) -> bool:
    k = sum(escalated(s, tau))
    return 0 < k < data.n


# --- oracle ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleVertex:
    k: int
    solved: int
    cost_total: Decimal
    n: int

    @property
    def acc(self) -> Fraction:
        return Fraction(self.solved, self.n)

    @property
    def cost(self) -> Decimal:
        return self.cost_total / self.n

    @property
    def cps(self) -> Decimal | None:
        return self.cost_total / self.solved if self.solved else None


def oracle_vertices(data: PairData, rows: Sequence[int]) -> list[OracleVertex]:
    """Escalate the tasks of P = {delta = +1} cheapest first, by (c_L, task_id); no signal is paid.
    Unit value per task, so cheapest-first is optimal at every budget. Vertex k: the first k of P."""
    base_solved = sum(data.a_S[i] for i in rows)
    base_cost = sum((data.c_S[i] for i in rows), Decimal(0))
    p = sorted((i for i in rows if data.delta(i) == 1), key=lambda i: (data.c_L[i], int(data.task_ids[i])))
    out = [OracleVertex(0, base_solved, base_cost, len(rows))]
    for k, i in enumerate(p, 1):
        prev = out[-1]
        out.append(OracleVertex(k, prev.solved + 1, prev.cost_total + data.c_L[i], len(rows)))
    return out


def oracle_acc_at(vertices: Sequence[OracleVertex], cost_total: Decimal) -> Fraction | None:
    """Acc_O*(C) = max{Acc_O(k) : Cost_O(k) <= C}, with C given as a total over the same rows; None when
    even k = 0 costs more."""
    best: Fraction | None = None
    for v in vertices:
        if v.cost_total <= cost_total and (best is None or v.acc > best):
            best = v.acc
    return best


def oracle_ceiling(data: PairData, rows: Sequence[int]) -> Fraction:
    return Fraction(sum(max(data.a_S[i], data.a_L[i]) for i in rows), len(rows))


# --- policy-level comparisons (D7) -----------------------------------------------------------


def oracle_gap(vertices: Sequence[OracleVertex], pi: Outcome) -> Fraction | None:
    """Acc_O*(Cost(pi)) - Acc(pi), matched on USD per task."""
    best = oracle_acc_at(vertices, pi.cost_total)
    return None if best is None else best - pi.acc


def headroom_captured(vertices: Sequence[OracleVertex], pi: Outcome, small: Outcome) -> Fraction | None:
    """(Acc(pi) - Acc_S) / (Acc_O*(Cost(pi)) - Acc_S); undefined when the denominator is 0."""
    best = oracle_acc_at(vertices, pi.cost_total)
    if best is None or best == small.acc:
        return None
    return (pi.acc - small.acc) / (best - small.acc)


def cps_ratio(frontier: Outcome, pi: Outcome) -> Decimal | str | None:
    """CPS_L / CPS(pi) (the brief's Y). "inf" when CPS(pi) = 0; None when either CPS is undefined."""
    cl, cp = frontier.cps, pi.cps
    if cl is None or cp is None:
        return None
    if cp == 0:
        return "inf"
    return cl / cp


# --- budget variant and random reference -------------------------------------------------------


@dataclass(frozen=True)
class CurvePoint:
    tau: Tau
    cost_total: Decimal
    solved: int
    n: int


def curve_points(data: PairData, rows: Sequence[int], s: Sequence[Fraction], c_sigma: Sequence[Decimal]) -> list[CurvePoint]:
    out = []
    for tau in threshold_grid([s[i] for i in rows]):
        o = threshold(data, rows, s, c_sigma, tau)
        out.append(CurvePoint(tau, o.cost_total, o.solved, o.n))
    return out


def acc_at_budget(points: Sequence[CurvePoint], budget_per_task: Decimal | Fraction) -> Fraction | None:
    """Escalating in decreasing s with ties at the boundary broken uniformly at random, in expectation:
    the linear interpolation between consecutive threshold points in the (USD per task, Acc) plane.
    None below the escalate-none cost; the escalate-all accuracy above its cost."""
    if not points:
        return None
    n = points[0].n
    pts = sorted(points, key=lambda p: (p.cost_total, p.solved))
    budget = Fraction(budget_per_task) * n
    if budget < Fraction(pts[0].cost_total):
        return None
    for lo, hi in zip(pts, pts[1:], strict=False):
        c_lo, c_hi = Fraction(lo.cost_total), Fraction(hi.cost_total)
        if c_lo <= budget <= c_hi:
            if c_hi == c_lo:
                return Fraction(max(lo.solved, hi.solved), n)
            w = (budget - c_lo) / (c_hi - c_lo)
            return (lo.solved + w * (hi.solved - lo.solved)) / n
    return Fraction(pts[-1].solved, n)


@dataclass(frozen=True)
class RandomPoint:
    e: int
    acc: Fraction
    cost_total: Fraction  # expected total, exact
    n: int

    @property
    def cost(self) -> Fraction:
        return self.cost_total / self.n


def random_reference(data: PairData, rows: Sequence[int], e: int) -> RandomPoint:
    """Escalate e tasks chosen uniformly at random, no signal paid:
    E[Acc] = Acc_S + (e/n)(1/n) Σ delta;  E[Cost] = (1/n)(Σ c_S + (e/n) Σ c_L)."""
    n = len(rows)
    acc_s = Fraction(sum(data.a_S[i] for i in rows), n)
    dsum = sum(data.delta(i) for i in rows)
    cs = Fraction(sum((data.c_S[i] for i in rows), Decimal(0)))
    cl = Fraction(sum((data.c_L[i] for i in rows), Decimal(0)))
    return RandomPoint(e, acc_s + Fraction(e, n) * Fraction(dsum, n), cs + Fraction(e, n) * cl, n)


# --- pair-level -------------------------------------------------------------------------------


def discordant(data: PairData, rows: Sequence[int]) -> tuple[int, int]:
    """(b, c): b = #{a_S = 1, a_L = 0} (delta = -1), c = #{a_S = 0, a_L = 1} (delta = +1)."""
    b = sum(1 for i in rows if data.delta(i) == -1)
    c = sum(1 for i in rows if data.delta(i) == 1)
    return b, c


def mcnemar_counts(a_pi: Sequence[int], a_L: Sequence[int], rows: Sequence[int]) -> tuple[int, int]:
    """(b_pi, c_pi) between a policy's outcomes and frontier-only."""
    b = sum(1 for i in rows if a_pi[i] == 1 and a_L[i] == 0)
    c = sum(1 for i in rows if a_pi[i] == 0 and a_L[i] == 1)
    return b, c


def policy_outcomes(data: PairData, escalate: Sequence[bool]) -> list[int]:
    return [data.a_L[i] if escalate[i] else data.a_S[i] for i in range(data.n)]
