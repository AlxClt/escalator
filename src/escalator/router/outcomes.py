from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from escalator.datasets.config import REPO_ROOT
from escalator.datasets.manifest import result_hash
from escalator.env.sandbox import Result, SqlError
from escalator.eval.scorers import Score, score
from escalator.eval.tasks import GoldMismatch, Task
from escalator.router.execcache import Executor
from escalator.router.inventory import Run, RunSet
from escalator.router.signals import submit_of

DEFAULT_BASELINES = REPO_ROOT / "results" / "baselines.json"


class IntegrityError(RuntimeError):
    """Recomputed outcomes differ from results/baselines.json."""


class Scorer:
    def __init__(self, tasks: Mapping[str, Task], ex: Executor) -> None:
        self.tasks = tasks
        self.ex = ex
        self._gold: dict[str, Result] = {}

    def gold(self, task: Task) -> Result:
        hit = self._gold.get(task.question_id)
        if hit is not None:
            return hit
        out = self.ex.execute(task.db_id, task.gold_sql)
        if isinstance(out, SqlError):
            raise GoldMismatch(f"gold for {task.question_id} failed: {out.kind}: {out.message}")
        if result_hash([tuple(r) for r in out.rows]) != task.gold_result_hash:
            raise GoldMismatch(f"gold for {task.question_id} no longer matches gold_result_hash")
        self._gold[task.question_id] = out
        return out

    def score(self, run: Run) -> Score:
        sub = submit_of(run)
        if sub is None:
            return Score(False, 0.0, None)
        task = self.tasks[run.task_id]
        return score(self.ex.execute(task.db_id, sub.sql), self.gold(task), ordered=task.ordered,
                     tie_blocks=task.tie_blocks)

    def a(self, run: Run) -> int:
        return int(self.score(run).ex)


@dataclass(frozen=True)
class RunOutcomes:
    a: dict[str, int]  # task_id -> a(r) of the run (single-sample run sets)
    soft_f1: dict[str, float]


def outcomes(rs: RunSet, scorer: Scorer, task_ids: Sequence[str]) -> RunOutcomes:
    a: dict[str, int] = {}
    f1: dict[str, float] = {}
    for t in task_ids:
        s = scorer.score(rs.single(t))
        a[t], f1[t] = int(s.ex), s.soft_f1
    return RunOutcomes(a, f1)


def sample_outcomes(rs: RunSet, scorer: Scorer, task_ids: Sequence[str]) -> dict[str, list[int]]:
    """a_ij for each task's samples, in sample order."""
    return {t: [scorer.a(r) for r in rs.runs[t]] for t in task_ids}


def load_baselines(path: Path = DEFAULT_BASELINES) -> dict[str, dict[str, bool | None]]:
    """run_id -> task_id -> ex (None for an unscored provider_error task)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, bool | None]] = {}
    for r in raw["runs"]:
        out[str(r["run_id"])] = {str(t["task_id"]): t["ex"] for t in r["tasks"]}
    return out


def check_integrity(recomputed: Mapping[str, RunOutcomes], baselines: Mapping[str, Mapping[str, bool | None]],
                    task_ids: Sequence[str]) -> None:
    """a_S and a_L recomputed from traces must equal results/baselines.json task by task."""
    problems: list[str] = []
    for run_id, out in recomputed.items():
        recorded = baselines.get(run_id)
        if recorded is None:
            problems.append(f"{run_id}: not in results/baselines.json")
            continue
        bad = [t for t in task_ids if recorded.get(t) is None or int(bool(recorded[t])) != out.a[t]]
        if bad:
            problems.append(f"{run_id}: task ids {bad}")
    if problems:
        raise IntegrityError("recomputed EX differs from results/baselines.json:\n  " + "\n  ".join(problems))
