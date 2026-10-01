from __future__ import annotations

import pytest

from escalator.datasets import verify

pytestmark = pytest.mark.data


def test_v1_lock_validates(ctx: verify.DataContext) -> None:
    result = verify.gate_v1(ctx)
    assert result.ok, str(result)


def test_v2_locked_files_match(ctx: verify.DataContext) -> None:
    result = ctx.record(verify.gate_v2(ctx))
    assert result.ok, str(result)


def test_v3_no_unlocked_files(ctx: verify.DataContext) -> None:
    result = verify.gate_v3(ctx)
    assert result.ok, str(result)


def test_v5_interpreter_matches_env_lock(ctx: verify.DataContext) -> None:
    result = ctx.record(verify.gate_v5(ctx))
    assert result.ok, str(result)
