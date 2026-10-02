from __future__ import annotations

import pytest

from escalator.datasets import verify

pytestmark = pytest.mark.data


def test_t1_record_shape(ctx: verify.DataContext) -> None:
    result = verify.gate_t1(ctx)
    assert result.ok, str(result)


def test_t2_unique_ids_and_count(ctx: verify.DataContext) -> None:
    result = verify.gate_t2(ctx)
    assert result.ok, str(result)


def test_t3_join_with_minidev(ctx: verify.DataContext) -> None:
    result = verify.gate_t3(ctx)
    assert result.ok, str(result)


def test_t4_one_difficulty_per_id(ctx: verify.DataContext) -> None:
    result = verify.gate_t4(ctx)
    assert result.ok, str(result)


@pytest.mark.slow
def test_t5_gold_executes(ctx: verify.DataContext) -> None:
    result = verify.gate_t5(ctx)
    assert result.ok, str(result)
