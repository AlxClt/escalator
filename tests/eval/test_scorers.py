"""Scorer mechanics: value equality, top-level ORDER BY detection, EX, soft-F1, score().

The 20 hand-made (predicted, gold) pairs live separately in tests/test_scorer.py.
"""

from __future__ import annotations

import pytest

from escalator.env.sandbox import Result, SqlError, SqlValue
from escalator.eval import scorers
from escalator.eval.scorers import execution_match, has_top_level_order_by, score, soft_f1, values_equal

Rows = list[list[SqlValue]]


def _result(rows: Rows, columns: list[str] | None = None, truncated: bool = False) -> Result:
    width = len(rows[0]) if rows else 0
    return Result(columns=columns or [f"c{i}" for i in range(width)], rows=rows, truncated=truncated)


# --- values_equal ------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "equal"),
    [
        (None, None, True),
        (None, 0, False),
        (None, "", False),
        (5, 5.0, True),
        (0.1 + 0.2, 0.3, True),
        (1.0, 1.0 + 1e-7, True),
        (1.0, 1.0 + 1e-5, False),
        (0.0, 1e-10, True),
        (0.0, 1e-8, False),
        (10**18, 10**18 + 1, True),  # relative tolerance at large magnitudes
        ("5", 5, False),
        ("a", "a", True),
        ("a", "A", False),
        ("a ", "a", False),
        (b"\x00", b"\x00", True),
        (b"a", "a", False),
    ],
)
def test_values_equal(a: SqlValue, b: SqlValue, equal: bool) -> None:
    assert values_equal(a, b) is equal
    assert values_equal(b, a) is equal


# --- has_top_level_order_by --------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "ordered"),
    [
        ("SELECT a FROM t ORDER BY a", True),
        ("select a from t order  by a desc limit 3", True),
        ("SELECT a FROM t ORDER\nBY a", True),
        ("SELECT a FROM t", False),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a LIMIT 5)", False),
        ("SELECT a, RANK() OVER (ORDER BY b) FROM t", False),
        ("SELECT a FROM t WHERE a IN (SELECT a FROM u ORDER BY a) ORDER BY a", True),
        ("SELECT 'order by' FROM t", False),
        ('SELECT "order by" FROM t', False),
        ("SELECT [order by] FROM t", False),
        ("SELECT `order by` FROM t", False),
        ("SELECT a FROM t -- ORDER BY a", False),
        ("SELECT a FROM t /* ORDER BY a */", False),
        ("SELECT 'it''s' FROM t ORDER BY a", True),
        ("SELECT a FROM t_order BY_x", False),
    ],
)
def test_has_top_level_order_by(sql: str, ordered: bool) -> None:
    assert has_top_level_order_by(sql) is ordered


# --- EX ----------------------------------------------------------------------


def test_ex_identical() -> None:
    rows: Rows = [[1, "a"], [2, "b"]]
    assert execution_match(rows, rows, ordered=False)
    assert execution_match(rows, rows, ordered=True)


def test_ex_column_permutation() -> None:
    gold: Rows = [[1, "a", 2.5], [2, "b", 3.5]]
    pred: Rows = [["a", 2.5, 1], ["b", 3.5, 2]]
    assert execution_match(pred, gold, ordered=False)
    assert execution_match(pred, gold, ordered=True)


def test_ex_column_permutation_must_keep_rows_together() -> None:
    # Each column matches as a multiset, but no permutation reproduces the gold rows.
    gold: Rows = [[1, "a"], [2, "b"]]
    pred: Rows = [[1, "b"], [2, "a"]]
    assert not execution_match(pred, gold, ordered=False)


def test_ex_same_values_in_two_columns() -> None:
    gold: Rows = [[1, 1, "x"], [2, 2, "y"]]
    pred: Rows = [["x", 1, 1], ["y", 2, 2]]
    assert execution_match(pred, gold, ordered=False)


def test_ex_extra_or_missing_column_fails() -> None:
    gold: Rows = [[1, "a"]]
    assert not execution_match([[1, "a", 0]], gold, ordered=False)
    assert not execution_match([[1]], gold, ordered=False)


def test_ex_row_order_ignored_when_unordered() -> None:
    gold: Rows = [[1, "a"], [2, "b"], [3, "c"]]
    pred: Rows = [[3, "c"], [1, "a"], [2, "b"]]
    assert execution_match(pred, gold, ordered=False)
    assert not execution_match(pred, gold, ordered=True)


def test_ex_multiset_duplicates_count() -> None:
    gold: Rows = [["a"], ["a"], ["b"]]
    assert not execution_match([["a"], ["b"]], gold, ordered=False)
    assert not execution_match([["a"], ["b"], ["b"]], gold, ordered=False)
    assert execution_match([["b"], ["a"], ["a"]], gold, ordered=False)


def test_ex_nulls() -> None:
    gold: Rows = [[None, 1], [2, None]]
    assert execution_match([[2, None], [None, 1]], gold, ordered=False)
    assert not execution_match([[0, 1], [2, None]], gold, ordered=False)
    assert not execution_match([["", 1], [2, None]], gold, ordered=False)


def test_ex_float_tolerance() -> None:
    gold: Rows = [[33.333333333333336]]
    assert execution_match([[100 / 3]], gold, ordered=False)
    assert execution_match([[33.3333334]], gold, ordered=False)
    assert not execution_match([[33.33]], gold, ordered=False)


def test_ex_int_float_mix() -> None:
    assert execution_match([[5.0], [2.0]], [[2], [5]], ordered=False)


def test_ex_empty_results() -> None:
    assert execution_match([], [], ordered=False)
    assert execution_match([], [], ordered=True)
    assert not execution_match([], [[1]], ordered=False)
    assert not execution_match([[1]], [], ordered=False)


def test_ex_different_row_counts() -> None:
    assert not execution_match([[1], [2]], [[1]], ordered=False)


def test_ex_mixed_types_sort_without_error() -> None:
    gold: Rows = [[None], [1], ["a"], [b"\x01"]]
    pred: Rows = [[b"\x01"], ["a"], [None], [1.0]]
    assert execution_match(pred, gold, ordered=False)


def test_ex_permutation_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # Five identical columns: every assignment is a candidate; a cap of 0 tries none of them.
    gold: Rows = [[1, 1, 1, 1, 1]]
    monkeypatch.setattr(scorers, "MAX_PERMUTATIONS", 0)
    assert not execution_match(gold, gold, ordered=False)
    monkeypatch.setattr(scorers, "MAX_PERMUTATIONS", 1)
    assert execution_match(gold, gold, ordered=False)


# --- soft-F1 -----------------------------------------------------------------


def test_soft_f1_identical_and_empty() -> None:
    rows: Rows = [[1, "a"], [2, "b"]]
    assert soft_f1(rows, rows, ordered=False) == 1.0
    assert soft_f1([], [], ordered=False) == 1.0


def test_soft_f1_disjoint_and_one_empty() -> None:
    assert soft_f1([[3, "c"]], [[1, "a"]], ordered=False) == 0.0
    assert soft_f1([], [[1, "a"]], ordered=False) == 0.0
    assert soft_f1([[1, "a"]], [], ordered=False) == 0.0


def test_soft_f1_partial_row() -> None:
    # One of two cells matches: tp = 0.5, fp = 0.5, fn = 0.5 -> F1 = 0.5.
    assert soft_f1([[1, "x"]], [[1, "a"]], ordered=False) == pytest.approx(0.5)


def test_soft_f1_missing_row() -> None:
    # tp = 1, fn = 1, fp = 0 -> P = 1, R = 0.5 -> F1 = 2/3.
    assert soft_f1([[1]], [[1], [2]], ordered=False) == pytest.approx(2 / 3)


def test_soft_f1_extra_row() -> None:
    assert soft_f1([[1], [2]], [[1]], ordered=False) == pytest.approx(2 / 3)


def test_soft_f1_ignores_column_order_and_duplicates() -> None:
    gold: Rows = [[1, "a"], [2, "b"]]
    assert soft_f1([["b", 2], ["a", 1], ["a", 1]], gold, ordered=False) == 1.0


def test_soft_f1_order_sensitivity_follows_flag() -> None:
    gold: Rows = [[1], [2]]
    assert soft_f1([[2], [1]], gold, ordered=False) == 1.0
    assert soft_f1([[2], [1]], gold, ordered=True) == 0.0


def test_soft_f1_float_tolerance() -> None:
    assert soft_f1([[100 / 3]], [[33.333333333333336]], ordered=False) == 1.0


# --- score -------------------------------------------------------------------


def test_score_success() -> None:
    gold = _result([[1, "a"], [2, "b"]])
    out = score(_result([["b", 2], ["a", 1]]), gold, ordered=False)
    assert out == scorers.Score(ex=True, soft_f1=1.0, error=None)


def test_score_sql_error() -> None:
    out = score(SqlError("syntax", 'near "SELEC": syntax error'), _result([[1]]), ordered=False)
    assert out == scorers.Score(ex=False, soft_f1=0.0, error="syntax")


def test_score_ignores_column_names() -> None:
    out = score(_result([[1]], columns=["x"]), _result([[1]], columns=["count(*)"]), ordered=False)
    assert out.ex


def test_score_rejects_truncated() -> None:
    with pytest.raises(ValueError, match="predicted"):
        score(_result([[1]], truncated=True), _result([[1]]), ordered=False)
    with pytest.raises(ValueError, match="gold"):
        score(_result([[1]]), _result([[1]], truncated=True), ordered=False)
