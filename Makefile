# Recipes are plain commands so they run under cmd.exe (Windows) and sh alike.
# FORCE=1 lets data-lock overwrite data/sources.lock; REF=<40-hex> pins that Arcwise commit
# instead of main; NOCACHE=1 forces gold re-execution in data-verify.
# Python always runs as the interpreter pinned in .python-version, via uv --locked.

export NOCACHE
FORCE_FLAG := $(if $(filter 1,$(FORCE)),--force)
REF_FLAG := $(if $(REF),--ref $(REF))
NOCACHE_FLAG := $(if $(filter 1,$(NOCACHE)),--no-cache)
PAID_FLAG := $(if $(filter 1,$(PAID)),--paid)
OVERWRITE_FLAG := $(if $(filter 1,$(OVERWRITE)),--overwrite)
SMOKE_FLAG := $(if $(filter 1,$(SMOKE)),--smoke)
CHECK_FLAG := $(if $(filter 1,$(CHECK)),--check)
CONFIG_FLAG := $(if $(CONFIG),--config $(CONFIG))
OUT_FLAG := $(if $(OUT),--out-dir $(OUT))
PRESET ?= baselines
MODELS_FLAG := $(if $(MODELS),--models $(MODELS))
COLLECT_FLAGS = $(SMOKE_FLAG) $(MODELS_FLAG) $(PAID_FLAG) $(OVERWRITE_FLAG) $(CHECK_FLAG) $(CONFIG_FLAG)
PY := uv run --locked python

.PHONY: test test-local env-lock data data-lock data-verify manifest smoke baselines
.PHONY: sc-samples sc-estimate verifier-runs frontier-repeat publish-traces router plots results

test:
	$(PY) -m pytest -m "not data and not manifest and not local"

# Checks against the local week-2 traces, LLM cache and data (not in CI: traces and data are not committed).
test-local:
	$(PY) -m pytest -m local

env-lock:
	$(PY) -m escalator.datasets env

data-lock:
	$(PY) -m escalator.datasets lock $(FORCE_FLAG) $(REF_FLAG)
	$(MAKE) data-verify

data:
	$(PY) -m escalator.datasets fetch
	$(MAKE) data-verify

data-verify:
	$(PY) -m escalator.datasets verify --offline $(NOCACHE_FLAG)
	$(PY) -m pytest tests/datasets -m data

manifest: data-verify
	$(PY) -m escalator.datasets manifest
	$(PY) -m pytest tests/datasets -m manifest

# 10 tasks, local model, through the MCP server -> results/smoke.json. Local only (decision P1).
smoke:
	$(PY) -m escalator.eval.runner smoke --overwrite

# 3 small models + the frontier model on the pinned 200 -> results/baselines.json. Manual trigger only:
# the frontier model is refused without PAID=1. PRESET=challenging runs the challenging-stratum study;
# MODELS="a b" restricts the preset's models.
baselines:
	$(PY) -m escalator.eval.runner $(PRESET) $(MODELS_FLAG) $(PAID_FLAG) $(OVERWRITE_FLAG)

# Week 3 collection (configs/router.yaml). Local runs only after the smoke run, the estimate and a go;
# paid models (haiku-5-5, sonnet-5-5) are refused without PAID=1. SMOKE=1: 10 tasks, gemma4-e4b only.
# MODELS="a b" overrides the configured models; CHECK=1 runs the compatibility gates only (no model call).
sc-samples:
	$(PY) -m escalator.eval.collect sc-samples $(COLLECT_FLAGS)

# Refuses while src/escalator/agent/prompts/verifier_v1.yaml holds placeholders.
verifier-runs:
	$(PY) -m escalator.eval.collect verifier-runs $(COLLECT_FLAGS)

# Paid, manual only: exits non-zero with the projected cost unless PAID=1.
frontier-repeat:
	$(PY) -m escalator.eval.collect frontier-repeat $(COLLECT_FLAGS)

# Projected wall-clock of the full collection, from the SMOKE=1 runs.
sc-estimate:
	$(PY) -m escalator.eval.collect estimate

# RUNS="run_id ..." -> traces/published/<run_id>/{meta.json, steps.jsonl.gz}
publish-traces:
	$(PY) -m escalator.trace.published $(RUNS)

# Offline routing simulation over the runs listed in configs/router.yaml: no LLM call, $0.
# traces -> results/signals.json, results/router.json (byte-identical across invocations).
router:
	$(PY) -m escalator.router $(CONFIG_FLAG) $(OUT_FLAG)

# results/router.json -> results/figures/ (PNG + SVG). Fails, after the per-model figures, while
# configs/router.yaml headline_small_tier is unset.
plots:
	$(PY) -m escalator.router.plots

results: router plots
