"""python -m escalator.eval.collect {sc-samples,verifier-runs,frontier-repeat,estimate}
    [--smoke] [--models NAME ...] [--paid] [--overwrite]

Collects the traces the week-3 signals need, beyond the week-2 baselines (settings: configs/router.yaml):

- sc-samples: k samples per task at temperature T, sample j with seeds[j] (self_consistency signal);
- verifier-runs: one fresh run per task with the verifier prompt, at the baseline's sampling;
- frontier-repeat: one more frontier pass, sample_idx 1 (flip rate). Paid: defined, run by hand only;
- estimate: projected wall-clock of the full collection, from the smoke runs (--smoke).

One run id per (kind, model) holds every sample: traces/<kind>-<model>-<prompt_version>/, records in
(task_id, sample_idx, step) order. Before the first LLM call, each run's meta is compared with its
model's baseline meta (eval.compat): any difference outside sampling, identity and timestamps aborts
with a field-level diff. Paid models are refused without --paid (make ... PAID=1), with the projected
cost printed. provider_error, resume through the cache (--overwrite) and exit codes are the runner's:
1 provider_error, 2 refused, 3 spend cap, 4 provider setup failed or cache miss in readonly mode.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import anyio
from dotenv import load_dotenv

from escalator.agent.loop import LLM, RunContext, TaskOutcome, ToolSession, max_tokens_for, run_task
from escalator.agent.prompts import Prompt, PromptError, load_prompt, load_prompt_with_tools, placeholder_fields
from escalator.agent.tools import build_manifest, db_ids_under, input_schemas, manifest_sha256
from escalator.datasets.config import REPO_ROOT, load_config
from escalator.eval.compat import CompatError, check_compat
from escalator.eval.runner import (
    ENV_LOCK,
    AgentConfig,
    RunRefused,
    SpendCapExceeded,
    load_agent_config,
    paid_models,
    prepare_run_dir,
    sampling,
    server_session,
    single_leaf,
)
from escalator.eval.tasks import Task
from escalator.llm.cache import CACHE_SCHEMA, DEFAULT_CACHE, Cache, CacheMode
from escalator.llm.cost import DEFAULT_PRICES, PriceTable
from escalator.llm.errors import CacheMiss, ProviderSetupError
from escalator.llm.models import ModelSpec, load_models
from escalator.llm.types import Request, Response
from escalator.router.config import RouterConfig, load_router_config
from escalator.trace.published import DEFAULT_TRACES, RunNotFound, load_meta, load_steps
from escalator.trace.schema import RunKind, RunMeta, StepRecord, TierMeta, TraceWriter, build_run_meta

COMMANDS: dict[str, RunKind] = {
    "sc-samples": "self_consistency", "verifier-runs": "verifier", "frontier-repeat": "frontier_repeat",
}
RUN_PREFIX: dict[RunKind, str] = {
    "baseline": "baselines", "self_consistency": "sc", "verifier": "verifier", "frontier_repeat": "frontier-repeat",
}
SMOKE_MODELS = ("gemma4-e4b",)
SMOKE_TASKS = 10  # the first 10 manifest tasks, as `make smoke`
NUM_PARALLEL_ENV = "OLLAMA_NUM_PARALLEL"


@dataclass(frozen=True)
class Sample:
    sample_idx: int
    temperature: float | None
    seed: int | None


@dataclass(frozen=True)
class Job:
    kind: RunKind
    spec: ModelSpec
    run_id: str
    prompt: Prompt
    samples: tuple[Sample, ...]
    baseline_run_id: str


@dataclass(frozen=True)
class JobReport:
    job: Job
    outcomes: list[TaskOutcome]

    @property
    def provider_error_task_ids(self) -> list[str]:
        return [o.task_id for o in self.outcomes if o.end == "provider_error"]


@dataclass
class OllamaInfo:
    """Filled when the Ollama provider is first built (first cache miss): None for a cached rerun."""

    version: Callable[[], str | None] = lambda: None
    params: dict[str, dict[str, object]] = field(default_factory=dict)  # model tag -> /api/show parameters


# --- planning ----------------------------------------------------------------------


def sc_samples(spec: ModelSpec, rcfg: RouterConfig) -> tuple[Sample, ...]:
    """Ollama: T and seeds[j] for sample j = 0..k-1. Anthropic accepts no sampling parameter: samples
    1..k, so that no sample sends the baseline's request (sample_idx 0, same parameters)."""
    sc = rcfg.self_consistency
    if spec.provider == "ollama":
        return tuple(Sample(j, sc.temperature, sc.seeds[j]) for j in range(sc.k))
    return tuple(Sample(j + 1, None, None) for j in range(sc.k))


def baseline_sampling(spec: ModelSpec, cfg: AgentConfig, sample_idx: int) -> tuple[Sample, ...]:
    temperature, seed = sampling(spec, cfg.seed)
    return (Sample(sample_idx, temperature, seed),)


def run_id_for(kind: RunKind, spec: ModelSpec, prompt: Prompt, *, smoke: bool) -> str:
    prefix = RUN_PREFIX[kind] + ("-smoke" if smoke else "")
    return f"{prefix}-{spec.name}-{prompt.version}"


def baseline_run_id(kind: RunKind, spec: ModelSpec, rcfg: RouterConfig) -> str:
    if kind == "frontier_repeat":
        if spec.name != rcfg.frontier.model:
            raise RunRefused(f"{spec.name} is not the inventory's frontier model {rcfg.frontier.model}")
        return rcfg.frontier.baseline
    runs = rcfg.small.get(spec.name)
    if runs is None:
        raise RunRefused(f"{spec.name} has no baseline in configs/router.yaml inventory.small")
    return runs.baseline


def plan_job(kind: RunKind, spec: ModelSpec, *, rcfg: RouterConfig, cfg: AgentConfig, prompt: Prompt,
             smoke: bool) -> Job:
    if kind == "self_consistency":
        samples = sc_samples(spec, rcfg)
    elif kind == "verifier":
        samples = baseline_sampling(spec, cfg, 0)
    elif kind == "frontier_repeat":
        samples = baseline_sampling(spec, cfg, 1)
    else:
        raise ValueError(f"no collection for run kind {kind}")
    return Job(kind, spec, run_id_for(kind, spec, prompt, smoke=smoke), prompt, samples,
               baseline_run_id(kind, spec, rcfg))


def job_meta(job: Job, *, prices: PriceTable, tool_sha: str, server_version: str, cfg: AgentConfig,
             env_lock: dict[str, str], num_parallel: str | None) -> RunMeta:
    temps = {s.temperature for s in job.samples}
    seeds = {s.seed for s in job.samples}
    if len(temps) != 1:
        raise ValueError(f"{job.run_id}: samples must share one temperature")
    per_sample = len(seeds) > 1
    spec = job.spec
    return build_run_meta(
        job.run_id, prices_sha256=prices.sha256, prices_retrieved_on=prices.retrieved_on,
        model_digests={spec.name: spec.model_id},
        tier_config=TierMeta(name=spec.name, tier=spec.tier, provider=spec.provider, model_id=spec.model_id,
                             settings=dict(spec.settings)),
        prompt_version=job.prompt.version, prompt_sha256=job.prompt.sha256, tool_manifest_sha256=tool_sha,
        mcp_server_version=server_version, mcp_sdk_version=importlib.metadata.version("mcp"),
        temperature=next(iter(temps)), seed=None if per_sample else next(iter(seeds)),
        sample_idxs=[s.sample_idx for s in job.samples], max_steps=cfg.loop.max_steps,
        retry_cap=cfg.loop.retry_cap, render_max_rows=cfg.loop.render.max_rows,
        render_max_chars=cfg.loop.render.max_chars, overflow_margin=cfg.loop.overflow_margin,
        max_tokens=max_tokens_for(spec, cfg.loop), cache_schema=CACHE_SCHEMA, ollama_version=None,
        env_lock=env_lock, run_kind=job.kind,
        sample_seeds={str(s.sample_idx): s.seed for s in job.samples if s.seed is not None} if per_sample else None,
        baseline_run_id=job.baseline_run_id,
        ollama_num_parallel=num_parallel if spec.provider == "ollama" else None,
    )


def expected_prompt(job: Job) -> tuple[str, str] | None:
    return (job.prompt.version, job.prompt.sha256) if job.kind == "verifier" else None


# --- running -----------------------------------------------------------------------


@dataclass(frozen=True)
class Gated:
    job: Job
    meta: RunMeta
    run_ctx: RunContext


def gate(jobs: Sequence[Job], *, llm: LLM, prices: PriceTable, cfg: AgentConfig, server_version: str,
         db_ids: Sequence[str], traces_root: Path, env_lock: dict[str, str], num_parallel: str | None) -> list[Gated]:
    """Every job's meta against its baseline's, before any LLM call; CompatError lists the diffs."""
    out: list[Gated] = []
    errors: list[str] = []
    for job in jobs:
        tools = build_manifest(db_ids, job.prompt)
        tool_sha = manifest_sha256(tools)
        meta = job_meta(job, prices=prices, tool_sha=tool_sha, server_version=server_version, cfg=cfg,
                        env_lock=env_lock, num_parallel=num_parallel)
        try:
            base = load_meta(job.baseline_run_id, traces_root)
        except RunNotFound as exc:
            errors.append(f"{job.run_id}: baseline missing: {exc}")
            continue
        try:
            check_compat(meta, base, kind=job.kind, expected_prompt=expected_prompt(job))
        except CompatError as exc:
            errors.append(str(exc))
            continue
        run_ctx = RunContext(run_id=job.run_id, llm=llm, prompt=job.prompt, tools=tools,
                             schemas=input_schemas(db_ids),
                             cache_context={"tool_manifest_sha": tool_sha, "mcp_server_version": server_version},
                             config=cfg.loop)
        out.append(Gated(job, meta, run_ctx))
    if errors:
        raise CompatError("compatibility gate failed; no LLM call was made\n" + "\n".join(errors))
    return out


async def run_job(g: Gated, tasks: Sequence[Task], *, session: ToolSession, traces_root: Path, overwrite: bool,
                  ollama: OllamaInfo, max_usd: Decimal | None) -> JobReport:
    job, run_dir = g.job, traces_root / g.job.run_id
    prepare_run_dir(run_dir, overwrite=overwrite)
    outcomes: list[TaskOutcome] = []
    spent = Decimal(0)
    total = len(tasks) * len(job.samples)
    with TraceWriter(run_dir) as writer:
        writer.write_meta(g.meta)
        try:
            for task in sorted(tasks, key=lambda t: int(t.question_id)):
                for s in job.samples:
                    t0 = time.monotonic()
                    try:
                        o = await run_task(task, job.spec, sample_idx=s.sample_idx, temperature=s.temperature,
                                           seed=s.seed, session=session, run_ctx=g.run_ctx)
                    except CacheMiss as exc:
                        raise CacheMiss(f"task {task.question_id} (sample {s.sample_idx}): no cache entry {exc} "
                                        "in readonly mode") from exc
                    for rec in o.records:
                        writer.append(rec)
                    outcomes.append(o)
                    spent += o.usd_spent
                    hits = sum(r.cache_hit for r in o.records)
                    print(f"{job.run_id} [{len(outcomes)}/{total}] task {task.question_id} sample {s.sample_idx}: "
                          f"{o.end}, {len(o.records)} steps ({hits} cached), {time.monotonic() - t0:.1f} s", flush=True)
                    if max_usd is not None and spent > max_usd:
                        raise SpendCapExceeded(f"{job.run_id}: spent ${spent} > cap ${max_usd} after task "
                                               f"{task.question_id}")
        finally:
            fresh = any(not r.cache_hit for o in outcomes for r in o.records)
            if job.spec.provider == "ollama" and fresh:
                assert job.spec.tag is not None
                writer.write_meta(g.meta.model_copy(update={
                    "ollama_version": ollama.version(), "ollama_modelfile_params": ollama.params.get(job.spec.tag),
                }))
    return JobReport(job, outcomes)


async def collect(jobs: Sequence[Job], tasks: Sequence[Task], *, llm: LLM, prices: PriceTable, cfg: AgentConfig,
                  session: ToolSession, server_version: str, db_ids: Sequence[str], traces_root: Path,
                  overwrite: bool, env_lock: dict[str, str], num_parallel: str | None = None,
                  ollama: OllamaInfo | None = None, max_usd: Decimal | None = None) -> list[JobReport]:
    """All gates first, then the jobs in order."""
    gated = gate(jobs, llm=llm, prices=prices, cfg=cfg, server_version=server_version, db_ids=db_ids,
                 traces_root=traces_root, env_lock=env_lock, num_parallel=num_parallel)
    info = ollama or OllamaInfo()
    return [await run_job(g, tasks, session=session, traces_root=traces_root, overwrite=overwrite, ollama=info,
                          max_usd=max_usd) for g in gated]


# --- projections -------------------------------------------------------------------


def _by_task(steps: Sequence[StepRecord], attr: str, task_ids: set[str] | None = None) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in steps:
        if task_ids is not None and r.task_id not in task_ids:
            continue
        v = getattr(r, attr)
        if v is not None:
            out[r.task_id] = out.get(r.task_id, Decimal(0)) + Decimal(v)
    return out


def projected_cost(job: Job, traces_root: Path, task_ids: set[str] | None) -> Decimal:
    """n_samples x the nominal usd of the baseline run on the same tasks."""
    usd = _by_task(load_steps(job.baseline_run_id, traces_root), "usd", task_ids)
    return len(job.samples) * sum(usd.values(), Decimal(0))


@dataclass(frozen=True)
class Estimate:
    model: str
    kind: RunKind
    n_samples: int
    baseline_ms: Decimal  # Σ latency_ms of the baseline run, all tasks
    ratio: Decimal | None  # smoke latency per sample / baseline latency, same tasks
    ratio_from: str | None  # the smoke run the ratio was measured on

    @property
    def projected_hours(self) -> Decimal | None:
        if self.ratio is None:
            return None
        return self.n_samples * self.baseline_ms * self.ratio / Decimal(3_600_000)


def smoke_ratio(smoke_run: str, baseline_run: str, n_samples: int, traces_root: Path) -> Decimal:
    smoke = _by_task(load_steps(smoke_run, traces_root), "latency_ms")
    base = _by_task(load_steps(baseline_run, traces_root), "latency_ms", set(smoke))
    denominator = sum(base.values(), Decimal(0))
    if denominator == 0:
        raise ValueError(f"{baseline_run} has no latency on the tasks of {smoke_run}")
    return sum(smoke.values(), Decimal(0)) / n_samples / denominator


def estimates(jobs: Sequence[Job], smoke_jobs: Sequence[Job], traces_root: Path) -> list[Estimate]:
    """projected wall-clock = n_samples x Σ baseline latency x r, with r measured on the smoke run of
    the same kind (one smoke model: its r is applied to every model of that kind)."""
    ratios: dict[RunKind, tuple[Decimal, str]] = {}
    for sj in smoke_jobs:
        try:
            ratios[sj.kind] = (smoke_ratio(sj.run_id, sj.baseline_run_id, len(sj.samples), traces_root), sj.run_id)
        except RunNotFound:
            continue
    out: list[Estimate] = []
    for job in jobs:
        base_ms = sum(_by_task(load_steps(job.baseline_run_id, traces_root), "latency_ms").values(), Decimal(0))
        r = ratios.get(job.kind)
        out.append(Estimate(job.spec.name, job.kind, len(job.samples), base_ms, r[0] if r else None,
                            r[1] if r else None))
    return out


# --- CLI ---------------------------------------------------------------------------


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


def _prompts(kind: RunKind, rcfg: RouterConfig, cfg: AgentConfig) -> Prompt:
    base = load_prompt(cfg.prompt_version)
    if kind != "verifier":
        return base
    if rcfg.verifier.prompt_version == cfg.prompt_version:
        raise RunRefused(f"the verifier prompt must not be the baseline's ({cfg.prompt_version})")
    return load_prompt_with_tools(rcfg.verifier.prompt_version, base)


def _models(kind: RunKind, rcfg: RouterConfig, args: argparse.Namespace) -> list[str]:
    if args.models:
        return list(args.models)
    if args.smoke:
        return list(SMOKE_MODELS)
    if kind == "self_consistency":
        return list(rcfg.self_consistency.models)
    if kind == "verifier":
        return list(rcfg.verifier.models)
    return [rcfg.frontier.model]


def plan_jobs(kind: RunKind, args: argparse.Namespace, rcfg: RouterConfig, cfg: AgentConfig) -> list[Job]:
    models_cfg = load_models()
    names = _models(kind, rcfg, args)
    unknown = [n for n in names if n not in models_cfg.models]
    if unknown:
        raise RunRefused(f"unknown model(s) {unknown}; known: {sorted(models_cfg.models)}")
    prompt = _prompts(kind, rcfg, cfg)
    return [plan_job(kind, models_cfg.models[n], rcfg=rcfg, cfg=cfg, prompt=prompt, smoke=args.smoke) for n in names]


def _smoke_task_ids() -> set[str]:
    raw = json.loads((REPO_ROOT / "data" / "manifest.json").read_text(encoding="utf-8"))
    return {str(e["question_id"]) for e in raw["tasks"][:SMOKE_TASKS]}


def _estimate(rcfg: RouterConfig, cfg: AgentConfig, traces_root: Path) -> int:
    models_cfg = load_models()
    jobs: list[Job] = []
    smoke_jobs: list[Job] = []
    for kind, names in (("self_consistency", rcfg.self_consistency.models), ("verifier", rcfg.verifier.models)):
        try:
            prompt = _prompts(kind, rcfg, cfg)
        except (PromptError, RunRefused, OSError) as exc:  # e.g. no verifier file yet: skip that kind
            print(f"{kind}: skipped ({exc})", file=sys.stderr)
            continue
        jobs += [plan_job(kind, models_cfg.models[n], rcfg=rcfg, cfg=cfg, prompt=prompt, smoke=False) for n in names]
        smoke_jobs += [plan_job(kind, models_cfg.models[n], rcfg=rcfg, cfg=cfg, prompt=prompt, smoke=True)
                       for n in SMOKE_MODELS]
    print(f"{'kind':<17} {'model':<12} {'samples':>7} {'baseline h':>10} {'r':>7} {'projected h':>11}  r from")
    for e in estimates(jobs, smoke_jobs, traces_root):
        base_h = e.baseline_ms / Decimal(3_600_000)
        r = "-" if e.ratio is None else f"{e.ratio:.3f}"
        proj = "-" if e.projected_hours is None else f"{e.projected_hours:.2f}"
        print(f"{e.kind:<17} {e.model:<12} {e.n_samples:>7} {base_h:>10.2f} {r:>7} {proj:>11}  {e.ratio_from or '-'}")
    return 0


class _NoLLM:
    def complete(self, req: Request, *, context: dict[str, str]) -> Response:
        raise AssertionError("--check never calls a model")


def _check_gates(jobs: Sequence[Job], *, prices: PriceTable, cfg: AgentConfig, db_root: Path, traces_root: Path,
                 env_lock: dict[str, str], num_parallel: str | None) -> int:
    """Start the MCP server and run every gate; no task is loaded and no model is called."""

    async def go() -> list[Gated]:
        async with server_session(db_root) as (_session, server_version):
            return gate(jobs, llm=_NoLLM(), prices=prices, cfg=cfg, server_version=server_version,
                        db_ids=db_ids_under(db_root), traces_root=traces_root, env_lock=env_lock,
                        num_parallel=num_parallel)

    try:
        gated = anyio.run(go)
    except BaseException as exc:
        leaf = single_leaf(exc)
        if isinstance(leaf, CompatError):
            print(f"refused: {leaf}", file=sys.stderr)
            return 2
        raise
    for g in gated:
        print(f"gate ok: {g.job.run_id} vs {g.job.baseline_run_id} (samples {g.meta.sample_idxs}, "
              f"T={g.meta.temperature}, seeds={g.meta.sample_seeds or g.meta.seed})")
    return 0


def _main(command: str, args: argparse.Namespace) -> int:
    start = time.monotonic()
    cfg = load_agent_config()
    rcfg = load_router_config()
    traces_root = _env_path("ESCALATOR_TRACES_DIR", DEFAULT_TRACES)
    if command == "estimate":
        return _estimate(rcfg, cfg, traces_root)
    kind = COMMANDS[command]
    try:
        jobs = plan_jobs(kind, args, rcfg, cfg)
    except RunRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    if kind == "verifier":
        holes = placeholder_fields(jobs[0].prompt) if jobs else []
        if holes:
            print(f"refused: {rcfg.verifier.prompt_version}.yaml still holds placeholders in {holes}; "
                  "the project owner writes the verifier prose before any verifier run", file=sys.stderr)
            return 2

    prices = PriceTable.load(DEFAULT_PRICES)
    modes: dict[str, CacheMode] = {"readwrite": "readwrite", "readonly": "readonly", "off": "off"}
    mode = os.environ.get("ESCALATOR_CACHE_MODE", "readwrite")
    if mode not in modes:
        print(f"ESCALATOR_CACHE_MODE={mode!r}: expected one of {sorted(modes)}", file=sys.stderr)
        return 2
    db_root = load_config().data_root / "bird"
    env_lock = {str(k): str(v) for k, v in json.loads(ENV_LOCK.read_text(encoding="utf-8")).items()}
    num_parallel = os.environ.get(NUM_PARALLEL_ENV) or None
    if args.check:
        return _check_gates(jobs, prices=prices, cfg=cfg, db_root=db_root, traces_root=traces_root,
                            env_lock=env_lock, num_parallel=num_parallel)
    task_ids = _smoke_task_ids() if args.smoke else None
    paid = set(paid_models([j.spec for j in jobs], prices))
    for job in jobs:
        if job.spec.name in paid:
            try:
                cost = f"${projected_cost(job, traces_root, task_ids)}"
            except RunNotFound as exc:
                cost = f"unknown ({exc})"
            print(f"{job.run_id}: paid model {job.spec.name}, projected cost {cost} "
                  f"({len(job.samples)} x the nominal usd of {job.baseline_run_id})")
    if paid and not args.paid and modes[mode] != "readonly":
        print(f"refused: paid model(s) {', '.join(sorted(paid))} need --paid (PAID=1); runs calling online "
              "models are only ever triggered by hand", file=sys.stderr)
        return 2
    if num_parallel is None and any(j.spec.provider == "ollama" for j in jobs):
        print(f"warning: {NUM_PARALLEL_ENV} is not set in the environment or .env; meta records null",
              file=sys.stderr)

    from escalator.eval.tasks import load_tasks
    from escalator.llm.adapter import Adapter
    from escalator.llm.models import check_ollama
    from escalator.llm.providers import ollama
    from escalator.llm.providers.base import LazyProvider, Provider

    all_tasks = load_tasks()
    tasks = [t for t in all_tasks if t.question_id in task_ids] if task_ids is not None else all_tasks
    models_cfg = load_models()
    info = OllamaInfo()
    tags = sorted({j.spec.tag for j in jobs if j.spec.provider == "ollama" and j.spec.tag is not None})

    def check(p: ollama.OllamaProvider) -> str:
        version = check_ollama(models_cfg, p)
        for tag in tags:
            info.params[tag] = p.show(tag)
        return version

    lazy_ollama = LazyProvider("ollama", ollama.OllamaProvider, ollama.normalize, check=check)
    info.version = lambda: lazy_ollama.server_version
    providers: dict[str, Provider] = {"ollama": lazy_ollama}
    if any(j.spec.provider == "anthropic" for j in jobs):
        from escalator.llm.providers import anthropic

        providers["anthropic"] = LazyProvider("anthropic", anthropic.AnthropicProvider, anthropic.normalize)
    try:
        cache = Cache(_env_path("ESCALATOR_CACHE_PATH", DEFAULT_CACHE), modes[mode])
    except CacheMiss as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 4
    cap = os.environ.get("ESCALATOR_MAX_USD_PER_RUN")
    max_usd = Decimal(cap) if cap else None

    async def go() -> list[JobReport]:
        async with server_session(db_root) as (session, server_version):
            return await collect(jobs, tasks, llm=Adapter(providers, cache, prices), prices=prices, cfg=cfg,
                                 session=session, server_version=server_version, db_ids=db_ids_under(db_root),
                                 traces_root=traces_root, overwrite=args.overwrite, env_lock=env_lock,
                                 num_parallel=num_parallel, ollama=info, max_usd=max_usd)

    try:
        reports = anyio.run(go)
    except BaseException as exc:  # re-raised below unless it is a planned stop
        leaf = single_leaf(exc)
        if isinstance(leaf, (RunRefused, CompatError)):
            print(f"refused: {leaf}", file=sys.stderr)
            return 2
        if isinstance(leaf, SpendCapExceeded):
            print(f"stopped: {leaf}", file=sys.stderr)
            return 3
        if isinstance(leaf, (ProviderSetupError, CacheMiss)):
            print(f"stopped: {leaf}", file=sys.stderr)
            return 4
        if leaf is not exc:
            raise leaf from exc
        raise
    finally:
        cache.close()
    for rep in reports:
        ends = dict(sorted(Counter(o.end for o in rep.outcomes).items()))
        nominal = sum((o.usd_nominal for o in rep.outcomes), Decimal(0))
        spent = sum((o.usd_spent for o in rep.outcomes), Decimal(0))
        latency = sum((r.latency_ms or 0) for o in rep.outcomes for r in o.records)
        print(f"{rep.job.run_id}: {len(rep.outcomes)} runs ends={ends} usd_nominal={nominal} usd_spent={spent} "
              f"llm_latency={latency / 3_600_000:.2f} h")
        if rep.provider_error_task_ids:
            print(f"  provider_error (re-run with OVERWRITE=1 to resume through the cache): "
                  f"{rep.provider_error_task_ids}", file=sys.stderr)
    print(f"done in {time.monotonic() - start:.1f} s")
    return 1 if any(r.provider_error_task_ids for r in reports) else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.eval.collect")
    p.add_argument("command", choices=[*COMMANDS, "estimate"])
    p.add_argument("--smoke", action="store_true",
                   help=f"the first {SMOKE_TASKS} manifest tasks, {', '.join(SMOKE_MODELS)} only")
    p.add_argument("--models", nargs="+", help="override the configured models (keys of configs/models.yaml)")
    p.add_argument("--paid", action="store_true", help="allow paid models (runs calling online models are manual)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing run (resumes through the cache)")
    p.add_argument("--check", action="store_true",
                   help="run the compatibility gates against a live MCP server and exit; no model is called")
    args = p.parse_args(argv)
    load_dotenv(REPO_ROOT / ".env", override=False)
    return _main(args.command, args)


if __name__ == "__main__":
    sys.exit(main())
