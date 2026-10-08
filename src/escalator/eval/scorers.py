"""Scoring a predicted result set against gold: execution accuracy (EX) and soft-F1. Pure, no I/O.

EX rules:
- columns are permutation-invariant: names are ignored, the column count must match;
- rows are a multiset (order ignored, duplicate counts matter), unless the task asks for an output
  order (the hand-checked list in configs/scoring.yaml), in which case rows are compared in order,
  except inside tie blocks: runs of gold rows tied on the sort key, whose order is free;
- numbers compare with a tolerance (`5 == 5.0`), NULL equals NULL, no text/number coercion;
- two empty results match; one empty result never matches a non-empty one.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from escalator.env.sandbox import ErrorKind, Result, SqlError, SqlValue

REL_TOL = 1e-6
ABS_TOL = 1e-9
MAX_PERMUTATIONS = 10_000  # column assignments tried before EX gives up (False)

Row = Sequence[SqlValue]
Rows = Sequence[Row]
SortKey = tuple[int, int | float | str | bytes]
TieBlocks = Sequence[Sequence[int]]  # 0-based gold row positions, consecutive, whose order is free
T = TypeVar("T")


def values_equal(a: SqlValue, b: SqlValue) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return type(a) is type(b) and a == b


def _key(v: SqlValue) -> SortKey:
    """Total order over SQLite values: NULL < numbers < text < blob."""
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    return (3, v)


def _row_key(row: Row) -> tuple[SortKey, ...]:
    return tuple(_key(v) for v in row)


def _seq_equal(a: Sequence[SqlValue], b: Sequence[SqlValue]) -> bool:
    return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b, strict=True))


# --- row order -------------------------------------------------------------


def check_tie_blocks(blocks: TieBlocks) -> None:
    """Each block holds two or more consecutive ascending positions >= 0; blocks never overlap."""
    last = -1
    for block in sorted(blocks, key=lambda b: b[0] if b else -1):
        b = list(block)
        if len(b) < 2 or b[0] <= last or b != list(range(b[0], b[0] + len(b))):
            raise ValueError(f"invalid tie block {b}: need 2+ consecutive positions, disjoint, >= 0")
        last = b[-1]


def _segments(n: int, ordered: bool, tie_blocks: TieBlocks) -> list[range]:
    """Consecutive row ranges, compared in order with each range a multiset: the whole result when
    unordered; otherwise one range per row, or per tie block."""
    if not ordered:
        return [range(n)]
    ends = {b[0]: b[-1] + 1 for b in tie_blocks}
    out: list[range] = []
    i = 0
    while i < n:
        end = min(ends.get(i, i + 1), n)
        out.append(range(i, end))
        i = end
    return out


def _canonical(items: Sequence[T], segments: Sequence[range], key: Callable[[T], tuple[object, ...]]) -> list[T]:
    """Items sorted within each segment, segments kept in order."""
    out: list[T] = []
    for seg in segments:
        out.extend(sorted((items[i] for i in seg), key=key))
    return out


# --- EX --------------------------------------------------------------------


def _columns(rows: Rows, width: int) -> list[list[SqlValue]]:
    return [[row[j] for row in rows] for j in range(width)]


def _rows_match(pred: Rows, gold_ref: Rows, perm: Sequence[int], segments: Sequence[range]) -> bool:
    """`perm[j]` is the gold column that predicted column j stands for."""
    width = len(perm)
    moved: list[Row] = []
    for row in pred:
        out: list[SqlValue] = [None] * width
        for j, k in enumerate(perm):
            out[k] = row[j]
        moved.append(out)
    canon = _canonical(moved, segments, _row_key)
    return all(_seq_equal(p, g) for p, g in zip(canon, gold_ref, strict=True))


def execution_match(pred: Rows, gold: Rows, *, ordered: bool, tie_blocks: TieBlocks = ()) -> bool:
    if not pred or not gold:
        return not pred and not gold
    if len(pred) != len(gold):
        return False
    width = len(gold[0])
    if len(pred[0]) != width:
        return False
    segments = _segments(len(gold), ordered, tie_blocks)
    pred_cols = [_canonical(c, segments, _key) for c in _columns(pred, width)]
    gold_cols = [_canonical(c, segments, _key) for c in _columns(gold, width)]
    # Necessary condition: a predicted column can only stand for a gold column with the same values.
    candidates = [[k for k in range(width) if _seq_equal(pc, gold_cols[k])] for pc in pred_cols]
    if any(not c for c in candidates):
        return False
    gold_ref = _canonical(gold, segments, _row_key)

    tries = 0
    perm: list[int] = []
    used: set[int] = set()

    def search(j: int) -> bool:
        nonlocal tries
        if tries >= MAX_PERMUTATIONS:
            return False
        if j == width:
            tries += 1
            return _rows_match(pred, gold_ref, perm, segments)
        for k in candidates[j]:
            if k in used:
                continue
            perm.append(k)
            used.add(k)
            found = search(j + 1)
            perm.pop()
            used.discard(k)
            if found:
                return True
        return False

    return search(0)


# --- soft-F1 ---------------------------------------------------------------


def _dedupe(rows: Rows) -> list[Row]:
    seen: set[tuple[SortKey, ...]] = set()
    out: list[Row] = []
    for row in rows:
        k = _row_key(row)
        if k not in seen:
            seen.add(k)
            out.append(row)
    return out


def _contains(row: Row, v: SqlValue) -> bool:
    return any(values_equal(v, x) for x in row)


def _same_cells(a: Row, b: Row) -> bool:
    return _seq_equal(sorted(a, key=_key), sorted(b, key=_key))


def _align_ties(gold: Rows, pred: Rows, tie_blocks: TieBlocks) -> list[Row]:
    """Gold with each tie block reordered so that a gold row matching the predicted row at the same
    position sits there; unmatched gold rows keep their relative order in the remaining positions."""
    out = list(gold)
    for block in tie_blocks:
        pos = [i for i in block if i < len(gold)]
        free = [gold[i] for i in pos]
        placed: dict[int, Row] = {}
        for i in pos:
            hit = next((r for r in free if i < len(pred) and _same_cells(r, pred[i])), None)
            if hit is not None:
                free.remove(hit)
                placed[i] = hit
        rest = iter(free)
        for i in pos:
            out[i] = placed[i] if i in placed else next(rest)
    return out


def soft_f1(pred: Rows, gold: Rows, *, ordered: bool, tie_blocks: TieBlocks = ()) -> float:
    """BIRD Mini-Dev soft-F1: rows de-duplicated and paired by position, cells matched by membership
    within the row (so column order never matters). BIRD pairs rows in set-iteration order; here an
    unordered comparison sorts both sides first so pairing is deterministic and order-insensitive,
    and an ordered one lines gold tie blocks up with the prediction."""
    if not pred and not gold:
        return 1.0
    if ordered:
        p, g = _dedupe(pred), _dedupe(_align_ties(gold, pred, tie_blocks))
    else:
        p, g = _dedupe(sorted(pred, key=_row_key)), _dedupe(sorted(gold, key=_row_key))
    tp = fp = fn = 0.0
    for i, g_row in enumerate(g):
        if i >= len(p):
            fn += 1
            continue
        p_row, width = p[i], len(g_row)
        tp += sum(_contains(g_row, v) for v in p_row) / width
        fp += sum(not _contains(g_row, v) for v in p_row) / width
        fn += sum(not _contains(p_row, v) for v in g_row) / width
    fp += max(len(p) - len(g), 0)
    precision = tp / (tp + fp) if tp + fp > 0 else 0.0
    recall = tp / (tp + fn) if tp + fn > 0 else 0.0
    return 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0


# --- score -----------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    ex: bool
    soft_f1: float
    error: ErrorKind | None  # the prediction's SQL error, if it failed to execute


def score(pred: Result | SqlError, gold: Result, *, ordered: bool, tie_blocks: TieBlocks = ()) -> Score:
    if gold.truncated:
        raise ValueError("gold result is truncated: execute with row_cap=None")
    check_tie_blocks(tie_blocks)
    if tie_blocks and max(b[-1] for b in tie_blocks) >= len(gold.rows):
        raise ValueError(f"tie blocks {[list(b) for b in tie_blocks]} exceed the {len(gold.rows)} gold rows")
    if isinstance(pred, SqlError):
        return Score(False, 0.0, pred.kind)
    if pred.truncated:
        raise ValueError("predicted result is truncated: execute with row_cap=None")
    return Score(
        execution_match(pred.rows, gold.rows, ordered=ordered, tie_blocks=tie_blocks),
        soft_f1(pred.rows, gold.rows, ordered=ordered, tie_blocks=tie_blocks),
        None,
    )
