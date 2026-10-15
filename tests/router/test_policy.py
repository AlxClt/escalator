"""router/policy.py: week-3 spec tests 9 (threshold sweep), 10 (oracle) and 11 (budget variant)."""

from __future__ import annotations

import itertools
from decimal import Decimal
from fractions import Fraction

import pytest

from escalator.router.policy import (
    PairData,
    acc_at_budget,
    cps_ratio,
    curve_points,
    discordant,
    frontier_only,
    headroom_captured,
    is_nondegenerate,
    oracle_acc_at,
    oracle_ceiling,
    oracle_gap,
    oracle_vertices,
    random_reference,
    small_only,
    threshold,
    threshold_grid,
)

D = Decimal
F = Fraction

# 10-task hand fixture (columns: a_S, a_L, c_L, s); c_S = 0.05 and c_sigma = 0.01 on every task.
ROWS = [
    (1, 1, "0.10", F(0)),
    (0, 1, "0.20", F(1)),
    (0, 1, "0.30", F(2, 3)),
    (1, 0, "0.10", F(2, 3)),
    (0, 0, "0.20", F(1, 3)),
    (1, 1, "0.40", F(0)),
    (0, 1, "0.10", F(1, 3)),
    (1, 1, "0.20", F(1, 3)),
    (0, 0, "0.30", F(1)),
    (1, 1, "0.10", F(0)),
]
DATA = PairData(tuple(str(i + 1) for i in range(10)), tuple(r[0] for r in ROWS), tuple(r[1] for r in ROWS),
                tuple(D("0.05") for _ in ROWS), tuple(D(r[2]) for r in ROWS), tuple("simple" for _ in ROWS))
S = [r[3] for r in ROWS]
C_SIGMA = [D("0.01")] * 10
ALL = range(10)


# --- test 9: threshold sweep --------------------------------------------------------------------

# tau -> (solved, cost total, escalated, helpful, wasted, harmful)
EXPECTED = {
    None: (7, D("2.60"), 10, 3, 6, 1),
    F(0): (7, D("2.00"), 7, 3, 3, 1),
    F(1, 3): (6, D("1.50"), 4, 2, 1, 1),
    F(2, 3): (6, D("1.10"), 2, 1, 1, 0),
    F(1): (5, D("0.60"), 0, 0, 0, 0),
}


def test_grid_is_minus_inf_then_observed_values() -> None:
    assert threshold_grid(S) == [None, F(0), F(1, 3), F(2, 3), F(1)]


@pytest.mark.parametrize("tau", list(EXPECTED))
def test_threshold_point(tau: Fraction | None) -> None:
    o = threshold(DATA, ALL, S, C_SIGMA, tau)
    solved, total, esc, helpful, wasted, harmful = EXPECTED[tau]
    assert (o.solved, o.cost_total, o.escalated, o.helpful, o.wasted, o.harmful) == (
        solved, total, esc, helpful, wasted, harmful)
    assert o.acc == F(solved, 10) and o.cost == total / 10 and o.cps == total / solved
    assert o.escalation_rate == F(esc, 10)


def test_escalate_all_has_frontier_accuracy_at_a_higher_cost() -> None:
    all_ = threshold(DATA, ALL, S, C_SIGMA, None)
    fo = frontier_only(DATA, ALL)
    assert all_.acc == fo.acc == F(7, 10)
    assert fo.cost_total == D("2.00") < all_.cost_total


def test_escalate_none_pays_small_and_signal() -> None:
    none = threshold(DATA, ALL, S, C_SIGMA, F(1))
    assert none.acc == small_only(DATA, ALL).acc == F(1, 2)
    assert none.cost_total == small_only(DATA, ALL).cost_total + D("0.10")


def test_nondegenerate_points() -> None:
    assert [t for t in threshold_grid(S) if is_nondegenerate(DATA, S, t)] == [F(0), F(1, 3), F(2, 3)]


def test_cps_undefined_without_solves_and_ratio() -> None:
    zero = PairData(("1",), (0,), (0,), (D(0),), (D("1"),), ("simple",))
    assert small_only(zero, [0]).cps is None
    pi = threshold(DATA, ALL, S, C_SIGMA, F(1, 3))
    assert cps_ratio(frontier_only(DATA, ALL), pi) == (D("2.00") / 7) / (D("1.50") / 6)
    free = PairData(("1",), (1,), (1,), (D(0),), (D("1"),), ("simple",))
    assert cps_ratio(frontier_only(free, [0]), small_only(free, [0])) == "inf"


def test_discordant_counts() -> None:
    assert discordant(DATA, ALL) == (1, 3)


# --- test 10: oracle ----------------------------------------------------------------------------


def test_oracle_cheapest_first_with_task_id_tie_break() -> None:
    v = oracle_vertices(DATA, ALL)
    assert [(x.k, x.solved, x.cost_total) for x in v] == [
        (0, 5, D("0.50")), (1, 6, D("0.60")), (2, 7, D("0.80")), (3, 8, D("1.10"))]
    tied = PairData(("5", "3", "9"), (0, 0, 0), (1, 1, 1), (D(0),) * 3, (D("1"), D("1"), D("0.5")), ("s",) * 3)
    order = [x.cost_total for x in oracle_vertices(tied, range(3))]
    assert order == [D(0), D("0.5"), D("1.5"), D("2.5")]
    # equal c_L: task 3 before task 5 (rows 1 then 0)
    p = sorted((i for i in range(3) if tied.delta(i) == 1), key=lambda i: (tied.c_L[i], int(tied.task_ids[i])))
    assert [tied.task_ids[i] for i in p] == ["9", "3", "5"]


def test_oracle_step_function_and_ceiling() -> None:
    v = oracle_vertices(DATA, ALL)
    assert oracle_acc_at(v, D("0.49")) is None
    assert [oracle_acc_at(v, D(c)) for c in ("0.50", "0.59", "0.60", "0.79", "0.80", "1.09", "1.10", "9")] == [
        F(1, 2), F(1, 2), F(3, 5), F(3, 5), F(7, 10), F(7, 10), F(4, 5), F(4, 5)]
    assert oracle_ceiling(DATA, ALL) == F(8, 10) == v[-1].acc


def test_matched_cost_gap_and_headroom() -> None:
    v = oracle_vertices(DATA, ALL)
    small = small_only(DATA, ALL)
    gaps = {tau: oracle_gap(v, threshold(DATA, ALL, S, C_SIGMA, tau)) for tau in EXPECTED}
    assert gaps == {None: F(1, 10), F(0): F(1, 10), F(1, 3): F(1, 5), F(2, 3): F(1, 5), F(1): F(1, 10)}
    assert headroom_captured(v, threshold(DATA, ALL, S, C_SIGMA, F(1, 3)), small) == F(1, 3)
    flat = PairData(("1",), (1,), (1,), (D(0),), (D(1),), ("s",))
    fv = oracle_vertices(flat, [0])
    assert headroom_captured(fv, small_only(flat, [0]), small_only(flat, [0])) is None


def test_oracle_on_a_resample_counts_repeats() -> None:
    v = oracle_vertices(DATA, [6, 6, 0])  # task 7 twice: two oracle escalations
    assert [(x.solved, x.cost_total) for x in v] == [(1, D("0.15")), (2, D("0.25")), (3, D("0.35"))]


def test_random_reference() -> None:
    r0, r10, r5 = (random_reference(DATA, ALL, e) for e in (0, 10, 5))
    assert r0.acc == F(1, 2) and r0.cost_total == F(D("0.50"))
    assert r10.acc == F(7, 10) and r10.cost_total == F(D("2.50"))
    assert r5.acc == F(3, 5) and r5.cost == F(D("1.50")) / 10


# --- test 11: budget variant ----------------------------------------------------------------------


def _expected_over_tie_breaks(data: PairData, s: list[Fraction], c_sigma: list[Decimal], j: int,
                              ) -> tuple[Fraction, Fraction]:
    """Escalate the first j tasks in decreasing s, every tie order equally likely: (E[cost total], E[solved])."""
    n = data.n
    blocks = [[i for i in range(n) if s[i] == v] for v in sorted(set(s), reverse=True)]
    costs, solved = [], []
    for perm in itertools.product(*(itertools.permutations(b) for b in blocks)):
        order = [i for block in perm for i in block]
        esc = set(order[:j])
        costs.append(sum((data.c_S[i] + c_sigma[i] + (data.c_L[i] if i in esc else 0) for i in range(n)), Decimal(0)))
        solved.append(sum(data.a_L[i] if i in esc else data.a_S[i] for i in range(n)))
    k = len(costs)
    return sum((Fraction(c) for c in costs), Fraction(0)) / k, Fraction(sum(solved), k)


SMALL = PairData(tuple(str(i) for i in range(1, 9)), (1, 0, 0, 1, 0, 1, 0, 0), (1, 1, 0, 0, 1, 1, 1, 0),
                 tuple(D("0.02") for _ in range(8)),
                 (D("0.30"), D("0.10"), D("0.20"), D("0.40"), D("0.10"), D("0.30"), D("0.25"), D("0.15")),
                 ("s",) * 8)
SMALL_S = [F(1), F(2, 3), F(2, 3), F(2, 3), F(1, 3), F(1, 3), F(0), F(2, 3)]


@pytest.mark.parametrize("j", range(9))
def test_budget_equals_expectation_over_tie_breaks(j: int) -> None:
    c_sigma = [D("0.01")] * 8
    exp_cost, exp_solved = _expected_over_tie_breaks(SMALL, SMALL_S, c_sigma, j)
    points = curve_points(SMALL, range(8), SMALL_S, c_sigma)
    assert acc_at_budget(points, exp_cost / 8) == exp_solved / 8


def test_budget_bounds() -> None:
    c_sigma = [D("0.01")] * 8
    points = curve_points(SMALL, range(8), SMALL_S, c_sigma)
    assert acc_at_budget(points, D("0.029")) is None  # below escalate-none (0.03 per task)
    assert acc_at_budget(points, D("100")) == F(sum(SMALL.a_L), 8)
