# Project Status - Escalator

**Last Updated:** 02/10/2026
**Project Start:** 30/09/2026

---

## Current Step

Steps are direct references to steps in [project_specs.md](project_specs.md)

### Step 0: Data — done

- `make data` and its verification suite are built (spec: `docs/tasks/make-data (rev 2).md`). The benchmark is Arcwise-Plat-Full (498 tasks) on the Mini-Dev SQLite databases, pinned at Arcwise commit `fe766045c55b6875a43b30e9ac7683df5582f8cf`.
- Every gate passes on Python 3.12.14 / SQLite 3.53.1. All 498 gold queries return rows; `gold_timeout_s` is 2089.
- 3 effective description files are not UTF-8 (`env/schema.py` must handle them).
- `make manifest` pinned the evaluation subset in `data/manifest.json`:
  - frame 484 = 498 minus 14 exclusions pinned in `configs/manifest.yaml` (12 date-dependent; 518 and 701, whose gold exceeds the 5 s sandbox timeout);
  - 200 tasks: 61 simple, 100 moderate, 39 challenging (largest-remainder allocation, seed 20261001).
- Gold timing is load-sensitive: cold-cache runs pushed golds that take about 1 s up to 6 s. That is why the exclusions are pinned instead of re-derived from A2.

## Next step (details to be added)

### Step 1: Infrastructure

Part 1 (spec: `docs/tasks/week1_part_1.md`) is built and its hermetic tests pass (U1–U6, I1 under `tests/infra/`):

- [x] cost meter
- [x] cache
- [x] adapter (Ollama and Anthropic providers)
- [x] trace schema
- [x] sandbox

Part 1 exit — done (02/10/2026):

- [x] Ollama runs on a separate host, reached through `OLLAMA_BASE_URL` in `.env`.
- [x] Digests pinned with `python -m escalator.llm pin` in `configs/models.yaml` and `configs/prices.yaml`; they match the library short digests `ee6656371218` (e4b), `38044be4f923` (12b) and `e0812a55773b` (31b).
- [x] `ping gemma4-e4b` twice: miss, then `cache_hit=True` at $0 with 0 provider calls.
- [x] `ping sonnet-5-5 --paid` twice: accepted with `thinking: between_tools` and `effort: medium`, no cache usage reported, second call a cache hit.
- [x] Usage fields of the recorded bodies match the U1 fixtures. The fixtures stay hand-written: the pings use no tools, so no recorded body has a tool call yet.

To be built (part 2):

- MCP server
- scorer
- CI: `make smoke` from cache (the `data` job was removed: the dataset is installed locally, never in CI)
- Create scorer unit test file with placeholder for the data. DO NOT CREATE THE DATA YOURSELF 

Manual work to be done:

- Create scorer unit test with 20 hand-made (predicted, gold) pairs covering column reordering, row reordering, NULL handling, float tolerance, empty results.
- Add those tests in tests/test_scorer.py

Step 1 IS NOT COMPLETE UNTIL THE hand made test pairs have been written.

Decisions to make for part 2:

| # | Decision | Why it is open | Options |
| --- | --- | --- | --- |
| P1 | How CI runs `make smoke` without the dataset | The spec has CI run `make smoke` from cache, but smoke executes SQL on the BIRD databases and CI no longer installs them | Commit a small fixture database plus a replay cache for CI; or keep smoke local only and have CI run `make test` alone |
| P3 | Tasks whose gold result exceeds the 1000-row cap | 7 of the 200 tasks: 346 (29,936 rows), 340 (25,061), 11 (7,804), 532 (4,430), 1124 (3,339), 407 (1,693), 528 (1,664). The scorer runs the submitted SQL uncapped, so these tasks can still be scored correct. But while working, the model only ever sees 1000 rows and `truncated=true`, so it may "fix" a correct query (add a `LIMIT`, an aggregate, a filter) and turn it wrong. That would be a systematic penalty on these 7 tasks, plausibly larger for the small tier | Keep them and report their accuracy separately (no manifest change); add the total row count to the truncated tool result so the model knows the size is real; or exclude them, which re-pins `data/manifest.json` |
| P4 | Ollama latency includes model load | The first e4b call took 55.5 s for a 2-token reply, most of it presumably loading the model; `latency_ms` would skew latency metrics | Warm each model up before a sweep; or record `total_duration - load_duration` from the Ollama body |
| P5 | Recorded tool-call fixtures | U1's tool-call payloads are hand-written | Record one tool-calling body per provider once the MCP server and agent loop run, and swap them in |
| P6 | Counting small-tier tool-call failures | Ollama returns tool arguments already parsed, so `args=None` may never occur for the small tier; a call it fails to parse may come back as plain text instead (to verify) | Count such turns as validation failures, or as `end = no_tool_call`; decide before the baseline sweep |

Consequences to carry into week 2:

- **Frontier temperature cannot be set.** Sonnet 5.5 rejects non-default sampling parameters, so frontier outcomes are samples at the provider default, not T = 0. A k = 2 frontier sweep is the only way to measure their flip rate.
- **Gemma 4 sizes differ from the requested 3B / 9B / ~30B.** Gemma 4 has no 3B or 9B; e4b and 12b are the nearest sizes with native tool calling in Ollama. Gemma 2 has 2B/9B/27B but no tool calling. `e2b` is the alternative if "~3B" should mean smaller than e4b.
- **QAT tags for all three sizes,** so the 4-bit scheme is the same kind across sizes. The library's `-it-q4_K_M` tags were re-pointed on 01/10/2026; pinning by digest is what protects the measurement.
- **Tag digests to verify at pull.** The Ollama library lists `ee6656371218` (e4b), `38044be4f923` (12b) and `e0812a55773b` (31b) as of 02/10/2026. Record the full digests from `/api/tags`.

Target completion date: 06/10/2026
