"""eval/metrics.py: week-3 spec tests 12 (AUC) and 13 (statistics)."""

from __future__ import annotations

import random
from fractions import Fraction

import pytest

from escalator.eval.metrics import (
    Bootstrap,
    auc,
    holm,
    mcnemar_exact,
    mcnemar_exact_fraction,
    quantile7,
    wilson,
)

# --- McNemar --------------------------------------------------------------------------------


def test_mcnemar_reference_value() -> None:
    assert mcnemar_exact_fraction(10, 2) == Fraction(158, 4096)
    assert mcnemar_exact(10, 2) == pytest.approx(0.038574, abs=1e-6)
    assert mcnemar_exact(2, 10) == mcnemar_exact(10, 2)


def test_mcnemar_no_discordant_pairs() -> None:
    assert mcnemar_exact(0, 0) == 1.0


def test_mcnemar_is_capped_at_1() -> None:
    assert mcnemar_exact(3, 3) == 1.0


# --- Wilson ---------------------------------------------------------------------------------


def test_wilson_reference_value() -> None:
    iv = wilson(100, 200)
    assert iv is not None
    assert (round(iv.lo, 4), round(iv.hi, 4)) == (0.4314, 0.5686)


def test_wilson_bounds_and_empty() -> None:
    zero, full = wilson(0, 10), wilson(10, 10)
    assert zero is not None and full is not None
    assert zero.lo == pytest.approx(0.0) and 0 < zero.hi < 1
    assert full.hi == pytest.approx(1.0) and 0 < full.lo < 1
    assert wilson(0, 0) is None


# --- Holm -----------------------------------------------------------------------------------


def test_holm_known_vector() -> None:
    # Sorted 0.01, 0.02, 0.03, 0.04 with m = 4: 0.04, 0.06, 0.06 (monotone), 0.06.
    assert holm([0.03, 0.01, 0.04, 0.02]) == pytest.approx([0.06, 0.04, 0.06, 0.06])
    assert holm([0.5, 0.9]) == pytest.approx([1.0, 1.0])
    assert holm([]) == []


# --- AUC (test 12) --------------------------------------------------------------------------


def brute_auc(scores: list[float], labels: list[bool]) -> float | None:
    pos = [s for s, y in zip(scores, labels, strict=True) if y]
    neg = [s for s, y in zip(scores, labels, strict=True) if not y]
    if not pos or not neg:
        return None
    total = sum((1.0 if p > q else 0.5 if p == q else 0.0) for p in pos for q in neg)
    return total / (len(pos) * len(neg))


@pytest.mark.parametrize("seed", range(20))
def test_auc_equals_brute_force_with_ties(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(2, 30)
    scores = [rng.choice([0.0, 1 / 3, 2 / 3, 1.0]) for _ in range(n)]
    labels = [rng.random() < 0.4 for _ in range(n)]
    expected = brute_auc(scores, labels)
    got = auc(scores, labels)
    assert (got is None) == (expected is None)
    if expected is not None:
        assert got == pytest.approx(expected, abs=1e-12)


def test_auc_empty_class_is_null() -> None:
    assert auc([0.1, 0.2], [True, True]) is None
    assert auc([0.1, 0.2], [False, False]) is None
    assert auc([1.0, 0.0], [True, False]) == 1.0
    assert auc([1.0, 1.0], [True, False]) == 0.5


# --- quantile and bootstrap (test 13) ---------------------------------------------------------


def test_quantile_matches_numpy_linear() -> None:
    # numpy.quantile([1, 2, 3, 4], [0.025, 0.5, 0.975]) == [1.075, 2.5, 3.925]
    assert [quantile7([4, 1, 3, 2], q) for q in (0.025, 0.5, 0.975)] == pytest.approx([1.075, 2.5, 3.925])
    assert quantile7([7.0], 0.3) == 7.0


def test_bootstrap_is_deterministic_and_shared() -> None:
    a, b = Bootstrap(50, 200, seed=11), Bootstrap(50, 200, seed=11)
    assert a.indices == b.indices
    assert Bootstrap(50, 200, seed=12).indices != a.indices
    assert len(a.indices) == 200 and all(len(r) == 50 and all(0 <= i < 50 for i in r) for r in a.indices)

    data = [i % 3 for i in range(50)]
    seen: list[tuple[int, ...]] = []

    def mean(rows: object) -> float:
        assert isinstance(rows, tuple)
        seen.append(rows)
        return sum(data[i] for i in rows) / len(rows)

    iv1 = a.interval(mean)
    iv2 = a.interval(lambda rows: sum(data[i] for i in rows) / len(rows))
    assert iv1 == iv2 and iv1.n_undefined == 0
    assert seen == list(a.indices)  # every statistic sees the one shared matrix, in order


def test_bootstrap_counts_undefined_resamples() -> None:
    bs = Bootstrap(5, 400, seed=3)
    labels = [True, False, False, False, False]

    def stat(rows: object) -> float | None:
        assert isinstance(rows, tuple)
        return auc([float(i) for i in rows], [labels[i] for i in rows])

    iv = bs.interval(stat)
    expected_undefined = sum(1 for r in bs.indices if all(labels[i] for i in r) or not any(labels[i] for i in r))
    assert iv.n_undefined == expected_undefined > 0


def test_bootstrap_interval_on_a_constant_is_degenerate() -> None:
    iv = Bootstrap(10, 50, seed=1).interval(lambda _rows: 0.25)
    assert (iv.lo, iv.hi, iv.n_undefined) == (0.25, 0.25, 0)
    assert Bootstrap(10, 50, seed=1).interval(lambda _rows: None).lo is None

