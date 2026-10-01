# escalator

**Claim (unmeasured — all cells below are blank by construction).**
On a pinned 200-task subset of BIRD Mini-Dev, a small-model-first escalation policy reaches
`<ACC_ROUTED>`% execution accuracy (95% CI `<CI>`), within **`<X>` points** of frontier-only,
at **`<Y>`×** lower attributed USD per solved task. Escalation signal AUC against
$\mathbb{1}[\delta_i=+1]$ is `<AUC>`; the realized router captures `<PCT>`% of the oracle gap.

> This README was written before any sweep, per the week-0 discipline: headers, row labels,
> units and intervals fixed first, cells filled later. Every `<TOKEN>` is an unfilled cell.
> Every filled number will carry an interval or be explicitly labelled a point estimate.

---

## 1. The frontier

![accuracy vs attributed USD per solved task](results/figures/frontier.png)

Four points on the accuracy/cost plane — local-only, routed at two thresholds, frontier-only —
plus the oracle-routing bound as a dashed line. The oracle line is **bias-corrected** by
$\hat\varphi/2$; see §5.3 for why the raw single-draw oracle sits too high.

Regenerated from `results/*.json` by `make results`. The figure is never hand-edited.

---

## 2. Results

### 2.1 Headline

Accuracy is the stratified estimator $\hat\mu_{\text{str}} = \sum_h W_h \bar a_h$ with strata
weights $W_h$ read from `data/manifest.json`. Intervals are percentile bootstrap over tasks,
resampled **within stratum**, 2000 resamples. No finite-population correction — the estimand is
accuracy on BIRD-like text-to-SQL tasks, not on Mini-Dev's 500 (§6.3).

| Policy | EX | 95% CI | USD/task | USD/solved | Escalation rate | Steps/task |
|---|---|---|---|---|---|---|
| local-only `<model-a>` | — | — | — | — | 0% | — |
| local-only `<model-b>` | — | — | — | — | 0% | — |
| local-only `<model-c>` | — | — | — | — | 0% | — |
| routed, high $\tau$ | — | — | — | — | — | — |
| routed, low $\tau$ | — | — | — | — | — | — |
| frontier-only `<model-f>` | — | — | — | — | 100% | — |
| oracle (bias-corrected) | — | — | — | — | — | n/a |

All USD figures are **attributed** cost, not billed cost (§4.4). Prices pinned in
`configs/prices.yaml` with `retrieved_on: <DATE>`.

### 2.2 Paired comparisons

Policies are compared on identical tasks, so the correct test is McNemar on discordant pairs.
$\rho$ is the re-draw stability diagnostic of §5.4; $\rho \gtrsim 1$ means the p-value is not
reproducible across sweeps.

| Comparison | $b$ | $c$ | $\Delta$EX | 95% CI on $\Delta$ | exact binomial $p$ | $\rho$ |
|---|---|---|---|---|---|---|
| routed (low $\tau$) vs frontier-only | — | — | — | — | — | — |
| routed (low $\tau$) vs local-only | — | — | — | — | — | — |
| routed (high $\tau$) vs routed (low $\tau$) | — | — | — | — | — | — |

### 2.3 Signal quality and the oracle gap

The gap between oracle routing and realized routing is the honest measure of the signal.

| Signal | Extra cost | AUC vs $\mathbb{1}[\delta_i=+1]$ | 95% CI | Realized EX at matched cost | % of oracle gap captured |
|---|---|---|---|---|---|
| `error` | $0 | — | — | — | — |
| `self_consistency` ($k$=3, $T$=0.7) | 3× small tier, local | — | — | — | — |
| `verifier` | 2× small tier, local | — | — | — | — |

Negative-transfer rate $P(\delta_i = -1)$ = `<NT>`, of which approximately `<NT_NOISE>` is
attributable to sampling noise rather than capability difference (§5.3).

If AUC ≈ 0.5 for all three signals, that is the result and it is stated as such here.

### 2.4 By difficulty stratum

A router whose escalation rate is flat across difficulty is not tracking difficulty.

| Stratum | $W_h$ | $n_h$ | EX local | EX frontier | EX routed | Escalation rate |
|---|---|---|---|---|---|---|
| simple | — | — | — | — | — | — |
| moderate | — | — | — | — | — | — |
| challenging | — | — | — | — | — | — |

### 2.5 Hard slice (authored, uncontaminated)

Reported separately from the BIRD numbers, never pooled. `<N_HARD>` questions authored against
the same databases, where the naive SQL differs from the statistically defensible SQL. Ground
truth is committed SQL plus a written rationale per question in `data/hard_slice/`.

| Failure mode | $n$ | EX local | EX frontier | EX routed |
|---|---|---|---|---|
| Fan-out duplication | — | — | — | — |
| NULL semantics | — | — | — | — |
| Denominator ambiguity | — | — | — | — |
| Ties under `LIMIT` | — | — | — | — |
| Temporal boundary | — | — | — | — |
| Survivorship | — | — | — | — |
| Silent coercion | — | — | — | — |

### 2.6 Operational

| Metric | local | frontier | routed |
|---|---|---|---|
| Tool-validation failure rate | — | — | — |
| Execution-error rate | — | — | — |
| Timeout rate | — | — | — |
| Step-limit exhaustion rate | — | — | — |
| Wall-clock p50 / p95 per task | — | — | — |
| Soft-F1 (secondary) | — | — | — |
| Response-cache hit rate | — | — | — |
| Prefix-cache read share of input tokens | n/a | — | — |

A large EX / soft-F1 gap localizes near-misses.

---

## 3. Reproduce

```
make data      # verify the 200 pinned task ids and row hashes against data/manifest.json
make sweeps    # both tiers over the pinned set; $0 and <5 min if the shipped cache is warm
make results   # metrics, tables and figures, recomputed from results/traces/*.jsonl
```

`make results` touches no network and no LLM. It is a pure function of the committed traces, so
a stranger reproduces every number in this README without an API key and without spending.

`make sweeps` replays from the shipped response cache (`cache/responses.sqlite`, pruned to the
pinned set). With an API key and a cold cache it re-bills; see §4.4.

---

## 4. Method

### 4.1 Agent loop

1. Render a schema card for the task's database; optionally schema-link to the ~5 relevant tables.
2. Model emits a tool call: inspect schema, sample rows, execute SQL, or submit.
3. Tool arguments are validated against JSON schema; on failure a typed error returns to the model, up to 2 retries.
4. SQL executes in a read-only sandbox: 5s statement timeout, 1000-row cap, `ATTACH`/`PRAGMA` rejected.
5. Loop halts on submit or at 8 steps; one JSONL trace record is written per step.

### 4.2 Scoring

Execution accuracy: the submitted query's result set is compared to the gold query's.
Comparison is order-insensitive on both rows and columns unless the question specifies ordering,
with float tolerance and explicit NULL handling. The scorer is unit-tested against 20 hand-built
`(predicted, gold)` pairs covering column reordering, row reordering, NULLs, float tolerance and
empty results before any sweep is scored.

### 4.3 Router

For task $i$, let $a_i^S, a_i^L \in \{0,1\}$ be per-tier correctness, $c_i^S, c_i^L$ their costs,
and $s_i$ an escalation signal computed **without ground truth and without consulting the large
tier**. A policy escalates $E = \{i : s_i > \tau\}$.

$$\text{Acc}(\tau) = \frac{1}{n}\Big(\sum_{i \notin E} a_i^S + \sum_{i \in E} a_i^L\Big), \qquad
\text{Cost}(\tau) = \frac{1}{n}\Big(\sum_i c_i^S + \sum_{i \in E} c_i^L\Big)$$

Escalation costs $c_i^S + c_i^L$ — the failed small-model attempt is billed too. Per-task gain is
$\delta_i = a_i^L - a_i^S \in \{-1,0,+1\}$. Oracle routing escalates exactly $\{i : \delta_i = +1\}$,
ordered by $\delta_i / c_i^L$ under a budget; it is the upper bound for any router using any
signal. Signal quality is the AUC of $s_i$ against $\mathbb{1}[\delta_i = +1]$. Three signals are
implemented (§2.3), all computable on the cheap tier alone.

### 4.4 Cost accounting

Two meters, recorded separately on every trace record:

- `usd_billed` — money leaving the account on this run. Zero on a response-cache hit.
- `usd_attributed` — what the call cost when first made, from the stored usage object.

**Every USD figure in this README is attributed.** Billed cost would make any policy evaluated
after the cache warmed appear free, which would turn the x-axis of the headline plot into an
artifact of evaluation order.

Input tokens are priced in three categories — fresh, cache-write, cache-read — because provider
prefix caching bills them at different multipliers. Pricing all input at the fresh rate overstates
cost by roughly 2.5× on a prefix-cached run, in the flattering direction.

Two caches, unrelated mechanisms:

| | Response cache (local) | Prefix cache (provider) |
|---|---|---|
| What it does | Memoizes a full request→response pair | Reuses KV state for a static prompt prefix |
| Key | `sha256` over model id, messages, tools, system, $T$, top-$p$, seed, max tokens, stop | Provider-side, prefix match |
| Effect on calls | Eliminates them | None |
| Effect on billing | $0 on hit | Discounted input tokens |
| Lifetime | Durable, committed | Minutes |

The response cache is prefix-fragile inside an agent loop: step $t$'s key contains steps $1..t-1$.
With per-step replay probability $q$ over $s$ steps, expected hits are $(1-q^s)/(1-q)$ — 84% at
$q = 0.95, s = 8$, 42% at $q = 0.80$. Tool results are therefore canonicalized before entering the
message history, and no run id, timestamp, latency or path is allowed into context. Realized hit
rate is reported in §2.6.

Sweeps are executed grouped by `db_id` so the schema prefix stays warm; sampling order (stratified)
and execution order (grouped) are decoupled.

**Finding — prefix caching and schema linking are not additive** (§2.6, `results/ablation_prompt.json`).
Schema linking cuts prompt tokens by roughly an order of magnitude, which shrinks the absolute
saving from caching the prefix, and the linked card is task-dependent, so it is not a shared prefix
at all and may fall below the provider's minimum cacheable length. Measured as a 2×2.

### 4.5 Offline routing simulation

Both tiers run over all 200 tasks once. Every outcome and cost is stored in the traces. Every
router variant, signal and threshold is then evaluated by replay over those recorded outcomes at
zero marginal token cost — the full $\tau$ sweep, three signals, the oracle bound and the
budget-knapsack variant are all post-processing over two completed sweeps.

Because the simulation replays one frozen realization, all policies are compared on the *same
draw*, not merely on the same tasks. The pairing is therefore tighter than task-level pairing:
sampling noise is held fixed across policies rather than averaged over. §5 is about what that
freezing costs.

### 4.6 Trace schema

One JSONL record per step:

```
{task_id, run_id, step, tier, model, model_digest, tool, args, result_hash,
 tokens_in_fresh, tokens_in_cache_write, tokens_in_cache_read, tokens_out,
 latency_ms, usd_billed, usd_attributed, cache_hit, prompt_version}
```

Metrics, routing simulation and the failure taxonomy all read traces. Nothing downstream of a
trace makes a live call.

---

## 5. Statistical protocol

### 5.1 Estimator

$n = 200$, stratified by BIRD difficulty. The point estimate is $\hat\mu_{\text{str}} = \sum_h W_h \bar a_h$,
**not** the unweighted mean — those coincide only under proportional allocation, and $\theta$
correlates strongly with difficulty, so under any other allocation the unweighted mean estimates
accuracy on a uniform-over-difficulty population that does not exist. $W_h$ is pinned in
`data/manifest.json` at sampling time and is a property of the benchmark version sampled from.
Per-stratum figures in §2.4 are within-stratum and unweighted.

### 5.2 Variance

Model $a_i \sim \text{Bern}(\theta_i)$, $\theta_i$ the task's intrinsic success rate. By the law of
total variance, with $\mu = \mathbb{E}[\theta]$ and $\sigma^2 = \operatorname{Var}(\theta)$ over the
task population:

$$\operatorname{Var}(\hat\mu) = \underbrace{\frac{\mu(1-\mu) - \sigma^2}{n}}_{\text{sampling noise}} +
\underbrace{\frac{\sigma^2}{n}}_{\text{task heterogeneity}} = \frac{\mu(1-\mu)}{n}$$

The two components sum exactly to the binomial variance, so the bootstrap over tasks is *correct*
for the population claim — nothing is missing from it, and caching costs it nothing. On text-to-SQL
most $\theta_i$ sit near 0 or 1, so $\sigma^2$ is large and nearly all of the ±7pp at $n=200$ is
"a different 200 tasks could have been drawn," not "a re-run could differ."

Conditional on these 200 tasks — the only variance a resweep can move — the standard error is
$\sqrt{\varphi / 2n}$, where $\varphi$ is the flip rate below.

### 5.3 Flip rate, and the quantities it contaminates

Each tier is run twice over the pinned set. $\hat\varphi = \frac{1}{n}\#\{i : a_i^{(1)} \neq a_i^{(2)}\}$
is unbiased for $\varphi = \frac{2}{n}\sum_i \theta_i(1-\theta_i)$, with no distributional assumption.
Small tier is local and free; frontier costs one extra sweep.

| | $\hat\varphi$ | 95% CI | conditional SE $\sqrt{\varphi/2n}$ |
|---|---|---|---|
| small tier $\varphi^S$ | — | — | — |
| frontier tier $\varphi^L$ | — | — | — |

Freezing one draw costs little on the mean and a lot on anything built from a max or a difference
of two draws:

- **Oracle bound is upward-biased.** Single-draw oracle accuracy estimates
  $\theta^S + \theta^L - \theta^S\theta^L$, whereas the reproducible bound is
  $\max(\theta^S, \theta^L)$. The gap is $\min(\theta^S,\theta^L) - \theta^S\theta^L \ge 0$, equal to
  $\varphi/2$ in the symmetric case. Noise manufactures tasks the small model happened to fail and
  the frontier happened to pass; the oracle banks a gain that is not repeatable. The dashed line in
  §1 is corrected by $\hat\varphi/2$ and the raw value is also reported.
- **Negative transfer is inflated.** $P(\delta_i = -1) = \theta^S(1-\theta^L)$ under independence,
  so two identically capable noisy tiers produce apparent negative transfer. Reported alongside
  $\hat\varphi$.

### 5.4 McNemar stability

McNemar conditions on $b + c$, a small count with no averaging to protect it. The diagnostic
reported in §2.2 is

$$\rho = \frac{n(\varphi^S + \varphi^L)}{b + c}$$

an upper bound on the expected share of discordant mass that a re-draw would move. Noise-induced
discordance splits symmetrically between $b$ and $c$ **only if** $\varphi^S = \varphi^L$; a noisier
small tier contributes more spurious mass to $c$, biasing McNemar toward "the frontier is better."
Both flip rates are therefore measured, not just the frontier's.

### 5.5 Ratios

USD per solved task is a ratio of two random quantities. It is bootstrapped over tasks (2000
resamples, within-stratum), never propagated analytically.

---

## 6. Limitations

### 6.1 Contamination
BIRD is widely published and plausibly memorized to some degree by both tiers. This cannot be
fixed here. It is the reason the authored hard slice (§2.5) exists and the reason the two are
never pooled.

### 6.2 Label noise
A CIDR 2026 analysis of annotation errors in BIRD and Spider 2.0-Snow found that correcting
annotations shifted execution accuracy across five agents by −3% to +31% relative; on a 100-problem
sample of BIRD Dev, corrections moved absolute performance by −2% to +19% and reordered rankings by
up to three positions. All comparisons here are paired, so label noise largely cancels in the
*difference* between policies — the headline claim is robust to it. It does not cancel in the
*level*: `<ACC_FRONTIER>`% EX is a statement about the agent and about the gold annotations jointly.
`<N_LABEL_ERRORS>` of the `<N_LABELLED>` hand-labelled failures were found to be benchmark label
errors rather than agent errors. Where an expert-verified Mini-Dev variant is accessible, §2.1 is
reported on it as well.

### 6.3 Population and interval width
The estimand is accuracy on BIRD-like text-to-SQL tasks, so no finite-population correction is
applied. Defining the population as Mini-Dev's 500 examples instead would multiply the variance by
$(1 - n/N) = 0.6$ and narrow the half-width by $\approx 22\%$ — at the cost of a claim that only
covers 500 specific questions, which invites the question of why all 500 were not simply run.

### 6.4 $n = 200$
A proportion near 0.5 has a 95% half-width of roughly 7pp. Differences smaller than that are not
claimed, in either direction.

### 6.5 Reproducibility, precisely
Two distinct claims, both qualified:

- **Metric reproducibility holds unconditionally.** `make results` is a pure function of the
  committed traces.
- **Trajectory reproducibility holds only through the shipped cache.** `make sweeps` replays a
  frozen realization. It does not demonstrate that the provider would emit the same tokens again.
  $T = 0$ is not determinism: batch composition, MoE routing and kernel nondeterminism all make the
  replay probability < 1. §5.3 quantifies the residual.

Model identity is pinned by digest, not by mutable tag, and the provider-returned model string is
asserted against the requested one on every call; the cache key carries a schema version so a change
to prompt rendering invalidates loudly rather than serving incompatible entries.

### 6.6 Single benchmark, single dialect
One benchmark family and one SQL dialect. Nothing here establishes transfer to another dialect,
another schema style, or non-SQL analysis tasks.

---

## 7. Not in scope

Stated as a commitment, not an omission.

- No UI.
- No RAG, no vector store, no embeddings.
- No fine-tuning.
- No multi-agent crew. A verifier pass is a tool call, not an agent.
- No new benchmark — this consumes one.
- No attempt at SOTA. This measures a trade-off; it does not climb a leaderboard.

---

## 8. Related work

**ReViSQL** (arXiv 2603.20004) establishes the cost-Pareto framing for text-to-SQL, reporting a 30B
model matching prior SOTA at 7.5× lower per-query cost. The framing here is aligned with that
literature, not novel to it.

**CIDR 2026 annotation-error analysis** of BIRD and Spider 2.0-Snow motivates the label-noise
treatment in §6.2 and the paired-comparison protocol in §5.
