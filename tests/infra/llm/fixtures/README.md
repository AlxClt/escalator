# Provider payload fixtures

These bodies are **hand-written in each provider's documented response shape**, not recorded from
live calls (no live call had been made when U1 was written). Replace them with real recorded bodies
from the first manual Ollama / Anthropic runs (`python -m escalator.llm ping ...`; the raw body is in
`.cache/llm.sqlite`), and update the expected token counts in `tests/infra/llm/test_cost.py` to match.
