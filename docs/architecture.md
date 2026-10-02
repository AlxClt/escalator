Architecture of the escalator project

escalator/
├── src/escalator/
│   ├── util/
│   │   └── canon.py        # the one canonical JSON (sorted keys, UTF-8, bytes/non-finite floats tagged)
│   ├── llm/
│   │   ├── types.py        # Request (every sent parameter), Response, Usage (4 token buckets), ToolCall
│   │   ├── errors.py       # PriceMissing, CacheMiss, ProviderError, ContextOverflow, ...
│   │   ├── adapter.py      # complete(req, context=...) -> Response: cache, provider + retry, normalize, price
│   │   ├── cache.py        # sqlite .cache/llm.sqlite, key = sha256(canon({v, req, ctx})), raw bodies
│   │   ├── cost.py         # usage -> exact Decimal USD via configs/prices.yaml
│   │   ├── models.py       # configs/models.yaml tiers; Ollama tag -> digest check at startup
│   │   ├── __main__.py     # python -m escalator.llm {pin,ping}
│   │   └── providers/      # base (protocol), ollama (/api/chat, httpx), anthropic (official SDK)
│   ├── env/
│   │   ├── sandbox.py      # execute(db_id, sql): ro+immutable conn per call, authorizer allowlist, 30 s wall clock, row cap
│   │   ├── schema.py       # schema card rendering, schema linking
│   │   └── server.py       # MCP server, stdio: the four tools over the sandbox
│   ├── agent/
│   │   ├── loop.py         # ReAct-style, max_steps, explicit stop; MCP client
│   │   ├── tools.py        # canonical tool manifest, validation, provider translation
│   │   └── prompts/        # versioned, one file per version
│   ├── router/
│   │   ├── signals.py      # error / self-consistency / verifier
│   │   └── policy.py       # threshold sweep, oracle, budget knapsack
│   ├── trace/
│   │   ├── schema.py       # StepRecord per LLM call (steps.jsonl), RunMeta (meta.json), strict reader
│   │   └── replay.py       # deterministic re-run from trace
│   ├── eval/
│   │   ├── runner.py
│   │   ├── scorers.py      # EX, plus soft-F1 as a secondary
│   │   └── metrics.py
│   └── datasets/           # fetch-and-verify layer: python -m escalator.datasets {lock,fetch,verify,env,diagnose-gold,manifest}
│       ├── config.py       # configs/data.yaml and the data/raw layout
│       ├── lock.py         # sources.lock (3 sources) and env.lock: schema, validate, write
│       ├── bootstrap.py    # lock: resolve Arcwise SHA, download, discover zip members, reuse under FORCE=1
│       ├── fetch.py        # hash-while-streaming downloads, atomic rename, zip-slip-safe extraction
│       ├── gold.py         # read-only immutable SQLite, per-query deadline, gold-check cache
│       ├── descriptions.py # description overlay: BIRD database_description/ + Arcwise schemas/
│       ├── verify.py       # offline gates V/D/M/T/S and audits
│       └── manifest.py     # pinned exclusions, stratified sampling, task and gold-result hashes
├── configs/               # models.yaml (tiers, digests), prices.yaml (date-stamped), policy params; data.yaml, manifest.yaml (seed, pinned exclusions)
├── data/
│   ├── sources.lock       # pinned sources: bird_minidev_zip, arcwise_plat_full, arcwise_schemas
│   ├── env.lock           # pinned Python and SQLite versions
│   ├── NOTICE             # upstream attribution and licenses
│   ├── gold_allowlist.json # human-edited gold exceptions
│   ├── audit/             # verification audits: ids, counts, file/column names only
│   ├── raw/               # gitignored: fetched data, gold-check cache
│   └── manifest.json      # the 200 pinned tasks: ids, difficulty, task_hash, gold_result_hash
├── results/               # committed metrics JSON — the actual evidence
├── notebooks/             # plots only, generated from results/
├── traces/                # gitignored except traces/published/ (runs behind published numbers)
├── tests/                 # tests/datasets/{unit,integration}; tests/infra/{llm,trace,env,integration}
├── Makefile               # make test | data | data-lock | data-verify | env-lock | manifest | smoke | baselines | results
└── README.md

Data flow: `data-lock` writes `sources.lock` → `data` fetches whatever is missing or invalid →
`data-verify` checks it offline and writes `data/audit/` → `manifest` removes the exclusions pinned in
`configs/manifest.yaml` and samples `data/manifest.json`. The benchmark is Arcwise-Plat-Full,
executed on the Mini-Dev SQLite databases; Mini-Dev's JSON supplies difficulty labels only.
