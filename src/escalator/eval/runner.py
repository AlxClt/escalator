"""python -m escalator.eval.runner {smoke,baselines} [--models NAME ...] [--paid] [--overwrite]

Runs the agent loop over a preset of configs/agent.yaml: one MCP server subprocess per run, reused
across tasks; tasks run sequentially in task-id order. Writes traces/<run_id>/{meta.json,steps.jsonl}
per model and results/<preset>.json, scored with eval.tasks.

Paid models (a non-zero price in configs/prices.yaml) are refused without --paid: frontier runs are
only ever triggered by hand (`make baselines PAID=1`). Tasks that end in provider_error are
excluded from scoring and listed, and the runner exits 1; a re-run resumes through the cache.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import anyio
import yaml
from dotenv import load_dotenv
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from escalator.agent.loop import LLM, LoopConfig, RunContext, TaskOutcome, ToolSession, run_task
from escalator.agent.prompts import Prompt, load_prompt
from escalator.agent.tools import (
    RenderCaps,
    build_manifest,
    check_server_tools,
    db_ids_under,
    input_schemas,
    manifest_sha256,
)
from escalator.datasets.config import REPO_ROOT, load_config
from escalator.eval.scorers import Score
from escalator.eval.tasks import Task
from escalator.llm.cache import CACHE_SCHEMA, DEFAULT_CACHE, Cache, CacheMode
from escalator.llm.cost import DEFAULT_PRICES, PriceTable
from escalator.llm.models import ModelSpec, load_models
from escalator.trace.schema import META_FILE, STEPS_FILE, TierMeta, TraceWriter, build_run_meta

DEFAULT_AGENT_CONFIG = REPO_ROOT / "configs" / "agent.yaml"
ENV_LOCK = REPO_ROOT / "data" / "env.lock"
RESULTS_SCHEMA = 1

Scorer = Callable[[Task, str], Score]


class RunRefused(RuntimeError):
    """The run must not start (paid model without --paid, existing run directory)."""


class SpendCapExceeded(RuntimeError):
    """ESCALATOR_MAX_USD_PER_RUN was exceeded; the run stopped after the current task."""


# --- configuration -------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    models: tuple[str, ...]
    tasks: int | None  # first N manifest tasks; None = all
    out: Path


@dataclass(frozen=True)
class AgentConfig:
    prompt_version: str
    loop: LoopConfig
    seed: int
    presets: dict[str, Preset]


def _int(raw: dict[str, object], key: str, where: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where}: {key} must be an integer")
    return value


def _section(raw: dict[str, object], key: str, where: str) -> dict[str, object]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{where}: {key} must be a mapping")
    return {str(k): v for k, v in value.items()}


def load_agent_config(path: Path = DEFAULT_AGENT_CONFIG) -> AgentConfig:
    where = str(path)
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{where}: expected a mapping")
    raw = {str(k): v for k, v in loaded.items()}
    version = raw.get("prompt_version")
    if not isinstance(version, str):
        raise ValueError(f"{where}: prompt_version must be a string")
    render = _section(raw, "render", where)
    max_tokens = _section(raw, "max_tokens", where)
    loop = LoopConfig(
        max_steps=_int(raw, "max_steps", where),
        retry_cap=_int(raw, "validation_retry_cap", where),
        render=RenderCaps(_int(render, "max_rows", where), _int(render, "max_chars", where)),
        overflow_margin=_int(raw, "overflow_margin", where),
        max_tokens={tier: _int(max_tokens, tier, where) for tier in ("S", "L")},
    )
    presets: dict[str, Preset] = {}
    for name, entry in _section(raw, "presets", where).items():
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: presets.{name} must be a mapping")
        models, tasks, out = entry.get("models"), entry.get("tasks"), entry.get("out")
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models) or not isinstance(out, str):
            raise ValueError(f"{where}: presets.{name} needs models (list) and out (path)")
        if tasks != "all" and (isinstance(tasks, bool) or not isinstance(tasks, int) or tasks <= 0):
            raise ValueError(f"{where}: presets.{name}.tasks must be a positive integer or 'all'")
        presets[name] = Preset(tuple(models), None if tasks == "all" else int(tasks), REPO_ROOT / out)
    return AgentConfig(version, loop, _int(raw, "seed", where), presets)


def paid_models(specs: Sequence[ModelSpec], prices: PriceTable) -> list[str]:
    out = []
    for spec in specs:
        price = prices.price(spec.model_id)  # PriceMissing: a missing entry is never free
        if price.input > 0 or price.output > 0:
            out.append(spec.name)
    return out


def check_paid(specs: Sequence[ModelSpec], prices: PriceTable, *, paid: bool) -> None:
    """Refuse paid models unless the run was explicitly marked paid."""
    names = paid_models(specs, prices)
    if names and not paid:
        raise RunRefused(f"paid model(s) {', '.join(names)} need --paid (make baselines PAID=1); "
                         "frontier runs are only triggered by hand")


def sampling(spec: ModelSpec, seed: int) -> tuple[float | None, int | None]:
    """Small tier: T = 0 with a fixed seed. Frontier: provider defaults (Sonnet 5.5 rejects sampling params)."""
    return (0.0, seed) if spec.tier == "S" else (None, None)


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


# --- MCP server --------------------------------------------------------------------


@asynccontextmanager
async def server_session(db_root: Path) -> AsyncIterator[tuple[ClientSession, str]]:
    """Start the MCP server over stdio, initialize, and check its tools against the canonical ones
    before any LLM call. Yields the session and the server version."""
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "escalator.env.server", "--db-root", str(db_root)], cwd=REPO_ROOT)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        check_server_tools((await session.list_tools()).tools, db_ids_under(db_root))
        yield session, init.server_info.version


# --- running and scoring -------------------------------------------------------------


@dataclass(frozen=True)
class ModelReport:
    run_id: str
    spec: ModelSpec
    summary: dict[str, object]
    tasks: list[dict[str, object]]
    provider_error_task_ids: list[str]


@dataclass(frozen=True)
class RunReport:
    preset: str
    prompt: Prompt
    models: list[ModelReport]

    @property
    def exit_code(self) -> int:
        return 1 if any(m.provider_error_task_ids for m in self.models) else 0

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": RESULTS_SCHEMA,
            "preset": self.preset,
            "prompt_version": self.prompt.version,
            "prompt_sha256": self.prompt.sha256,
            "runs": [{"run_id": m.run_id, "model": m.spec.name, "model_id": m.spec.model_id, "tier": m.spec.tier,
                      **m.summary, "tasks": m.tasks} for m in self.models],
        }


def prepare_run_dir(run_dir: Path, *, overwrite: bool) -> None:
    existing = [f for f in (STEPS_FILE, META_FILE) if (run_dir / f).exists()]
    if existing and not overwrite:
        raise RunRefused(f"{run_dir} already holds a run; pass --overwrite (a re-run resumes through the cache)")
    for f in existing:
        (run_dir / f).unlink()


def task_row(task: Task, o: TaskOutcome, score: Score | None) -> dict[str, object]:
    def total(field: str) -> int | None:
        values = [getattr(r, field) for r in o.records]
        return None if any(v is None for v in values) else sum(values)

    return {
        "task_id": task.question_id, "difficulty": task.difficulty, "sample_idx": o.sample_idx, "end": o.end,
        "steps": len(o.records), "n_tool_calls": o.n_tool_calls, "n_validation_failures": o.n_validation_failures,
        "submit_status": o.submit_status,
        "ex": None if score is None else score.ex,
        "soft_f1": None if score is None else score.soft_f1,
        "sql_error": None if score is None else score.error,
        "usd": str(o.usd_nominal), "usd_spent": str(o.usd_spent),
        "tokens_in": total("tokens_in"), "tokens_out": total("tokens_out"), "latency_ms": total("latency_ms"),
    }


def summarize(rows: list[dict[str, object]], outcomes: list[TaskOutcome]) -> dict[str, object]:
    scored = [(r, o) for r, o in zip(rows, outcomes, strict=True) if r["ex"] is not None]
    n_correct = sum(1 for r, _ in scored if r["ex"] is True)
    usd_scored = sum((o.usd_nominal for _, o in scored), Decimal(0))
    n_calls = sum(o.n_tool_calls for o in outcomes)
    n_invalid = sum(o.n_validation_failures for o in outcomes)
    return {
        "n_tasks": len(outcomes), "n_scored": len(scored), "n_correct": n_correct,
        "ex": n_correct / len(scored) if scored else None,
        "usd_nominal": str(usd_scored),
        "usd_spent": str(sum((o.usd_spent for o in outcomes), Decimal(0))),
        "usd_per_solved": str(usd_scored / n_correct) if n_correct else None,
        "ends": dict(sorted(Counter(o.end for o in outcomes).items())),
        "n_tool_calls": n_calls, "n_validation_failures": n_invalid,
        "validation_failure_rate": n_invalid / n_calls if n_calls else None,
        "provider_error_task_ids": [o.task_id for o in outcomes if o.end == "provider_error"],
    }


async def run_model(tasks: Sequence[Task], spec: ModelSpec, *, cfg: AgentConfig, session: ToolSession,
                    run_ctx: RunContext, writer: TraceWriter, sample_idxs: Sequence[int] = (0,),
                    max_usd: Decimal | None = None) -> list[TaskOutcome]:
    """Tasks in task-id order, each written to the trace as soon as it ends."""
    temperature, seed = sampling(spec, cfg.seed)
    outcomes: list[TaskOutcome] = []
    spent = Decimal(0)
    for task in sorted(tasks, key=lambda t: int(t.question_id)):
        for idx in sample_idxs:
            o = await run_task(task, spec, sample_idx=idx, temperature=temperature, seed=seed, session=session,
                               run_ctx=run_ctx)
            for rec in o.records:
                writer.append(rec)
            outcomes.append(o)
            spent += o.usd_spent
            if max_usd is not None and spent > max_usd:
                raise SpendCapExceeded(f"{run_ctx.run_id}: spent ${spent} > cap ${max_usd} after task {task.question_id}")
    return outcomes


async def execute_run(*, preset: str, specs: Sequence[ModelSpec], tasks: Sequence[Task], llm: LLM,
                      prices: PriceTable, prompt: Prompt, cfg: AgentConfig, session: ToolSession,
                      server_version: str, db_ids: Sequence[str], traces_root: Path, overwrite: bool,
                      score: Scorer, ollama_version: str | None, max_usd: Decimal | None = None) -> RunReport:
    tools = build_manifest(db_ids, prompt)
    tool_sha = manifest_sha256(tools)
    cache_context = {"tool_manifest_sha": tool_sha, "mcp_server_version": server_version}
    env_lock = {str(k): str(v) for k, v in json.loads(ENV_LOCK.read_text(encoding="utf-8")).items()}
    by_id = {t.question_id: t for t in tasks}
    reports: list[ModelReport] = []
    for spec in specs:
        run_id = f"{preset}-{spec.name}-{prompt.version}"
        run_dir = traces_root / run_id
        prepare_run_dir(run_dir, overwrite=overwrite)
        temperature, seed = sampling(spec, cfg.seed)
        meta = build_run_meta(
            run_id, prices_sha256=prices.sha256, prices_retrieved_on=prices.retrieved_on,
            model_digests={spec.name: spec.model_id},
            tier_config=TierMeta(name=spec.name, tier=spec.tier, provider=spec.provider, model_id=spec.model_id,
                                 settings=dict(spec.settings)),
            prompt_version=prompt.version, prompt_sha256=prompt.sha256, tool_manifest_sha256=tool_sha,
            mcp_server_version=server_version, mcp_sdk_version=importlib.metadata.version("mcp"),
            temperature=temperature, seed=seed, sample_idxs=[0], max_steps=cfg.loop.max_steps,
            retry_cap=cfg.loop.retry_cap, render_max_rows=cfg.loop.render.max_rows,
            render_max_chars=cfg.loop.render.max_chars, overflow_margin=cfg.loop.overflow_margin,
            max_tokens=cfg.loop.max_tokens[spec.tier], cache_schema=CACHE_SCHEMA, ollama_version=ollama_version,
            env_lock=env_lock,
        )
        run_ctx = RunContext(run_id=run_id, llm=llm, prompt=prompt, tools=tools, schemas=input_schemas(db_ids),
                             cache_context=cache_context, config=cfg.loop)
        with TraceWriter(run_dir) as writer:
            writer.write_meta(meta)
            outcomes = await run_model(tasks, spec, cfg=cfg, session=session, run_ctx=run_ctx, writer=writer,
                                       max_usd=max_usd)
        rows: list[dict[str, object]] = []
        for o in outcomes:
            task = by_id[o.task_id]
            if o.end == "provider_error":
                s: Score | None = None  # infra, not a model failure: excluded from scoring
            elif o.submitted_sql is not None:
                s = score(task, o.submitted_sql)
            else:
                s = Score(False, 0.0, None)
            rows.append(task_row(task, o, s))
        summary = summarize(rows, outcomes)
        ids = summary["provider_error_task_ids"]
        assert isinstance(ids, list)
        reports.append(ModelReport(run_id, spec, summary, rows, [str(i) for i in ids]))
    return RunReport(preset, prompt, reports)


# --- CLI ---------------------------------------------------------------------------


def single_leaf(exc: BaseException) -> BaseException:
    """The one exception inside nested single-member groups (the MCP client's task groups wrap
    whatever the run raises); `exc` itself when it is not such a group."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    return exc


def _main(args: argparse.Namespace) -> int:
    start = time.monotonic()
    cfg = load_agent_config()
    if args.preset not in cfg.presets:
        print(f"unknown preset {args.preset!r}; known: {sorted(cfg.presets)}", file=sys.stderr)
        return 2
    preset = cfg.presets[args.preset]
    models_cfg = load_models()
    names = args.models or list(preset.models)
    unknown = [n for n in names if n not in models_cfg.models]
    if unknown:
        print(f"unknown model(s) {unknown}; known: {sorted(models_cfg.models)}", file=sys.stderr)
        return 2
    specs = [models_cfg.models[n] for n in names]
    prices = PriceTable.load(DEFAULT_PRICES)
    try:
        check_paid(specs, prices, paid=args.paid)
    except RunRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    prompt = load_prompt(cfg.prompt_version)

    # Heavier imports only once the run is allowed to start.
    from escalator.env.sandbox import Sandbox
    from escalator.eval.tasks import load_tasks, score_sql
    from escalator.llm.adapter import Adapter
    from escalator.llm.models import check_ollama
    from escalator.llm.providers.base import Provider

    all_tasks = load_tasks()
    tasks = all_tasks if preset.tasks is None else all_tasks[: preset.tasks]
    providers: dict[str, Provider] = {}
    ollama_version: str | None = None
    if any(s.provider == "ollama" for s in specs):
        from escalator.llm.providers.ollama import OllamaProvider

        ollama = OllamaProvider()
        ollama_version = check_ollama(models_cfg, ollama)
        providers["ollama"] = ollama
    if any(s.provider == "anthropic" for s in specs):
        from escalator.llm.providers.anthropic import AnthropicProvider

        providers["anthropic"] = AnthropicProvider()
    modes: dict[str, CacheMode] = {"readwrite": "readwrite", "readonly": "readonly", "off": "off"}
    mode = os.environ.get("ESCALATOR_CACHE_MODE", "readwrite")
    if mode not in modes:
        print(f"ESCALATOR_CACHE_MODE={mode!r}: expected one of {sorted(modes)}", file=sys.stderr)
        return 2
    cache = Cache(_env_path("ESCALATOR_CACHE_PATH", DEFAULT_CACHE), modes[mode])
    cap = os.environ.get("ESCALATOR_MAX_USD_PER_RUN")
    max_usd = Decimal(cap) if cap else None
    sandbox = Sandbox.from_config()
    db_root = load_config().data_root / "bird"
    traces_root = _env_path("ESCALATOR_TRACES_DIR", REPO_ROOT / "traces")

    async def go() -> RunReport:
        async with server_session(db_root) as (session, server_version):
            return await execute_run(
                preset=args.preset, specs=specs, tasks=tasks, llm=Adapter(providers, cache, prices), prices=prices,
                prompt=prompt, cfg=cfg, session=session, server_version=server_version,
                db_ids=db_ids_under(db_root), traces_root=traces_root, overwrite=args.overwrite,
                score=lambda task, sql: score_sql(sandbox, task, sql), ollama_version=ollama_version,
                max_usd=max_usd,
            )

    try:
        report = anyio.run(go)
    except BaseException as exc:  # re-raised below unless it is a planned stop
        leaf = single_leaf(exc)
        if isinstance(leaf, (RunRefused, SpendCapExceeded)):
            print(f"stopped: {leaf}", file=sys.stderr)
            return 2 if isinstance(leaf, RunRefused) else 3
        if leaf is not exc:
            raise leaf from exc
        raise
    finally:
        cache.close()
    preset.out.parent.mkdir(parents=True, exist_ok=True)
    preset.out.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for m in report.models:
        s = m.summary
        print(f"{m.run_id}: EX {s['n_correct']}/{s['n_scored']} ends={s['ends']} "
              f"usd_nominal={s['usd_nominal']} usd_spent={s['usd_spent']}")
        if m.provider_error_task_ids:
            print(f"  provider_error (excluded from scoring, re-run to resume): {m.provider_error_task_ids}",
                  file=sys.stderr)
    print(f"wrote {preset.out.relative_to(REPO_ROOT).as_posix()} in {time.monotonic() - start:.1f} s")
    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.eval.runner")
    p.add_argument("preset", help="a preset of configs/agent.yaml, e.g. smoke or baselines")
    p.add_argument("--models", nargs="+", help="override the preset's models (keys of configs/models.yaml)")
    p.add_argument("--paid", action="store_true", help="allow paid models (frontier runs are manual only)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing traces/<run_id>/")
    args = p.parse_args(argv)
    # Secrets and machine-local settings (ESCALATOR_ANTHROPIC_KEY, OLLAMA_BASE_URL); real env vars win.
    load_dotenv(REPO_ROOT / ".env", override=False)
    return _main(args)


if __name__ == "__main__":
    sys.exit(main())
