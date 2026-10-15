Architecture of the escalator project

escalator/
├── src/escalator/
│   ├── util/
│   │   └── canon.py        # the one canonical JSON (sorted keys, UTF-8, bytes/non-finite floats tagged)
│   ├── llm/
│   │   ├── types.py        # Request (every sent parameter), Response, Usage (4 token buckets), ToolCall
│   │   ├── errors.py       # PriceMissing, CacheMiss, ProviderError, ProviderSetupError, UnsupportedParameter, ...
│   │   ├── adapter.py      # complete(req, context=...) -> Response: cache, provider + retry, normalize, price
│   │   ├── cache.py        # sqlite .cache/llm.sqlite, key = sha256(canon({v, req, ctx})), raw bodies
│   │   ├── cost.py         # usage -> exact Decimal USD via configs/prices.yaml
│   │   ├── models.py       # configs/models.yaml tiers; Ollama tag -> digest check before the first provider call (cache hits skip it)
│   │   ├── __main__.py     # python -m escalator.llm {pin,ping}
│   │   └── providers/      # base (protocol, LazyProvider: built and checked at the first cache miss), ollama (/api/chat, httpx), anthropic (official SDK); module-level normalize parses cached bodies without a client
│   ├── env/
│   │   ├── sandbox.py      # execute(db_id, sql): ro+immutable conn per call, authorizer allowlist, no random/now, 30 s wall clock, row cap; introspect() for server-owned lookups
│   │   ├── schema.py       # schema card rendering (descriptions overlay), schema linking
│   │   └── server.py       # MCP server (mcp low-level Server), stdio: the four tools over the sandbox, stateless
│   ├── agent/
│   │   ├── loop.py         # run_task: ReAct-style over the MCP session, max_steps 8, retry cap 2, overflow check, one StepRecord per LLM call
│   │   ├── tools.py        # canonical manifest + hash, server startup check, jsonschema validation, result classification and display caps
│   │   └── prompts/        # <version>.yaml (v1; verifier_v1: templates only, tools from the baseline), one file per version; loader, slot renderer and placeholder check in __init__.py
│   ├── router/
│   │   ├── config.py       # configs/router.yaml: SC and verifier settings, explicit run inventory, bootstrap
│   │   ├── inventory.py    # listed runs only, role-checked, grouped per (task, sample); analysis set; no eval import
│   │   ├── execcache.py    # label-free execution of submitted SQL, outcomes persisted in .cache/exec.sqlite
│   │   ├── signals.py      # error / self-consistency / verifier from small-tier runs only (never gold, never frontier)
│   │   ├── outcomes.py     # a(r) through eval.scorers; integrity check against results/baselines.json
│   │   ├── policy.py       # threshold sweep, oracle frontier, budget interpolation, random reference
│   │   ├── report.py       # D5-D8 metrics, shared bootstrap, McNemar + Holm -> router.json / signals.json
│   │   ├── __main__.py     # python -m escalator.router (make router)
│   │   └── plots.py        # python -m escalator.router.plots (make plots): results/figures/
│   ├── trace/
│   │   ├── schema.py       # StepRecord v2 per LLM call (steps.jsonl, per-call fields as lists), RunMeta v3 (meta.json: run_kind, sample seeds, baseline gated against), strict reader
│   │   ├── published.py    # traces/published/<run_id>/ (meta.json + deterministic steps.jsonl.gz); run lookup published-first
│   │   └── replay.py       # replay_task: re-run a task against a readonly cache, check every request key
│   ├── eval/
│   │   ├── runner.py       # python -m escalator.eval.runner {smoke,baselines}: one MCP server per run, sequential tasks, paid guard, traces + results/<preset>.json
│   │   ├── collect.py      # python -m escalator.eval.collect {sc-samples,verifier-runs,frontier-repeat,estimate}: extra runs for the signals, gated against the baselines
│   │   ├── compat.py       # compatibility gate: RunMeta fields that must match the baseline vs may differ
│   │   ├── tasks.py        # manifest tasks (task_hash checked), ordered flag and tie blocks from configs/scoring.yaml; gold re-executed via the sandbox, checked against gold_result_hash
│   │   ├── scorers.py      # EX (columns permutation-invariant, multiset rows unless the task is ordered, numeric tolerance), soft-F1 secondary
│   │   └── metrics.py      # Wilson, exact McNemar, Holm, AUC (ties 1/2), shared-matrix percentile bootstrap; stdlib only
│   └── datasets/           # fetch-and-verify layer: python -m escalator.datasets {lock,fetch,verify,env,diagnose-gold,manifest}
│       ├── config.py       # configs/data.yaml and the data/raw layout
│       ├── lock.py         # sources.lock (3 sources) and env.lock: schema, validate, write
│       ├── bootstrap.py    # lock: resolve Arcwise SHA, download, discover zip members, reuse under FORCE=1
│       ├── fetch.py        # hash-while-streaming downloads, atomic rename, zip-slip-safe extraction
│       ├── gold.py         # read-only immutable SQLite, per-query deadline, gold-check cache
│       ├── descriptions.py # description overlay: BIRD database_description/ + Arcwise schemas/
│       ├── verify.py       # offline gates V/D/M/T/S and audits
│       └── manifest.py     # pinned exclusions, stratified sampling, task and gold-result hashes
├── configs/               # models.yaml (tiers, digests), prices.yaml (date-stamped), policy params; data.yaml, manifest.yaml (seed, pinned exclusions), scoring.yaml (hand-checked ordered tasks and their tie blocks), agent.yaml (prompt version, loop limits, display caps, run presets), router.yaml (SC/verifier settings, run inventory, bootstrap)
├── data/
│   ├── sources.lock       # pinned sources: bird_minidev_zip, arcwise_plat_full, arcwise_schemas
│   ├── env.lock           # pinned Python and SQLite versions
│   ├── NOTICE             # upstream attribution and licenses
│   ├── gold_allowlist.json # human-edited gold exceptions
│   ├── audit/             # verification audits: ids, counts, file/column names only
│   ├── raw/               # gitignored: fetched data, gold-check cache
│   └── manifest.json      # the 200 pinned tasks: ids, difficulty, task_hash, gold_result_hash
├── results/               # committed metrics JSON — the actual evidence
├── traces/                # gitignored except traces/published/ (runs behind published numbers)
├── tests/                 # tests/datasets/{unit,integration}; tests/infra/{llm,trace,env,integration}; tests/eval (scorer, gold side); tests/test_scorer.py (20 hand-made pairs); tests/test_server.py (MCP, needs data/raw); tests/agent (loop, tools, prompts, runner, replay over a real MCP server on tests/fixtures/agent_db.sql)
├── Makefile               # make test | test-local | data | data-lock | data-verify | env-lock | manifest | smoke | baselines | sc-samples | verifier-runs | frontier-repeat | sc-estimate | publish-traces | results
└── README.md

Data flow: `data-lock` writes `sources.lock` → `data` fetches whatever is missing or invalid →
`data-verify` checks it offline and writes `data/audit/` → `manifest` removes the exclusions pinned in
`configs/manifest.yaml` and samples `data/manifest.json`. The benchmark is Arcwise-Plat-Full,
executed on the Mini-Dev SQLite databases; Mini-Dev's JSON supplies difficulty labels only.

Run flow: `eval/runner.py` starts the MCP server (`env/server.py`) over stdio and checks its tools against
`agent/tools.py` → for each task, `agent/loop.py` fetches the schema card with `get_schema`, renders the
prompt (`agent/prompts/`), calls the model through `llm/adapter.py` (cache first), validates each tool
call client-side and executes it on the server → one `StepRecord` per LLM call in
`traces/<run_id>/steps.jsonl` → the submitted SQL is scored by `eval/tasks.py` → `results/<preset>.json`.

Collection flow (week 3): `eval/collect.py` plans one run per (kind, model) from `configs/router.yaml`
→ starts the MCP server → builds each run's `RunMeta` and compares it with the baseline's
(`eval/compat.py`), all before the first LLM call → runs every task × sample through `agent/loop.py`
exactly as the runner does → `traces/<kind>-<model>-<prompt_version>/`. `make publish-traces` copies
runs into `traces/published/`, which the simulation reads through the inventory.

Routing flow (week 3, $0): `make router` loads the inventory's runs (`router/inventory.py`) → recomputes
a_S and a_L with `eval.scorers` and checks them against `results/baselines.json` (`router/outcomes.py`) →
computes the signals from small-tier runs (`router/signals.py`, submitted SQL executed through
`router/execcache.py`) → simulates every policy (`router/policy.py`) → `results/signals.json` and
`results/router.json` (`router/report.py`) → `make plots` draws `results/figures/` (`router/plots.py`).
