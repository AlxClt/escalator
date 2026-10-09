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
PY := uv run --locked python

.PHONY: test env-lock data data-lock data-verify manifest smoke baselines

test:
	$(PY) -m pytest -m "not data and not manifest"

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
# the frontier model is refused without PAID=1.
baselines:
	$(PY) -m escalator.eval.runner baselines $(PAID_FLAG) $(OVERWRITE_FLAG)
