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
│   └── eval/
│       ├── runner.py
│       ├── scorers.py      # EX, plus soft-F1 as a secondary
│       └── metrics.py
├── configs/               # model tiers, prices (date-stamped), policy params
├── data/manifest.json     # pinned task ids + row hashes
├── results/               # committed metrics JSON — the actual evidence
├── notebooks/             # plots only, generated from results/
├── tests/
├── Makefile               # make smoke | make baselines | make results
└── README.md
