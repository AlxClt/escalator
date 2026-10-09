# Project specifications

## 1 Project requirements

### Description

Build a tool-using agent that answers natural-language questions over relational data by writing and executing SQL, then quantify the accuracy–cost frontier of a small-model-first escalation policy against local-only and frontier-only baselines on a fixed, pinned benchmark subset with execution-verified ground truth. The deliverable is not the agent. The deliverable is the measurement: a curve of execution accuracy against USD per solved task, an oracle upper bound on what any router could achieve, and an honest accounting of how much of that bound a realizable escalation signal actually captures.

The head articfact is one plot, at the top of the README, with at least four points on the accuracy/cost plane — local-only, frontier-only, routed at two thresholds — plus the oracle-routing frontier as a dashed upper bound.

### Objective

On a pinned 200-task subset of Arcwise-Plat-Full (corrected version of BIRD Mini-Dev), an escalation policy that runs a self-hosted small model first and escalates to a frontier model on an uncertainty signal achieves execution accuracy within X points of frontier-only at Y× lower cost per solved task.

The project succeeds if X and Y are measured with confidence intervals and the escalation signal's discriminative power is reported honestly, including the case where it turns out to be weak. A negative result, well measured, is a publishable portfolio outcome. A positive result with no CI is not.

Secondary objective. Characterize the gap between oracle routing — escalate exactly the tasks the small model gets wrong and the large model gets right — and realized routing, which escalates on an observable signal. That gap is the honest measure of the signal, and almost nobody reports it.

### Milestones

#### Step 0: Data

Write and execute the `make data`

#### Step 1: Infrastructure

Build in this order: cost meter → cache → adapter → trace schema → sandbox → MCP server → scorer → CI.

**Exit criterion**
`make smoke` runs 10 tasks end-to-end on a local model through the MCP server, emits results/smoke.json, and a second invocation completes in under 5 seconds at $0 from cache. CI runs `make smoke` from cache on push.

#### Step 2: Agent and baselines

Agent loop, tools, prompts v1. Local-only baseline across 3 small models on the pinned 200.

Manual trigger only: One frontier sweep.

**Exit criterion**
`results/baselines.json` committed, containing per-task outcomes and costs for all four model configurations. Two or more points on the accuracy/cost plane. Traces for every failed task stored.

#### Step 3: The router

Implement the three signals. Simulate all routing policies offline over step 2's traces. Sweep τ. Compute the oracle frontier. Compute signal AUC. Compute negative-transfer rate.

If the self-consistency signal needs k = 3 small-tier samples you did not collect in step 2, collect them now; it is local and free.

**Exit criterion**
The headline plot exists and is committed. You know, numerically, whether your router works.

#### Step 4: depth and ship

Manual work:

- Hard slice: authoring it, running all policies over it, reporting separately.
- Hand-labelling ~100 failed trajectories into a taxonomy: wrong table, wrong join, wrong aggregate, hallucinated column, premature submit, step-limit exhaustion, execution error unrecovered, correct-but-scored-wrong by hand.
- Noting how many failures are actually benchmark label errors. Given the documented annotation-error rate in BIRD, it is likely to find some. Reporting that number is a strong signal.

README, plots, limitations section. Pinning the repo.

**Exit criterion**
A stranger can run make results and reproduce your table.

#### Out of scope

- No UI. No Streamlit, no Chainlit.
- No RAG, no vector store, no embeddings.
- No fine-tuning.
- No provider-side tool execution, including API-hosted MCP connectors. - Both tiers run tools through the same client in the loop.
- No multi-agent crew. A verifier pass is a tool call, not an agent.
- No new benchmark. It is consuming one, not building one.
- No attempt at SOTA. The objective is measuring a trade-off, not climbing a leaderboard.

## Engineering design

### Tech stack

- Runtime: Python 3.12, uv with a committed lockfile, ruff, pyright, pytest
- Execution DB: SQLite
- Trace analytics: DuckDB over JSONL traces
- MCP: mcp==2.3.0 pinned, stdio, low-level Server with hand-written input schemas, plus the v2 Client
- Frontier adapter: Official anthropic SDK, hand-written adapter
- Small tier adapter: Ollama native API (`/api/chat`) via httpx
- Schemas / config: Pydantic v2 for trace records and configs, plain YAML
- Stats: scipy, numpy, sklearn.metrics.roc_auc_score
- Plots: matplotlib scripts reading results/*.json
- CI: GitHub Actions running make smoke in replay mode
- Packaging: Dockerfile pinning the Python and SQLite builds

### Technical specifications

- LLM adapter, cache, cost meter:
To be built first, before the agent. It is what makes the budget survivable: a re-run after a bug fix in the scorer costs $0 rather than $15. The cache key must include everything that changes the output. The cost table lives in configs/prices.yaml with a retrieved_on date, because prices move and an undated cost claim is meaningless.

- Sandbox
Read-only connection, statement timeout of 30s, result row cap of 1000, reject ATTACH and PRAGMA (authorizer allowlist: SELECT, READ, FUNCTION, RECURSIVE). Reject non-deterministic SQL (`random`, `randomblob`, `now`, `CURRENT_DATE/TIME/TIMESTAMP`) so tool output never depends on chance or the run date. Pin the SQLite/DuckDB version. Sort result sets before comparison, or use a set-comparison that is order-insensitive unless the question specifies ordering — getting this wrong silently destroys the accuracy numbers, so unit-test the scorer against 20 known pairs before any sweep.

- Agent loop
Max 8 steps. Tools validated against JSON schema, in the client only (`agent/tools.py`, against the manifest from `tools/list`); the server never re-validates argument shape. On validation failure, return a typed error message to the model and retry up to 2 times. Count those retries — tool-call validation failure rate is a reliability metric that will be used for step 4.

- Trace
One JSONL line per step: {task_id, run_id, step, tier, model, tool, args, result_hash, tokens_in, tokens_out, latency_ms, usd}. Everything downstream — metrics, routing simulation, failure taxonomy — reads traces, never live API calls. This decoupling is the single most important structural decision in the repo.

### Architecture

Architecture is described in [docs/architecture.md](docs/architecture.md)

### Agent tools

Four tools, exposed by the MCP server, with the same manifest for both tiers. The server is stateless: no scoring, no gold or manifest access. Spec: [docs/tasks/mcp-server.md](docs/tasks/mcp-server.md)

- get_schema(db_id, tables?): the schema card from `env/schema.py`, byte-identical to the one in the prompt. Tables sorted by name, columns in declaration order, with type, PK/FK markers and the effective BIRD/Arcwise descriptions
- sample_rows(db_id, table): Deterministic: first 5 rows ORDER BY rowid, or by primary key for WITHOUT ROWID tables. No randomness, no state
- execute_sql(db_id, sql): runs through the sandbox: read-only connection, 30s deadline via progress handler, authorizer allowlist, 1000-row fetch cap
- submit_answer(db_id, sql): identical to execute_sql. The loop treats a successful submit as terminal; the server has no notion of termination

Results are one compact JSON text: `{columns, n_rows, n_rows_is_lower_bound, rows}`, with at most 50 preview rows, `n_rows` counted up to 1000, text cells clipped at 200 characters and blobs shown as `<blob:N bytes>`. Errors are `isError` results `{error, message}` with `error` one of `sql_error | timeout | forbidden | unknown_table | unknown_db`. `SERVER_VERSION` in `env/server.py` is bumped whenever tools, output format or limits change, and is hashed into the client's LLM cache key.

## Metrics

### Primary metrics

- Execution accuracy (EX), per policy, with paired CIs
- USD per task and USD per solved task
- Escalation rate

### Secondary

In this section, Per-task escalation gain is δi = aiL − aiS ∈ {−1, 0, +1}.

- Oracle-routing accuracy at matched cost (upper bound), realized accuracy, and the gap
- Negative-transfer rate (small model was right and large mmodel wrong).
- Signal AUC against 𝟙[δi = +1] where δi is the per task escalation gain: δi = aiL − aiS with aiL and aiS the large and small model correctness (0 or 1)
- Accuracy by BIRD difficulty stratum, and escalation rate by stratum.

### Operational

- Steps per task, tool-validation failure rate, execution-error rate, timeout rate, wall-clock latency per task
- Soft-F1 as a secondary correctness metric. It measures similarity between predicted and gold result tables and is less strict than EX, so a large EX/soft-F1 gap localizes near-misses.


### External references and sources

- Original BIRD benchmark: https://arxiv.org/abs/2305.03111
- **ReViSQL** (Zhu, Jin, Choi, Kang, arXiv:2603.20004, 2026) — establishes the cost-Pareto framing for text-to-SQL; the 30B-A3B instantiation matches prior SOTA at 7.5× lower per-query cost. This
  repo is aligned with that framing, not novel against it.
- **Arcwise-Plat-SQL / Arcwise-Plat-Full** (Jin et al., 2026) — expert-verified BIRD Mini-Dev variants and the annotation-error analysis behind them. Paper: https://arxiv.org/html/2601.08778v3
- Corrected BIRD benchmark data source: https://github.com/uiuc-kang-lab/text_to_sql_benchmarks