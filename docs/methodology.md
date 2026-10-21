# Data, Methodology and statistical protocol

## Data

The databases used are 11 SQLite databases from the BIRD Mini-Dev benchmark (JinLi et al., NeurIPS 2023, arXiv:2305.03111.
), with database descriptions files and difficulty labels: [https://github.com/bird-bench/mini_dev](https://github.com/bird-bench/mini_dev)

The tasks the the Agent sees and is evaluated on come from Arcwise-Plat-Full, a corrected version of the original BIRD Mini-Dev benchmark tasks (Jin et al., 2026, arXiv:2601.08778): [https://github.com/uiuc-kang-lab/text_to_sql_benchmarks](https://github.com/uiuc-kang-lab/text_to_sql_benchmarks)

Example of a task, with the gold reference query under the "SQL" key:

```
  {
    "question_id": "1472",
    "question": "In 2012, who had the least consumption in LAM?",
    "evidence": "Year 2012 can be presented as Between 201201 And 201212; The first 4 strings of the Date values in the yearmonth table can represent year.",
    "SQL": "SELECT T1.CustomerID FROM customers AS T1 INNER JOIN yearmonth AS T2 ON T1.CustomerID = T2.CustomerID WHERE T1.Segment = 'LAM' AND SUBSTR(T2.Date, 1, 4) = '2012' GROUP BY T1.CustomerID ORDER BY SUM(T2.Consumption) ASC LIMIT 1",
    "db_id": "debit_card_specializing"
  },
```

## Method

### Models

We study the task escalation from small tier models (S) to frontier paid models (L).

Small tier models tested:

- gemma4:e4b
- gemma4:12b
- gemma4:31b
- claude-haiku-5-5

frontier models tested:

- claude-sonnet-5.5
- claude-opus-5.5 (only in the complementary study on the challenging tasks strata)

### Agent loop

---
The agent is a ReAct-style agent performing a maximum of 8 model steps with an explicit stop condition.
It has four tools:

- `get_schema`: gets the database schema card description
- `sample_rows`: gets a sample of rows from a given table
- `execute_sql`: executes an SQL query and returns the result
- `submit_answer`: runs exactly as `execute_sql` but the loop treats it as terminal. Used by the agent to signal its query is ready.

We store One JSONL trace line per model call:
`{task_id, run_id, sample_idx, step, tier, model, tool, args, tool_status, result_hash, tokens_in, tokens_out, latency_ms, usd, end}`.

Everything downstream is computed from the stored traces.

The agent runs as the signle client of an MCP server containing the sandbox SQLite connection and the four tools.

### Router

---
For task `i`, with `S` and `L` respectively indicating small and large tier, let:

- `a_i^S, a_i^L ∈ {0,1}` be per-tier correctness
- `c_i^S, c_i^L` be per-tier costs
- `c_i^σ` the cost of the signal's extra runs (see signals description below)
- `s_i` an error signal computed without ground truth

A policy escalates `E = {i : s_i > τ}`:

```
Acc(τ)  = (1/n) [ Σ_{i∉E} a_i^S + Σ_{i∈E} a_i^L ]
Cost(τ) = (1/n) [ Σ_i (c_i^S + c_i^σ) + Σ_{i∈E} c_i^L ]
```

`c_i^S` is zero for the 3 gemma models, we consider them as local models, though this assumption can be discussed.

Per-task gain is `δ_i = a_i^L − a_i^S`:

- `δ_i = 1` means that the escalation is successfull (the large model succeed, where the small model failed)
- `δ_i = 0` means that the escalation was useless (Both model failed or succeeded)
- `δ_i = -1` means that the escalation was detrimental (the large model failed, where the small model succeed)

Since the error signals are computed without ground truth, it is possible that they trigger detrimental escalations (SEE TABLE XX)

The **unconstrained oracle** escalates exactly `{i : δ_i = +1}`: it is a single point.

We define the **oracle frontier** as the curve traced by admitting those tasks in increasing `c_i^L` under a sweeping budget, which gives the dashed line on the plot. It can be read as the "maximum accuracy achievable at matched cost".

Signal quality is computed as AUC of `s_i` against `1[δ_i = +1]`, i.e. against the unconstrained oracle's escalation decisions.

### Signals definition

---

- `error`: `s_i = 1` if the baseline run has no answer, i.e. it ended without submitting (no tool call,
  step limit, validation retries exhausted, context overflow) or its submitted SQL fails when
  executed; `s_i = 0` otherwise.
- `self_consistency`: k = 3 extra runs of the small model at T = 0.7 (seeds 1, 2, 3), separate from
  the T = 0 baseline. `m` is the size of the largest group of samples that agree with one another,
  and `s_i = 1 − m/3`, so `s_i ∈ {0, 1/3, 2/3, 1}`. This way, `s_i` correspond to the proportion of samples that disagree.
  Extra cost (`c_i^σ`): 3 small-tier runs.
- `verifier`: An extra run of the same model, with the same baseline settings (T = 0, same seed) but a different prompt (`verifier_v1`) conveying the same guidelines but formulated differently. The agent
  solves the task again from scratch and never sees the baseline answer. `s_i = 1` if the verifier's answer is different from the baseline's, `s_i = 0` if they agree. `s_i ∈ {0, 1}`. Extra cost (`c_i^σ`): 1 small-tier run.

## Statistical protocol

### Metrics

**EX (%):**

EX (%) is the accuracy of an agent over the SQL tasks. This notation is taken from the original BIRD benchmark.

For a Task `T_i`, the model generates a query `Q_i` which returns a result `R_i` when executed over the sandbox.
Each task `T_i` has a corresponding gold query `Q_g,i` returning a gold result `R_g,i`.

For a task `T_i`, `EX(T_i)=1` when `R_i=R_g,i`. Column and row order in the result is ignored unless the question in the task explicitely requires it (example: "Give the sales volume per region for 2026 ordered by decreasing order")

EX (%) is tha average of all `EX(T_i)` expressed as a percentage.

**Soft F1:**

We report the soft F1 as a secondary metric in Table 6 in [results.md](results.md)

Let:

- `tp` (true positives) the number of concordant rows between the predicted result and the glod result
- `fp` (false positives) the number of predicted rows absent from the gold result
- `fn` (true positives) the number of gold result rows absent from the predicted result

The soft F1 is computed like the F1 score with the above devinitions of tp, fp and fn:

- Precision: `p = tp/(tp+fp)`
- Recall: `r = tp/(tp+fn)`
- Soft F1: `F1 = 2 / (1/r + 1/p)`

### Tasks

n = 200 tasks, fixed in advance, stratified by dificulty stratum.

### Policies comparison

Policies comparisons are paired: two policies are compared using exact McNemar test on the discordant pairs of those two policies. The null hypothesis is that both policies are equally accurate.

Mc Nemar test statistic is  `X² = (b - c)²/(b+c)` where `b` is the number of tasks where only the routed policy is right, and `c` the nulber of tasks where only the frontier is right. Under the null hypothesis, the test statistic follow a chi-squared distribution with one degree of freedom.

### Single proportion measures

For EX, escalation rate and P(δ) we use the Wilson score interval

### Bootstrap intervals

For paired differences (Δ vs frontier, Δ vs the small model), USD/solved task, the cost ratio, oracle gap and AUC, we use a single shared bootstrap over tasks with 2000 resamples.

### Multiplicity

The search for the best escalation routine covers every signal/threshold/model combination.

To counteract the problem of multiple comparison, we apply the Holm–Bonferroni correction to control the family wise error rate. Considering the different signal and thresholds tested, there are 16 non degenerate policy/thresholds combinations after dropping the ones escalating either all or zero tasks. We use m = 16 for the correction.

---
