from __future__ import annotations

import pytest

from escalator.datasets import verify

pytestmark = pytest.mark.data


def test_m1_minidev_shape(ctx: verify.DataContext) -> None:
    result = verify.gate_m1(ctx)
    assert result.ok, str(result)


def test_m2_duplicates_are_exact_copies(ctx: verify.DataContext) -> None:
    result = verify.gate_m2(ctx)
    assert result.ok, str(result)
