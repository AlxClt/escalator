# Project specifications

## 1 Project requirements

### Description

Build a tool-using agent that answers natural-language questions over relational data by writing and executing SQL, then quantify the accuracy–cost frontier of a small-model-first escalation policy against local-only and frontier-only baselines on a fixed, pinned benchmark subset with execution-verified ground truth. The deliverable is not the agent. The deliverable is the measurement: a curve of execution accuracy against USD per solved task, an oracle upper bound on what any router could achieve, and an honest accounting of how much of that bound a realizable escalation signal actually captures.

The head articfact is one plot, at the top of the README, with at least four points on the accuracy/cost plane — local-only, frontier-only, routed at two thresholds — plus the oracle-routing frontier as a dashed upper bound.

### Objective

On a pinned 200-task subset of BIRD Mini-Dev, an escalation policy that runs a self-hosted small model first and escalates to a frontier model on an uncertainty signal achieves execution accuracy within X points of frontier-only at Y× lower cost per solved task.

The project succeeds if X and Y are measured with confidence intervals and the escalation signal's discriminative power is reported honestly, including the case where it turns out to be weak. A negative result, well measured, is a publishable portfolio outcome. A positive result with no CI is not.

Secondary objective. Characterize the gap between oracle routing — escalate exactly the tasks the small model gets wrong and the large model gets right — and realized routing, which escalates on an observable signal. That gap is the honest measure of the signal, and almost nobody reports it.

### Milestones

#### Step 0: Data

Write and execute the `make data`

#### Step 1: Infrastructure

Build in this order: cost meter → cache → adapter → trace schema → sandbox → MCP server → scorer → CI.

Manual work to be added: scorer unit test with 20 hand-made (predicted, gold) pairs covering column reordering, row reordering, NULL handling, float tolerance, empty results.

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
- MCP: mcp==2.0.x pinned, stdio, MCPServer plus the v2 Client
- Frontier adapter: Official anthropic SDK, hand-written adapter
- Small tier adapter: OpenAI-compatible HTTP via httpx
- Schemas / config: Pydantic v2 for trace records and configs, plain YAML
- Stats: scipy, numpy, sklearn.metrics.roc_auc_score
- Plots: matplotlib scripts reading results/*.json
- CI: GitHub Actions running make smoke in replay mode
- Packaging: Dockerfile pinning the Python and SQLite builds

### Technical specifications

- LLM adapter, cache, cost meter:
To be built first, before the agent. It is what makes the budget survivable: a re-run after a bug fix in the scorer costs $0 rather than $15. The cache key must include everything that changes the output. The cost table lives in configs/prices.yaml with a retrieved_on date, because prices move and an undated cost claim is meaningless.

- Sandbox
Read-only connection, statement timeout of 5s, result row cap of 1000, reject ATTACH and PRAGMA. Pin the SQLite/DuckDB version. Sort result sets before comparison, or use a set-comparison that is order-insensitive unless the question specifies ordering — getting this wrong silently destroys the accuracy numbers, so unit-test the scorer against 20 known pairs before any sweep.

- Agent loop
Max 8 steps. Tools validated against JSON schema; on validation failure, return a typed error message to the model and retry up to 2 times. Count those retries — tool-call validation failure rate is a reliability metric that will be used for step 4.

- Trace
One JSONL line per step: {task_id, run_id, step, tier, model, tool, args, result_hash, tokens_in, tokens_out, latency_ms, usd}. Everything downstream — metrics, routing simulation, failure taxonomy — reads traces, never live API calls. This decoupling is the single most important structural decision in the repo.

### Architecture

Architecture is described in [docs/architecture.md](docs/architecture.md)

### Agent tools

Four tools, exposed by the MCP server, with the same manifest for both tiers

- inspect_schema: Output sorted by table, then column position, so it serializes identically every call
- sample_rows: Deterministic: ORDER BY rowid LIMIT n, or by primary key for WITHOUT ROWID tables. No randomness, no stat
- execute_sql: Read-only connection, 5 s deadline via progress handler, ATTACH/PRAGMA denied by the authorizer, 1000-row fetch cap
- submit_answer: Terminal: the loop stops on a successful submit. Compile check with EXPLAIN only; the query is not executed at submit

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