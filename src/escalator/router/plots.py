"""python -m escalator.router.plots [--router results/router.json] [--out-dir results/figures]   (make plots)

results/router.json -> per small model, two frontier figures (PNG + SVG):
- frontier-<model>-cps: y = EX, x = USD per solved task (linear, from 0);
- frontier-<model>-cost: y = EX, x = USD per task (the plane where the oracle gap is measured).

The EX axis runs from the lowest plotted value minus 0.2 (floored at 0) up to 1.

Points: small-only, frontier-only, and per signal escalate-all and every non-degenerate threshold point,
joined in tau order (one marker and one colour per signal); 95% bootstrap intervals on both axes. The
oracle frontier is dashed through its vertices, then flat at its ceiling to the right edge (no budget
buys more than the ceiling); the random-escalation reference is dotted. Footer: n,
prices retrieved_on, run ids.

The headline figure is the frontier plot of configs/router.yaml `headline_small_tier`. That key has no
default: while it is unset, the per-model figures are written and the command then fails (exit 2).

Output is deterministic: fixed svg.hashsalt, no date metadata.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from escalator.datasets.config import REPO_ROOT  # noqa: E402
from escalator.router.config import DEFAULT_ROUTER_CONFIG, load_router_config  # noqa: E402

# Reference categorical palette (dataviz skill, slots 1-3: the set validated all-pairs in light mode,
# worst CVD dE 9.2, normal-vision dE 24.0). Baselines and references stay in neutral ink.
SIGNAL_STYLE = {
    "error": ("#2a78d6", "o", "error"),
    "self_consistency": ("#eb6834", "s", "self-consistency"),
    "verifier": ("#1baf7a", "^", "verifier"),
}
INK, INK_2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#fcfcfb"
AXES = {"cps": ("cost_per_solved", "USD per solved task"), "cost": ("cost_per_task", "USD per task")}
Y_MARGIN = 0.2  # the EX axis starts this far below the lowest plotted value (owner's choice), never below 0

# JSON from results/router.json: nested objects of unknown shape, read defensively.
Json = Any


class HeadlineUnset(RuntimeError):
    pass


def _style() -> None:
    plt.rcParams.update({
        "svg.hashsalt": "escalator", "svg.fonttype": "none", "font.size": 9, "axes.edgecolor": MUTED,
        "axes.labelcolor": INK_2, "xtick.color": INK_2, "ytick.color": INK_2, "axes.spines.top": False,
        "axes.spines.right": False, "figure.facecolor": SURFACE, "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
    })


def _xy(point: Json, x_key: str) -> tuple[float, float, list[float], list[float]] | None:
    """(x, y, xerr, yerr) of a policy point, or None when x is undefined (e.g. nothing solved)."""
    x, y = point[x_key], point["acc_bootstrap"]
    if not isinstance(x["value"], (int, float)):
        return None
    xv, yv = float(x["value"]), float(y["value"])
    xlo, xhi = x["ci95"] if x.get("ci95") and None not in x["ci95"] else (xv, xv)
    ylo, yhi = y["ci95"] if y.get("ci95") and None not in y["ci95"] else (yv, yv)
    return xv, yv, [max(xv - xlo, 0.0), max(xhi - xv, 0.0)], [max(yv - ylo, 0.0), max(yhi - yv, 0.0)]


def _errpoint(ax: Any, p: tuple[float, float, list[float], list[float]], **kw: Any) -> None:
    x, y, xe, ye = p
    ax.errorbar([x], [y], xerr=[[xe[0]], [xe[1]]], yerr=[[ye[0]], [ye[1]]], elinewidth=1, capsize=0, **kw)


def y_floor(ys: list[float]) -> float:
    """Lowest plotted EX minus Y_MARGIN, or 0 when that is negative (or nothing is plotted)."""
    return max(0.0, min(ys) - Y_MARGIN) if ys else 0.0


def rand_points(pair: Json, x_key: str) -> list[tuple[float, float]]:
    return [(r[x_key]["value"], r["acc"]["value"]) for r in pair["random_reference"]["points"]
            if isinstance(r[x_key]["value"], (int, float))]


def frontier_figure(router: Json, model: str, axis: str) -> Any:
    pair = router["pairs"][model]
    x_key, x_label = AXES[axis]
    fig, ax = plt.subplots(figsize=(7.2, 5.0), dpi=150)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ys: list[float] = []  # every plotted EX value, for the axis floor

    oracle = [(v[x_key]["value"], v["acc"]["value"]) for v in pair["oracle"]["vertices"]
              if isinstance(v[x_key]["value"], (int, float))]
    ys += [float(y) for _, y in oracle] + [float(y) for _, y in rand_points(pair, x_key)]
    if oracle:
        ax.plot(*zip(*oracle), linestyle="--", color=INK_2, linewidth=1.5, label="oracle frontier", zorder=2)
    rand = rand_points(pair, x_key)
    if rand:
        ax.plot(*zip(*rand), linestyle=":", color=MUTED, linewidth=1.5, label="random escalation", zorder=2)

    for name, (color, marker, label) in SIGNAL_STYLE.items():
        sig = pair["signals"].get(name, {})
        if sig.get("status") != "ok":
            continue
        pts = [sig["threshold_curve"][0]] + list(sig["nondegenerate_points"])  # escalate-all, then by tau
        xy = [p for p in (_xy(q, x_key) for q in pts) if p is not None]
        if not xy:
            continue
        ys += [p[1] for p in xy]
        ax.plot([p[0] for p in xy], [p[1] for p in xy], color=color, linewidth=2, zorder=3)
        for k, p in enumerate(xy):
            _errpoint(ax, p, fmt=marker, color=color, markersize=7, markeredgecolor=SURFACE, markeredgewidth=1.5,
                      ecolor=color, zorder=4, label=f"{label} signal" if k == 0 else None)

    for key, marker, label in (("small_only", "D", f"small-only ({model})"),
                               ("frontier_only", "*", f"frontier-only ({pair['frontier_model']})")):
        p = _xy(pair["baselines"][key], x_key)
        if p is not None:
            ys.append(p[1])
            _errpoint(ax, p, fmt=marker, color=INK, markersize=9 if marker == "*" else 7, ecolor=INK_2, zorder=5,
                      label=label)

    # Past its last vertex the oracle cannot gain accuracy: Acc_O*(C) stays at the ceiling for any larger
    # budget, so the dashed line continues flat to the right edge.
    xmax = ax.get_xlim()[1]
    if oracle and xmax > oracle[-1][0]:
        ax.plot([oracle[-1][0], xmax], [oracle[-1][1], oracle[-1][1]], linestyle="--", color=INK_2, linewidth=1.5,
                zorder=2)
    ax.set_xlim(0, xmax)
    ax.set_ylim(y_floor(ys), 1)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Execution accuracy (EX)")
    ax.set_title(f"{model} -> {pair['frontier_model']}: accuracy vs {x_label}", color=INK, fontsize=11, loc="left")
    ax.legend(loc="lower right", frameon=False, fontsize=8)
    runs = ", ".join(sorted(rid for rid, info in router["inputs"].items()
                            if info["model"] in (model, pair["frontier_model"])))
    footer = (f"n = {pair['n']} tasks; prices retrieved {router['config']['prices_retrieved_on']}; "
              f"95% bootstrap intervals (B = {router['config']['bootstrap']['B']}). Signal points: escalate-all, "
              f"then each non-degenerate threshold, joined in threshold order. Runs: {runs}")
    fig.text(0.01, 0.01, footer, fontsize=6, color=INK_2, wrap=True, va="bottom")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    return fig


def save(fig: Any, path_stem: Path) -> list[Path]:
    path_stem.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for ext, meta in (("svg", {"Date": None, "Creator": None}), ("png", {"Software": None})):
        p = path_stem.with_suffix(f".{ext}")
        fig.savefig(p, format=ext, metadata=meta)
        out.append(p)
    plt.close(fig)
    return out


def write_figures(router: Json, out_dir: Path, headline: str | None) -> list[Path]:
    """Per-model figures first; then the headline, or HeadlineUnset while it is not chosen."""
    _style()
    written: list[Path] = []
    for model in sorted(router["pairs"]):
        for axis in AXES:
            written += save(frontier_figure(router, model, axis), out_dir / f"frontier-{model}-{axis}")
    if headline is None:
        raise HeadlineUnset("configs/router.yaml: headline_small_tier is not set; it has no default and is chosen "
                            "by the project owner. Per-model figures were written; the headline was not.")
    if headline not in router["pairs"]:
        raise HeadlineUnset(f"headline_small_tier {headline!r} is not a pair of router.json")
    for ext in ("svg", "png"):
        src, dst = out_dir / f"frontier-{headline}-cps.{ext}", out_dir / f"headline.{ext}"
        shutil.copyfile(src, dst)
        written.append(dst)
    return written


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.router.plots")
    p.add_argument("--router", type=Path, default=REPO_ROOT / "results" / "router.json")
    p.add_argument("--config", type=Path, default=DEFAULT_ROUTER_CONFIG)
    p.add_argument("--out-dir", type=Path, default=REPO_ROOT / "results" / "figures")
    args = p.parse_args(argv)
    router = json.loads(args.router.read_text(encoding="utf-8"))
    headline = load_router_config(args.config).headline_small_tier
    try:
        written = write_figures(router, args.out_dir, headline)
    except HeadlineUnset as exc:
        print(f"wrote the per-model figures to {args.out_dir}", file=sys.stderr)
        print(f"stopped: {exc}", file=sys.stderr)
        return 2
    print(f"wrote {len(written)} files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
