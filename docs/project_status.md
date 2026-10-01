# Project Status - Escalator

**Last Updated:** 01/10/2026
**Project Start:** 30/09/2026

---

## Current Step

Steps are direct references to steps in [project_specs.md](project_specs.md)

### Step 0: Data — done

- `make data` and its verification suite are built (spec: `docs/tasks/make-data (rev 2).md`). The benchmark is Arcwise-Plat-Full (498 tasks) on the Mini-Dev SQLite databases, pinned at Arcwise commit `fe766045c55b6875a43b30e9ac7683df5582f8cf`.
- Every gate passes on Python 3.12.14 / SQLite 3.53.1. All 498 gold queries return rows; `gold_timeout_s` is 2089.
- Audit findings for `make manifest`:
  - 2 golds (518, 701) exceed the 5 s sandbox timeout;
  - 12 golds are date-dependent;
  - 3 effective description files are not UTF-8.
- Next: `make manifest` (gold-result hashing, exclusions, sampling of the pinned subset).

## Next step (details to be added)

### Step 1: Infrastructure

To be built:

- cost meter
- cache
- adapter
- trace schema
- sandbox
- MCP server
- scorer
- CI
- Create scorer unit test file with placeholder for the data. DO NOT CREATE THE DATA YOURSELF 

Manual work to be done:

- Create scorer unit test with 20 hand-made (predicted, gold) pairs covering column reordering, row reordering, NULL handling, float tolerance, empty results.
- Add those tests in tests/test_scorer.py

Step 1 IS NOT COMPLETE UNTIL THE éà hand made test pairs have been written.

Target completion date: 06/10/2026
