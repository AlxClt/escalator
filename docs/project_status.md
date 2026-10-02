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

Part 1 exit, manual work still to do:

- Install Ollama >= 0.20.2 and pull `gemma4:e4b-it-qat`, `gemma4:12b-it-qat`, `gemma4:31b-it-qat`.
- Set `OLLAMA_BASE_URL` in `.env` to the machine hosting Ollama (no default; a remote server needs `OLLAMA_HOST=0.0.0.0`).
- `uv run python -m escalator.llm pin` to write the full digests into `configs/models.yaml` and `configs/prices.yaml` (they read `UNPINNED` until then, and the adapter refuses unpinned models). Check them against the library short digests `ee6656371218` (e4b), `38044be4f923` (12b) and `e0812a55773b` (31b).
- `uv run python -m escalator.llm ping gemma4-e4b`: the second call must show `cache_hit=True`, $0 and 0 provider calls.
- Anthropic ping, run by hand only: `uv run python -m escalator.llm ping sonnet-5-5 --paid`.
- Replace the hand-written payload fixtures in `tests/infra/llm/fixtures/` with the recorded bodies from those pings.

To be built (part 2):

- MCP server
- scorer
- CI
- Create scorer unit test file with placeholder for the data. DO NOT CREATE THE DATA YOURSELF 

Manual work to be done:

- Create scorer unit test with 20 hand-made (predicted, gold) pairs covering column reordering, row reordering, NULL handling, float tolerance, empty results.
- Add those tests in tests/test_scorer.py

Step 1 IS NOT COMPLETE UNTIL THE hand made test pairs have been written.

Part 2 flag: a gold result over 1000 rows can never be matched through the capped tool view. Count such tasks when the scorer lands.

Consequences to carry into week 2:

- **Frontier temperature cannot be set.** Sonnet 5.5 rejects non-default sampling parameters, so frontier outcomes are samples at the provider default, not T = 0. A k = 2 frontier sweep is the only way to measure their flip rate.
- **Gemma 4 sizes differ from the requested 3B / 9B / ~30B.** Gemma 4 has no 3B or 9B; e4b and 12b are the nearest sizes with native tool calling in Ollama. Gemma 2 has 2B/9B/27B but no tool calling. `e2b` is the alternative if "~3B" should mean smaller than e4b.
- **QAT tags for all three sizes,** so the 4-bit scheme is the same kind across sizes. The library's `-it-q4_K_M` tags were re-pointed on 01/10/2026; pinning by digest is what protects the measurement.
- **Tag digests to verify at pull.** The Ollama library lists `ee6656371218` (e4b), `38044be4f923` (12b) and `e0812a55773b` (31b) as of 02/10/2026. Record the full digests from `/api/tags`.

Target completion date: 06/10/2026
