"""Pre-registered analysis of the challenging-stratum study (docs/prereg-31b-challenging.md).

python scripts/prereg_31b_challenging.py [--sonnet results/challenging/sonnet] [--opus results/challenging/opus]
    [--out results/challenging/prereg.json]

Reads each pair's results/challenging/<frontier>/{signals.json, router.json} (written by `make router`) and
writes the pre-registered tests. Pure post-processing: no model call, no execution, deterministic.

- Primary, for each frontier: the routed policy (gemma4-31b, escalate iff the verifier disagrees, i.e.
  verifier tau = 0) against gemma4-31b alone: delta EX with a 95% bootstrap CI, two-sided exact McNemar,
  Holm-adjusted over the two frontiers.
- Secondary (descriptive): frontier-only vs gemma4-31b alone; routed vs frontier-only; Opus vs Sonnet;
  the held-out subset (tasks not in data/manifest.json, i.e. not used to choose the policy).
The bootstrap uses the router config's B and seed, over the analysis set sorted by task id.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from escalator.datasets.config import REPO_ROOT
from escalator.eval.metrics import Bootstrap, holm, mcnemar_exact, wilson

SMALL = "gemma4-31b"
FRONTIERS = ("sonnet", "opus")
JsonObj = dict[str, object]


def _num(x: float) -> float:
    return float(f"{x:.10g}")


def _rows(signals: JsonObj) -> list[dict[str, object]]:
    rows = signals["rows"]
    assert isinstance(rows, list)
    return sorted((r for r in rows if r["small_model"] == SMALL), key=lambda r: int(str(r["task_id"])))


def paired(a: Sequence[int], ref: Sequence[int], B: int, seed: int) -> JsonObj:
    """Policy outcomes `a` vs reference `ref` on the same tasks: EX of both, delta with a bootstrap CI,
    exact McNemar (wins = a right and ref wrong, losses = the reverse)."""
    n = len(a)
    wins = sum(1 for x, y in zip(a, ref, strict=True) if x == 1 and y == 0)
    losses = sum(1 for x, y in zip(a, ref, strict=True) if x == 0 and y == 1)
    iv = Bootstrap(n, B, seed).interval(lambda idx: (sum(a[i] for i in idx) - sum(ref[i] for i in idx)) / len(idx))

    def ex(v: Sequence[int]) -> JsonObj:
        w = wilson(sum(v), n)
        assert w is not None
        return {"value": _num(sum(v) / n), "k": sum(v), "n": n, "ci95": [_num(w.lo), _num(w.hi)], "method": "wilson"}

    return {
        "n": n, "ex_policy": ex(a), "ex_reference": ex(ref),
        "delta_acc": {"value": _num((sum(a) - sum(ref)) / n), "ci95": [_num(iv.lo or 0.0), _num(iv.hi or 0.0)],
                      "method": "bootstrap", "n_undefined": iv.n_undefined},
        "mcnemar": {"wins": wins, "losses": losses, "p": _num(mcnemar_exact(wins, losses))},
    }


def outcomes(rows: Sequence[dict[str, object]]) -> dict[str, list[int]]:
    a_s = [int(str(r["a_S"])) for r in rows]
    a_l = [int(str(r["a_L"])) for r in rows]
    s_ver = [r["s_verifier"] for r in rows]
    if any(s is None for s in s_ver):
        raise SystemExit("the verifier signal is not collected in this signals.json")
    routed = [al if float(str(s)) > 0 else as_ for as_, al, s in zip(a_s, a_l, s_ver, strict=True)]
    return {"small": a_s, "frontier": a_l, "routed": routed, "escalated": [int(float(str(s)) > 0) for s in s_ver]}


def analyse(pairs: dict[str, tuple[JsonObj, JsonObj]], holdout: set[str]) -> JsonObj:
    """pairs: frontier -> (signals.json, router.json) of that pair."""
    out: JsonObj = {"small_model": SMALL, "policy": "escalate iff the verifier run disagrees (verifier tau = 0)"}
    primary: JsonObj = {}
    secondary: JsonObj = {}
    held: JsonObj = {}
    outs: dict[str, dict[str, list[int]]] = {}
    ids: dict[str, list[str]] = {}
    for name, (signals, router) in pairs.items():
        cfg = router["config"]
        assert isinstance(cfg, dict)
        B, seed = int(cfg["bootstrap"]["B"]), int(cfg["bootstrap"]["seed"])
        rows = _rows(signals)
        o = outcomes(rows)
        outs[name], ids[name] = o, [str(r["task_id"]) for r in rows]
        frontier = str(router["pairs"][SMALL]["frontier_model"])  # type: ignore[index]
        primary[name] = {"frontier_model": frontier, "escalated": sum(o["escalated"]),
                         **paired(o["routed"], o["small"], B, seed)}
        _cross_check(primary[name], router)
        secondary[name] = {
            "frontier_only_vs_small": paired(o["frontier"], o["small"], B, seed),
            "routed_vs_frontier_only": paired(o["routed"], o["frontier"], B, seed),
        }
        keep = [i for i, t in enumerate(ids[name]) if t in holdout]
        held[name] = {"task_ids": "tasks of the study not in data/manifest.json",
                      **paired([o["routed"][i] for i in keep], [o["small"][i] for i in keep], B, seed)}
    names = list(primary)
    for name, adj in zip(names, holm([float(primary[k]["mcnemar"]["p"]) for k in names]), strict=True):  # type: ignore[index]
        primary[name]["mcnemar"]["p_holm"] = _num(adj)  # type: ignore[index]
    if {"sonnet", "opus"} <= set(outs) and ids["sonnet"] == ids["opus"]:
        cfg = pairs["sonnet"][1]["config"]
        assert isinstance(cfg, dict)
        secondary["opus_vs_sonnet"] = paired(outs["opus"]["frontier"], outs["sonnet"]["frontier"],
                                             int(cfg["bootstrap"]["B"]), int(cfg["bootstrap"]["seed"]))
    out.update({"primary": primary, "holm_family": names, "secondary": secondary, "held_out": held})
    return out


def _cross_check(prim: JsonObj, router: JsonObj) -> None:
    """When the router has the verifier tau = 0 point, its EX and its test vs small-only must match."""
    sig = router["pairs"][SMALL]["signals"]["verifier"]  # type: ignore[index]
    point = next((q for q in sig.get("threshold_curve", []) if q["tau_exact"] == "0"), None)
    if point is None:
        return
    test = point["vs_small_only"]["test"]
    assert (test["wins"]["value"], test["losses"]["value"]) == (prim["mcnemar"]["wins"], prim["mcnemar"]["losses"])  # type: ignore[index]
    assert abs(point["acc"]["value"] - prim["ex_policy"]["value"]) < 1e-9  # type: ignore[index]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python scripts/prereg_31b_challenging.py")
    p.add_argument("--sonnet", type=Path, default=REPO_ROOT / "results" / "challenging" / "sonnet")
    p.add_argument("--opus", type=Path, default=REPO_ROOT / "results" / "challenging" / "opus")
    p.add_argument("--sample-manifest", type=Path, default=REPO_ROOT / "data" / "manifest.json")
    p.add_argument("--out", type=Path, default=REPO_ROOT / "results" / "challenging" / "prereg.json")
    args = p.parse_args(argv)
    pairs = {name: (json.loads((d / "signals.json").read_text(encoding="utf-8")),
                    json.loads((d / "router.json").read_text(encoding="utf-8")))
             for name, d in (("sonnet", args.sonnet), ("opus", args.opus))}
    sample = {str(t["question_id"]) for t in json.loads(args.sample_manifest.read_text(encoding="utf-8"))["tasks"]}
    all_ids = {str(r["task_id"]) for r in _rows(pairs["sonnet"][0])}
    result = analyse(pairs, all_ids - sample)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8",
                        newline="\n")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
