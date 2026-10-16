# Pre-registration: gemma4-31b escalation on the challenging stratum

**Status: binding** from the commit that introduced this line (`git log -- docs/prereg-31b-challenging.md`),
made on 16/10/2026 before any run of the study. That commit's hash is cited with the results. Nothing
below changes from here on.

## Question

On the hardest Arcwise-Plat-Full tasks, does escalating from gemma4-31b to a frontier model, on the
verifier signal, improve execution accuracy (EX) over gemma4-31b alone? How much does it cost?

On the pinned 200-task sample (week 3), no escalation policy from gemma4-31b was significant, overall
or within any stratum. The challenging stratum showed the largest observed gain on only 39 tasks: +7.7
pp for the routed policy and +10.3 pp for frontier-only (README template, Table 4b).

## Population

- **All 95 challenging tasks of the sampling frame**, listed in `data/manifest-challenging.json`
  (Mini-Dev difficulty `challenging`). The frame is Arcwise-Plat-Full minus the 14 exclusions pinned in
  `configs/manifest.yaml`: gold that depends on the run date, or that exceeds the 5 s sandbox limit. No
  other challenging task can be scored.
- 39 of the 95 are in the 200-task sample; their week-2 and week-3 runs are reused from the cache with
  identical requests. 56 are new.
- **Scoring:** `eval/scorers.py`, unchanged. Output order is enforced only for the tasks in
  `configs/scoring-challenging.yaml`: 220 (alphabetical) and 1011 (ranked by shortest lap time, by the
  owner's decision). No tie blocks are needed: none exist at any `LIMIT` boundary.
- **Prices:** `claude-opus-5-5` at $4.00 / $20.00 per Mtok, confirmed by the owner on 16/10/2026;
  Sonnet as in `configs/prices.yaml` (retrieved 2026-10-14).

## Policy (fixed now)

- **Small tier:** `gemma4:31b-it-qat` at its pinned digest. T = 0, seed 20261001, prompt `v1`, the
  week-2 loop settings.
- **Signal:** one verifier run of the same model with prompt `verifier_v1` (T = 0, seed 20261001). The
  signal fires when the verifier's executed result differs from the baseline's, or when either run has
  no executable answer.
- **Rule:** escalate a task iff the signal fires (verifier, τ = 0). This is gemma4-31b's best routed
  point on the 200-task sample, chosen there in-sample.
- **Frontier models,** each run once over the 95 tasks at provider defaults (the API rejects sampling
  parameters), prompt `v1`:
  - `claude-sonnet-5-5`: thinking `between_tools`, effort `medium`;
  - `claude-opus-5-5`: thinking `adaptive`, effort `medium`; the settings of its week-2 smoke run.
- **Cost:** a routed task pays the 31b baseline and verifier runs (local, priced $0), plus the frontier
  run if escalated. Prices are those of `configs/prices.yaml` at the binding commit.

## Analyses

**Analysis set:** the 95 tasks minus any task that ends in `provider_error` in any run after a resume
through the cache. Excluded ids are reported.

**Primary, for each frontier model (Sonnet, Opus):** the routed policy against gemma4-31b alone, paired
on the same tasks.

1. Δ EX = EX(routed) − EX(31b alone), with a 95% percentile bootstrap CI: B = 2000, the seed of the
   study's router config (20261015), tasks resampled with replacement. This is the main result.
2. A two-sided exact McNemar test on the tasks where the two policies disagree. The p-values of the
   two frontier models are **Holm-adjusted together**, family-wise α = 0.05.

**Secondary (descriptive, no confirmatory claim):**

- frontier-only vs gemma4-31b alone, and routed vs frontier-only (Δ EX with CI, exact McNemar);
- Opus vs Sonnet (frontier-only), paired;
- **held-out sensitivity analysis:** the primary analyses restricted to the 56 tasks not in the 200-task
  sample. They played no part in choosing the policy;
- costs (USD per task, USD per solved task), the escalation rate and the composition of escalations;
- the full threshold curves of every 31b signal, the oracle frontier and AUCs, as produced by
  `make router`.

The analysis is computed by `scripts/prereg_31b_challenging.py` from the outputs of `make router` with
`configs/router-challenging-sonnet.yaml` and `configs/router-challenging-opus.yaml`.

## Expected power (stated before the runs)

Exact power of the two-sided test at n = 95, extrapolated from the 39 tasks already observed
(31b → Sonnet):

| Comparison vs 31b alone | Observed on 39 | Power, α = 0.05 | Power, Holm-adjusted (α = 0.025 first) |
|---|---|---|---|
| routed, verifier τ = 0 | 3 discordant, all helpful | 0.75 | 0.60 |
| frontier-only | 8 discordant, 6 helpful | 0.53 | 0.41 |

These figures rest on very few discordant tasks:
- The routed figure assumes every escalation keeps helping. With 90% helpful escalations it falls to
  0.45; with 80%, to 0.22.
- Opus's disagreement rates on these tasks are unknown.

A non-significant result is therefore likely, and it will be reported as an estimate: Δ with its CI,
which narrows from about ±14 pp to about ±9 pp. It will not be read as evidence that the policies
perform the same.

## Fixed in advance

- No task is added or removed, and no other policy, threshold or prompt becomes primary, after any
  result is seen.
- No extra run after the results, except a cache resume of `provider_error` tasks.
- Each paid model runs once (sample 0). The frontier flip rate is not measured and is a stated
  limitation.
- Every result is reported, whatever its direction.
