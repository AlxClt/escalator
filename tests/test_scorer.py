"""The 20 hand-made (predicted, gold) scorer pairs: the Step 1 exit gate before any sweep.

TODO(manual): the pairs are written by hand, 4 per category, in PAIRS below. They are fixtures:
never edit a pair to make a test pass; a failing pair means the scorer is wrong (or the pair's
expectation is, which is a deliberate, reviewed change).

Rules the pairs check (escalator.eval.scorers):
- columns are permutation-invariant, names ignored, the column count must match;
- rows are a multiset, unless the gold SQL has a top-level ORDER BY (`ordered=True`);
- numbers compare with REL_TOL / ABS_TOL (`5 == 5.0`), NULL equals NULL, no text/number coercion;
- two empty results match, one empty result never matches a non-empty one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest

from escalator.env.sandbox import Result, SqlValue
from escalator.eval.scorers import score

Category = Literal["column_reordering", "row_reordering", "null_handling", "float_tolerance", "empty_results"]
CATEGORIES: tuple[Category, ...] = (
    "column_reordering", "row_reordering", "null_handling", "float_tolerance", "empty_results",
)
PAIRS_PER_CATEGORY = 4


@dataclass(frozen=True)
class Pair:
    id: str
    category: Category
    pred_columns: list[str]
    pred_rows: list[list[SqlValue]]
    gold_columns: list[str]
    gold_rows: list[list[SqlValue]]
    ordered: bool  # True when the gold SQL has a top-level ORDER BY
    expect_ex: bool
    expect_soft_f1: float | None = None  # None: soft-F1 not asserted for this pair


PAIRS: list[Pair] = [
    # --- column_reordering (4) ---
    # Pair(id="col-1", category="column_reordering",
    #      pred_columns=[...], pred_rows=[[...]],
    #      gold_columns=[...], gold_rows=[[...]],
    #      ordered=False, expect_ex=..., expect_soft_f1=None),
    # --- row_reordering (4) ---
    # --- null_handling (4) ---
    # --- float_tolerance (4) ---
    # --- empty_results (4) ---
]


@pytest.mark.parametrize("pair", PAIRS, ids=lambda p: p.id)
def test_hand_made_pair(pair: Pair) -> None:
    pred = Result(columns=pair.pred_columns, rows=pair.pred_rows, truncated=False)
    gold = Result(columns=pair.gold_columns, rows=pair.gold_rows, truncated=False)
    out = score(pred, gold, ordered=pair.ordered)
    assert out.ex is pair.expect_ex
    if pair.expect_soft_f1 is not None:
        assert out.soft_f1 == pytest.approx(pair.expect_soft_f1)


def test_twenty_pairs_cover_every_category() -> None:
    if not PAIRS:
        pytest.skip("hand-made scorer pairs not written yet (Step 1 exit criterion)")
    assert len(PAIRS) == PAIRS_PER_CATEGORY * len(CATEGORIES)
    assert len({p.id for p in PAIRS}) == len(PAIRS), "pair ids must be unique"
    for category in CATEGORIES:
        assert sum(p.category == category for p in PAIRS) == PAIRS_PER_CATEGORY, category
