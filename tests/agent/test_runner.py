"""Runner wiring: paid guard (13), provider errors (9), rerun from cache (15)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from agent_helpers import LARGE, SMALL, Server, prices, question_of
from runner_helpers import RUN_ID, run_preset, scripted

from escalator.eval.runner import RunRefused, SpendCapExceeded, check_paid, main, single_leaf
from escalator.trace.schema import META_FILE, STEPS_FILE, StepRecord, read_meta, read_steps

# --- 13. paid guard ---------------------------------------------------------------------


def test_paid_model_refused_without_flag() -> None:
    with pytest.raises(RunRefused, match="PAID=1"):
        check_paid([SMALL, LARGE], prices(small_input="0", small_output="0"), paid=False)
    check_paid([SMALL, LARGE], prices(), paid=True)


def test_local_model_runs_without_flag() -> None:
    check_paid([SMALL], prices(small_input="0", small_output="0"), paid=False)


def test_baselines_refused_before_any_work(capsys: pytest.CaptureFixture[str]) -> None:
    """Real configs: the frontier model is paid, so `make baselines` without PAID=1 stops at once
    (no task loading, no server, no LLM call; the network guard would catch a call)."""
    assert main(["baselines"]) == 2
    assert "PAID=1" in capsys.readouterr().err


# --- 9. provider error ---------------------------------------------------------------------


def test_provider_error_excluded_and_exit_nonzero(server: Server, tmp_path: Path) -> None:
    report = run_preset(server, tmp_path, scripted(fail=lambda req: question_of(req).endswith("Q two")))
    assert report.exit_code == 1
    [model] = report.models
    assert model.provider_error_task_ids == ["2"]
    rows = {r["task_id"]: r for r in model.tasks}
    assert rows["2"]["end"] == "provider_error" and rows["2"]["ex"] is None
    assert model.summary["n_scored"] == 2 and model.summary["n_correct"] == 2


# --- 15. rerun from cache ----------------------------------------------------------------------


def test_second_run_is_all_hits_at_zero_cost(server: Server, tmp_path: Path) -> None:
    first_provider = scripted()
    first = run_preset(server, tmp_path, first_provider)
    run_dir = tmp_path / "traces" / RUN_ID
    first_steps = read_steps(run_dir / STEPS_FILE)
    assert first.exit_code == 0 and first_provider.calls == 6
    assert Decimal(str(first.models[0].summary["usd_spent"])) > 0
    # Sequential and sorted by task id, then step.
    assert [(s.task_id, s.step) for s in first_steps] == [(t, i) for t in ("1", "2", "10") for i in (0, 1)]
    meta = read_meta(run_dir / META_FILE)
    assert meta.run_id == RUN_ID and meta.prompt_version == "v1" and meta.max_steps == 8
    assert meta.tier_config.model_id == SMALL.model_id and meta.temperature == 0.0

    with pytest.raises(RunRefused):
        run_preset(server, tmp_path, scripted())  # an existing run needs --overwrite

    second_provider = scripted()
    second = run_preset(server, tmp_path, second_provider, cache_mode="readonly", overwrite=True)
    second_steps = read_steps(run_dir / STEPS_FILE)
    assert second_provider.calls == 0
    assert all(s.cache_hit for s in second_steps)
    assert second.models[0].summary["usd_spent"] == "0"
    assert second.models[0].summary["usd_nominal"] == first.models[0].summary["usd_nominal"]

    def strip(steps: list[StepRecord]) -> list[dict[str, object]]:
        return [s.model_dump(exclude={"cache_hit"}) for s in steps]

    assert strip(first_steps) == strip(second_steps)


# --- planned stops ---------------------------------------------------------------------------


def test_spend_cap_stops_the_run(server: Server, tmp_path: Path) -> None:
    provider = scripted()
    with pytest.raises(SpendCapExceeded):
        run_preset(server, tmp_path, provider, max_usd=Decimal("0.000001"))
    assert provider.calls == 2  # stopped after the first task


def test_single_leaf_unwraps_task_group_errors() -> None:
    leaf = RunRefused("x")
    assert single_leaf(ExceptionGroup("g", [ExceptionGroup("h", [leaf])])) is leaf
    pair = ExceptionGroup("g", [leaf, ValueError("y")])
    assert single_leaf(pair) is pair and single_leaf(leaf) is leaf
