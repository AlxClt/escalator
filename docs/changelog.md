# Changelog

All notable changes to Escalator willb be documented in this file

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Added

- `make data`, `make data-lock`, `make data-verify` and `make env-lock`: fetch Arcwise-Plat-Full, its description `schemas/` and the BIRD Mini-Dev SQLite bundle at the versions pinned in `data/sources.lock`, then verify them offline (gates V, D, M, T, S) and write audits to `data/audit/`.
- `data/env.lock` pins the Python and SQLite versions data is verified on; `.python-version` pins Python 3.12.14 and every target runs through `uv run --locked`.
- Gold-execution check (T5) with a cache in `data/raw/_cache/gold_check.json`; `NOCACHE=1` forces re-execution. `gold_timeout_s` is set to 2089 s by the gold-timeout rule.
- `escalator.datasets.descriptions.effective_descriptions(db_id)`: BIRD description files overlaid with Arcwise's.
- CI workflow running `make test` (hermetic: the dataset is installed locally with `make data`, never in CI).
- `make manifest`: pins the 200-task evaluation subset in `data/manifest.json` (seed 20261001 and hand-pinned exclusions in `configs/manifest.yaml`, difficulty-stratified sample, per-task `task_hash` and `gold_result_hash`).
- Cost meter (`escalator.llm.cost`, `configs/prices.yaml`): normalized usage in four token buckets to exact `Decimal` USD; unknown models raise `PriceMissing`, prices must be quoted strings, and any provider prompt-cache usage raises `UnexpectedCacheUsage`.
- Response cache (`escalator.llm.cache`): SQLite at `.cache/llm.sqlite` keyed by the hash of the whole request plus context, storing raw provider bodies; modes `readwrite`, `readonly` (a miss raises `CacheMiss`) and `off`.
- LLM adapter (`escalator.llm.adapter.Adapter.complete`) with Ollama (native `/api/chat`) and Anthropic (official SDK) providers, retries with backoff on 429/5xx/timeouts, and pinned tiers in `configs/models.yaml`.
- `python -m escalator.llm pin` writes the full Ollama digests into `configs/models.yaml` and `configs/prices.yaml`; `python -m escalator.llm ping <model>` runs one prompt twice to show the cache hit (the frontier tier requires `--paid`).
- Trace schema (`escalator.trace.schema`): `StepRecord` lines in `traces/<run_id>/steps.jsonl` and a `RunMeta` header; the reader is strict and reports a crashed run as `TruncatedTrace`.
- SQLite sandbox (`escalator.env.sandbox.Sandbox.execute`): read-only immutable connection per call, authorizer allowlist, one statement, 30 s wall-clock limit, 1000-row cap.
- MCP server (`python -m escalator.env.server --db-root data/raw/bird`, `mcp==2.3.0` low-level `Server`, stdio): tools `get_schema`, `sample_rows`, `execute_sql` and `submit_answer` returning compact JSON (50-row preview, 1000-row count, 200-char cells), errors `sql_error | timeout | forbidden | unknown_table | unknown_db`; `SERVER_VERSION` versions the tool contract.
- Schema card (`escalator.env.schema.render_card`): tables sorted by name, columns in declaration order with type, PK/FK markers and the effective BIRD/Arcwise descriptions.
- `scripts/mcp_overhead.py`: per-call MCP overhead against direct sandbox calls.
- Scorer (`escalator.eval.scorers.score`): execution accuracy with permutation-invariant columns, rows compared as a multiset except for the hand-checked `ordered_tasks` in `configs/scoring.yaml` (compared in order, except within their pinned tie blocks), numeric tolerance (`REL_TOL = 1e-6`, `ABS_TOL = 1e-9`), NULL equal to NULL and no text/number coercion; BIRD Mini-Dev soft-F1 as the secondary metric.
- Gold side of scoring (`escalator.eval.tasks`): `load_tasks` reads `data/manifest.json` and checks each `task_hash`; `score_sql` re-executes the gold query through the sandbox on every call and raises `GoldMismatch` if it no longer matches `gold_result_hash`.
- Agent loop (`escalator.agent.loop.run_task`): ReAct-style over the MCP session, at most 8 LLM calls, validation retries capped at 2 consecutive, ends `submitted | max_steps | no_tool_call | validation_exhausted | context_overflow | provider_error`; assistant turns are appended verbatim (thinking blocks included).
- Client tool layer (`escalator.agent.tools`): canonical tool manifest and `manifest_sha256`, a startup check of the server's tools, `jsonschema` validation with typed errors returned to the model, result classification, and display caps (50 rows, 4000 chars) from `configs/agent.yaml`.
- Prompt version v1 (`src/escalator/agent/prompts/v1.yaml`) with a slot renderer (`{{db_id}}`, `{{schema_card}}`, `{{question}}`, `{{evidence_section}}`, `{{evidence}}`); one file per version, shared by every tier.
- Runner (`python -m escalator.eval.runner {smoke,baselines}`, `make smoke`, `make baselines`): one MCP server per run, sequential tasks, traces under `traces/<preset>-<model>-<prompt_version>/`, scored results in `results/<preset>.json`; paid models are refused without `--paid` (`PAID=1`) unless `ESCALATOR_CACHE_MODE=readonly`, `provider_error` tasks are excluded from scoring and exit 1, and `ESCALATOR_MAX_USD_PER_RUN` stops a run. Providers are built, and the Ollama digests checked, only at the first cache miss (`LazyProvider`), so a cached rerun needs no live provider, no `OLLAMA_BASE_URL` and no API key; a setup failure (`ProviderSetupError`) or a readonly cache miss exits 4.
- Trace replay (`escalator.trace.replay.replay_task`): re-runs a task from its trace against a readonly cache and reports any request key that does not reproduce.
- `configs/agent.yaml`: prompt version, step budget, retry cap, display caps, overflow margin, per-tier `max_tokens`, seed and run presets.

### Changed

- The sandbox denies non-deterministic SQL: `random()`, `randomblob()`, `now` and `CURRENT_DATE/TIME/TIMESTAMP`.
- `.env.example`: `OLLAMA_BASE_URL` (required, no default, so Ollama can run on another machine) replaces `SMALL_TIER_BASE_URL` and `SMALL_TIER_API_KEY`; `python -m escalator.llm` loads `.env`; `ESCALATOR_CACHE_MODE` takes `readwrite`, `readonly` or `off`.
- `traces/` is gitignored except `traces/published/`.
- **BREAKING:** trace `SCHEMA_VERSION` 2: `tool`, `args`, `args_raw`, `tool_status`, `result_hash` and `result_preview` are lists in call order (one entry per tool call); `end` adds `validation_exhausted` and `context_overflow`, `tool_status` adds `ignored`; response fields are nullable for `provider_error`; `RunMeta` records the tier config, prompt and tool-manifest hashes, MCP server and SDK versions, sampling and loop settings.
- `Task` carries the question and evidence text.
- Context overflow is detected by the agent loop (`prompt tokens ≥ num_ctx − overflow_margin`) and recorded as an outcome; the Ollama provider no longer raises `ContextOverflow`, which is removed.
