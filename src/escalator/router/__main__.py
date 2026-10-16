"""python -m escalator.router [--config configs/router.yaml] [--out-dir results]   (make router)

The config's optional `manifest`, `scoring` and `baselines` keys select another task set (e.g. the
challenging-stratum study); without them, the pinned 200-task sample.

Traces -> results/signals.json (one row per small model and task) and results/router.json (every
metric). No LLM call, no network, $0: it reads the runs listed in configs/router.yaml and executes
submitted SQL through the sandbox (outcomes cached in .cache/exec.sqlite).

Before anything else, a_S and a_L are recomputed through the scorer and compared with
results/baselines.json task by task (exit 2 on any mismatch, listing the task ids). Two invocations
give byte-identical files.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from escalator.datasets.config import REPO_ROOT
from escalator.eval.metrics import Bootstrap
from escalator.eval.tasks import Task
from escalator.router.config import DEFAULT_ROUTER_CONFIG, RouterConfig, load_router_config
from escalator.router.execcache import Executor
from escalator.router.inventory import (
    DEFAULT_MANIFEST,
    BaselineSettings,
    InventoryError,
    RunSet,
    all_runsets,
    analysis_set,
    load_small_runs,
    load_strata,
    read_frontier_runs,
)
from escalator.router.outcomes import (
    DEFAULT_BASELINES,
    IntegrityError,
    RunOutcomes,
    Scorer,
    check_integrity,
    load_baselines,
    outcomes,
    sample_outcomes,
)
from escalator.router.policy import PairData
from escalator.router.report import (
    SCHEMA_VERSION,
    JsonObj,
    PairInput,
    apply_holm,
    num,
    operational,
    pair_report,
    signal_inputs,
)
from escalator.router.signals import SIGNALS, compute_signals
from escalator.trace.published import DEFAULT_TRACES

ROUTER_JSON = "router.json"
SIGNALS_JSON = "signals.json"


def dumps(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def scored_tasks(rs: RunSet, task_ids: Sequence[str]) -> list[str]:
    """The tasks a baseline run was scored on in week 2 (provider_error tasks were not)."""
    return [t for t in task_ids if rs.single(t).end != "provider_error"]


def build(rcfg: RouterConfig, settings: BaselineSettings, tasks: Mapping[str, Task], ex: Executor, *,
          strata: Mapping[str, str], traces_root: Path = DEFAULT_TRACES,
          baselines_path: Path = DEFAULT_BASELINES) -> tuple[JsonObj, JsonObj]:
    task_ids = sorted(strata, key=int)
    small = {m: load_small_runs(m, rcfg, settings, task_ids, traces_root) for m in rcfg.small}
    frontier = read_frontier_runs(rcfg, settings, task_ids, traces_root)
    runsets = all_runsets(small.values(), frontier)
    A, excluded = analysis_set(task_ids, runsets)
    if not A:
        raise InventoryError("the analysis set is empty")
    scorer = Scorer(tasks, ex)

    # Integrity: a_S and a_L recomputed from traces through the scorer = results/baselines.json.
    recomputed: dict[str, RunOutcomes] = {}
    for rs in [s.baseline for s in small.values()] + [frontier.baseline]:
        recomputed[rs.run_id] = outcomes(rs, scorer, scored_tasks(rs, task_ids))
    baselines = load_baselines(baselines_path)
    for run_id, out in recomputed.items():
        check_integrity({run_id: out}, baselines, list(out.a))
    L = recomputed[frontier.baseline.run_id]
    a_L1 = None
    if frontier.repeat is not None:
        rep = outcomes(frontier.repeat, scorer, A)
        a_L1 = tuple(rep.a[t] for t in A)

    bs = Bootstrap(len(A), rcfg.bootstrap.B, rcfg.bootstrap.seed)
    pairs: JsonObj = {}
    tests: list[JsonObj] = []
    signal_rows: list[JsonObj] = []
    statuses: JsonObj = {}
    c_L = tuple(frontier.baseline.single(t).cost for t in A)
    for m, runs in small.items():
        S = recomputed[runs.baseline.run_id]
        table = compute_signals(runs, A, ex)
        sig = signal_inputs(table, runs, A)
        data = PairData(tuple(A), tuple(S.a[t] for t in A), tuple(L.a[t] for t in A),
                        tuple(runs.baseline.single(t).cost for t in A), c_L, tuple(strata[t] for t in A))
        base_runs = [runs.baseline.single(t) for t in A]
        inp = PairInput(
            model=m, data=data, signals=sig,
            small_tokens=tuple(r.tokens for r in base_runs), small_latency_ms=tuple(r.latency_ms for r in base_runs),
            sc_outcomes=sample_outcomes(runs.self_consistency, scorer, A) if runs.self_consistency else None,
            a_L1=a_L1,
            operational={"small": operational(runs.baseline, A, S.soft_f1),
                         "frontier": operational(frontier.baseline, A, L.soft_f1)},
        )
        report = pair_report(inp, bs)
        pairs[m] = {"frontier_model": frontier.model, **report.json}
        tests += report.tests
        statuses[m] = dict(table.status)
        for i, t in enumerate(A):
            row = table.rows[t]
            sc, ver = sig["self_consistency"], sig["verifier"]
            signal_rows.append({
                "small_model": m, "task_id": t, "stratum": strata[t],
                "a_S": data.a_S[i], "a_L": data.a_L[i], "delta": data.delta(i),
                "a_L1": None if a_L1 is None else a_L1[i],
                "s_error": row.s_error, "s_sc": num(row.s_sc), "m_sc": row.m_sc, "s_verifier": row.s_verifier,
                "c_S": num(data.c_S[i]), "c_L": num(data.c_L[i]),
                "c_sc": num(sc.c_sigma[i]) if sc.status == "ok" else None,
                "c_verifier": num(ver.c_sigma[i]) if ver.status == "ok" else None,
                "small_tokens_S": inp.small_tokens[i], "small_latency_ms_S": inp.small_latency_ms[i],
                "small_tokens_sc": sc.tokens[i] if sc.status == "ok" else None,
                "small_latency_ms_sc": sc.latency_ms[i] if sc.status == "ok" else None,
                "small_tokens_verifier": ver.tokens[i] if ver.status == "ok" else None,
                "small_latency_ms_verifier": ver.latency_ms[i] if ver.status == "ok" else None,
            })
    apply_holm(tests)

    analysis = {"n": len(A), "excluded_task_ids": excluded,
                "rule": "pinned tasks for which no run of the inventory ended in provider_error"}
    roles: JsonObj = {}
    for m, runs in small.items():
        for role, rs in (("baseline", runs.baseline), ("self_consistency", runs.self_consistency),
                         ("verifier", runs.verifier)):
            if rs is not None:
                roles[rs.run_id] = {"model": m, "role": role, "meta_sha256": rs.meta_sha256}
    for role, rs in (("frontier_baseline", frontier.baseline), ("frontier_repeat", frontier.repeat)):
        if rs is not None:
            roles[rs.run_id] = {"model": frontier.model, "role": role, "meta_sha256": rs.meta_sha256}
    sc_cfg = rcfg.self_consistency
    router: JsonObj = {
        "schema_version": SCHEMA_VERSION,
        "inputs": roles,
        "config": {
            "self_consistency": {"k": sc_cfg.k, "temperature": sc_cfg.temperature, "seeds": list(sc_cfg.seeds),
                                 "anthropic_models": "no sampling parameter accepted: samples at provider defaults, "
                                                     "sample_idx 1..k"},
            "bootstrap": {"B": rcfg.bootstrap.B, "seed": rcfg.bootstrap.seed},
            "prices_retrieved_on": frontier.baseline.meta.prices_retrieved_on,
            "verifier_prompt_version": rcfg.verifier.prompt_version,
        },
        "analysis_set": analysis,
        "multiplicity": {"method": "holm", "family_size": len(tests),
                         "family": "every non-degenerate threshold point of every collected signal across all "
                                   "small models, each tested vs frontier-only (exact McNemar)"},
        "pairs": pairs,
    }
    signals_json: JsonObj = {
        "schema_version": SCHEMA_VERSION,
        "analysis_set": analysis,
        "signal_status": statuses,
        "status_reasons": {"not_collected": "the signal's run id is null in configs/router.yaml inventory"},
        "signals": list(SIGNALS),
        "rows": signal_rows,
    }
    return router, signals_json


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.router")
    p.add_argument("--config", type=Path, default=DEFAULT_ROUTER_CONFIG)
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "results")
    args = p.parse_args(argv)
    start = time.monotonic()

    from escalator.env.sandbox import Sandbox
    from escalator.eval.runner import load_agent_config
    from escalator.eval.tasks import load_preset_tasks
    from escalator.router.execcache import CachedExecutor

    rcfg = load_router_config(args.config)
    agent = load_agent_config()
    tasks = {t.question_id: t for t in load_preset_tasks(rcfg.manifest, rcfg.scoring)}
    ex = CachedExecutor(Sandbox.from_config())
    try:
        router, signals = build(rcfg, BaselineSettings(agent.prompt_version, agent.seed), tasks, ex,
                                strata=load_strata(rcfg.manifest or DEFAULT_MANIFEST),
                                baselines_path=rcfg.baselines or DEFAULT_BASELINES)
    except (InventoryError, IntegrityError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    finally:
        ex.close()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for name, obj in ((ROUTER_JSON, router), (SIGNALS_JSON, signals)):
        (args.out_dir / name).write_text(dumps(obj), encoding="utf-8", newline="\n")
    a = router["analysis_set"]
    assert isinstance(a, dict)
    print(f"wrote {ROUTER_JSON} and {SIGNALS_JSON} (n = {a['n']}, excluded {a['excluded_task_ids']}; "
          f"{ex.executed} queries executed) in {time.monotonic() - start:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
