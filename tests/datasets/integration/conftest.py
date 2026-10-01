"""Integration gates over the real pinned data.

Missing or incomplete data must FAIL these tests, never skip them: a skipped gate is a
vacuous pass. Each test asserts on the matching `verify.gate_*` function.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from escalator.datasets import verify
from escalator.datasets.config import load_config


@pytest.fixture(scope="session")
def ctx() -> Iterator[verify.DataContext]:
    context = verify.DataContext(load_config(), use_cache=os.environ.get("NOCACHE") != "1")
    try:
        _ = context.lock
    except (OSError, ValueError) as exc:
        pytest.fail(f"cannot load the lock: {exc}")
    yield context
    context.close()


@pytest.fixture(scope="session", autouse=True)
def v4_files_unchanged(ctx: verify.DataContext) -> Iterator[None]:
    """V4: re-hash every locked file after the whole session; fail on any change or sidecar."""
    before = verify.snapshot(ctx)
    yield
    ctx.close()
    result = verify.gate_v4(ctx, before)
    if not result.ok:
        pytest.fail(str(result))
