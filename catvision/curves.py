"""Pure math for CatVision: brightness curves and color matrices.

Nothing in here talks to Windows, so it can be unit tested anywhere.

Two kinds of output are produced from a :class:`Settings` object:

* a *gamma ramp* -- three 256-entry lookup tables (red, green, blue) with
  16-bit values.  The graphics card applies this table to every pixel right
  before it is sent to the monitor.  This is the same mechanism used by the
  brightness/gamma sliders in GPU control panels and by night-light apps.
* a *color matrix* -- a 3x3 matrix applied by the Windows Magnification
  accessibility API (the same engine as Windows' own "Color filters").
  It is used for things a per-channel lookup table cannot do, such as
  grayscale, night-vision tint and color-blindness assistance.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields

RAMP_SIZE = 256
RAMP_MAX = 65535

# (minimum, maximum) for every numeric setting.  Used by the GUI sliders and
# to sanitise anything loaded from disk.
LIMITS: dict[str, tuple[float, float]] = {
    "shadow_boost": (0.0, 100.0),
    "exposure": (0.0, 100.0),
    "gamma": (0.5, 3.0),
    "black_floor": (0.0, 0.30),
    "brightness": (0.5, 2.0),
    "contrast": (0.5, 2.0),
    "white_cap": (0.5, 1.0),
    "red": (0.5, 1.5),
    "green": (0.5, 1.5),
    "blue": (0.5, 1.5),
    "saturation": (0.0, 2.0),
    "filter_strength": (0.0, 1.0),
}

# Display name for every color filter, in the order shown in the GUI.
COLOR_FILTERS: dict[str, str] = {
    "none": "None",
    "grayscale": "Grayscale (high contrast mono)",
    "night_vision": "Night vision (green)",
    "night_vision_br": "Night vision (blue / red)",
    "amber": "Amber (warm mono, easy on eyes)",
    "ice": "Ice (cool mono)",
    "deuteranopia": "Red-green assist (deuteranopia)",
    "protanopia": "Red-green assist (protanopia)",
    "tritanopia": "Blue-yellow assist (tritanopia)",
}

# Custom (user-made) filters are referenced as "custom:<name>".
CUSTOM_PREFIX = "custom:"

# Rec.709 luma weights.
LUMA = (0.2126, 0.7152, 0.0722)


@dataclass
class Settings:
    """Everything that defines one look.  Defaults are 'no change'."""

    shadow_boost: float = 0.0      # 0..100, lifts dark tones, keeps highlights
    exposure: float = 0.0          # 0..100, strong even gain for very dark games
    gamma: float = 1.0             # >1 brightens midtones, <1 darkens
    black_floor: float = 0.0       # raises pure black to this level (0..0.3)
    brightness: float = 1.0        # overall gain
    contrast: float = 1.0          # around mid-grey
    white_cap: float = 1.0         # maximum output, tames flashes/glare
    red: float = 1.0               # per-channel gain (tint)
    green: float = 1.0
    blue: float = 1.0
    saturation: float = 1.0        # 0 = grey, 1 = normal, 2 = vivid
    color_filter: str = "none"     # key of COLOR_FILTERS
    filter_strength: float = 1.0   # 0..1 blend of the color filter

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Build settings from untrusted data (config file), ignoring junk."""
        s = cls()
        if not isinstance(data, dict):
            return s
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name == "color_filter":
                if isinstance(value, str) and is_valid_filter_key(value):
                    s.color_filter = value
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                setattr(s, f.name, value)
        return s.clamped()

    def clamped(self) -> "Settings":
        data = asdict(self)
        for name, (lo, hi) in LIMITS.items():
            data[name] = min(hi, max(lo, float(data[name])))
        if not is_valid_filter_key(data["color_filter"]):
            data["color_filter"] = "none"
        return Settings(**data)

    def to_dict(self) -> dict:
        return asdict(self)


def is_valid_filter_key(key) -> bool:
    if not isinstance(key, str):
        return False
    if key in COLOR_FILTERS:
        return True
    return key.startswith(CUSTOM_PREFIX) and 0 < len(key) - len(CUSTOM_PREFIX) <= 60


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def shadow_boost_factor(shadow_boost: float) -> float:
    """Map the 0..100 slider to the strength of the log curve (0..200)."""
    if shadow_boost <= 0:
        return 0.0
    return 201.0 ** (min(shadow_boost, 100.0) / 100.0) - 1.0


def exposure_curve(x: float, exposure: float) -> float:
    """Night-vision style gain: amplify dim light evenly, roll off highlights.

    ``(1 - e^(-k x)) / (1 - e^(-k))``.  Unlike Shadow boost's log curve (which
    is extremely steep right at black and then flattens, giving a foggy look
    when a whole scene is dark), this spreads a very dark scene's tones over
    the full range so shapes keep their depth.  0 = off.
    """
    k = min(max(exposure, 0.0), 100.0) / 100.0 * 20.0
    if k < 1e-6:
        return x
    return (1.0 - math.exp(-k * x)) / (1.0 - math.exp(-k))


def tone_curve(x: float, s: Settings) -> float:
    """Brightness transfer function shared by all three channels.

    ``x`` and the result are in 0..1.
    """
    y = exposure_curve(_clamp01(x), s.exposure)
    if s.gamma != 1.0:
        y = y ** (1.0 / s.gamma)
    a = shadow_boost_factor(s.shadow_boost)
    if a > 0:
        # Logarithmic lift: huge boost near black, almost none near white,
        # so dark corners open up without blowing out the sky.
        y = math.log1p(a * y) / math.log1p(a)
    y *= s.brightness
    y = (y - 0.5) * s.contrast + 0.5
    y = _clamp01(y)
    y = s.black_floor + (1.0 - s.black_floor) * y
    return y


# A tint curve colors the picture by brightness, after the tone curve:
# (dark color gains, bright color gains, power).  Gains move from the first
# to the second as brightness rises, so night vision can have deep green
# shadows and pale phosphor highlights, or blue shadows and red highlights.
TintCurve = tuple[tuple[float, float, float], tuple[float, float, float], float]
NO_TINT: TintCurve = ((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 1.0)


def build_ramp(s: Settings, tint: TintCurve | None = None) -> list[list[int]]:
    """Return ``[red, green, blue]`` 256-entry tables of 16-bit values.

    ``tint`` colors the result by brightness (tint-style color filters).
    """
    s = s.clamped()
    lo, hi, power = tint or NO_TINT
    tone = [tone_curve(i / (RAMP_SIZE - 1), s) for i in range(RAMP_SIZE)]
    ramp = []
    for c, gain in enumerate((s.red, s.green, s.blue)):
        channel = []
        prev = 0
        for y in tone:
            g = gain * (lo[c] + (hi[c] - lo[c]) * y ** power)
            v = int(round(_clamp01(y * g) * s.white_cap * RAMP_MAX))
            v = max(prev, v)  # graphics drivers expect non-decreasing ramps
            channel.append(v)
            prev = v
        ramp.append(channel)
    return ramp


def identity_ramp() -> list[list[int]]:
    channel = [i * 257 for i in range(RAMP_SIZE)]  # 255 * 257 == 65535
    return [list(channel), list(channel), list(channel)]


def is_identity_ramp(ramp: list[list[int]], tolerance: int = 0) -> bool:
    ident = identity_ramp()
    return all(
        abs(a - b) <= tolerance
        for ch, ich in zip(ramp, ident)
        for a, b in zip(ch, ich)
    )


def limit_ramp_deviation(ramp: list[list[int]], max_steps: int = 127) -> list[list[int]]:
    """Clamp each entry to within ``max_steps`` (8-bit levels) of identity.

    By default Windows rejects gamma ramps that stray too far from a straight
    line.  This keeps as much of the effect as Windows allows.
    """
    out = []
    for channel in ramp:
        new = []
        prev = 0
        for i, v in enumerate(channel):
            lo = max(0, i - max_steps) * 257
            hi = min(255, i + max_steps) * 257
            v = min(hi, max(lo, v))
            v = max(prev, v)
            new.append(v)
            prev = v
        out.append(new)
    return out


def blend_ramp(ramp: list[list[int]], t: float) -> list[list[int]]:
    """Blend between identity (t=0) and ``ramp`` (t=1)."""
    ident = identity_ramp()
    return [
        [int(round(i + (v - i) * t)) for v, i in zip(ch, ich)]
        for ch, ich in zip(ramp, ident)
    ]


# ---------------------------------------------------------------------------
# Color matrices (3x3, column-vector convention: out = M @ [r, g, b])
# ---------------------------------------------------------------------------

Matrix = list[list[float]]

IDENTITY3: Matrix = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]

# Machado et al. (2009) color vision deficiency simulation, severity 1.0.
_CVD_SIM: dict[str, Matrix] = {
    "protanopia": [
        [0.152286, 1.052583, -0.204868],
        [0.114503, 0.786281, 0.099216],
        [-0.003882, -0.048116, 1.051998],
    ],
    "deuteranopia": [
        [0.367322, 0.860646, -0.227968],
        [0.280085, 0.672501, 0.047413],
        [-0.011820, 0.042940, 0.953970],
    ],
    "tritanopia": [
        [1.255528, -0.076749, -0.178779],
        [-0.078411, 0.930809, 0.147602],
        [0.004733, 0.691367, 0.303900],
    ],
}

# Daltonization: move the color information a viewer cannot see into
# channels they can.
_RED_GREEN_SHIFT: Matrix = [[0.0, 0.0, 0.0], [0.7, 1.0, 0.0], [0.7, 0.0, 1.0]]
_BLUE_YELLOW_SHIFT: Matrix = [[1.0, 0.0, 0.7], [0.0, 1.0, 0.7], [0.0, 0.0, 0.0]]

# Tint-style filters as brightness-dependent tint curves (see build_ramp).
# Tuned on dark in-game screenshots.
TINT_CURVES: dict[str, TintCurve] = {
    # Image-intensifier green: deep green shadows, pale phosphor highlights.
    "night_vision": ((0.25, 1.0, 0.30), (0.78, 1.0, 0.72), 2.0),
    # Blue shadows -> purple -> red/pink highlights.  (Each channel can only
    # rise with brightness, so the brightest parts keep a little blue.)
    "night_vision_br": ((0.05, 0.60, 2.60), (1.35, 0.40, 0.25), 1.25),
    "amber": ((1.0, 0.72, 0.25), (1.0, 0.72, 0.25), 1.0),
    "ice": ((0.55, 0.85, 1.0), (0.55, 0.85, 1.0), 1.0),
}

# Single-color approximations of the tint filters, used for color previews
# and as the starting point when copying a filter.
MONO_TINTS: dict[str, tuple[float, float, float]] = {
    "grayscale": (1.0, 1.0, 1.0),
    "night_vision": (0.30, 1.0, 0.35),
    "night_vision_br": (0.70, 0.45, 1.00),
    "amber": (1.0, 0.72, 0.25),
    "ice": (0.55, 0.85, 1.0),
}


def mat_mul(a: Matrix, b: Matrix) -> Matrix:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def mat_add(a: Matrix, b: Matrix) -> Matrix:
    return [[a[i][j] + b[i][j] for j in range(3)] for i in range(3)]


def mat_scale(a: Matrix, k: float) -> Matrix:
    return [[a[i][j] * k for j in range(3)] for i in range(3)]


def mat_lerp(a: Matrix, b: Matrix, t: float) -> Matrix:
    return mat_add(mat_scale(a, 1.0 - t), mat_scale(b, t))


def saturation_matrix(sat: float) -> Matrix:
    gray = [list(LUMA) for _ in range(3)]
    return mat_lerp(gray, IDENTITY3, sat)


def mono_tint_matrix(tint) -> Matrix:
    """Grayscale, then multiply by a tint color."""
    return [[t * w for w in LUMA] for t in tint]


def filter_matrix(name: str, custom: dict[str, Matrix] | None = None) -> Matrix:
    if name.startswith(CUSTOM_PREFIX):
        m = (custom or {}).get(name)
        return [row[:] for row in m] if m else [row[:] for row in IDENTITY3]
    if name in MONO_TINTS:
        return mono_tint_matrix(MONO_TINTS[name])
    if name in _CVD_SIM:
        sim = _CVD_SIM[name]
        shift = _BLUE_YELLOW_SHIFT if name == "tritanopia" else _RED_GREEN_SHIFT
        error = mat_add(IDENTITY3, mat_scale(sim, -1.0))
        return mat_add(IDENTITY3, mat_mul(shift, error))
    return [row[:] for row in IDENTITY3]


def color_matrix(s: Settings, custom: dict[str, Matrix] | None = None) -> Matrix:
    """Combined saturation + color filter matrix for the given settings.

    ``custom`` maps "custom:<name>" keys to the user's own filter matrices.
    """
    s = s.clamped()
    m = saturation_matrix(s.saturation)
    if s.color_filter != "none" and s.filter_strength > 0:
        f = mat_lerp(IDENTITY3, filter_matrix(s.color_filter, custom), s.filter_strength)
        m = mat_mul(f, m)
    return m


def filter_tint(key: str, custom_tints: dict[str, tuple] | None = None) -> TintCurve | None:
    """The tint curve of a tint-style filter, or None for other filters.

    Tint filters are drawn by the brightness table, which also works per
    display.  Filters that mix colors (grayscale, color-blind assist,
    channel mixers) can't be.
    """
    if key.startswith(CUSTOM_PREFIX):
        tint = (custom_tints or {}).get(key)
        if tint is None or all(abs(t - 1.0) < 1e-6 for t in tint):
            return None
        return (tuple(tint), tuple(tint), 1.0)
    return TINT_CURVES.get(key)


def plan_color(s: Settings, custom: dict[str, Matrix] | None = None,
               custom_tints: dict[str, tuple] | None = None,
               all_displays: bool = True) -> tuple[TintCurve | None, Matrix]:
    """Decide how to show the settings' colors.

    Returns ``(tint_curve, matrix)``.  Tint filters are drawn as a tint curve
    in the brightness table, *after* the tone curve, like the phosphor screen
    of real night vision.  On all displays the picture is first turned
    grayscale by the (screen-wide) color matrix; with only some displays
    chosen that step is skipped, so the tint keeps some of the game's color.
    """
    s = s.clamped()
    tint = filter_tint(s.color_filter, custom_tints)
    if tint is None or s.filter_strength <= 0:
        return None, color_matrix(s, custom)
    k = s.filter_strength
    lo, hi, power = tint
    curve = (tuple(1.0 + (t - 1.0) * k for t in lo), tuple(1.0 + (t - 1.0) * k for t in hi), power)
    m = saturation_matrix(s.saturation)
    if all_displays:
        m = mat_mul(mat_lerp(IDENTITY3, filter_matrix("grayscale"), k), m)
    return curve, m


def split_for_displays(s: Settings, custom: dict[str, Matrix] | None = None,
                       custom_tints: dict[str, tuple] | None = None):
    """``plan_color`` for when only some displays are chosen."""
    return plan_color(s, custom, custom_tints, all_displays=False)


def is_identity_matrix(m: Matrix, tolerance: float = 1e-6) -> bool:
    return all(abs(m[i][j] - IDENTITY3[i][j]) <= tolerance for i in range(3) for j in range(3))


def to_mag_color_effect(m: Matrix) -> list[float]:
    """Flatten into the 5x5 row-major MAGCOLOREFFECT layout.

    The Magnification API multiplies a *row* vector ``[r, g, b, a, 1]`` by the
    matrix, so rows are inputs and columns are outputs: it wants ``M``
    transposed.
    """
    out = [0.0] * 25
    for i in range(3):
        for j in range(3):
            out[i * 5 + j] = float(m[j][i])
    out[3 * 5 + 3] = 1.0
    out[4 * 5 + 4] = 1.0
    return out


def apply_matrix(m: Matrix, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply a color matrix to one 0..1 RGB color (used for previews)."""
    return tuple(_clamp01(sum(m[i][k] * rgb[k] for k in range(3))) for i in range(3))  # type: ignore[return-value]
