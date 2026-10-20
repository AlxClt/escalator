"""Rebuild a preset's results file (results/<preset>.json) from its recorded traces.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal

from escalator.agent.loop import TaskOutcome
from escalator.agent.prompts import load_prompt
from escalator.env.sandbox import Sandbox
from escalator.eval.runner import ModelReport, RunReport, load_agent_config, summarize, task_row
from escalator.eval.scorers import Score
from escalator.eval.tasks import load_preset_tasks, score_sql
from escalator.llm.models import load_models
from escalator.router.inventory import load_runset
from escalator.router.signals import submit_of


def outcome_from_trace(run) -> TaskOutcome:  # noqa: ANN001  (inventory.Run)
    """The TaskOutcome the loop returned, rebuilt from the run's records."""
    sub = submit_of(run) if run.end == "submitted" else None
    status = None
    for rec in run.records:
        for name, st in zip(rec.tool, rec.tool_status, strict=True):
            if name == "submit_answer" and st not in ("validation_error", "ignored") and status is None:
                status = st
    return TaskOutcome(
        task_id=run.task_id, sample_idx=run.sample_idx, model=run.records[0].model, end=run.end,
        submitted_sql=sub.sql if sub else None, submit_status=status, records=run.records,
        n_tool_calls=sum(r.n_tool_calls for r in run.records),
        n_validation_failures=sum(st == "validation_error" for r in run.records for st in r.tool_status),
        usd_nominal=sum((Decimal(r.usd) for r in run.records if r.usd is not None), Decimal(0)),
        usd_spent=sum((Decimal(r.usd) for r in run.records if r.usd is not None and not r.cache_hit), Decimal(0)),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python scripts/rebuild_preset_results.py")
    p.add_argument("preset")
    p.add_argument("--check-only", action="store_true", help="compare with the existing file, write nothing")
    args = p.parse_args(argv)
    cfg = load_agent_config()
    preset = cfg.presets[args.preset]
    prompt = load_prompt(cfg.prompt_version)
    specs = [load_models().models[n] for n in preset.models]
    tasks = {t.question_id: t for t in load_preset_tasks(preset.manifest, preset.scoring)}
    sandbox = Sandbox.from_config()
    reports = []
    for spec in specs:
        run_id = f"{args.preset}-{spec.name}-{prompt.version}"
        rs = load_runset(run_id)
        outcomes = [outcome_from_trace(rs.single(t)) for t in sorted(rs.runs, key=int)]
        rows = []
        for o in outcomes:
            task = tasks[o.task_id]
            if o.end == "provider_error":
                s: Score | None = None
            elif o.submitted_sql is not None:
                s = score_sql(sandbox, task, o.submitted_sql)
            else:
                s = Score(False, 0.0, None)
            rows.append(task_row(task, o, s))
        summary = summarize(rows, outcomes)
        ids = summary["provider_error_task_ids"]
        assert isinstance(ids, list)
        reports.append(ModelReport(run_id, spec, summary, rows, [str(i) for i in ids]))
    new = RunReport(args.preset, prompt, reports).to_json()
    if preset.out.is_file():
        old = json.loads(preset.out.read_text(encoding="utf-8"))
        new_runs = {r["run_id"]: r for r in new["runs"]}  # type: ignore[union-attr]
        for run in old["runs"]:
            if json.dumps(new_runs.get(run["run_id"]), sort_keys=True) != json.dumps(run, sort_keys=True):
                print(f"refused: {run['run_id']} rebuilt from its trace differs from the existing file", file=sys.stderr)
                return 1
            print(f"{run['run_id']}: rebuilt run identical to the existing file")
    if args.check_only:
        return 0
    preset.out.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for r in new["runs"]:  # type: ignore[union-attr]
        print(f"{r['run_id']}: EX {r['n_correct']}/{r['n_scored']} ends={r['ends']}")
    print(f"wrote {preset.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
