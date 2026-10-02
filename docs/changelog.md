# Changelog

All notable changes to Escalator willb be documented in this file

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)

## [Unreleased]

### Added

- `make data`, `make data-lock`, `make data-verify` and `make env-lock`: fetch Arcwise-Plat-Full, its description `schemas/` and the BIRD Mini-Dev SQLite bundle at the versions pinned in `data/sources.lock`, then verify them offline (gates V, D, M, T, S) and write audits to `data/audit/`.
- `data/env.lock` pins the Python and SQLite versions data is verified on; `.python-version` pins Python 3.12.14 and every target runs through `uv run --locked`.
- Gold-execution check (T5) with a cache in `data/raw/_cache/gold_check.json`; `NOCACHE=1` forces re-execution. `gold_timeout_s` is set to 2089 s by the gold-timeout rule.
- `escalator.datasets.descriptions.effective_descriptions(db_id)`: BIRD description files overlaid with Arcwise's.
- CI workflow running `make test`, and `make data` with `data/raw` cached on the lock hash.
- `make manifest`: pins the 200-task evaluation subset in `data/manifest.json` (seed 20261001 and hand-pinned exclusions in `configs/manifest.yaml`, difficulty-stratified sample, per-task `task_hash` and `gold_result_hash`).
