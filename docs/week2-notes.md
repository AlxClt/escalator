# Week 2 notes: agent loop, tools, prompts v1, runner

Spec: `docs/tasks/week2-agent-loop-task.md`. This file lists every week-1 conflict found and every decision taken where the spec was silent. Where week-1 naming differed from the spec, the implemented name was kept.

## Decisions taken with the project owner

| # | Decision | Why |
|---|---|---|
| A1 | `prompts/v1.yaml` holds short, complete real text. There is no `has_placeholders` flag and no sentinel detection. | The spec contradicted itself (placeholders only vs. "simple but complete"). The placeholder mechanism is obsolete. |
| A2 | Paid guard: the runner refuses any model with a non-zero price in `configs/prices.yaml` unless `--paid` is passed (`make baselines PAID=1`). It checks before any work: no task load, no server, no LLM call. | Frontier runs are manual only. |
| A3 | No concurrency. Tasks run sequentially in task-id order through one MCP session, so traces are written in `(task_id, sample_idx, step)` order directly. Spec test 16 is dropped. | Simpler. `llm/cache.py` keeps its single-threaded connection. |
| A4 | A `{{db_id}}` slot was added to the `system` template. | The model must pass `db_id` to every tool, and no spec slot (nor the schema card) told it which database the task is on. |
| A5 | Context overflow is detected in the loop, not in the Ollama provider. `OllamaProvider.normalize` no longer raises, and `ContextOverflow` was removed. | A raise inside `normalize` leaves no `Response`, so the trace record would lack tokens, latency and `cache_hit`. |
| A6 | A cached rerun needs no live provider. Providers are built, and Ollama's pinned digests checked (`check_ollama`), only at the first cache miss (`LazyProvider`). Cached bodies are parsed by the module-level `normalize` of each provider. | Hits are safe without the check: the cache key holds the exact model id, Ollama digest included, and every entry was written after a check in its process. |
| A7 | In readonly cache mode the paid guard lets paid models through. | A readonly cache raises `CacheMiss` before any provider call, so the rerun costs $0 and needs no API key. |

## Week-1 conflicts

| # | Conflict | Resolution |
|---|---|---|
| C1 | `trace/replay.py`, `eval/runner.py` and `make smoke` did not exist (the spec says week 1 is done). CI runs `make test`, not `make smoke`. | Built all three. CI unchanged (decision P1: smoke stays local). |
| C2 | `End` has `no_tool_call` where the spec says `no_submit`, and lacks `validation_exhausted` and `context_overflow`. | Kept `no_tool_call`; added the two values. |
| C3 | `ToolStatus` (`ok, validation_error, execution_error, timeout, denied`) has no `submitted`, and no value for calls that are not executed. | Server `forbidden` → `denied`; `sql_error` and `unknown_table` → `execution_error`; `timeout` → `timeout`. A submit is executed on the server and its status is the SQL outcome, so "submitted query that errors" reads from the trace. Added `ignored`. |
| C4 | Scalar `tool/args/args_raw/tool_status/result_hash/result_preview` cannot describe several calls per response. | **Trace `SCHEMA_VERSION` 2:** the six fields are lists in call order, of length `n_tool_calls`. `stop_reason`, token counts, `latency_ms` and `usd` are nullable, for the `provider_error` record (no response). No v1 traces existed. `test_trace_io.py` and `test_rerun_is_free.py` were moved to the v2 shape. |
| C5 | `RunMeta.manifest_sha256` already means the `data/manifest.json` hash. Most spec meta fields were missing. | Kept it. Added `schema_version, tier_config, prompt_sha256, tool_manifest_sha256, mcp_server_version, mcp_sdk_version, temperature, seed, sample_idxs, max_steps, retry_cap, render_max_rows, render_max_chars, overflow_margin, max_tokens`. |
| C6 | The Anthropic provider always sends `disable_parallel_tool_use: true` (week-1 "single tool call per turn"). The spec says to set no parallel flags. | Kept week 1. The loop still handles several calls per response the same way for both tiers, because Ollama can return several. |
| C7 | `Task` had no `question` or `evidence`. | Added both, with defaults, filled by `load_tasks`. Scoring is unchanged. |
| C8 | No `TierConfig`. | `llm.models.ModelSpec` is used. |
| C9 | The spec asks for configurable concurrency. | Dropped (A3). |
| C10 | The server's `db_id` enum depends on `--db-root`. | The client's canonical schemas take the same sorted `.sqlite` stems, so the manifest hash differs between the fixture and the real databases, as intended. |
| C11 | `jsonschema` was only a transitive dependency (through `mcp`). | Declared `jsonschema==4.26.0`, the version already locked. No new package. |

## Decisions where the spec was silent

- **Validation counter counts steps.** A step (LLM call) with any invalid call increments the consecutive-failure counter, and a step whose calls are all valid resets it. `n_validation_failures` counts every invalid call. Valid calls in a step that also holds an invalid call are still executed.
- **End precedence** after each call: `provider_error` → `context_overflow` (the turn's calls are recorded as `ignored`, not executed) → `no_tool_call` → `submitted` → `validation_exhausted` → `max_steps`.
- **Overflow condition:** `prompt_tokens ≥ num_ctx − overflow_margin` (margin 1024, `configs/agent.yaml`) or `prompt + output ≥ num_ctx` (the week-1 condition, kept).
- **Submit:** the first valid `submit_answer` is executed on the server; calls after it get `ignored_after_submit`. An invalid submit is not a submit.
- **Rendering:** result sets (`execute_sql`, `sample_rows`, `submit_answer`) are re-serialized as canonical JSON `{columns, rows, n_rows, n_rows_is_lower_bound, rows_shown, truncated}`. Rows are capped at 50, then dropped from the end until the text fits in 4000 chars. Counts and truncation are fields, not prose, so all natural-language text stays in the prompt file. `get_schema` output and error objects pass through uncapped: capping the card would hide schema.
- **Validation error content:** `{"error_type": "validation_error", "tool", "detail": {"reason": "unparsable_arguments" | "unknown_tool" | "schema", "errors": [{path, message}] sorted}}`, canonical JSON.
- **Harness errors:** `unknown_db`, any non-`{error, message}` error body, or a JSON-RPC error on a validated call raises `HarnessError`. There is no retry, and it is not scored.
- **Schema card:** fetched with `get_schema` through the session at task start, so it is byte-identical to what the tool serves.
- **Cache context:** `{"tool_manifest_sha", "mcp_server_version"}`. The key name `tool_manifest_sha` is the one week 1 already used.
- **`max_tokens` per tier:** 2048 for the small tier, 16000 for the frontier tier (room for `between_tools` thinking).
- **Sampling:** the small tier runs at `T = 0` with seed 20261001. The frontier tier sends neither temperature nor seed.
- **Run directory:** `traces/<preset>-<model>-<prompt_version>/`. An existing run is refused without `--overwrite` (`make smoke` passes it; `make baselines OVERWRITE=1`).
- **Spend cap:** `ESCALATOR_MAX_USD_PER_RUN` (`.env.example`) stops a run once actual spend exceeds it, after the current task (exit 3).
- **Lazy setup failures:** a failure while building a provider or in its check (missing env var, unreachable pod, stale digest) raises `ProviderSetupError`, which is not a `ProviderError`. It aborts the run (exit 4) instead of ending each task as `provider_error`. It surfaces at the first cache miss; tasks before it are valid hits and stay in the trace.
- **`RunMeta.ollama_version`:** the version of the Ollama server that served this run's misses, `null` when every call was a hit. `meta.json` is written at the start and rewritten at the end of each model's run.
- **Readonly cache miss:** `CacheMiss` (or a missing readonly cache file) stops the run with exit 4, naming the task.
- **Smoke tasks:** the first 10 manifest tasks in manifest order (ids 11, 17, 25, 28, 36, 45, 48, 62, 77, 79).
- **Fixture DB:** built from `tests/fixtures/agent_db.sql` at test time, not committed as a binary. Tests share one real MCP server subprocess through an anyio blocking portal.

## Open: verify during the first `make smoke`

- **Not yet run.** The Ollama endpoint in `.env` (RunPod) was down while this was built, so the `make smoke` contract (10 tasks, `results/smoke.json`, second run under 5 s at $0) is unverified.
- **The 5 s target under WSL on `/mnt/c` looks out of reach.** Measured with a warm OS cache:
  - runner imports: 4.9 s (`mcp` alone ≈ 3 s);
  - server subprocess import: 4.4 s;
  - re-running the 10 smoke golds: 3.5 s.

  The audit shows those golds take tens of ms, so the cost is 9P file I/O, not SQL. Running from the WSL filesystem (or natively) should remove most of it.
- **Overflow under-count risk:** Ollama's `prompt_eval_count` may exclude prompt tokens reused from its KV cache. If so, overflow detection under-counts. Compare it with the expected prompt size on the first smoke.
- **P6 (still open):** if Gemma returns malformed tool calls as plain text, they currently end the task as `no_tool_call`, not as validation failures.
