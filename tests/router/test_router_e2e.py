"""`make router` end to end on a hand-built inventory (week-3 spec tests 14 and 15): fixture traces on the
fixture database, real sandbox execution and scoring, no network."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from router_helpers import ended, meta, submitted

from escalator.datasets.manifest import result_hash
from escalator.env.sandbox import Result, Sandbox
from escalator.eval.tasks import Task
from escalator.router.__main__ import build, dumps
from escalator.router.config import RouterConfig, parse_router_config
from escalator.router.execcache import CachedExecutor
from escalator.router.inventory import BaselineSettings
from escalator.router.outcomes import IntegrityError
from escalator.trace.schema import META_FILE, STEPS_FILE, RunMeta, StepRecord, TraceWriter

FIXTURE_SQL = Path(__file__).resolve().parents[1] / "fixtures" / "agent_db.sql"
SETTINGS = BaselineSettings("v1", 20261001)

# task -> (gold SQL, stratum)
GOLD = {
    "1": ("SELECT COUNT(*) FROM player", "simple"),
    "2": ("SELECT name FROM team WHERE city = 'Bern'", "moderate"),
    "3": ("SELECT SUM(goals) FROM player", "challenging"),
    "4": ("SELECT name FROM player WHERE goals IS NULL", "simple"),
    "5": ("SELECT MAX(goals) FROM player", "moderate"),
    "6": ("SELECT city FROM team WHERE id = 1", "simple"),
}
WRONG = "SELECT name FROM team"
ERROR = "SELECT SUM(gols) FROM player"
# small baseline: 1 ok, 2 wrong, 3 sql error, 4 no submit, 5 ok, 6 wrong  -> a_S = 1 0 0 0 1 0
SMALL = {"1": "SELECT COUNT(id) FROM player", "2": WRONG, "3": ERROR, "4": None, "5": GOLD["5"][0],
         "6": "SELECT city FROM team WHERE id = 2"}
# frontier: 1 ok, 2 ok, 3 ok, 4 wrong, 5 wrong, 6 ok  -> a_L = 1 1 1 0 0 1
FRONTIER = {"1": GOLD["1"][0], "2": GOLD["2"][0], "3": GOLD["3"][0], "4": WRONG, "5": WRONG, "6": GOLD["6"][0]}
A_S = {"1": 1, "2": 0, "3": 0, "4": 0, "5": 1, "6": 0}
A_L = {"1": 1, "2": 1, "3": 1, "4": 0, "5": 0, "6": 1}
SC = {  # three samples per task
    "1": [SMALL["1"], SMALL["1"], "SELECT COUNT(*) FROM team"],
    "2": [WRONG, GOLD["2"][0], ERROR],
    "3": [None, None, None],
    "4": [GOLD["4"][0], WRONG, WRONG],
    "5": [GOLD["5"][0], GOLD["5"][0], GOLD["5"][0]],
    "6": [WRONG, GOLD["6"][0], "SELECT city FROM team WHERE id = 2"],
}
VERIFIER = {"1": GOLD["1"][0], "2": GOLD["2"][0], "3": GOLD["3"][0], "4": None, "5": GOLD["5"][0], "6": WRONG}


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    path = tmp_path / "bird" / "fixture" / "fixture.sqlite"
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript(FIXTURE_SQL.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return Sandbox(tmp_path, ["fixture"], expected_sqlite=sqlite3.sqlite_version, timeout_s=5)


def tasks(sandbox: Sandbox) -> dict[str, Task]:
    out = {}
    for t, (sql, stratum) in GOLD.items():
        res = sandbox.execute("fixture", sql, row_cap=None)
        assert isinstance(res, Result)
        out[t] = Task(t, "fixture", stratum, sql, result_hash([tuple(r) for r in res.rows]), False)
    return out


def _records(run_id: str, preds: dict[str, str | None], *, sample_idx: int = 0, usd: str = "0",
             tier: str = "S") -> list[StepRecord]:
    recs: list[StepRecord] = []
    for t, sql in preds.items():
        if sql is None:
            recs += ended("no_tool_call", task_id=t, sample_idx=sample_idx, run_id=run_id, usd=usd, tier=tier)
        else:
            recs += submitted(sql, task_id=t, sample_idx=sample_idx, run_id=run_id, usd=usd, tier=tier)
    return recs


def write_run(root: Path, m: RunMeta, records: list[StepRecord]) -> None:
    records = sorted(records, key=lambda r: (int(r.task_id), r.sample_idx, r.step))
    with TraceWriter(root / m.run_id) as w:
        w.write_meta(m)
        for r in records:
            w.append(r)


def write_inventory(root: Path, *, provider_error_task: str | None = None) -> None:
    small = _records("b-s", SMALL)
    if provider_error_task is not None:
        small = [r for r in small if r.task_id != provider_error_task]
        small += ended("provider_error", task_id=provider_error_task, run_id="b-s")
    write_run(root, meta("b-s", "small"), small)
    sc: list[StepRecord] = []
    for j in range(3):
        sc += _records("sc-s", {t: v[j] for t, v in SC.items()}, sample_idx=j)
    write_run(root, meta("sc-s", "small", run_kind="self_consistency", temperature=0.7, seed=None,
                         sample_idxs=[0, 1, 2], sample_seeds={"0": 1, "1": 2, "2": 3}), sc)
    write_run(root, meta("v-s", "small", run_kind="verifier", prompt_version="verifier_v1"),
              _records("v-s", VERIFIER))
    frontier = [r.model_copy(update={"usd": f"0.0{int(r.task_id)}" if r.end else "0"})
                for r in _records("b-l", FRONTIER, tier="L")]
    write_run(root, meta("b-l", "large", tier="L", provider="anthropic"), frontier)


def write_baselines(path: Path, a_s: dict[str, int] = A_S, a_l: dict[str, int] = A_L) -> None:
    runs = [{"run_id": rid, "tasks": [{"task_id": t, "ex": bool(a[t])} for t in GOLD]}
            for rid, a in (("b-s", a_s), ("b-l", a_l))]
    path.write_text(json.dumps({"runs": runs}), encoding="utf-8")


def rcfg(*, verifier: str | None = "v-s", sc: str | None = "sc-s") -> RouterConfig:
    return parse_router_config({
        "self_consistency": {"k": 3, "temperature": 0.7, "seeds": [1, 2, 3], "models": ["small"]},
        "verifier": {"prompt_version": "verifier_v1", "models": ["small"]},
        "inventory": {"small": {"small": {"baseline": "b-s", "self_consistency": sc, "verifier": verifier}},
                      "frontier": {"model": "large", "baseline": "b-l", "repeat": None}},
        "bootstrap": {"B": 200, "seed": 5},
    })


def run_build(tmp_path: Path, sandbox: Sandbox, cfg: RouterConfig, *, cache: str = "exec.sqlite",
              baselines: Path | None = None) -> tuple[str, str]:
    ex = CachedExecutor(sandbox, tmp_path / cache, salt="t")
    try:
        router, signals = build(cfg, SETTINGS, tasks(sandbox), ex, strata={t: g for t, (_, g) in GOLD.items()},
                                traces_root=tmp_path / "traces", baselines_path=baselines or tmp_path / "baselines.json")
    finally:
        ex.close()
    return dumps(router), dumps(signals)


@pytest.fixture
def inventory(tmp_path: Path) -> Path:
    write_inventory(tmp_path / "traces")
    write_baselines(tmp_path / "baselines.json")
    return tmp_path


# --- test 15: end to end, deterministic ---------------------------------------------------------


def test_router_is_byte_identical_across_invocations(inventory: Path, sandbox: Sandbox) -> None:
    first = run_build(inventory, sandbox, rcfg())
    again = run_build(inventory, sandbox, rcfg())  # reuses the persisted execution outcomes
    fresh = run_build(inventory, sandbox, rcfg(), cache="other.sqlite")  # recomputes them
    assert first == again == fresh


def test_router_numbers(inventory: Path, sandbox: Sandbox) -> None:
    router = json.loads(run_build(inventory, sandbox, rcfg())[0])
    pair = router["pairs"]["small"]
    assert router["analysis_set"] == {"n": 6, "excluded_task_ids": [], "rule": router["analysis_set"]["rule"]}
    assert pair["baselines"]["small_only"]["acc"]["value"] == pytest.approx(2 / 6)
    assert pair["baselines"]["frontier_only"]["acc"]["value"] == pytest.approx(4 / 6)
    # b = #{a_S=1, a_L=0} = {5}; c = #{a_S=0, a_L=1} = {2, 3, 6}
    assert (pair["pair_level"]["b"]["value"], pair["pair_level"]["c"]["value"]) == (1, 3)
    assert pair["oracle"]["ceiling"]["value"] == pytest.approx(5 / 6)
    # error signal: tasks 3 (sql error) and 4 (no submit) fire; escalating them gains task 3 only.
    err = pair["signals"]["error"]
    [point] = err["nondegenerate_points"]
    assert point["escalation_rate"]["value"] == pytest.approx(2 / 6)
    assert point["acc"]["value"] == pytest.approx(3 / 6)
    assert point["escalation_composition"]["helpful"]["count"]["value"] == 1
    assert point["escalation_composition"]["wasted"]["count"]["value"] == 1
    assert point["cost_per_task"]["value"] == pytest.approx((0.03 + 0.04) / 6)
    test = point["test_vs_frontier_only"]
    assert test["b"]["value"] == 1 and test["c"]["value"] == 2 and "p_holm" in test
    # self-consistency: m = [2, 1, 0, 2, 3, 1] -> s = [1/3, 2/3, 1, 1/3, 0, 2/3]
    signals = json.loads(run_build(inventory, sandbox, rcfg())[1])
    assert [r["m_sc"] for r in signals["rows"]] == [2, 1, 0, 2, 3, 1]
    # t1: COUNT(id) and COUNT(*) give the same result: the verifier agrees with the baseline
    assert [r["s_verifier"] for r in signals["rows"]] == [0, 1, 1, 1, 0, 1]
    assert [r["s_error"] for r in signals["rows"]] == [0, 0, 1, 1, 0, 0]


def test_every_value_has_an_interval_or_is_a_point_estimate(inventory: Path, sandbox: Sandbox) -> None:
    router = json.loads(run_build(inventory, sandbox, rcfg())[0])
    bad: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            if "value" in node and not ("ci95" in node or node.get("point_estimate") is True):
                bad.append(path)
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(router, "")
    assert bad == []


def test_uncollected_verifier_is_not_collected_not_zero(inventory: Path, sandbox: Sandbox) -> None:
    router_s, signals_s = run_build(inventory, sandbox, rcfg(verifier=None))
    router, signals = json.loads(router_s), json.loads(signals_s)
    assert router["pairs"]["small"]["signals"]["verifier"] == {"status": "not_collected"}
    assert signals["signal_status"]["small"]["verifier"] == "not_collected"
    assert all(r["s_verifier"] is None and r["c_verifier"] is None for r in signals["rows"])
    assert "v-s" not in router["inputs"]


def test_provider_error_excludes_the_task_everywhere(tmp_path: Path, sandbox: Sandbox) -> None:
    write_inventory(tmp_path / "traces", provider_error_task="4")
    a_s = dict(A_S)
    write_baselines(tmp_path / "baselines.json", a_s)
    baselines = json.loads((tmp_path / "baselines.json").read_text(encoding="utf-8"))
    baselines["runs"][0]["tasks"][3]["ex"] = None  # week 2 never scored a provider_error task
    (tmp_path / "baselines.json").write_text(json.dumps(baselines), encoding="utf-8")
    router = json.loads(run_build(tmp_path, sandbox, rcfg())[0])
    assert router["analysis_set"]["n"] == 5 and router["analysis_set"]["excluded_task_ids"] == ["4"]


# --- test 14: integrity ---------------------------------------------------------------------------


def test_integrity_check_passes_and_names_a_perturbed_task(inventory: Path, sandbox: Sandbox) -> None:
    perturbed = dict(A_L)
    perturbed["5"] = 1
    write_baselines(inventory / "perturbed.json", A_S, perturbed)
    with pytest.raises(IntegrityError, match=r"b-l: task ids \['5'\]"):
        run_build(inventory, sandbox, rcfg(), baselines=inventory / "perturbed.json")


def test_inventory_role_mismatch_is_refused(inventory: Path, sandbox: Sandbox) -> None:
    from escalator.router.inventory import InventoryError

    m = json.loads((inventory / "traces" / "sc-s" / META_FILE).read_text(encoding="utf-8"))
    m["sample_seeds"] = {"0": 1, "1": 2, "2": 4}
    (inventory / "traces" / "sc-s" / META_FILE).write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(InventoryError, match="sample_seeds"):
        run_build(inventory, sandbox, rcfg())
    assert (inventory / "traces" / "sc-s" / STEPS_FILE).is_file()
