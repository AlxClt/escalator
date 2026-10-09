"""ReAct-style agent loop over a hand-written MCP client session. No agent framework.

One step = one LLM call. The loop builds the same request for both tiers (provider wire
translation stays in llm/providers/), appends each assistant turn exactly as returned, executes
tool calls through the MCP session after client-side validation, and returns one StepRecord per
LLM call. Execution is sequential.

Termination (`end`, set on the final record only), checked in this order after each call:
provider_error (the call failed), context_overflow (small tier filled num_ctx; the turn's calls
are not executed), no_tool_call, submitted, validation_exhausted, max_steps.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

import mcp_types as types
from mcp.shared.exceptions import MCPError

from escalator.agent.prompts import Prompt
from escalator.agent.tools import (
    GET_SCHEMA,
    IGNORED_AFTER_SUBMIT,
    SUBMIT_ANSWER,
    HarnessError,
    RenderCaps,
    classify,
    error_text,
    render_result,
    validate,
)
from escalator.eval.tasks import Task
from escalator.llm import cache as cache_mod
from escalator.llm.errors import ProviderError
from escalator.llm.models import ModelSpec
from escalator.llm.types import JsonDict, Message, Request, Response, ToolCall, ToolSpec
from escalator.trace.schema import End, StepRecord, ToolStatus, preview
from escalator.util import canon


class ToolSession(Protocol):
    """The part of mcp ClientSession the loop uses; tests may stub it."""

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> types.CallToolResult: ...


class LLM(Protocol):
    """llm.adapter.Adapter; replay wraps it to record request keys."""

    def complete(self, req: Request, *, context: dict[str, str]) -> Response: ...


@dataclass(frozen=True)
class LoopConfig:
    max_steps: int = 8
    retry_cap: int = 2  # consecutive validation retries allowed; one more failure ends the task
    render: RenderCaps = field(default_factory=RenderCaps)
    overflow_margin: int = 1024
    max_tokens: Mapping[str, int] = field(default_factory=lambda: {"S": 2048, "L": 16000})  # by tier


@dataclass(frozen=True)
class RunContext:
    run_id: str
    llm: LLM
    prompt: Prompt
    tools: tuple[ToolSpec, ...]  # canonical manifest sent to the model
    schemas: Mapping[str, JsonDict]  # canonical input schemas, for validation
    cache_context: Mapping[str, str]  # tool_manifest_sha, mcp_server_version
    config: LoopConfig


@dataclass(frozen=True)
class TaskOutcome:
    task_id: str
    sample_idx: int
    model: str
    end: End
    submitted_sql: str | None  # the prediction; the scorer executes it
    submit_status: ToolStatus | None  # how the submitted query ran on the server
    records: tuple[StepRecord, ...]
    n_tool_calls: int
    n_validation_failures: int
    usd_nominal: Decimal
    usd_spent: Decimal  # nominal usd over non-hits


@dataclass(frozen=True)
class _CallResult:
    call: ToolCall
    status: ToolStatus
    shown: str | None  # what the model sees (or would see, for a submit); None when not executed


async def call_tool(session: ToolSession, name: str, args: JsonDict) -> types.CallToolResult:
    try:
        return await session.call_tool(name, dict(args))
    except MCPError as exc:
        raise HarnessError(f"server rejected an already-validated {name} call: {exc}") from exc


async def schema_card(session: ToolSession, db_id: str) -> str:
    """The card the server serves for get_schema: byte-identical to the one in the prompt."""
    status, text = classify(GET_SCHEMA, await call_tool(session, GET_SCHEMA, {"db_id": db_id}))
    if status != "ok":
        raise HarnessError(f"get_schema failed for {db_id}: {text[:500]}")
    return text


def overflowed(resp: Response, num_ctx: int, margin: int) -> bool:
    """Ollama truncates silently past num_ctx: end the task once the prompt reaches num_ctx - margin."""
    prompt, out = resp.usage.uncached_in, resp.usage.out
    if prompt is None:
        return False
    return prompt >= num_ctx - margin or prompt + (out or 0) >= num_ctx


def _record(run_ctx: RunContext, task: Task, tier: ModelSpec, sample_idx: int, step: int, resp: Response,
            results: list[_CallResult], end: End | None) -> StepRecord:
    return StepRecord(
        run_id=run_ctx.run_id, task_id=task.question_id, tier=tier.tier, model=tier.model_id, sample_idx=sample_idx,
        step=step, request_key=resp.request_key, cache_hit=resp.cache_hit, stop_reason=resp.stop_reason,
        tool=[r.call.name for r in results],
        args=[r.call.args for r in results],
        args_raw=[r.call.args_raw if r.call.args is None else None for r in results],
        n_tool_calls=len(results),
        tool_status=[r.status for r in results],
        result_hash=[canon.sha256(r.shown) if r.shown is not None else None for r in results],
        result_preview=[preview(r.shown) if r.shown is not None else None for r in results],
        text=resp.text, tokens_in=resp.usage.uncached_in, tokens_out=resp.usage.out,
        tokens_cache_read=resp.usage.cache_read, tokens_cache_write=resp.usage.cache_write,
        latency_ms=resp.latency_ms, usd=str(resp.usd), end=end,
    )


def _failed_record(run_ctx: RunContext, task: Task, tier: ModelSpec, sample_idx: int, step: int,
                   request_key: str) -> StepRecord:
    return StepRecord(
        run_id=run_ctx.run_id, task_id=task.question_id, tier=tier.tier, model=tier.model_id, sample_idx=sample_idx,
        step=step, request_key=request_key, cache_hit=False, stop_reason=None, tool=[], args=[], args_raw=[],
        n_tool_calls=0, tool_status=[], result_hash=[], result_preview=[], text="", tokens_in=None,
        tokens_out=None, tokens_cache_read=None, tokens_cache_write=None, latency_ms=None, usd=None,
        end="provider_error",
    )


def build_request(tier: ModelSpec, messages: list[Message], run_ctx: RunContext, *, sample_idx: int,
                  temperature: float | None, seed: int | None) -> Request:
    fields: dict[str, object] = {
        **tier.settings,
        "model": tier.model_id,
        "messages": tuple(messages),
        "tools": run_ctx.tools,
        "temperature": temperature,
        "seed": seed,
        "sample_idx": sample_idx,
        "max_tokens": run_ctx.config.max_tokens[tier.tier],
    }
    return Request.model_validate(fields)


async def run_task(task: Task, tier: ModelSpec, *, sample_idx: int, temperature: float | None, seed: int | None,
                   session: ToolSession, run_ctx: RunContext) -> TaskOutcome:
    """Run one task to termination. `temperature=None` sends no sampling parameter (frontier tier)."""
    cfg, prompt = run_ctx.config, run_ctx.prompt
    context = dict(run_ctx.cache_context)
    card = await schema_card(session, task.db_id)
    messages: list[Message] = [
        Message(role="system", content=prompt.system_text(db_id=task.db_id, schema_card=card)),
        Message(role="user", content=prompt.user_text(question=task.question, evidence=task.evidence)),
    ]
    num_ctx = tier.settings.get("num_ctx")
    records: list[StepRecord] = []
    end: End | None = None
    submitted_sql: str | None = None
    submit_status: ToolStatus | None = None
    consecutive_invalid = n_invalid = n_calls = 0

    step = 0
    while end is None:
        req = build_request(tier, messages, run_ctx, sample_idx=sample_idx, temperature=temperature, seed=seed)
        try:
            resp = run_ctx.llm.complete(req, context=context)
        except ProviderError:
            records.append(_failed_record(run_ctx, task, tier, sample_idx, step, cache_mod.key(req, context)))
            end = "provider_error"
            break
        n_calls += len(resp.tool_calls)

        results: list[_CallResult] = []
        if isinstance(num_ctx, int) and overflowed(resp, num_ctx, cfg.overflow_margin):
            results = [_CallResult(c, "ignored", None) for c in resp.tool_calls]
            end = "context_overflow"
        elif not resp.tool_calls:
            end = "no_tool_call"
        else:
            step_invalid = False
            for call in resp.tool_calls:
                if submitted_sql is not None:
                    results.append(_CallResult(call, "ignored", IGNORED_AFTER_SUBMIT))
                    continue
                error = validate(call, run_ctx.schemas)
                if error is not None:
                    step_invalid = True
                    n_invalid += 1
                    results.append(_CallResult(call, "validation_error", error_text(error)))
                    continue
                assert call.args is not None
                status, text = classify(call.name, await call_tool(session, call.name, call.args))
                results.append(_CallResult(call, status, render_result(call.name, status, text, cfg.render)))
                if call.name == SUBMIT_ANSWER:
                    submitted_sql, submit_status = str(call.args["sql"]), status
            consecutive_invalid = consecutive_invalid + 1 if step_invalid else 0
            if submitted_sql is not None:
                end = "submitted"
            elif consecutive_invalid > cfg.retry_cap:
                end = "validation_exhausted"
            elif step == cfg.max_steps - 1:
                end = "max_steps"

        records.append(_record(run_ctx, task, tier, sample_idx, step, resp, results, end))
        if end is None:
            messages.append(resp.as_message())
            for r in results:
                assert r.shown is not None
                messages.append(Message(role="tool", content=r.shown, tool_call_id=r.call.id))
            step += 1

    return TaskOutcome(
        task_id=task.question_id, sample_idx=sample_idx, model=tier.model_id, end=end,
        submitted_sql=submitted_sql, submit_status=submit_status, records=tuple(records), n_tool_calls=n_calls,
        n_validation_failures=n_invalid,
        usd_nominal=sum((Decimal(r.usd) for r in records if r.usd is not None), Decimal(0)),
        usd_spent=sum((Decimal(r.usd) for r in records if r.usd is not None and not r.cache_hit), Decimal(0)),
    )
