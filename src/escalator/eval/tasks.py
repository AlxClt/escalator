"""The pinned tasks and their gold: load data/manifest.json, re-execute gold through the sandbox, score.

Gold is re-executed on every scoring call (decision P7) and checked against the manifest's
`gold_result_hash`, so drift in the data, the engine or the sandbox fails loudly instead of
silently moving the accuracy numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from escalator.datasets.config import REPO_ROOT, load_config
from escalator.datasets.manifest import result_hash, task_hash
from escalator.datasets.verify import DataContext, normalize_id
from escalator.env.sandbox import Result, Sandbox, SqlError
from escalator.eval.scorers import Score, check_tie_blocks, score

DEFAULT_MANIFEST = REPO_ROOT / "data" / "manifest.json"
DEFAULT_SCORING_CONFIG = REPO_ROOT / "configs" / "scoring.yaml"

TieBlocks = tuple[tuple[int, ...], ...]


class GoldMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    question_id: str
    db_id: str
    difficulty: str
    gold_sql: str
    gold_result_hash: str
    ordered: bool  # the question asks for an output order: rows are compared in order
    tie_blocks: TieBlocks = ()  # gold row positions tied on the sort key: their order is free


def _tie_blocks(value: object, where: str) -> TieBlocks:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{where}: expected a list of tie blocks")
    blocks: list[tuple[int, ...]] = []
    for block in value:
        if not isinstance(block, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in block):
            raise ValueError(f"{where}: a tie block is a list of row positions, got {block!r}")
        blocks.append(tuple(block))
    check_tie_blocks(blocks)
    return tuple(blocks)


def load_ordered_tasks(path: Path = DEFAULT_SCORING_CONFIG) -> dict[str, TieBlocks]:
    """`ordered_tasks` from configs/scoring.yaml: hand-checked id -> its tie blocks (possibly none)."""
    raw: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = raw.get("ordered_tasks") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        raise ValueError(f"{path}: ordered_tasks must map task ids to their tie blocks")
    return {normalize_id(qid): _tie_blocks(blocks, f"{path}: ordered_tasks.{qid}") for qid, blocks in entries.items()}


def load_tasks(
    manifest_path: Path = DEFAULT_MANIFEST,
    ctx: DataContext | None = None,
    ordered_tasks: dict[str, TieBlocks] | None = None,
) -> list[Task]:
    """The manifest's tasks with their gold SQL; a task whose inputs no longer hash to the manifest's
    `task_hash` raises GoldMismatch. `ordered_tasks` defaults to configs/scoring.yaml."""
    raw: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        raise GoldMismatch(f"{manifest_path}: expected an object with a tasks list")
    ordered = load_ordered_tasks() if ordered_tasks is None else ordered_tasks
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
            blocks = ordered.get(qid, ())
            n_gold = entry.get("gold_row_count")
            if blocks and (not isinstance(n_gold, int) or max(b[-1] for b in blocks) >= n_gold):
                raise GoldMismatch(f"task {qid}: tie blocks exceed its {n_gold} gold rows")
            tasks.append(Task(qid, str(entry.get("db_id")), str(entry.get("difficulty")),
                              str(rec.get("SQL")), str(entry.get("gold_result_hash")), qid in ordered, blocks))
        unknown = set(ordered) - {t.question_id for t in tasks}
        if unknown:
            raise GoldMismatch(f"ordered_tasks not in the manifest: {sorted(unknown, key=int)}")
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
    return score(pred, gold_result(sandbox, task), ordered=task.ordered, tie_blocks=task.tie_blocks)
