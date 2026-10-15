# Week 3 plan: signals, offline routing, headline plot

Working plan for `docs/tasks/week3-router-task.md` (the spec), approved on 15/10/2026. This file holds the
implementation design and where it stands. Phase 0 findings, conflicts with week 1/2, decisions and
measurements live in `docs/week3-notes.md`.

Branch: `feature/week3-router`. Commit only through `/update-docs-and-commit`, at each completed step.

## Status (end of 15/10/2026)

| Step | State |
|---|---|
| 1. Collection infrastructure | Done, `ca36d4f` |
| 2. Checkpoint A: verifier prose, smoke runs, estimate | Done. Prose `d034aa6`; smoke runs validated; estimate in `week3-notes.md` |
| 3. Metrics primitives (`eval/metrics.py`) | Done, 31 tests pass |
| 4. Inventory, execution cache, signals | To do |
| 5. Policy simulation | To do |
| 6. `make router` end to end | To do |
| 7. Plots | To do |
| 8. Checkpoint B: full collection | **Running** (started by the owner, 15/10 ~08:35 UTC) |
| 9. Notes tables, docs, PR | To do |

### Collection in flight (step 8)

The owner started these runs (the owner triggers every collection run):
- `make sc-samples MODELS="gemma4-e4b gemma4-12b gemma4-31b"` → `sc-gemma4-{e4b,12b,31b}-v1`, run in that order. Projected 16.8 h.
- `make sc-samples MODELS=haiku-5-5 PAID=1` → `sc-haiku-5-5-v1` (paid, ~$0.77).
- Not started yet: the verifier runs. Gemma: `make verifier-runs MODELS="gemma4-e4b gemma4-12b gemma4-31b"`, ~4.7 h projected. Haiku: `make verifier-runs MODELS=haiku-5-5 PAID=1`, ~$0.26.

The runs record `git_sha` `d034aa6` with `git_dirty: true`. The dirt is the uncommitted `eval/metrics.py`, its tests and `week3-notes.md`, which collection does not use. Record this in the notes.

## Resume checklist

1. **Environment.** `.venv` is a dead WSL venv that Windows uv cannot rebuild (unless the owner deleted it). Use a separate env: `$env:UV_PROJECT_ENVIRONMENT = "<scratch>\venv"; uv sync --locked`. Run everything from PowerShell, never WSL.
2. **State of each run.** For every `traces/sc-*` / `traces/verifier-*` run:
   - the number of `(task_id, sample_idx)` runs (SC 600, verifier 200);
   - ends, `provider_error` task ids, ordering;
   - meta: `run_kind`, seeds, `ollama_version`, Modelfile params.

   An interrupted or `provider_error` run resumes through the cache with `OVERWRITE=1` (owner's call).
3. **Wall-clock.** Record the measured LLM latency per run in `week3-notes.md` ("Checkpoint B"), against the projection.
4. **Publish.** Once a run is complete, `make publish-traces RUNS="..."`. Also publish the five week-2 baselines (copies; the originals stay). Then fill the run ids in `configs/router.yaml` `inventory`.
5. **Continue** with step 4 below. Steps 4–7 need no run and can be built and tested on the week-2 traces and the smoke runs.

## Decisions already taken (details in `week3-notes.md`)

- **Four pairs:** e4b, 12b, 31b and haiku-5-5, each with sonnet-5-5.
- **Spend guard:** `PAID=1`.
- **No concurrency:** test 1 loses its concurrency part.
- **Haiku SC samples** use `sample_idx` 1..3 at provider-default sampling: not T = 0.7, and not comparable 1:1 with Gemma's SC. Say so next to its numbers.
- **Error signal:** a `denied` submit counts as an execution failure.
- **No cache-key change** (`sample_idx` was already hashed).

## Remaining design

### Step 4: inventory, execution cache, signals

- **`router/inventory.py`:**
  - Load each run named in `configs/router.yaml` through `trace/published.load_meta` / `load_steps` (published first).
  - Validate each run's meta against its role: tier, `run_kind`, prompt version (`v1` for baseline/SC/repeat, `verifier_v1` for the verifier), sampling (SC: `sample_seeds` = config seeds, T = config T for Ollama; baseline T = 0 for Ollama).
  - Group records into runs keyed `(task_id, sample_idx)`; SC samples are identified by position j (sorted `sample_idx`).
  - Expose `read_frontier_runs()` as a separate function, so the no-label test can monkeypatch it to raise.
  - **Analysis set A:** pinned tasks for which no inventory run ended in `provider_error`.
  - **Stratum:** read from `data/manifest.json` `difficulty`; the path is configurable for fixtures.
- **`router/execcache.py`:**
  - `Executor` protocol: `execute(db_id, sql) -> Result | SqlError`.
  - `CachedExecutor(sandbox, path=.cache/exec.sqlite)`, keyed by `sha256(db_id, sql, sources.lock sha, sqlite version)`. It stores rows as canonical JSON (bytes hex-tagged via `util.canon`) or the error kind.
  - Purpose: a query near the 30 s timeout keeps its first outcome, so `make router` stays byte-identical.
  - Always `row_cap=None`.
- **`router/signals.py`:** no import of `eval.tasks`.
  - `submit_of(run)`: the first `submit_answer` call that is not `validation_error` / `ignored`, with its `args` (`db_id`, `sql`) and `tool_status`.
  - `ans(run, ex)`: rows or ⊥.
  - `eq(u, v) = u is not None and v is not None and execution_match(u, v, ordered=False)` (`eval.scorers`; the scorer is not modified).
  - `error_signal(run)`: 1 iff `end ∈ FAILED_ENDS` or the submit status is in {`execution_error`, `timeout`, `denied`}. Read from the trace; no execution needed.
  - `sc_signal(samples, ex) -> (s, m)`, with `m = max_j Σ_j' eq(...)`. ⊥ never agrees, including with itself.
  - `verifier_signal(base, ver, ex)`.
  - A signal whose inventory id is null → `status: not_collected` and null values, never zeros.
- **Tests:**
  - test 5: one case per `end`, plus submitted + error / timeout / ok;
  - test 6: the full SC table, including the non-transitive tolerance case (e.g. values 1.0, 1.0+0.9e-6, 1.0+1.8e-6) and sample-order invariance;
  - test 7: verifier;
  - test 8: monkeypatch `eval.tasks.gold_result`, `eval.tasks.load_tasks` and `inventory.read_frontier_runs` to raise, then compute the signals.
- **Local check:** compare the error signal with `end` / `submit_status` in `results/baselines.json`; count disagreements between the trace's submit status and label-free re-execution.

### Step 5: `router/policy.py`, pure, on per-task arrays

Each function takes `a_S`, `a_L`, `c_S`, `c_L`, `c_sigma` and `s` (`Decimal` costs). Spec references are §D6–D7 and §Statistical protocol.
- **Threshold grid and curve:**
  - `threshold_grid(s)` = {−∞} ∪ distinct values of s.
  - `threshold_curve`: one point per τ, with `E(τ) = {s > τ}` and cost `c_S + c_σ + 1[E]·c_L`. Fields: Acc, Cost, CPS (ratio of sums; undefined when nothing is solved), escalation rate, and the helpful / wasted / harmful composition.
  - Non-degenerate points: `0 < |E| < n`.
- **Oracle:**
  - `oracle_vertices`: P ordered by `(c_L, task_id)`.
  - `oracle_acc_at(C)` = max `Acc_O(k)` with `Cost_O(k) ≤ C`.
  - Ceiling = `Σ max(a_S, a_L) / n`.
- **Budget variant:** `acc_at_budget(curve, B)` interpolates linearly between consecutive threshold points sorted by cost. Below the escalate-none cost it is undefined (null); above escalate-all it is escalate-all. Test 11 checks it against the expectation over every tie-break order on ≤ 8 tasks.
- **`random_reference(e)`:** the closed forms of D6.
- **Per policy (D7):** oracle gap at matched USD per task, headroom captured (undefined when the denominator is 0), ΔAcc versus frontier-only, `ratio = CPS_L / CPS(π)` (`"inf"` + flag when CPS(π) = 0), per-stratum results.
- **Tests:** test 9 (hand-built 10-task fixture) and test 10 (oracle).

### Step 6: `make router` (`python -m escalator.router`)

- **Integrity check:** recompute `a_S` and `a_L` with `eval.tasks.score_sql` through the `CachedExecutor` path. Assert equality with `results/baselines.json` (`runs[].tasks[].ex`) task by task; any mismatch is a hard failure listing task ids. Test 14 includes a perturbed fixture.
- **Per pair:**
  - baselines (small-only, frontier-only, escalate-all per signal);
  - oracle vertices and ceiling;
  - per signal: status, AUC against `δ = +1`, AUC^fail against `a_S = 0`, the full threshold curve, and the non-degenerate points with McNemar against frontier-only;
  - the random reference;
  - NT (`b / n`, and `b / #{a_S = 1}`), `c / n`;
  - flip rates: `F_S` from SC; `F_L` only with the repeat, else `not_measured`;
  - operational: steps per task, validation-failure rate, execution-error and timeout rates, latency, soft-F1 (taken from `baselines.json`, the scorer is unchanged).
- **Statistics:**
  - one Holm family: every non-degenerate point of every collected signal, across the four small models;
  - one `Bootstrap(n, B=2000, seed)` per invocation, shared by every statistic; thresholds stay fixed, and the oracle is recomputed inside each resample;
  - Wilson intervals for proportions;
  - every number carries an interval or `point_estimate: true`.
- **Small-tier compute per policy:** mean Σ(`tokens_in` + `tokens_out`) and Σ `latency_ms` over the small runs the policy needs. Tool time is not in the trace.
- **Output:**
  - `results/signals.json`: one row per `(small_model, task_id)`; unavailable signals are null, with the reason at file level.
  - `results/router.json`: `schema_version`; inputs (run ids + `meta.json` sha256 each); config (k, T, seeds, B, seed, prices `retrieved_on`); the analysis set (`n`, excluded ids); then per small model.
  - Encoding: `json.dumps(sort_keys=True, indent=2)`, floats rounded to 10 d.p., τ = −∞ as `"-inf"`.
- **Determinism and fixtures (test 15):** run twice → byte-identical. Fixture under `tests/fixtures/router/`: hand-built traces + `baselines.json` + manifest + `router.yaml`, on `tests/fixtures/agent_db.sql`. Verifier null → `"not_collected"`.

### Step 7: plots

- Add `matplotlib` (pinned) in a `plots` dependency group (`uv add --group plots`). CI changes to `uv sync --locked --all-groups`, and `make plots` uses `uv run --locked --group plots`. Record the justification in the notes.
- `router/plots.py`: for each small model, x = USD per solved task and x = USD per task. The plot shows:
  - small-only, frontier-only, escalate-all and the non-degenerate points, one marker per signal, joined in τ order;
  - the oracle frontier (dashed) and the random reference (dotted);
  - 95% bootstrap intervals on both axes;
  - a footer with n, prices `retrieved_on` and the run ids.
- PNG + SVG under `results/figures/`; `svg.hashsalt` fixed and date metadata stripped.
- The headline step fails clearly while `headline_small_tier` is unset; this key is never set by me.
- `make results` = `router` + `plots`. Test 16.

### Step 9: closing

- `week3-notes.md`: one raw table per small model (n, Acc_S, Acc_L, b, c, oracle ceiling, AUC and AUC^fail per signal, and per non-degenerate point: escalation rate, Acc, Cost, CPS, oracle gap, raw and Holm p), plus every silent-spec decision and measured wall-clock.
- Docs via the commit skill; push; PR.

## Not to do

- No run calling Anthropic models.
- No collection run without the owner's go.
- Never touch the scorer, `data/*`, `prices.yaml`, sandbox limits, `v1.yaml`, loop semantics, week-2 traces or `results/baselines.json`.
- Never set `headline_small_tier`.
- No prompt prose.
- Never run `frontier-repeat`.
