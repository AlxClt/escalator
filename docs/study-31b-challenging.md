# Study: gemma4-31b escalation on the challenging stratum

Pre-registration: `docs/prereg-31b-challenging.md`, binding at commit `a7b01ad` (16/10/2026 13:55 UTC).
Every run of the study started after that commit; the first started at 14:04 UTC.

- **Population:** all 95 challenging tasks of the Arcwise-Plat-Full frame (`data/manifest-challenging.json`). Analysis set: all 95; no task ended in `provider_error`.
- **Policy:** gemma4-31b answers; one verifier run; escalate iff the verifier's result disagrees (verifier τ = 0).
- **Frontiers:** claude-sonnet-5-5 and claude-opus-5-5, one pass each.
- **Outputs:** `results/challenging/prereg.json` (`scripts/prereg_31b_challenging.py`), `results/challenging/{sonnet,opus}/router.json`, and figures under `results/challenging/{sonnet,opus}/figures/`.

## Primary result (pre-registered)

Routed policy vs gemma4-31b alone, paired on the 95 tasks. EX intervals are Wilson; Δ intervals are bootstrap (B = 2000, seed 20261015). The McNemar test is exact, two-sided, with Holm over the two frontiers.

| Frontier | Escalated | EX routed % [CI] | EX 31b alone % [CI] | **Δ EX (pp) [95% CI]** | Wins / losses | p | Holm p |
|---|---|---|---|---|---|---|---|
| claude-sonnet-5-5 | 14 / 95 | 83.2 [74.4, 89.4] | 72.6 [62.9, 80.6] | **+10.5 [+5.3, +17.9]** | 10 / 0 | 0.0020 | **0.0039** |
| claude-opus-5-5 | 14 / 95 | 81.1 [72.0, 87.7] | 72.6 [62.9, 80.6] | **+8.4 [+3.2, +14.7]** | 8 / 0 | 0.0078 | **0.0078** |

Both pre-registered tests reject "no difference" at the family-wise 5% level. On these 95 tasks, escalating gemma4-31b's verifier disagreements to either frontier model raises EX by about 8–11 points. No escalated task was lost: every escalation either fixed a wrong answer or left the outcome unchanged. Sonnet's escalations were 10 helpful, 4 wasted, 0 harmful; Opus's were 8, 6, 0.

## Secondary results (descriptive, as pre-registered)

| Comparison | EX % [CI] vs reference % [CI] | Δ (pp) [95% CI] | Wins / losses | p |
|---|---|---|---|---|
| Sonnet only vs 31b alone | 89.5 [81.7, 94.2] vs 72.6 [62.9, 80.6] | +16.8 [+8.4, +25.3] | 18 / 2 | 0.0004 |
| Opus only vs 31b alone | 87.4 [79.2, 92.6] vs 72.6 [62.9, 80.6] | +14.7 [+6.3, +24.2] | 17 / 3 | 0.0026 |
| routed (Sonnet) vs Sonnet only | 83.2 vs 89.5 | −6.3 [−13.7, +0.0] | 2 / 8 | 0.11 |
| routed (Opus) vs Opus only | 81.1 vs 87.4 | −6.3 [−13.7, +0.0] | 3 / 9 | 0.15 |
| Opus only vs Sonnet only | 87.4 vs 89.5 | −2.1 [−9.5, +5.3] | 5 / 7 | 0.77 |

The routed policy stays below frontier-only by 6.3 pp. That difference is not significant (p = 0.11 and 0.15), and the data allow anything from about a 14 pp loss to no loss. Opus and Sonnet are not distinguishable here.

**Held-out sensitivity** (the 56 tasks outside the 200-task sample, which played no part in choosing the policy):

| Frontier | n | EX routed vs 31b alone % | Δ (pp) [95% CI] | Wins / losses | p |
|---|---|---|---|---|---|
| Sonnet | 56 | 87.5 vs 75.0 | +12.5 [+5.4, +21.4] | 7 / 0 | 0.016 |
| Opus | 56 | 85.7 vs 75.0 | +10.7 [+3.6, +19.6] | 6 / 0 | 0.031 |

The effect holds on the tasks never used to select the policy.

**Cost** (API USD; the local tier is priced at $0; prices retrieved 2026-10-14, Opus confirmed 16/10/2026):

| Pair | Routed USD/task | Routed USD/solved [CI] | Frontier-only USD/solved | Cost-per-solved ratio [CI] |
|---|---|---|---|---|
| 31b → Sonnet | 0.0029 | 0.0035 [0.0017, 0.0056] | 0.0211 | 6.1× [3.9, 12.2] |
| 31b → Opus | 0.0067 | 0.0082 [0.0043, 0.0129] | 0.0553 | 6.7× [4.3, 12.6] |

**Signal and oracle:**
- verifier AUC against escalation gain: 0.752 [0.623, 0.866] (Sonnet pair), 0.697 [0.574, 0.821] (Opus pair);
- oracle ceiling: 91.6% (Sonnet pair), 90.5% (Opus pair);
- full curves, other signals and operational metrics: `results/challenging/{sonnet,opus}/router.json`.

## Power: expected vs observed

The pre-registration put the routed test's power at 0.60 after Holm, assuming every escalation keeps helping. Observed: 10 and 8 discordant tasks, all helpful. This matches the optimistic assumption, which rested on 3 of 3 helpful escalations on the 39 tasks in the sample. The 95% CIs on Δ have half-widths of about 6 pp, narrower than the ±9 pp projected, because the losses were zero.

## Notes from collection

- **Runs:**
  - `challenging-gemma4-31b-v1`;
  - `sc-challenging-gemma4-31b-v1` (k = 3, T = 0.7, seeds 1/2/3);
  - `verifier-challenging-gemma4-31b-verifier_v1`;
  - `challenging-sonnet-5-5-v1`;
  - `challenging-opus-5-5-v1`.

  All are published in `traces/published/`, and their meta records the challenging manifest's hash.
- **Cache reuse:**
  - Sonnet, 31b, SC and verifier made fresh calls only on the 56 new tasks. The 39 tasks of the 200-task sample replayed from the cache, and their outcomes are identical to week 2.
  - Opus made fresh calls on 92 tasks; its 3 smoke tasks (28, 36, 62) replayed.
- **Spend:** Sonnet $0.99 (projected $1.15), Opus $4.38 (projected $3.9).
- **Ollama 0.40.5** served the new 31b calls. The 39 cached tasks came from week-3 runs on 0.40.4. The version is not part of the compatibility gate.
- **First 31b attempt:** it stopped at its first cache miss, before calling any model. The runner checked the digests of every pinned Ollama model, while the pod held only 31b. The fix (`b2d493c`, the run's own models only) came before the real run. The failed attempt had replayed 3 cached tasks and collected nothing.
- **Results file rebuilt:** `results/challenging/baselines.json` was rebuilt from the traces with `scripts/rebuild_preset_results.py`, because each `make baselines` invocation rewrites it with its own model only.
  - The rebuild rescored all three runs through the runner's code.
  - The Opus run, still present from its live invocation, came out identical.
  - The integrity check of `make router` therefore compares two passes of the same scorer for 31b and Sonnet.
- **`git_dirty`:** true for every run, because of the owner's uncommitted `README.md` / `.gitignore` edits. The study code was the same in all runs (`b2d493c`, and `023fd5c` = `b2d493c` plus `README.md` / `.gitignore`).
- **Not measured:** the frontier flip rate (one pass per paid model, as pre-registered).
