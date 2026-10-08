"""The pinned tasks and their gold: load data/manifest.json, re-execute gold through the sandbox, score.

Gold is re-executed on every scoring call (decision P7) and checked against the manifest's
`gold_result_hash`, so drift in the data, the engine or the sandbox fails loudly instead of
silently moving the accuracy numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from escalator.datasets.config import REPO_ROOT, load_config
from escalator.datasets.manifest import result_hash, task_hash
from escalator.datasets.verify import DataContext, normalize_id
from escalator.env.sandbox import Result, Sandbox, SqlError
from escalator.eval.scorers import Score, has_top_level_order_by, score

DEFAULT_MANIFEST = REPO_ROOT / "data" / "manifest.json"


class GoldMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    question_id: str
    db_id: str
    difficulty: str
    gold_sql: str
    gold_result_hash: str
    ordered: bool  # gold has a top-level ORDER BY: rows are compared in order


def make_task(question_id: str, db_id: str, difficulty: str, gold_sql: str, gold_result_hash: str) -> Task:
    return Task(question_id, db_id, difficulty, gold_sql, gold_result_hash, has_top_level_order_by(gold_sql))


def load_tasks(manifest_path: Path = DEFAULT_MANIFEST, ctx: DataContext | None = None) -> list[Task]:
    """The manifest's tasks with their gold SQL; a task whose inputs no longer hash to the manifest's
    `task_hash` raises GoldMismatch."""
    raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise GoldMismatch(f"{manifest_path}: expected an object with a tasks list")
    own = ctx is None
    data = DataContext(load_config()) if ctx is None else ctx
    try:
        tasks: list[Task] = []
        for entry in raw["tasks"]:
            if not isinstance(entry, dict):
                raise GoldMismatch(f"{manifest_path}: malformed task entry")
            qid = normalize_id(entry.get("question_id"))
            rec = data.platfull_by_id.get(qid)
            if rec is None:
                raise GoldMismatch(f"task {qid} is not in Arcwise-Plat-Full")
            if task_hash(rec) != entry.get("task_hash"):
                raise GoldMismatch(f"task {qid}: inputs no longer match the manifest task_hash")
            tasks.append(make_task(qid, str(entry.get("db_id")), str(entry.get("difficulty")),
                                   str(rec.get("SQL")), str(entry.get("gold_result_hash"))))
        return tasks
    finally:
        if own:
            data.close()


def gold_result(sandbox: Sandbox, task: Task) -> Result:
    """Gold rows through the sandbox (the model side's code path), verified against the manifest hash."""
    out = sandbox.execute(task.db_id, task.gold_sql, row_cap=None)
    if isinstance(out, SqlError):
        raise GoldMismatch(f"gold for {task.question_id} failed in the sandbox: {out.kind}: {out.message}")
    if result_hash([tuple(r) for r in out.rows]) != task.gold_result_hash:
        raise GoldMismatch(f"gold for {task.question_id} no longer matches gold_result_hash")
    return out


def score_sql(sandbox: Sandbox, task: Task, sql: str) -> Score:
    pred = sandbox.execute(task.db_id, sql, row_cap=None)
    return score(pred, gold_result(sandbox, task), ordered=task.ordered)
