"""scripts/prereg_31b_challenging.py on a hand-built 6-task fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prereg_31b_challenging.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prereg_31b_challenging", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A_S = [0, 1, 0, 1, 1, 0]
S_VER = [1, 0, 1, 0, 1, 0]  # escalate tasks 1, 3, 5
A_L = {"sonnet": [1, 1, 0, 0, 1, 1], "opus": [1, 0, 1, 1, 0, 1]}


def _pair(name: str) -> tuple[dict[str, object], dict[str, object]]:
    rows = [{"small_model": "gemma4-31b", "task_id": str(i + 1), "a_S": A_S[i], "a_L": A_L[name][i],
             "s_verifier": S_VER[i]} for i in range(6)]
    router = {"config": {"bootstrap": {"B": 200, "seed": 1}},
              "pairs": {"gemma4-31b": {"frontier_model": f"claude-{name}-5-5",
                                       "signals": {"verifier": {"threshold_curve": []}}}}}
    return {"rows": rows}, router


def test_primary_secondary_and_held_out() -> None:
    mod = _module()
    res = mod.analyse({"sonnet": _pair("sonnet"), "opus": _pair("opus")}, holdout={"4", "5", "6"})
    s, o = res["primary"]["sonnet"], res["primary"]["opus"]
    # sonnet: routed = [1,1,0,1,1,0] vs small [0,1,0,1,1,0] -> 1 win, 0 losses
    assert (s["mcnemar"]["wins"], s["mcnemar"]["losses"], s["escalated"]) == (1, 0, 3)
    assert s["delta_acc"]["value"] == pytest.approx(1 / 6)
    # opus: routed = [1,1,1,1,0,0] -> wins at 1 and 3, a loss at 5
    assert (o["mcnemar"]["wins"], o["mcnemar"]["losses"]) == (2, 1)
    assert s["mcnemar"]["p_holm"] == 1.0 and o["mcnemar"]["p_holm"] == 1.0 and res["holm_family"] == ["sonnet", "opus"]
    ovs = res["secondary"]["opus_vs_sonnet"]["mcnemar"]
    assert (ovs["wins"], ovs["losses"]) == (2, 2)
    fo = res["secondary"]["sonnet"]["frontier_only_vs_small"]["mcnemar"]
    assert (fo["wins"], fo["losses"]) == (2, 1)  # sonnet right where 31b wrong at 1, 6; wrong where right at 4
    held = res["held_out"]["sonnet"]
    assert held["n"] == 3 and (held["mcnemar"]["wins"], held["mcnemar"]["losses"]) == (0, 0)


def test_uncollected_verifier_is_refused() -> None:
    mod = _module()
    signals, router = _pair("sonnet")
    for r in signals["rows"]:  # type: ignore[union-attr]
        r["s_verifier"] = None
    with pytest.raises(SystemExit, match="not collected"):
        mod.analyse({"sonnet": (signals, router)}, holdout=set())
