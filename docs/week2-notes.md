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

## Prompt v2 candidates (first smoke run, 09/10/2026)

Found by the error analysis of `traces/smoke-gemma4-e4b-v1/`, run with `gemma4:e4b-it-qat` and prompt v1 (EX 1/10). Every failure was the agent's: the harness, scorer and config checked out. v1 stays as it is, since a version is never edited after a run used it; these changes go into a new `prompts/v2.yaml`.

| # | Observed (tasks) | Candidate change for v2 | Expected effect |
|---|---|---|---|
| V1 | Answered in text instead of calling `submit_answer` (11, 45, 62). Task 45's query was EX-correct. | State that `submit_answer` is the only way to answer, and that an answer written as text is discarded. | Protocol: recovers tasks the agent had already solved. |
| V2 | Empty one-token reply right after a successful query (36, 48, 79). Task 79's query was EX-correct. | "When a query's result answers the question, your next call is `submit_answer` with that same query." | Protocol: same as V1. |
| V3 | Resent an identical failing query until `max_steps` (17 ×7, 25 ×6, 77 ×6). | "Never resend a query that failed: read the error message, find the right table or column in the schema, and change the query." | Frees the 6 of 8 steps lost to loops at T = 0. |
| V4 | Took a column from the wrong table: `CharterNum`, `FundingType`, `GSserved`, `Virtual` live in `schools`, not `frpm` (17, 25, 77, 79). Also left names with spaces unquoted, causing syntax errors (48, 77). | "Before using a column, check which table it belongs to in the schema and qualify it with that table's alias." Strengthen the quoting rule with an example such as `"School Name"`. | Fewer `no such column` errors and syntax errors. |
| V5 | Did not apply the evidence's formula or filter (48: denominator not restricted to Orange; 62: ×100 formula replaced by a stored fraction column). | "Apply the external knowledge literally: its formulas, values and filters take precedence over your own reading of the question." | SQL quality. |
| V6 | Returned fewer columns than asked (36: one administrator out of the three the evidence mentions). | "Return every value the question or the external knowledge asks for, including all of several items when there can be more than one." | SQL quality. |
| V7 | Ignored a value description that says which rows to exclude (11: rows with a NULL `School` are districts, not schools). | "Read the value descriptions in the schema: some say which rows do not describe the thing asked about." | SQL quality. Lowest priority: closest to fitting this database. |

**Measuring v2:**

- **Priority:** V1–V3 first. They are protocol fixes, and on these 10 tasks they alone would take EX from 1/10 to at most 3/10, because 45 and 79 were already solved. V4–V7 change how the SQL is written.
- **Overfitting risk:** the evidence is 10 tasks on one database (`california_schools`) with the smallest model. Measure v2 against v1 on the same smoke tasks, then on a wider sample, before adopting it, and keep the wording generic rather than naming these tables.
- **The frontier tier is affected too:** one prompt version is shared by all tiers, so v2 changes its behaviour as well and must be in place before the baselines.

## First `make smoke` (09/10/2026)

Run `smoke-gemma4-e4b-v1`: `gemma4:e4b-it-qat` on RunPod (Ollama 0.40.2), prompt v1, run from WSL.

**Contract:**

| Check | Result |
|---|---|
| 10 tasks, local model, through the MCP server, writes `results/smoke.json` | Pass. EX 1/10; ends: 6 `no_tool_call`, 3 `max_steps`, 1 `submitted`. |
| Trace well formed | Pass. 43 records sorted by `(task_id, step)`, `end` only on each task's last record, every `meta.json` field filled. |
| Second run all cache hits at $0 | Pass. Rerun with a readonly cache and `OLLAMA_BASE_URL` empty: 43/43 hits, no provider contacted, exit 0, `results/smoke.json` identical, steps identical except `cache_hit`, `meta.ollama_version` null. |
| Second run under 5 s | **Fail.** 77 s cold, 60 s warm. |

**Resolved:**

- **Overflow under-count risk:** did not occur. Prompt tokens grow with the whole history (4,291 at step 0, up to 6,075), so `prompt_eval_count` counts the full prompt, not only what Ollama's KV cache did not reuse.
- **Harness, scorer and config:** checked by the error analysis. All 9 failures were the agent's (see "Prompt v2 candidates"):
  - the schema card held every column the agent got wrong;
  - every SQL error message reached the model;
  - the three empty replies were Gemma's own output: `done_reason: stop`, 1 output token, no content, tool call or thinking;
  - EX matched gold for the one success (28) and failed on real differences for the near misses.

**Still open:**

- **The 5 s target.** The warm rerun spent 8 s of CPU in 60 s of wall time: it waits on WSL's 9P file access to `C:` (`/mnt/c`). Importing `mcp` alone took 11.6 s. A cached run also re-executes every tool call (33 queries) and the gold queries against the databases, because the trace must reproduce tool results. Next step: run the repo from the WSL filesystem (e.g. `~/escalator`) and re-measure.
- **Hidden output tokens.** The first call after an SQL error produces about 300 more output tokens than the visible tool call (task 17: 408 vs 124), with `think: false` and no `thinking` field in the body. Gemma may still be reasoning and Ollama dropping the text. Correctness is unaffected, but `tokens_out` and latency are inflated. Check before the baselines.
- **P6.** No malformed tool call came back as plain text in this run (0 validation failures). The text-only endings were a different thing: final answers written as text (11, 45, 62) and empty replies (36, 48, 79). They end as `no_tool_call`, as specified; prompt v2 (V1, V2) targets them.
- **Soft-F1 near misses.** Task 11 scores 0.755 for 7,806 predicted rows against 7,804 gold rows. Pairing rows by position after sorting makes the metric harsh on a few extra rows. That's the specified behaviour, not a bug; keep it in mind when reading soft-F1.
- **Benchmark label, for the week-4 review.** Task 25's evidence refers to `"District Name"` (a `frpm` column), while its gold query filters `schools.District`.
- **Publishing runs.** This run's `meta.json` has `git_dirty: true` (an uncommitted `.gitignore` edit). Commit before a run whose numbers will be published, so `git_sha` identifies the code exactly.
