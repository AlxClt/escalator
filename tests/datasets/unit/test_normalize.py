from __future__ import annotations

import pytest

from escalator.datasets.verify import normalize_id


# U10
@pytest.mark.parametrize("value", [137, "137"])
def test_u10_ids_normalize(value: object) -> None:
    assert normalize_id(value) == "137"


def test_u10_zero_is_valid() -> None:
    assert normalize_id("0") == "0"
    assert normalize_id(0) == "0"


@pytest.mark.parametrize("value", ["0137", " 137", "137 ", "13a", "", "-1", -1, True, 1.0, None, "١٣"])
def test_u10_invalid_ids_raise(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_id(value)
