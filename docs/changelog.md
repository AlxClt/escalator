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

### Changed

- The sandbox denies non-deterministic SQL: `random()`, `randomblob()`, `now` and `CURRENT_DATE/TIME/TIMESTAMP`.
- `.env.example`: `OLLAMA_BASE_URL` (required, no default, so Ollama can run on another machine) replaces `SMALL_TIER_BASE_URL` and `SMALL_TIER_API_KEY`; `python -m escalator.llm` loads `.env`; `ESCALATOR_CACHE_MODE` takes `readwrite`, `readonly` or `off`.
- `traces/` is gitignored except `traces/published/`.
