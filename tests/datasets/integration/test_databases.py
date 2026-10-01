from __future__ import annotations

import pytest

from escalator.datasets import verify

pytestmark = pytest.mark.data


def test_d1_expected_database_dirs(ctx: verify.DataContext) -> None:
    result = verify.gate_d1(ctx)
    assert result.ok, str(result)


def test_d2_readonly_quick_check(ctx: verify.DataContext) -> None:
    result = verify.gate_d2(ctx)
    assert result.ok, str(result)


def test_d3_referenced_db_ids_are_expected(ctx: verify.DataContext) -> None:
    result = verify.gate_d3(ctx)
    assert result.ok, str(result)
