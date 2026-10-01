Architecture of the escalator project

escalator/
├── src/escalator/
│   ├── llm/
│   │   ├── adapter.py      # complete(messages, tools, model) -> Response
│   │   ├── cache.py        # sqlite, key = sha256(model|messages|tools|T|seed|max_tokens)
│   │   ├── cost.py         # usage -> USD via configs/prices.yaml
│   │   └── providers/      # ollama, anthropic, openai
│   ├── env/
│   │   ├── sandbox.py      # read-only conn, statement timeout, row cap
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
│   │   ├── schema.py       # one JSONL record per step
│   │   └── replay.py       # deterministic re-run from trace
│   ├── eval/
│   │   ├── runner.py
│   │   ├── scorers.py      # EX, plus soft-F1 as a secondary
│   │   └── metrics.py
│   └── datasets/           # fetch-and-verify layer: python -m escalator.datasets {lock,fetch,verify,env,diagnose-gold}
│       ├── config.py       # configs/data.yaml and the data/raw layout
│       ├── lock.py         # sources.lock (3 sources) and env.lock: schema, validate, write
│       ├── bootstrap.py    # lock: resolve Arcwise SHA, download, discover zip members, reuse under FORCE=1
│       ├── fetch.py        # hash-while-streaming downloads, atomic rename, zip-slip-safe extraction
│       ├── gold.py         # read-only immutable SQLite, per-query deadline, gold-check cache
│       ├── descriptions.py # description overlay: BIRD database_description/ + Arcwise schemas/
│       └── verify.py       # offline gates V/D/M/T/S and audits
├── configs/               # model tiers, prices (date-stamped), policy params; data.yaml
├── data/
│   ├── sources.lock       # pinned sources: bird_minidev_zip, arcwise_plat_full, arcwise_schemas
│   ├── env.lock           # pinned Python and SQLite versions
│   ├── NOTICE             # upstream attribution and licenses
│   ├── gold_allowlist.json # human-edited gold exceptions
│   ├── audit/             # verification audits: ids, counts, file/column names only
│   ├── raw/               # gitignored: fetched data, gold-check cache
│   └── manifest.json      # pinned task ids + row hashes
├── results/               # committed metrics JSON — the actual evidence
├── notebooks/             # plots only, generated from results/
├── tests/                 # tests/datasets/{unit,integration}
├── Makefile               # make test | data | data-lock | data-verify | env-lock | smoke | baselines | results
└── README.md

Data flow: `data-lock` writes `sources.lock` → `data` fetches whatever is missing or invalid →
`data-verify` checks it offline and writes `data/audit/`. The benchmark is Arcwise-Plat-Full,
executed on the Mini-Dev SQLite databases; Mini-Dev's JSON supplies difficulty labels only.
