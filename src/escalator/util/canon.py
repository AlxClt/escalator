"""The one canonical JSON encoding: cache keys, result hashes and trace hashing all call it."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping


def _tag(obj: object) -> object:
    """Replace non-JSON values by tagged objects; never drop them."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        if math.isfinite(obj):
            return obj
        return {"$float": "nan" if math.isnan(obj) else ("inf" if obj > 0 else "-inf")}
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"$hex": bytes(obj).hex()}
    if isinstance(obj, Mapping):
        out: dict[str, object] = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"canonical JSON keys must be str, got {type(k).__name__}")
            out[k] = _tag(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [_tag(v) for v in obj]
    raise TypeError(f"no canonical JSON encoding for {type(obj).__name__}")


def dumps(obj: object) -> bytes:
    """Sorted keys, UTF-8, no insignificant whitespace; bytes and non-finite floats tagged."""
    return json.dumps(
        _tag(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def sha256(obj: object) -> str:
    return hashlib.sha256(dumps(obj)).hexdigest()
