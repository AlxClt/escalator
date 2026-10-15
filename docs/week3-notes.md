# Week 3 notes: signals, offline routing, headline plot

Spec: `docs/tasks/week3-router-task.md`. This file lists:
- every week-1/2 conflict found;
- every decision taken where the spec was silent;
- the measured collection wall-clock;
- the raw result tables (no interpretation).

## Phase 0 findings

| Question (spec §0.3) | Finding |
|---|---|
| "Submitted query errored" | Derivable from the trace alone. The server executes a submit, and the `tool_status` of the `submit_answer` call on the final record (the one that is neither `validation_error` nor `ignored`) is the SQL outcome: `execution_error`, `timeout`, `denied` or `ok` (week-2 C3). The same value is `TaskOutcome.submit_status`. The spec's `no_submit` is `no_tool_call` (week-2 C2); `trace.schema.FAILED_ENDS` lists the failure ends. |
| Label-free result comparison | Exists without a scorer change. `eval.scorers.execution_match(u, v, ordered=False)` is a pure function of two row lists (its `gold` parameter is only a name). It keeps the scorer's value normalisation (`values_equal`: NULL equals NULL, rel 1e-6 / abs 1e-9 tolerance, no text/number coercion), column-permutation handling and multiset row semantics. The scorer's order rule comes from `configs/scoring.yaml` by task id, never from gold SQL, and signals never use it (row order is always ignored). Tolerance is a pairwise predicate, used directly (D3). |
| Soft-F1 | Implemented (`scorers.soft_f1`), and already a per-task field of `results/baselines.json`. |
| Cache-key preimage | `canon.sha256({"v": 1, "req": Request.model_dump(mode="json"), "ctx": {"tool_manifest_sha", "mcp_server_version"}})`. `Request` has had a `sample_idx` field since week 1, so it is already part of every key. |
| Plotting library | None locked, and numpy neither. |

## Decisions taken with the project owner

| # | Decision | Why |
|---|---|---|
| B1 | **Four (small, frontier) pairs**: gemma4-e4b, gemma4-12b, gemma4-31b and haiku-5-5, each with sonnet-5-5. | The spec says "three pairs", but `self_consistency.models` defaults to all four tier-S models, haiku included. |
| B2 | The spend guard for paid collection (haiku SC/verifier, frontier-repeat) is `PAID=1` (`--paid`), as in week 2. | The spec asks for `PAID=1` in §1c and `ESCALATOR_CONFIRM_SPEND=1` in test 4. |
| B3 | No concurrency. Test 1 loses its "identical at concurrency 1 vs 4" part. | Week-2 decision A3: tasks run sequentially through one MCP session. |
| B4 | The verifier prose is written by the project owner before checkpoint A. The verifier smoke run happens alongside the SC smoke run, and the verifier runs belong to the full collection. | Owner's instruction on the plan. |

## Week-1/2 conflicts

| # | Conflict | Resolution |
|---|---|---|
| D1 | §1c asks to extend the cache-key preimage with `sample_idx` when it is non-zero. `sample_idx` has always been a `Request` field, so it is already hashed into every key (`test_cache_key.py` already asserted that `sample_idx` changes the key). | No key change; `CACHE_SCHEMA` stays 1. Test 3 is covered by a golden key computed at `e20e05d` (`test_week2_key_is_byte_identical`), the explicit sample-1 test, and `make test-local`, which replays the 10 smoke tasks of all five week-2 baselines against the real cache: every key reproduces. |
| D2 | Haiku accepts no sampling parameter, so SC sample `j = 0` would send exactly its baseline request and replay the baseline from the cache. | Anthropic SC samples use `sample_idx` 1..k; Ollama keeps 0..k−1 as specified. The router identifies a sample by its position, not by `sample_idx`. |
| D3 | Week-2 decision A1 removed placeholder detection; the verifier file needs it. | `PLACEHOLDER` (`<<PROMPT_PLACEHOLDER: ...>>`), `placeholder_fields` and `has_placeholders` in `agent/prompts`. `make verifier-runs` refuses while any field holds a marker. |
| D4 | `parse_prompt` requires `tool_descriptions`; the verifier file must not have them. | `load_prompt_with_tools(version, tools_from)` takes the tools from the baseline prompt. Its `sha256` is the verifier file's own hash; the tool manifest hash equals the baseline's (tested). `trace/replay.py` does not replay verifier runs (it loads prompts with `load_prompt`). |
| D5 | The spec's `manifest_sha256` (gate list) is `RunMeta.tool_manifest_sha256`; `RunMeta.manifest_sha256` is the `data/manifest.json` hash (week-2 C5). | The gate checks both under their real names. |
| D6 | The new meta fields do not fit `RunMeta` (`extra="forbid"`, `schema_version: Literal[2]`). | **RunMeta schema v3**: `run_kind`, `sample_seeds`, `baseline_run_id`, `ollama_modelfile_params`, `ollama_num_parallel`, all defaulted, so week-2 v2 files read as `run_kind = baseline`. `StepRecord` is unchanged (v2). |
| D7 | `OLLAMA_NUM_PARALLEL` is a setting of the Ollama server (RunPod), which the client cannot read. | Recorded from the client environment (`.env`) as declared by the operator; `null` with a warning when unset. |
| D8 | Week 2 never published traces: `traces/published/` does not exist. | `make publish-traces RUNS=...` copies `meta.json` and writes a deterministic `steps.jsonl.gz` (gzip mtime 0). Week-2 baseline runs are published the same way (copied; originals untouched). Readers look in `published/` first, then `traces/<run_id>/`. |
| D9 | The spec's gate list omits `max_tokens`, `cache_schema` and the price table, which change the agent, its keys or its costs. | They are in `MUST_MATCH` too. Every `RunMeta` field is in exactly one of `MUST_MATCH` / `MAY_DIFFER` (tested). |
| D10 | `configs_sha256` necessarily differs from the baselines', because `configs/router.yaml` lives under `configs/`. | It is in `MAY_DIFFER`. Every field that changes the agent is gated individually. |
| D11 | `make sc-samples` with the default model list includes haiku-5-5, a paid model. | It is refused without `PAID=1`, printing haiku's projected cost (3 × its baseline nominal usd = $0.7728525). Gemma only: `make sc-samples MODELS="gemma4-e4b gemma4-12b gemma4-31b"`. |

## Decisions where the spec was silent

- **Run ids:** `sc-<model>-<prompt_version>`, `verifier-<model>-<verifier_version>` and `frontier-repeat-<model>-<prompt_version>`; smoke runs insert `-smoke` after the kind (`sc-smoke-gemma4-e4b-v1`).
- **SC meta:** the shared temperature goes in `temperature`, `seed` is `null`, and the per-sample seeds go in `sample_seeds` (`{"0": 1, "1": 2, "2": 3}`).
- **Gate timing:** all jobs of an invocation are gated before the first LLM call; one failing job aborts them all. `CHECK=1` runs only the gates, against a live MCP server, without loading tasks or calling a model.
- **Modelfile parameters:** read with `/api/show` when the Ollama provider is first built (first cache miss), next to the version check. Like `ollama_version`, they are `null` for a fully cached run.
- **The error signal executes the submitted query** through the sandbox (`row_cap=None`, the same path as EX), as D4 says, rather than reading the server's `tool_status` (which ran with the 1000-row cap). On the four week-2 small baselines the two never disagree, and the signal equals `end != submitted or sql_error` in `results/baselines.json` on every task.
- **Persistent execution cache** (`.cache/exec.sqlite`, gitignored): each (db, SQL) outcome is stored the first time and reused, so a query near the 30 s limit cannot flip between invocations. The key salt is the `sources.lock` hash, the SQLite version and the sandbox timeout.
- **Integrity check scope:** each baseline run is checked on every task it was scored on in week 2 (all 200), not only on A.
- **Analysis set:** built from every run of the inventory, verifier and SC runs included, so one `provider_error` in any run removes the task from all four pairs.
- **SC samples are identified by position** (sorted `sample_idx`), so haiku's 1..3 and Gemma's 0..2 are handled alike.
- **Operational rates:** a call "ignored after submit" is an `ignored` call with a rendered result (`result_hash` set); an `ignored` call without one is a context-overflow call, and it counts as emitted. The execution-error and timeout rates are over validated, executed, non-submit calls.
- **Bootstrap:** a resample where a statistic is undefined (CPS with no solve, AUC with an empty class, headroom with a zero denominator) or infinite (the CPS ratio when the policy's CPS is 0) is dropped for that statistic and counted in `n_undefined`. A point value of `+∞` is written `"inf"` with a flag.
- **EX carries two intervals:** Wilson (`acc`, the protocol's interval for a proportion) and a bootstrap one (`acc_bootstrap`, from the shared matrix), used by the plots so both axes carry bootstrap intervals as the spec asks.
- **Number format:** floats to 10 significant digits (not decimals: an exact McNemar p reaches 1e-40), keys sorted, τ = −∞ written `"-inf"` with an exact `tau_exact` string ("1/3").
- **Random reference:** written at 21 points, e = 0, n/20, …, n.
- **Budget variant:** `router.policy.acc_at_budget(points, B)` is exposed and tested against every tie-break order; `router.json` does not tabulate budgets (the spec names no grid).
- **Plots** live in `src/escalator/router/plots.py` (no plotting existed in weeks 1–2; `notebooks/` does not exist). Files: `results/figures/frontier-<model>-{cps,cost}.{png,svg}`, plus `headline.{png,svg}` (a copy of the chosen model's CPS figure) once `headline_small_tier` is set.
- **New dependency:** `matplotlib==3.11.2`, in a `plots` dependency group. Nothing was locked for plotting and the spec requires PNG + SVG figures. The group is in `[tool.uv] default-groups`, so CI's unchanged `uv sync --locked` installs it for test 16. `make router` does not import it.
- **Colours:** the reference categorical palette's slots 1–3 (blue, orange, aqua), the set documented as passing the all-pairs colour-vision checks in light mode. The palette validator could not be run here (no Node.js). Each signal also has its own marker shape.
- **Haiku's error signal is constant** (0 on every task: all 200 baseline answers were submitted and executed). It has no non-degenerate point and an AUC of exactly 0.5.
- **Haiku's SC samples are drawn at provider-default sampling**, not T = 0.7 (the API rejects sampling parameters), so its self-consistency signal is not comparable 1:1 with the Gemma models'.
- **`make smoke` on native Windows:** 10 tasks from the cache, $0, second run 4.2 s wall (the week-2 run from WSL took 60 s). The contract now holds.

## Measured wall-clock

**Checkpoint A (15/10/2026):** smoke runs by the owner, `git_sha` `ca36d4f`, dirty (`verifier_v1.yaml` was not yet committed; the run's `prompt_sha256` `4e1111f5…` equals the committed file, `d034aa6`). Ollama 0.40.4 (the week-2 runs recorded 0.40.3; the version is not gated). The Modelfile parameters of `gemma4:e4b-it-qat` are `temperature 1, top_k 64, top_p 0.95`; only `temperature` and `seed` are sent.

| Smoke run | Runs | Ends | LLM latency | Steps / run | Median ms / call | Output tokens / call |
|---|---|---|---|---|---|---|
| baseline e4b, same 10 tasks (week 2) | 10 | — | 0.025 h | 3.50 | 2,017 | 138 |
| `sc-smoke-gemma4-e4b-v1` | 30 | 20 no_tool_call, 7 submitted, 3 max_steps | 0.256 h | 3.27 | 4,954 | 245 |
| `verifier-smoke-gemma4-e4b-verifier_v1` | 10 | 4 no_tool_call, 3 submitted, 3 max_steps | 0.072 h | 4.40 | 5,156 | 147 |

Both reruns from a readonly cache are all hits and give traces identical except for `cache_hit`.

Ratios `r` (smoke latency per sample ÷ baseline latency, same tasks): SC 3.399, verifier 2.853. The slowdown is per call, not in the number of steps. The median call latency is 2.5× the baseline's; at T = 0.7 the model writes about 1.8× more output tokens; and the first call paid a 58 s model load. Projected wall-clock (`samples × Σ baseline latency × r`, with e4b's `r` applied to every model):

| Model | SC (3 samples) | Verifier |
|---|---|---|
| gemma4-e4b | 3.26 h | 0.91 h |
| gemma4-12b | 5.80 h | 1.62 h |
| gemma4-31b | 7.72 h | 2.16 h |
| haiku-5-5 (paid, by hand) | 1.89 h, $0.77 | 0.53 h |

**Checkpoint B:** started by the owner on 15/10/2026 at about 08:35 UTC: `sc-gemma4-{e4b,12b,31b}-v1` in sequence, and `sc-haiku-5-5-v1` in parallel (paid). The runs record `git_sha` `d034aa6` with `git_dirty: true`. The uncommitted files were `eval/metrics.py`, its tests and this file; collection does not use any of them. The verifier runs (started 09:23 UTC for haiku and 09:39 UTC for Gemma, recording `git_sha` `54c2f77`, clean) were run alongside the SC runs. All runs finished on 15/10: Ollama 0.40.4 throughout, the same Modelfile parameters, and every run's records sorted.

| Run | Runs | Ends | `provider_error` | LLM latency (projected) | Nominal usd (projected) |
|---|---|---|---|---|---|
| `sc-gemma4-e4b-v1` | 600 | 455 no_tool_call, 123 submitted, 22 max_steps | — | 2.51 h (3.26) | 0 |
| `sc-gemma4-12b-v1` | 600 | 536 submitted, 64 max_steps | — | 1.67 h (5.80) | 0 |
| `sc-gemma4-31b-v1` | 600 | 592 submitted, 5 no_tool_call, 3 max_steps | — | 2.16 h (7.72) | 0 |
| `sc-haiku-5-5-v1` | 600 | 600 submitted | — | 0.59 h (1.89) | 0.8002 (0.7729) |
| `verifier-gemma4-e4b-verifier_v1` | 200 | 100 no_tool_call, 78 submitted, 21 max_steps, 1 provider_error | 1068 | 1.07 h (0.91) | 0 |
| `verifier-gemma4-12b-verifier_v1` | 200 | 154 submitted, 46 max_steps | — | 0.71 h (1.62) | 0 |
| `verifier-gemma4-31b-verifier_v1` | 200 | 198 submitted, 2 max_steps | — | 0.76 h (2.16) | 0 |
| `verifier-haiku-5-5-verifier_v1` | 200 | 200 submitted | — | 0.20 h (0.53) | 0.2738 (0.2576) |

- **The projections were pessimistic** for 12b, 31b and haiku: e4b's smoke ratio (inflated by a 58 s cold load and a slower pod that morning) was applied to every model.
- **The e4b runs start with cache hits:** 98 of `sc-gemma4-e4b-v1`'s records and 44 of the e4b verifier's are hits. They are the smoke runs' requests, identical by construction (same tasks, seeds and prompt).
- **Task 1068 (e4b verifier):** step 0's `execute_sql` hit the 30 s sandbox timeout, then the step-1 LLM call failed after its retries (`provider_error`, infra). It is resumed through the cache (`make verifier-runs MODELS=gemma4-e4b OVERWRITE=1`); otherwise the analysis set A excludes 1068 for every pair.
- **SC and verifier calls overlapped on the pod** from 09:39 to about 12:20 UTC: two client processes, while `OLLAMA_NUM_PARALLEL` is unknown (`null`). `latency_ms` is measured by the client, so in that window it includes any queueing behind the other process's calls. If the server batched the two streams (`OLLAMA_NUM_PARALLEL` > 1), the outputs in that window could also differ numerically from a call served alone; the compatibility gate does not see this. Latency metrics for these runs are upper bounds.

## Results

To be filled after `make router` on the collected runs.
