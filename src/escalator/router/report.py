"""results/router.json and results/signals.json (week-3 spec D5-D8, "Statistical protocol", "Outputs").

Every number carries an interval or `point_estimate: true`:
- single proportions (EX, escalation rate, NT, per-stratum rates, composition shares): Wilson 95%;
- Cost, CPS, delta Acc vs frontier-only, the CPS ratio, oracle gap, headroom captured and AUC: percentile
  bootstrap over one shared B x n index matrix (thresholds fixed, the oracle recomputed per resample);
  a resample where a statistic is undefined is dropped for it and counted in `n_undefined`;
- counts and descriptive curves: point estimates.

Output is deterministic: floats to 10 significant digits, keys sorted, tau = -inf written "-inf".
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from escalator.eval.metrics import Bootstrap, auc, holm, mcnemar_exact, percentile_interval, quantile7, wilson
from escalator.router.inventory import Run, RunSet, SmallRunSets
from escalator.router.policy import (
    Outcome,
    PairData,
    Tau,
    cps_ratio,
    discordant,
    escalated,
    frontier_only,
    headroom_captured,
    is_nondegenerate,
    mcnemar_counts,
    oracle_ceiling,
    oracle_gap,
    oracle_vertices,
    policy_outcomes,
    random_reference,
    small_only,
    threshold,
    threshold_grid,
)
from escalator.router.signals import SIGNALS, SignalName, SignalTable

SCHEMA_VERSION = 2  # 2: vs_small_only (Δ EX and exact McNemar against the small model alone)
SIGNIFICANT = 10
STRATA = ("simple", "moderate", "challenging")
RANDOM_STEPS = 20  # random-reference points at e = 0, n/20, ..., n
JsonObj = dict[str, object]
Number = int | float | Fraction | Decimal


def num(x: Number | None) -> int | float | None:
    """10 significant digits (not decimals: an exact McNemar p can be 1e-40)."""
    if x is None:
        return None
    if isinstance(x, bool):
        return int(x)
    if isinstance(x, int):
        return x
    return float(f"{float(x):.{SIGNIFICANT}g}")


def tau_json(tau: Tau) -> float | str:
    return "-inf" if tau is None else num(tau)  # type: ignore[return-value]


def pe(x: Number | str | None) -> JsonObj:
    return {"value": x if isinstance(x, str) else num(x), "point_estimate": True}


def wilson_json(k: int, n: int) -> JsonObj:
    iv = wilson(k, n)
    if iv is None:
        return {"value": None, "k": k, "n": n, "point_estimate": True, "undefined": True}
    return {"value": num(Fraction(k, n)), "k": k, "n": n, "ci95": [num(iv.lo), num(iv.hi)], "method": "wilson"}


def boot_json(value: Number | str | None, values: Sequence[float | None]) -> JsonObj:
    iv = percentile_interval(values)
    out: JsonObj = {"value": value if isinstance(value, str) else num(value), "method": "bootstrap",
                    "ci95": [num(iv.lo), num(iv.hi)], "n_undefined": iv.n_undefined}
    if value == "inf":
        out["flag"] = "cps_of_policy_is_zero"
    return out


def _f(x: Number | str | None) -> float | None:
    """A resample's value: undefined (None) and +inf ("inf") are dropped from the interval."""
    if x is None or isinstance(x, str):
        return None
    return float(x)


# --- inputs per pair -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalInput:
    name: SignalName
    status: str  # "ok" | "not_collected"
    s: tuple[Fraction, ...] = ()
    c_sigma: tuple[Decimal, ...] = ()
    tokens: tuple[int, ...] = ()  # small-tier compute of the signal's extra runs, per task
    latency_ms: tuple[int, ...] = ()


@dataclass(frozen=True)
class PairInput:
    model: str
    data: PairData
    signals: dict[SignalName, SignalInput]
    small_tokens: tuple[int, ...]  # baseline run, per task
    small_latency_ms: tuple[int, ...]
    sc_outcomes: dict[str, list[int]] | None  # a_ij of the SC samples, for the small-tier flip rate
    a_L1: tuple[int, ...] | None  # frontier repeat, if collected
    operational: JsonObj  # per tier, from the baseline runs


def signal_inputs(table: SignalTable, small: SmallRunSets, task_ids: Sequence[str]) -> dict[SignalName, SignalInput]:
    def extra(rs: RunSet | None, attr: Callable[[Run], int | Decimal]) -> list[list[int | Decimal]]:
        return [[attr(r) for r in rs.runs[t]] if rs is not None else [] for t in task_ids]

    def summed(rs: RunSet | None) -> tuple[tuple[Decimal, ...], tuple[int, ...], tuple[int, ...]]:
        """Signal overhead per task: Σ cost, tokens and latency of the signal's extra runs (none for error).
        Every signal goes through this path, so a non-zero local price would propagate."""
        costs, tokens, latency = extra(rs, lambda r: r.cost), extra(rs, lambda r: r.tokens), extra(rs, lambda r: r.latency_ms)
        return (tuple(sum((Decimal(c) for c in cs), Decimal(0)) for cs in costs),
                tuple(int(sum(x)) for x in tokens), tuple(int(sum(x)) for x in latency))

    rows = [table.rows[t] for t in task_ids]
    out: dict[SignalName, SignalInput] = {}
    out["error"] = SignalInput("error", "ok", tuple(Fraction(r.s_error) for r in rows), *summed(None))
    for name, rs, values in (("self_consistency", small.self_consistency, [r.s_sc for r in rows]),
                             ("verifier", small.verifier, [None if r.s_verifier is None else Fraction(r.s_verifier)
                                                          for r in rows])):
        sig: SignalName = name  # type: ignore[assignment]
        if rs is None or table.status[sig] != "ok":
            out[sig] = SignalInput(sig, "not_collected")
            continue
        assert all(v is not None for v in values)
        out[sig] = SignalInput(sig, "ok", tuple(v for v in values if v is not None), *summed(rs))
    return out


def operational(rs: RunSet, task_ids: Sequence[str], soft_f1: Mapping[str, float]) -> JsonObj:
    """Baseline runs only. Steps = LLM calls. Validation failures over emitted calls, without calls
    ignored after a submit. Execution errors and timeouts over validated non-submit calls."""
    steps: list[int] = []
    emitted = invalid = executed = exec_errors = timeouts = 0
    latency: list[int] = []
    for t in task_ids:
        run = rs.single(t)
        steps.append(run.steps)
        latency.append(run.latency_ms)
        for rec in run.records:
            for name, status, shown in zip(rec.tool, rec.tool_status, rec.result_hash, strict=True):
                after_submit = status == "ignored" and shown is not None
                if after_submit:
                    continue
                emitted += 1
                if status == "validation_error":
                    invalid += 1
                elif status != "ignored" and name != "submit_answer":
                    executed += 1
                    exec_errors += status == "execution_error"
                    timeouts += status == "timeout"
    return {
        "steps_per_task": {"mean": pe(statistics.fmean(steps)), "median": pe(statistics.median(steps)),
                           "p90": pe(quantile7([float(s) for s in steps], 0.9))},
        "tool_validation_failure_rate": wilson_json(invalid, emitted),
        "execution_error_rate": wilson_json(exec_errors, executed),
        "timeout_rate": wilson_json(timeouts, executed),
        "llm_latency_ms_per_task": {"mean": pe(statistics.fmean(latency)), "median": pe(statistics.median(latency)),
                                    "note": "sum of latency_ms (nominal) over the run's LLM calls; tool time is "
                                            "not recorded in the trace and is excluded"},
        "soft_f1_mean": pe(statistics.fmean(soft_f1[t] for t in task_ids)),
    }


# --- policies and their bootstrap -----------------------------------------------------------------


@dataclass
class Policy:
    key: str
    outcome: Callable[[Sequence[int]], Outcome]
    escalate: list[bool] | None  # None: frontier-only (no small attempt)
    tokens: float  # small-tier compute per task (mean over A)
    latency_ms: float
    values: dict[str, list[float | None]] = field(default_factory=dict)


STATS = ("acc", "cost", "cps", "delta_acc", "delta_acc_small", "cps_ratio", "oracle_gap_pp", "headroom_captured")


def _stats(pi: Outcome, fo: Outcome, small: Outcome, vertices: Sequence[object]) -> dict[str, Number | str | None]:
    gap = oracle_gap(vertices, pi)  # type: ignore[arg-type]
    return {
        "acc": pi.acc, "cost": pi.cost, "cps": pi.cps, "delta_acc": pi.acc - fo.acc, "delta_acc_small": pi.acc - small.acc,
        "cps_ratio": cps_ratio(fo, pi),
        "oracle_gap_pp": None if gap is None else gap * 100,
        "headroom_captured": headroom_captured(vertices, pi, small),  # type: ignore[arg-type]
    }


def policy_json(p: Policy, data: PairData, rows: Sequence[int], fo: Outcome, small: Outcome,
                vertices: Sequence[object]) -> JsonObj:
    pi = p.outcome(rows)
    point = _stats(pi, fo, small, vertices)
    esc = p.escalate if p.escalate is not None else [True] * data.n
    by_stratum: JsonObj = {}
    for g in STRATA:
        idx = [i for i in rows if data.strata[i] == g]
        if not idx:
            continue
        solved = sum(data.a_L[i] if esc[i] else data.a_S[i] for i in idx)
        by_stratum[g] = {"n": pe(len(idx)), "acc": wilson_json(solved, len(idx)),
                         "escalation_rate": wilson_json(sum(esc[i] for i in idx), len(idx))}
    out: JsonObj = {
        "acc": wilson_json(pi.solved, pi.n),
        "acc_bootstrap": boot_json(point["acc"], p.values["acc"]),  # for the plots' joint intervals
        "cost_per_task": boot_json(point["cost"], p.values["cost"]),
        "cost_per_solved": boot_json(point["cps"], p.values["cps"]),
        "escalation_rate": wilson_json(pi.escalated, pi.n),
        "escalation_composition": {
            name: {"count": pe(k), "share_of_escalated": wilson_json(k, pi.escalated)}
            for name, k in (("helpful", pi.helpful), ("wasted", pi.wasted), ("harmful", pi.harmful))},
        "oracle_gap_pp": boot_json(point["oracle_gap_pp"], p.values["oracle_gap_pp"]),
        "headroom_captured": boot_json(point["headroom_captured"], p.values["headroom_captured"]),
        "vs_frontier_only": {"delta_acc": boot_json(point["delta_acc"], p.values["delta_acc"]),
                             "cps_ratio": boot_json(point["cps_ratio"], p.values["cps_ratio"])},
        "by_stratum": by_stratum,
        "small_tier_compute": {"tokens_per_task": pe(p.tokens), "latency_ms_per_task": pe(p.latency_ms),
                               "note": "baseline run plus the signal's extra runs, mean over A"},
    }
    if p.key != "small_only":
        a_pi = [data.a_L[i] if esc[i] else data.a_S[i] for i in rows]
        a_s = [data.a_S[i] for i in rows]
        wins = sum(1 for x, y in zip(a_pi, a_s, strict=True) if x == 1 and y == 0)
        losses = sum(1 for x, y in zip(a_pi, a_s, strict=True) if x == 0 and y == 1)
        out["vs_small_only"] = {
            "delta_acc": boot_json(point["delta_acc_small"], p.values["delta_acc_small"]),
            "test": {"wins": pe(wins), "losses": pe(losses), "p": pe(mcnemar_exact(wins, losses)),
                     "test": "exact McNemar vs small-only; wins = policy right and small-only wrong, losses = the reverse"},
        }
    return out


def mcnemar_json(a_pi: Sequence[int], data: PairData) -> JsonObj:
    b, c = mcnemar_counts(a_pi, data.a_L, range(data.n))
    return {"b": pe(b), "c": pe(c), "p": pe(mcnemar_exact(b, c)), "test": "exact McNemar vs frontier-only"}


@dataclass
class PairReport:
    json: JsonObj
    tests: list[JsonObj]  # the non-degenerate points' test objects (Holm family members)


def pair_report(inp: PairInput, bs: Bootstrap) -> PairReport:
    data, n = inp.data, inp.data.n
    rows = range(n)
    mean = statistics.fmean
    policies: list[Policy] = [
        Policy("small_only", lambda r: small_only(data, r), [False] * n, mean(inp.small_tokens),
               mean(inp.small_latency_ms)),
        Policy("frontier_only", lambda r: frontier_only(data, r), None, 0.0, 0.0),
    ]
    curves: dict[SignalName, list[tuple[Tau, Policy]]] = {}
    for name in SIGNALS:
        sig = inp.signals[name]
        if sig.status != "ok":
            continue
        tokens = mean(a + b for a, b in zip(inp.small_tokens, sig.tokens, strict=True))
        latency = mean(a + b for a, b in zip(inp.small_latency_ms, sig.latency_ms, strict=True))
        curves[name] = []
        for tau in threshold_grid(sig.s):
            pol = Policy(f"{name}@{tau_json(tau)}",
                         (lambda r, s=sig, t=tau: threshold(data, r, s.s, s.c_sigma, t)),
                         escalated(sig.s, tau), tokens, latency)
            policies.append(pol)
            curves[name].append((tau, pol))

    # One pass per resample: shared references, then every policy and AUC.
    auc_vals: dict[str, list[float | None]] = {f"{s}:{t}": [] for s in curves for t in ("gain", "fail")}
    for p in policies:
        p.values = {k: [] for k in STATS}
    for row in bs.indices:
        fo_r, sm_r, v_r = frontier_only(data, row), small_only(data, row), oracle_vertices(data, row)
        for p in policies:
            st = _stats(p.outcome(row), fo_r, sm_r, v_r)
            for k in STATS:
                p.values[k].append(_f(st[k]))
        for name in curves:
            s = inp.signals[name].s
            scores = [float(s[i]) for i in row]
            auc_vals[f"{name}:gain"].append(auc(scores, [data.delta(i) == 1 for i in row]))
            auc_vals[f"{name}:fail"].append(auc(scores, [data.a_S[i] == 0 for i in row]))

    fo, sm, vertices = frontier_only(data, rows), small_only(data, rows), oracle_vertices(data, rows)
    by_key = {p.key: p for p in policies}
    tests: list[JsonObj] = []
    signals_json: JsonObj = {}
    escalate_all: JsonObj = {}
    for name in SIGNALS:
        sig = inp.signals[name]
        if sig.status != "ok":
            signals_json[name] = {"status": sig.status}
            continue
        s = sig.s
        curve, nondeg = [], []
        for tau, pol in curves[name]:
            point = {"tau": tau_json(tau), "tau_exact": "-inf" if tau is None else str(tau),
                     **policy_json(pol, data, rows, fo, sm, vertices)}
            curve.append(point)
            if tau is None:
                escalate_all[name] = point
            if is_nondegenerate(data, s, tau):
                test = mcnemar_json(policy_outcomes(data, escalated(s, tau)), data)
                tests.append(test)
                nondeg.append({**point, "test_vs_frontier_only": test})
        signals_json[name] = {
            "status": "ok",
            "auc": boot_json(auc([float(x) for x in s], [data.delta(i) == 1 for i in rows]), auc_vals[f"{name}:gain"]),
            "auc_fail": boot_json(auc([float(x) for x in s], [data.a_S[i] == 0 for i in rows]),
                                  auc_vals[f"{name}:fail"]),
            "threshold_curve": curve,
            "nondegenerate_points": nondeg,
        }

    b, c = discordant(data, rows)
    n_solved_small = sum(data.a_S)
    flip_small: JsonObj
    if inp.sc_outcomes is None:
        flip_small = {"status": "not_collected"}
    else:
        flips = sum(1 for t in data.task_ids if len(set(inp.sc_outcomes[t])) > 1)
        flip_small = {"status": "ok", "rate": wilson_json(flips, n)}
    flip_frontier: JsonObj
    if inp.a_L1 is None:
        flip_frontier = {"status": "not_measured"}
    else:
        flip_frontier = {"status": "ok", "rate": wilson_json(sum(1 for i in rows if data.a_L[i] != inp.a_L1[i]), n)}

    oracle: JsonObj = {
        "vertices": [{"k": v.k, "acc": pe(v.acc), "cost_per_task": pe(v.cost), "cost_per_solved": pe(v.cps)}
                     for v in vertices],
        "ceiling": wilson_json(sum(max(a, l) for a, l in zip(data.a_S, data.a_L, strict=True)), n),
    }
    assert oracle_ceiling(data, rows) == Fraction(oracle["ceiling"]["k"], n)  # type: ignore[index]
    es = sorted({round(n * q / RANDOM_STEPS) for q in range(RANDOM_STEPS + 1)})
    rand = []
    for e in es:
        rp = random_reference(data, rows, e)
        rand.append({"e": e, "acc": pe(rp.acc), "cost_per_task": pe(rp.cost),
                     "cost_per_solved": pe(rp.cost_total / (rp.acc * n) if rp.acc else None)})

    sm_json = policy_json(by_key["small_only"], data, rows, fo, sm, vertices)
    sm_json["test_vs_frontier_only"] = mcnemar_json(list(data.a_S), data)
    out: JsonObj = {
        "n": n,
        "baselines": {"small_only": sm_json,
                      "frontier_only": policy_json(by_key["frontier_only"], data, rows, fo, sm, vertices),
                      "escalate_all": escalate_all},
        "oracle": oracle,
        "signals": signals_json,
        "random_reference": {"points": rand, "note": "escalate e tasks uniformly at random, no signal paid"},
        "pair_level": {
            "b": pe(b), "c": pe(c),
            "negative_transfer_rate": wilson_json(b, n),
            "negative_transfer_given_small_solved": wilson_json(b, n_solved_small),
            "escalation_useful_rate": wilson_json(c, n),
            "flip_rate": {"small": flip_small, "frontier": flip_frontier},
        },
        "operational": inp.operational,
        "sensitivity_frontier_sample_1": sensitivity(inp) if inp.a_L1 is not None else {"status": "not_measured"},
    }
    return PairReport(out, tests)


def sensitivity(inp: PairInput) -> JsonObj:
    """Pair-level metrics with a_L1 in place of a_L (point estimates; primary numbers stay on sample 0)."""
    assert inp.a_L1 is not None
    d = PairData(inp.data.task_ids, inp.data.a_S, inp.a_L1, inp.data.c_S, inp.data.c_L, inp.data.strata)
    rows = range(d.n)
    b, c = discordant(d, rows)
    out: JsonObj = {"status": "ok", "b": pe(b), "c": pe(c), "negative_transfer_rate": wilson_json(b, d.n),
                    "escalation_useful_rate": wilson_json(c, d.n),
                    "oracle_ceiling": wilson_json(sum(max(a, l) for a, l in zip(d.a_S, d.a_L, strict=True)), d.n)}
    for name, sig in inp.signals.items():
        if sig.status == "ok":
            scores = [float(x) for x in sig.s]
            out[f"auc_{name}"] = pe(auc(scores, [d.delta(i) == 1 for i in rows]))
    return out


def apply_holm(tests: Sequence[JsonObj]) -> None:
    """One family: every non-degenerate point of every collected signal across all small models."""
    ps = [float(t["p"]["value"]) for t in tests]  # type: ignore[index]
    for t, adj in zip(tests, holm(ps), strict=True):
        t["p_holm"] = pe(adj)
        t["holm_family_size"] = len(tests)

