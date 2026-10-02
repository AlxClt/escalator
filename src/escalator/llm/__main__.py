"""python -m escalator.llm {pin,ping}

pin   resolve each Ollama tag to its full digest via /api/tags and write it into configs/models.yaml
      and configs/prices.yaml.
ping  one fixed prompt through the adapter, twice; the second call must be a cache hit at $0.
      The frontier tier needs --paid: frontier runs are only ever triggered by hand.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from escalator.llm.adapter import Adapter
from escalator.llm.cache import DEFAULT_CACHE, Cache
from escalator.llm.cost import DEFAULT_PRICES, PriceTable
from escalator.llm.models import DEFAULT_MODELS, check_ollama, load_models
from escalator.llm.providers.base import Provider
from escalator.llm.providers.ollama import OllamaProvider
from escalator.llm.types import Message, Request

PING_PROMPT = "Reply with the single word: pong"


def pin(models_path: Path, prices_path: Path) -> int:
    provider = OllamaProvider()
    cfg = load_models(models_path)
    version = provider.version()
    live = provider.tags()
    models_text = models_path.read_text(encoding="utf-8")
    prices_text = prices_path.read_text(encoding="utf-8")
    missing = []
    for spec in cfg.models.values():
        if spec.provider != "ollama" or spec.tag is None:
            continue
        digest = live.get(spec.tag)
        if digest is None:
            missing.append(spec.tag)
            continue
        tag = re.escape(spec.tag)
        models_text, n = re.subn(rf"(tag: {tag}\n\s+digest: )\S+", rf"\g<1>{digest}", models_text)
        prices_text, m = re.subn(rf"(ollama/{tag}@)\S+?:", rf"\g<1>{digest}:", prices_text)
        if n != 1 or m != 1:
            print(f"{spec.tag}: could not locate its entry in models.yaml / prices.yaml", file=sys.stderr)
            return 1
        print(f"{spec.tag} -> {digest}")
    if missing:
        print(f"not pulled: {', '.join(missing)} (run `ollama pull <tag>`)", file=sys.stderr)
        return 1
    models_path.write_text(models_text, encoding="utf-8")
    prices_path.write_text(prices_text, encoding="utf-8")
    print(f"Ollama server {version}")
    return 0


def ping(name: str, paid: bool, cache_path: Path) -> int:
    cfg = load_models()
    spec = cfg.models[name]
    providers: dict[str, Provider] = {}
    if spec.provider == "anthropic":
        if not paid:
            print("frontier ping costs money: pass --paid to run it", file=sys.stderr)
            return 2
        from escalator.llm.providers.anthropic import AnthropicProvider

        providers["anthropic"] = AnthropicProvider()
    else:
        ollama = OllamaProvider()
        print(f"Ollama server {check_ollama(cfg, ollama)}")
        providers["ollama"] = ollama
    adapter = Adapter(providers, Cache(cache_path), PriceTable.load(DEFAULT_PRICES))
    req = Request(model=spec.model_id, messages=(Message(role="user", content=PING_PROMPT),), max_tokens=256,
                  **spec.settings)  # type: ignore[arg-type]  # settings are validated by Request
    for i in (1, 2):
        before = adapter.provider_calls
        r = adapter.complete(req, context={"tool_manifest_sha": "none"})
        print(f"run {i}: cache_hit={r.cache_hit} usd={r.usd} provider_calls={adapter.provider_calls - before}"
              f" latency_ms={r.latency_ms} stop={r.stop_reason} usage={r.usage.model_dump()} text={r.text!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m escalator.llm")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pin")
    pp = sub.add_parser("ping")
    pp.add_argument("name", help="a key of configs/models.yaml, e.g. gemma4-e4b")
    pp.add_argument("--paid", action="store_true", help="allow a paid frontier call")
    pp.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    args = p.parse_args(argv)
    if args.cmd == "pin":
        return pin(DEFAULT_MODELS, DEFAULT_PRICES)
    return ping(args.name, args.paid, args.cache)


if __name__ == "__main__":
    sys.exit(main())
