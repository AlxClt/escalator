"""router/plots.py: week-3 spec test 16. Figures generated from fixture router.json; the headline step
fails while headline_small_tier is unset; output is deterministic."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_router_e2e import GOLD, FIXTURE_SQL, rcfg, run_build, write_baselines, write_inventory

from escalator.env.sandbox import Sandbox
from escalator.router.plots import HeadlineUnset, frontier_figure, write_figures, y_floor


@pytest.fixture(scope="module")
def router_json(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    tmp = tmp_path_factory.mktemp("plots")
    path = tmp / "bird" / "fixture" / "fixture.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    sandbox = Sandbox(tmp, ["fixture"], expected_sqlite=sqlite3.sqlite_version, timeout_s=5)
    write_inventory(tmp / "traces")
    write_baselines(tmp / "baselines.json")
    assert len(GOLD) == 6
    return json.loads(run_build(tmp, sandbox, rcfg())[0])  # type: ignore[no-any-return]


def test_per_model_figures_then_headline_refused_while_unset(router_json: dict[str, object], tmp_path: Path) -> None:
    with pytest.raises(HeadlineUnset, match="headline_small_tier is not set"):
        write_figures(router_json, tmp_path, None)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["frontier-small-cost.png", "frontier-small-cost.svg", "frontier-small-cps.png",
                     "frontier-small-cps.svg"]
    assert not (tmp_path / "headline.png").exists()


def test_headline_is_the_chosen_models_cps_frontier(router_json: dict[str, object], tmp_path: Path) -> None:
    written = write_figures(router_json, tmp_path, "small")
    assert tmp_path / "headline.svg" in written
    assert (tmp_path / "headline.svg").read_bytes() == (tmp_path / "frontier-small-cps.svg").read_bytes()
    with pytest.raises(HeadlineUnset):
        write_figures(router_json, tmp_path / "x", "not-a-model")


def test_figures_are_deterministic(router_json: dict[str, object], tmp_path: Path) -> None:
    write_figures(router_json, tmp_path / "a", "small")
    write_figures(router_json, tmp_path / "b", "small")
    for name in ("frontier-small-cps.svg", "frontier-small-cost.svg", "frontier-small-cps.png"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes(), name
    svg = (tmp_path / "a" / "frontier-small-cps.svg").read_text(encoding="utf-8")
    assert "<dc:date>" not in svg and "oracle frontier" in svg and "n = 6 tasks" in svg


def test_ex_axis_starts_at_the_lowest_value_minus_0_2_floored_at_0(router_json: dict[str, object]) -> None:
    assert y_floor([0.81, 0.69, 0.86]) == pytest.approx(0.49)
    assert y_floor([0.105, 0.86]) == 0.0
    assert y_floor([]) == 0.0
    fig = frontier_figure(router_json, "small", "cost")
    lo, hi = fig.axes[0].get_ylim()
    # Lowest plotted EX in the fixture: small-only = 2/6 (also the oracle's first vertex and the random
    # reference's start); every signal point is higher.
    assert hi == 1.0 and lo == pytest.approx(2 / 6 - 0.2)

