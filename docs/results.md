# Results

## Table 1 — All Policies 


| Policy | Config | EX % [CI] | Δ vs frontier (pp) [CI] | McNemar b / c / p | Escalation rate % | USD/task (API) | LLM latency s/task | USD/solved (API) [CI] | Steps/task |
|---|---|---|---|---|---|---|---|---|---|
| local only, gemma4-12b | `gemma4:12b-it-qat`, T = 0 | 69.0 [62.3, 75.0] | −17.0 [−23.5, −10.5] | 7 / 41 / 6.2e-07 | n/a | 0.0000 | 10.2 | 0.0000 | 3.31 |
| frontier only | `claude-sonnet-5-5` | 86.0 [80.5, 90.1] | 0.0 | n/a | 100 | 0.0191 | 3.5 | 0.0222 [0.0204, 0.0242] | 1.62 |
| routed, `error`, τ = 0 | 12b → frontier | 80.0 [73.9, 85.0] | −6.0 [−11.0, −1.0] | 7 / 19 / 0.029 (Holm 0.35) | 12.5 | 0.0030 | 10.8 | 0.0038 [0.0024, 0.0053] | 3.56 |
| routed, `self_consistency`, τ = 0 | 12b → frontier | 83.0 [77.2, 87.6] | −3.0 [−7.5, 1.5] | 7 / 13 / 0.26 (Holm 1.00) | 29.0 | 0.0063 | 41.3 | 0.0075 [0.0057, 0.0096] | 13.68 |
| routed, `self_consistency`, τ = 1/3 | 12b → frontier | 76.0 [69.6, 81.4] | −10.0 [−15.5, −4.5] | 7 / 27 / 0.00082 (Holm 0.011) | 11.5 | 0.0028 | 40.7 | 0.0037 [0.0023, 0.0054] | 13.39 |
| routed, `self_consistency`, τ = 2/3 | 12b → frontier | 71.5 [64.9, 77.3] | −14.5 [−20.5, −8.5] | 7 / 36 / 9.0e-06 (Holm 1.3e-04) | 4.5 | 0.0011 | 40.4 | 0.0016 [0.0006, 0.0027] | 13.26 |
| routed, `verifier`, τ = 0 | 12b → frontier | 84.0 [78.3, 88.4] | −2.0 [−5.5, 1.5] | 4 / 8 / 0.39 (Holm 1.00) | 36.0 | 0.0078 | 24.4 | 0.0092 [0.0073, 0.0114] | 7.95 |
| routed, best τ *(in-sample)* = `verifier`, τ = 0 | 12b → frontier | 84.0 [78.3, 88.4] | −2.0 [−5.5, 1.5] | 4 / 8 / 0.39 (Holm 1.00) | 36.0 | 0.0078 | 24.4 | 0.0092 [0.0073, 0.0114] | 7.95 |
| **oracle, budget-matched** (at the best τ's USD/task) | 12b → frontier | 89.5 [84.5, 93.0] | +3.5 | n/a | 20.5 | 0.0046 | 11.1 | 0.0052 | 3.69 |
| **oracle, unconstrained** | 12b → frontier | 89.5 [84.5, 93.0] | +3.5 | n/a | 20.5 | 0.0046 | 11.1 | 0.0052 | 3.69 |

### Table 1b — Summary across small models

| Small model | Local only EX % [CI] | Frontier only EX % [CI] | Best routed point *(in-sample)*: EX % [CI], USD/solved | Oracle ceiling % [CI] |
|---|---|---|---|---|
| gemma4-e4b | 10.5 [7.0, 15.5] | 86.0 [80.5, 90.1] | `self_consistency` τ = 0: 85.5 [80.0, 89.7], 0.0222 | 87.0 [81.6, 91.0] |
| gemma4-12b | 69.0 [62.3, 75.0] | 86.0 [80.5, 90.1] | `verifier` τ = 0: 84.0 [78.3, 88.4], 0.0092 | 89.5 [84.5, 93.0] |
| gemma4-31b | 81.5 [75.5, 86.3] | 86.0 [80.5, 90.1] | `verifier` τ = 0: 83.5 [77.7, 88.0], 0.0022 | 92.0 [87.4, 95.0] |
| haiku-5-5 | 79.5 [73.4, 84.5] | 86.0 [80.5, 90.1] | `verifier` τ = 0: 84.5 [78.8, 88.9], 0.0062 | 90.0 [85.1, 93.4] |

Frontier-only USD/solved is 0.0222 [0.0204, 0.0242] for every pair.

## Table 2 — Escalation gain decomposition

`δ_i = a_i^L − a_i^S ∈ {−1, 0, +1}` (see [Methodology](docs/methodology.md)), frontier = claude-sonnet-5-5, n = 200.

| Small tier | P(δ = +1) % [CI] | P(δ = 0, both right) % | P(δ = 0, both wrong) % | **P(δ = −1)** % [CI] | Oracle EX ceiling % [CI] |
|---|---|---|---|---|---|
| gemma4-e4b | 76.5 [70.2, 81.8] | 9.5 | 13.0 | 1.0 [0.3, 3.6] | 87.0 [81.6, 91.0] |
| gemma4-12b | 20.5 [15.5, 26.6] | 65.5 | 10.5 | 3.5 [1.7, 7.0] | 89.5 [84.5, 93.0] |
| gemma4-31b | 10.5 [7.0, 15.5] | 75.5 | 8.0 | 6.0 [3.5, 10.2] | 92.0 [87.4, 95.0] |
| haiku-5-5 | 10.5 [7.0, 15.5] | 75.5 | 10.0 | 4.0 [2.0, 7.7] | 90.0 [85.1, 93.4] |

`P(δ = −1)` is negative transfer — the small model right, the frontier model wrong.

## Table 3 — Signal quality

| Signal | Small tier | AUC vs 1[δ=+1] [CI] | AUC vs 1[δ=−1] [CI] | Extra small-tier runs/task | Extra LLM latency s/task | Extra USD/task | Oracle gain captured at the signal's τ* % [CI] |
|---|---|---|---|---|---|---|---|
| `error` | gemma4-e4b | 0.737 [0.658, 0.808] | 0.081 [0.056, 0.107] ¹ | 0 | 0.0 | 0.0000 | 94 [90, 97] (τ = 0) |
| `self_consistency` | gemma4-e4b | 0.580 [0.492, 0.661] | 0.159 [0.030, 0.299] ¹ | 3 | 45.2 | 0.0000 | 98 [96, 100] (τ = 0) |
| `verifier` | gemma4-e4b | 0.618 [0.557, 0.686] | 0.285 [0.023, 0.548] ¹ | 1 | 19.3 | 0.0000 | 97 [95, 99] (τ = 0) |
| `error` | gemma4-12b | 0.759 [0.683, 0.836] | 0.435 [0.412, 0.458] ² | 0 | 0.0 | 0.0000 | 69 [55, 81] (τ = 0) |
| `self_consistency` | gemma4-12b | 0.759 [0.676, 0.836] | 0.350 [0.316, 0.380] ² | 3 | 30.0 | 0.0000 | 68 [53, 82] (τ = 0) |
| `verifier` | gemma4-12b | 0.780 [0.712, 0.848] | 0.536 [0.325, 0.747] ² | 1 | 12.7 | 0.0000 | 73 [58, 87] (τ = 0) |
| `error` | gemma4-31b | 0.548 [0.500, 0.625] | 0.495 [0.487, 0.500] | 0 | 0.0 | 0.0000 | 50 [33, 100] (τ = 0) |
| `self_consistency` | gemma4-31b | 0.567 [0.485, 0.670] | 0.548 [0.458, 0.677] | 3 | 39.0 | 0.0000 | 50 [33, 100] (τ = 1/3) |
| `verifier` | gemma4-31b | 0.642 [0.539, 0.750] | 0.590 [0.468, 0.734] | 1 | 13.6 | 0.0000 | 24 [−15, 53] (τ = 0) |
| `error` | haiku-5-5 ³ | 0.500 [0.500, 0.500] | 0.500 [0.500, 0.500] | 0 | 0.0 | 0.0000 | n/a (no non-degenerate point) |
| `self_consistency` | haiku-5-5 ³ | 0.616 [0.510, 0.723] | 0.500 [0.419, 0.642] | 3 | 10.7 | 0.0040 | 29 [4, 50] (τ = 0) |
| `verifier` | haiku-5-5 | 0.741 [0.636, 0.850] | 0.557 [0.421, 0.741] | 1 | 3.5 | 0.0014 | 48 [20, 72] (τ = 0) |

See [Methodology](docs/methodology.md) for the signals definitions

## Table 4 — By difficulty stratum

Labels are from the original Mini BIRD benchmark questions, they have not been updated according to Arcwise Plat Full corrections

| Stratum | n | EX e4b % | EX 12b % | EX 31b % | EX haiku % | EX frontier % | P(δ = +1), 12b % | Escalation rate, 12b at τ* % |
|---|---|---|---|---|---|---|---|---|
| simple | 61 | 6.6 [2.6, 15.7] | 83.6 [72.4, 90.8] | 91.8 [82.2, 96.4] | 82.0 [70.5, 89.6] | 91.8 [82.2, 96.4] | 11.5 [5.7, 21.8] | 19.7 [11.6, 31.3] |
| moderate | 100 | 13.0 [7.8, 21.0] | 66.0 [56.3, 74.5] | 80.0 [71.1, 86.7] | 79.0 [70.0, 85.8] | 85.0 [76.7, 90.7] | 23.0 [15.8, 32.2] | 38.0 [29.1, 47.8] |
| challenging | 39 | 10.3 [4.1, 23.6] | 53.8 [38.6, 68.4] | 69.2 [53.6, 81.4] | 76.9 [61.7, 87.4] | 79.5 [64.5, 89.2] | 28.2 [16.5, 43.8] | 56.4 [41.0, 70.7] |

τ* for 12b is `verifier`, τ = 0.

## Table 5 — Operational

Baseline runs of each tier (n = 200)

| Metric | gemma4-e4b | gemma4-12b | gemma4-31b | haiku-5-5 | sonnet-5-5 | routed (12b, `verifier` τ = 0) |
|---|---|---|---|---|---|---|
| steps/task (mean, p95) | 2.73, 6 | 3.31, 8 | 2.42, 5 | 2.00, 3 | 1.62, 2 | 7.95, 18 |
| step-limit exhaustion % [CI] | 3.5 [1.7, 7.0] | 12.5 [8.6, 17.8] | 1.0 [0.3, 3.6] | 0.0 [0.0, 1.9] | 0.0 [0.0, 1.9] | 0.0 [0.0, 1.9] |
| tool-validation failure % [CI] | 0.26 [0.05, 1.45] | 0.00 [0.00, 0.58] | 0.00 [0.00, 0.79] | 0.00 [0.00, 0.95] | 0.00 [0.00, 1.17] | 0.00 [0.00, 0.24] |
| SQL execution-error % [CI] | 18.5 [14.8, 22.9] | 5.1 [3.5, 7.5] | 0.3 [0.1, 2.0] | 1.0 [0.3, 3.6] | 0.0 [0.0, 3.0] | 4.7 [3.6, 6.1] |
| statement-timeout % [CI] | 0.57 [0.16, 2.05] | 0.00 [0.00, 0.78] | 0.00 [0.00, 1.33] | 0.00 [0.00, 1.88] | 0.00 [0.00, 3.00] | 0.00 [0.00, 0.32] |
| LLM latency s/task (mean, p95) | 5.7, 19.5 | 10.2, 24.7 | 13.6, 25.2 | 3.3, 5.9 | 3.5, 6.5 | 24.4, 53.4 |
| MCP round-trip overhead, ms/call (median / p95) | 1.88 / 2.53 | 1.88 / 2.53 | 1.88 / 2.53 | 1.88 / 2.53 | 1.88 / 2.53 | 1.88 / 2.53 |
| soft-F1 (secondary) | 0.125 | 0.762 | 0.882 | 0.876 | 0.902 | 0.890 |
| EX − soft-F1 gap (pp) | −2.0 | −7.2 | −6.7 | −8.1 | −4.2 | −5.0 |
