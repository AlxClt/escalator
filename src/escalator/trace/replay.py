"""Deterministic re-run of a task from its trace: rebuild the run from meta.json, re-run the loop
against a readonly cache and a live MCP server, and check that every request key reproduces.

A readonly cache never calls a provider: a key that is not cached raises CacheMiss, which is
reported as a mismatch at that step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from escalator.agent.loop import LoopConfig, RunContext, ToolSession, run_task
from escalator.agent.prompts import load_prompt
from escalator.agent.tools import RenderCaps, build_manifest, input_schemas, manifest_sha256
from escalator.eval.tasks import Task
from escalator.llm import cache as cache_mod
from escalator.llm.adapter import Adapter
from escalator.llm.cache import Cache
from escalator.llm.cost import PriceTable
from escalator.llm.errors import CacheMiss, LLMError
from escalator.llm.models import ModelSpec
from escalator.llm.providers.base import Provider, RawResponse
from escalator.llm.providers.ollama import split_model_id
from escalator.llm.types import Normalized, Request, Response
from escalator.trace.schema import META_FILE, STEPS_FILE, RunMeta, read_meta, read_steps


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeyMismatch:
    step: int
    expected: str | None  # None: the replay made more calls than the trace has
    got: str | None  # None: the replay stopped before this step


class _ParseOnly:
    """A provider that can normalize stored bodies but never calls out."""

    def __init__(self, inner: Provider) -> None:
        self.name = inner.name
        self._inner = inner

    def call(self, req: Request) -> RawResponse:
        raise LLMError("replay never calls a provider")

    def normalize(self, body: bytes, req: Request) -> Normalized:
        return self._inner.normalize(body, req)


def _parsers() -> dict[str, Provider]:
    import anthropic

    from escalator.llm.providers.anthropic import AnthropicProvider
    from escalator.llm.providers.ollama import OllamaProvider

    return {
        "ollama": _ParseOnly(OllamaProvider(base_url="http://replay.invalid")),
        "anthropic": _ParseOnly(AnthropicProvider(client=anthropic.Anthropic(api_key="replay-unused", max_retries=0))),
    }


class _KeyRecorder:
    def __init__(self, inner: Adapter) -> None:
        self.inner = inner
        self.keys: list[str] = []

    def complete(self, req: Request, *, context: dict[str, str]) -> Response:
        self.keys.append(cache_mod.key(req, context))
        return self.inner.complete(req, context=context)


def tier_spec(meta: RunMeta) -> ModelSpec:
    t = meta.tier_config
    if t.provider == "ollama":
        tag, digest = split_model_id(t.model_id)
        return ModelSpec(t.name, t.tier, t.provider, tag, digest, None, dict(t.settings))
    return ModelSpec(t.name, t.tier, t.provider, None, None, t.model_id, dict(t.settings))


def loop_config(meta: RunMeta) -> LoopConfig:
    return LoopConfig(max_steps=meta.max_steps, retry_cap=meta.retry_cap,
                      render=RenderCaps(meta.render_max_rows, meta.render_max_chars),
                      overflow_margin=meta.overflow_margin, max_tokens={meta.tier_config.tier: meta.max_tokens})


async def replay_task(run_dir: Path, task: Task, sample_idx: int, *, session: ToolSession, cache: Cache,
                      prices: PriceTable, db_ids: Sequence[str]) -> list[KeyMismatch]:
    """Re-run `task` as recorded in run_dir; the request keys that did not reproduce (empty: all did)."""
    if cache.mode != "readonly":
        raise ReplayError("replay needs a readonly cache: it must never call a provider")
    meta = read_meta(run_dir / META_FILE)
    expected = [r.request_key for r in read_steps(run_dir / STEPS_FILE)
                if r.task_id == task.question_id and r.sample_idx == sample_idx]
    if not expected:
        raise ReplayError(f"no trace records for task {task.question_id}, sample {sample_idx} in {run_dir}")
    prompt = load_prompt(meta.prompt_version)
    if prompt.sha256 != meta.prompt_sha256:
        raise ReplayError(f"prompt {meta.prompt_version} changed since the run (sha256 differs)")
    tools = build_manifest(db_ids, prompt)
    tool_sha = manifest_sha256(tools)
    if tool_sha != meta.tool_manifest_sha256:
        raise ReplayError("tool manifest differs from the run's (db set or descriptions changed)")
    recorder = _KeyRecorder(Adapter(_parsers(), cache, prices))
    run_ctx = RunContext(
        run_id=meta.run_id, llm=recorder, prompt=prompt, tools=tools, schemas=input_schemas(db_ids),
        cache_context={"tool_manifest_sha": tool_sha, "mcp_server_version": meta.mcp_server_version},
        config=loop_config(meta),
    )
    try:
        await run_task(task, tier_spec(meta), sample_idx=sample_idx, temperature=meta.temperature, seed=meta.seed,
                       session=session, run_ctx=run_ctx)
    except CacheMiss:
        pass  # the key was recorded before the lookup; the comparison below reports it
    got = recorder.keys
    out: list[KeyMismatch] = []
    for i in range(max(len(expected), len(got))):
        e = expected[i] if i < len(expected) else None
        g = got[i] if i < len(got) else None
        if e != g:
            out.append(KeyMismatch(i, e, g))
    return out
