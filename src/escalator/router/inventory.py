from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from escalator.datasets.config import REPO_ROOT
from escalator.router.config import RouterConfig
from escalator.trace.published import DEFAULT_TRACES, load_steps, run_dir
from escalator.trace.schema import META_FILE, End, RunKind, RunMeta, StepRecord, read_meta

DEFAULT_MANIFEST = REPO_ROOT / "data" / "manifest.json"
Role = Literal["baseline", "self_consistency", "verifier", "frontier_repeat"]


class InventoryError(ValueError):
    """A listed run is missing, incomplete, or not what its role says."""


@dataclass(frozen=True)
class BaselineSettings:
    """configs/agent.yaml values a run's role is checked against (passed as values: this module must
    not import eval.*, which reads gold)."""

    prompt_version: str
    seed: int


@dataclass(frozen=True)
class Run:
    """The trace records of one (run_id, task_id, sample_idx), in step order."""

    run_id: str
    task_id: str
    sample_idx: int
    records: tuple[StepRecord, ...]

    @property
    def end(self) -> End:
        end = self.records[-1].end
        if end is None:
            raise InventoryError(f"{self.run_id} task {self.task_id} sample {self.sample_idx}: no end on the final record")
        return end

    @property
    def cost(self) -> Decimal:
        """Σ nominal usd (cache hits carry the original call's usd)."""
        return sum((Decimal(r.usd) for r in self.records if r.usd is not None), Decimal(0))

    @property
    def tokens(self) -> int:
        return sum((r.tokens_in or 0) + (r.tokens_out or 0) for r in self.records)

    @property
    def latency_ms(self) -> int:
        return sum(r.latency_ms or 0 for r in self.records)

    @property
    def steps(self) -> int:
        """LLM calls in the run."""
        return len(self.records)


@dataclass(frozen=True)
class RunSet:
    run_id: str
    meta: RunMeta
    meta_sha256: str  # of meta.json's bytes, for router.json inputs
    runs: dict[str, tuple[Run, ...]]  # task_id -> its runs, sorted by sample_idx

    def single(self, task_id: str) -> Run:
        runs = self.runs[task_id]
        if len(runs) != 1:
            raise InventoryError(f"{self.run_id}: task {task_id} has {len(runs)} runs, expected 1")
        return runs[0]


@dataclass(frozen=True)
class SmallRunSets:
    """Everything one small model's signals may read: small-tier runs only."""

    model: str
    baseline: RunSet
    self_consistency: RunSet | None
    verifier: RunSet | None


@dataclass(frozen=True)
class FrontierRunSets:
    model: str
    baseline: RunSet
    repeat: RunSet | None


def group_runs(run_id: str, records: Iterable[StepRecord]) -> dict[str, tuple[Run, ...]]:
    by: dict[tuple[str, int], list[StepRecord]] = {}
    for r in records:
        if r.run_id != run_id:
            raise InventoryError(f"{run_id}: record of run {r.run_id}")
        by.setdefault((r.task_id, r.sample_idx), []).append(r)
    out: dict[str, list[Run]] = {}
    for (task_id, idx), recs in by.items():
        recs.sort(key=lambda r: r.step)
        if [r.step for r in recs] != list(range(len(recs))):
            raise InventoryError(f"{run_id} task {task_id} sample {idx}: steps are not 0..{len(recs) - 1}")
        out.setdefault(task_id, []).append(Run(run_id, task_id, idx, tuple(recs)))
    return {t: tuple(sorted(runs, key=lambda r: r.sample_idx)) for t, runs in out.items()}


def load_runset(run_id: str, traces_root: Path = DEFAULT_TRACES) -> RunSet:
    d = run_dir(run_id, traces_root)
    meta_bytes = (d / META_FILE).read_bytes()
    meta = read_meta(d / META_FILE)
    if meta.run_id != run_id:
        raise InventoryError(f"{d}: meta.json is for run {meta.run_id}")
    return RunSet(run_id, meta, hashlib.sha256(meta_bytes).hexdigest(), group_runs(run_id, load_steps(run_id, traces_root)))


# --- role validation ----------------------------------------------------------------------


def expected_samples(rs: RunSet, role: Role, rcfg: RouterConfig) -> list[int]:
    provider = rs.meta.tier_config.provider
    if role == "self_consistency":
        k = rcfg.self_consistency.k
        return list(range(k)) if provider == "ollama" else list(range(1, k + 1))
    return [1] if role == "frontier_repeat" else [0]


def check_role(rs: RunSet, role: Role, *, model: str, tier: Literal["S", "L"], rcfg: RouterConfig,
               agent: BaselineSettings, task_ids: Sequence[str]) -> None:
    m, problems = rs.meta, []
    kind: RunKind = role
    if m.run_kind != kind:
        problems.append(f"run_kind {m.run_kind} != {kind}")
    if m.tier_config.name != model or m.tier_config.tier != tier:
        problems.append(f"model {m.tier_config.name}/{m.tier_config.tier} != {model}/{tier}")
    want_prompt = rcfg.verifier.prompt_version if role == "verifier" else agent.prompt_version
    if m.prompt_version != want_prompt:
        problems.append(f"prompt_version {m.prompt_version} != {want_prompt}")
    samples = expected_samples(rs, role, rcfg)
    if m.sample_idxs != samples:
        problems.append(f"sample_idxs {m.sample_idxs} != {samples}")
    ollama = m.tier_config.provider == "ollama"
    if role == "self_consistency" and ollama:
        sc = rcfg.self_consistency
        if m.temperature != sc.temperature:
            problems.append(f"temperature {m.temperature} != {sc.temperature}")
        want_seeds = {str(j): s for j, s in enumerate(sc.seeds)}
        if m.sample_seeds != want_seeds:
            problems.append(f"sample_seeds {m.sample_seeds} != {want_seeds}")
    elif ollama:
        if (m.temperature, m.seed) != (0.0, agent.seed):
            problems.append(f"sampling T={m.temperature} seed={m.seed} != T=0.0 seed={agent.seed}")
    elif (m.temperature, m.seed) != (None, None):
        problems.append(f"an Anthropic run must send no sampling parameter, got T={m.temperature} seed={m.seed}")
    missing = sorted(set(task_ids) - set(rs.runs), key=int)
    extra = sorted(set(rs.runs) - set(task_ids), key=int)
    if missing or extra:
        problems.append(f"tasks: missing {missing[:10]}{'...' if len(missing) > 10 else ''}, extra {extra[:10]}")
    bad = [t for t, runs in rs.runs.items() if [r.sample_idx for r in runs] != samples]
    if bad:
        problems.append(f"tasks without exactly samples {samples}: {sorted(bad, key=int)[:10]}")
    if problems:
        raise InventoryError(f"{rs.run_id} as {role} of {model}: " + "; ".join(problems))


# --- loading ---------------------------------------------------------------------------------


def load_small_runs(model: str, rcfg: RouterConfig, agent: BaselineSettings, task_ids: Sequence[str],
                    traces_root: Path = DEFAULT_TRACES) -> SmallRunSets:
    entry = rcfg.small[model]

    def load(run_id: str | None, role: Role) -> RunSet | None:
        if run_id is None:
            return None
        rs = load_runset(run_id, traces_root)
        check_role(rs, role, model=model, tier="S", rcfg=rcfg, agent=agent, task_ids=task_ids)
        return rs

    base = load(entry.baseline, "baseline")
    assert base is not None
    return SmallRunSets(model, base, load(entry.self_consistency, "self_consistency"), load(entry.verifier, "verifier"))


def read_frontier_runs(rcfg: RouterConfig, agent: BaselineSettings, task_ids: Sequence[str],
                       traces_root: Path = DEFAULT_TRACES) -> FrontierRunSets:
    f = rcfg.frontier
    base = load_runset(f.baseline, traces_root)
    check_role(base, "baseline", model=f.model, tier="L", rcfg=rcfg, agent=agent, task_ids=task_ids)
    repeat = None
    if f.repeat is not None:
        repeat = load_runset(f.repeat, traces_root)
        check_role(repeat, "frontier_repeat", model=f.model, tier="L", rcfg=rcfg, agent=agent, task_ids=task_ids)
    return FrontierRunSets(f.model, base, repeat)


def all_runsets(small: Iterable[SmallRunSets], frontier: FrontierRunSets) -> list[RunSet]:
    out: list[RunSet] = []
    for s in small:
        out += [rs for rs in (s.baseline, s.self_consistency, s.verifier) if rs is not None]
    out += [rs for rs in (frontier.baseline, frontier.repeat) if rs is not None]
    return out


def analysis_set(task_ids: Sequence[str], runsets: Iterable[RunSet]) -> tuple[list[str], list[str]]:
    """(A, excluded): A = pinned tasks for which no listed run ended in provider_error; both sorted by
    task id."""
    excluded = {t for rs in runsets for t, runs in rs.runs.items() for r in runs if r.end == "provider_error"}
    ordered = sorted(task_ids, key=int)
    return [t for t in ordered if t not in excluded], [t for t in ordered if t in excluded]


def load_strata(manifest: Path = DEFAULT_MANIFEST) -> dict[str, str]:
    """task id -> difficulty (simple | moderate | challenging) of the pinned tasks."""
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    return {str(e["question_id"]): str(e["difficulty"]) for e in raw["tasks"]}
