"""User-made color filters.

Two kinds, both turned into a 3x3 color matrix for the Windows color-filter
engine:

* ``tint``  -- the picture becomes grayscale, then tinted with one color
  (like the built-in Night vision or Amber looks).
* ``mixer`` -- a channel mixer: each output color (red, green, blue) is a mix
  of the input colors.  This can do anything a color filter can, including
  copying a built-in filter and adjusting it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import curves
from .curves import COLOR_FILTERS, CUSTOM_PREFIX, IDENTITY3, MONO_TINTS, Matrix

TINT_RANGE = (0.0, 1.5)
MIXER_RANGE = (-2.0, 2.0)
MAX_NAME = 40


def _num(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


@dataclass
class CustomFilter:
    kind: str = "tint"
    tint: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    matrix: Matrix = field(default_factory=lambda: [row[:] for row in IDENTITY3])

    def to_matrix(self) -> Matrix:
        if self.kind == "tint":
            return curves.mono_tint_matrix(self.tint)
        return [row[:] for row in self.matrix]

    def to_dict(self) -> dict:
        return {"kind": self.kind, "tint": list(self.tint),
                "matrix": [list(r) for r in self.matrix]}

    @classmethod
    def from_dict(cls, data) -> "CustomFilter":
        f = cls()
        if not isinstance(data, dict):
            return f
        if data.get("kind") in ("tint", "mixer"):
            f.kind = data["kind"]
        tint = data.get("tint")
        if isinstance(tint, (list, tuple)) and len(tint) == 3:
            f.tint = [_num(v, *TINT_RANGE, 1.0) for v in tint]
        m = data.get("matrix")
        if (isinstance(m, (list, tuple)) and len(m) == 3
                and all(isinstance(r, (list, tuple)) and len(r) == 3 for r in m)):
            f.matrix = [[_num(m[i][j], *MIXER_RANGE, IDENTITY3[i][j]) for j in range(3)]
                        for i in range(3)]
        return f


def key_for(name: str) -> str:
    return CUSTOM_PREFIX + name


def name_from_key(key: str) -> str:
    return key[len(CUSTOM_PREFIX):] if key.startswith(CUSTOM_PREFIX) else key


def clean_name(name: str) -> str:
    return " ".join(str(name).split())[:MAX_NAME]


def copy_of_builtin(key: str) -> CustomFilter:
    """An editable copy of a built-in filter."""
    if key in MONO_TINTS:
        return CustomFilter("tint", list(MONO_TINTS[key]))
    m = curves.filter_matrix(key)
    return CustomFilter("mixer", matrix=[[round(v, 3) for v in row] for row in m])


def all_filter_choices(custom: dict[str, CustomFilter]) -> list[tuple[str, str]]:
    """``(key, display name)`` for every filter, built-ins first."""
    out = list(COLOR_FILTERS.items())
    out += [(key_for(name), f"{name}  (mine)") for name in sorted(custom, key=str.lower)]
    return out


def matrices(custom: dict[str, CustomFilter]) -> dict[str, Matrix]:
    return {key_for(name): f.to_matrix() for name, f in custom.items()}
