"""Statistics for the routing simulation (week-3 spec, "Statistical protocol"). Pure, stdlib only.

- Wilson score interval for a single proportion (z = 1.959964).
- Exact two-sided McNemar test on discordant pairs.
- Holm-Bonferroni adjustment.
- Mann-Whitney AUC, ties counting 1/2.
- Percentile bootstrap: one B x n index matrix per invocation, shared by every statistic so that intervals
  are jointly consistent; quantiles by numpy's default rule (linear, "type 7"), numpy not being locked.
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction

Z_95 = 1.959964


@dataclass(frozen=True)
class Interval:
    lo: float
    hi: float


def wilson(k: int, n: int, z: float = Z_95) -> Interval | None:
    """95% Wilson score interval for k successes out of n; None for n = 0."""
    if n == 0:
        return None
    if not 0 <= k <= n:
        raise ValueError(f"k={k} out of range for n={n}")
    p = k / n
    centre = p + z * z / (2 * n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return Interval(max(0.0, (centre - half) / denominator), min(1.0, (centre + half) / denominator))


def mcnemar_exact_fraction(b: int, c: int) -> Fraction:
    """p = min(1, 2 * sum_{t=0}^{min(b,c)} C(b+c, t) / 2^(b+c)); p = 1 when b + c = 0."""
    if b < 0 or c < 0:
        raise ValueError("discordant counts are non-negative")
    n = b + c
    if n == 0:
        return Fraction(1)
    tail = sum(math.comb(n, t) for t in range(min(b, c) + 1))
    return min(Fraction(1), Fraction(2 * tail, 2**n))


def mcnemar_exact(b: int, c: int) -> float:
    return float(mcnemar_exact_fraction(b, c))


def holm(ps: Sequence[float]) -> list[float]:
    """Holm-adjusted p-values, in the input order: p_(i)^adj = max_{t <= i} min(1, (m - t + 1) p_(t))."""
    m = len(ps)
    order = sorted(range(m), key=lambda i: (ps[i], i))
    out = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):  # rank = t - 1
        running = max(running, min(1.0, (m - rank) * ps[i]))
        out[i] = running
    return out


def auc(scores: Sequence[float], positive: Sequence[bool]) -> float | None:
    """P(s_pos > s_neg) + 1/2 P(s_pos = s_neg) over all (positive, negative) pairs; None if a class is
    empty. O(n log n): each positive counts the negatives below it and tied with it."""
    if len(scores) != len(positive):
        raise ValueError("scores and labels differ in length")
    neg = sorted(s for s, y in zip(scores, positive, strict=True) if not y)
    pos = [s for s, y in zip(scores, positive, strict=True) if y]
    if not pos or not neg:
        return None
    total = Fraction(0)
    for s in pos:
        below = bisect.bisect_left(neg, s)
        tied = bisect.bisect_right(neg, s) - below
        total += below + Fraction(tied, 2)
    return float(total / (len(pos) * len(neg)))


def quantile7(values: Sequence[float], q: float) -> float:
    """numpy.quantile(values, q) with its default method ("linear", Hyndman-Fan type 7)."""
    if not values:
        raise ValueError("quantile of an empty sample")
    xs = sorted(values)
    h = (len(xs) - 1) * q
    lo = math.floor(h)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (h - lo) * (xs[hi] - xs[lo])


@dataclass(frozen=True)
class BootstrapInterval:
    lo: float | None  # None when every resample was undefined
    hi: float | None
    n_undefined: int  # resamples where the statistic was undefined, dropped for this statistic


Statistic = Callable[[Sequence[int]], float | None]  # resampled positions in 0..n-1 -> value or undefined


class Bootstrap:
    """One B x n matrix of positions drawn with replacement from range(n) (the analysis set sorted by
    task id), from random.Random(seed). Every statistic of an invocation is evaluated on the same rows;
    the policy (thresholds) is fixed, only the data is resampled."""

    def __init__(self, n: int, B: int, seed: int) -> None:
        if n <= 0 or B <= 0:
            raise ValueError("bootstrap needs n > 0 and B > 0")
        rng = random.Random(seed)
        self.n, self.B, self.seed = n, B, seed
        self.indices: tuple[tuple[int, ...], ...] = tuple(tuple(rng.randrange(n) for _ in range(n)) for _ in range(B))

    def values(self, stat: Statistic) -> list[float | None]:
        return [stat(row) for row in self.indices]

    def interval(self, stat: Statistic, *, alpha: float = 0.05) -> BootstrapInterval:
        return percentile_interval(self.values(stat), alpha=alpha)


def percentile_interval(values: Sequence[float | None], *, alpha: float = 0.05) -> BootstrapInterval:
    """2.5% and 97.5% (alpha = 0.05) of the defined values; undefined ones are dropped and counted."""
    kept = [v for v in values if v is not None and not math.isnan(v)]
    undefined = len(values) - len(kept)
    if not kept:
        return BootstrapInterval(None, None, undefined)
    return BootstrapInterval(quantile7(kept, alpha / 2), quantile7(kept, 1 - alpha / 2), undefined)
