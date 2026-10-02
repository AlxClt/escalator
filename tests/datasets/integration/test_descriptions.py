from __future__ import annotations

import pytest

from escalator.datasets import verify

pytestmark = pytest.mark.data


def test_s1_arcwise_files_map_to_tables(ctx: verify.DataContext) -> None:
    result = verify.gate_s1(ctx)
    assert result.ok, str(result)


def test_s2_every_table_has_a_description(ctx: verify.DataContext) -> None:
    result = verify.gate_s2(ctx)
    assert result.ok, str(result)
