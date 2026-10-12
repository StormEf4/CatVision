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
from bisect import bisect_right
from dataclasses import asdict, dataclass, fields

RAMP_SIZE = 256
RAMP_MAX = 65535

# (minimum, maximum) for every numeric setting.  Used by the GUI sliders and
# to sanitise anything loaded from disk.
LIMITS: dict[str, tuple[float, float]] = {
    "shadow_boost": (0.0, 100.0),
    "exposure": (0.0, 100.0),
    "anti_glare": (0.0, 100.0),
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
    "night_vision_blue": "Night vision (blue)",
    "night_vision_red": "Night vision (red)",
    "night_vision_grey": "Night vision (grey)",
    "thermal": "Thermal style (colors by brightness)",
    "amber": "Amber (warm mono, easy on eyes)",
    "ice": "Ice (cool mono)",
}

# Filters that were renamed or replaced, so old saved settings still load.
LEGACY_FILTERS = {
    "night_vision_br": "night_vision_blue",
    # colour-blind assists were removed in 1.6
    "deuteranopia": "none", "protanopia": "none", "tritanopia": "none",
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
    anti_glare: float = 0.0        # 0..100, keeps bright areas from blooming (with exposure)
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
                value = LEGACY_FILTERS.get(value, value) if isinstance(value, str) else value
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


ANTI_GLARE_MAX_ROOM = 0.45   # share of the output range reserved for highlights at 100
_HIGHLIGHT_CURVE = 2.0       # gentle log shape for the highlight range


def exposure_curve(x: float, exposure: float, anti_glare: float = 0.0) -> float:
    """Night-vision style gain: amplify dim light evenly, roll off highlights.

    ``(1 - e^(-k x)) / (1 - e^(-k))``.  Unlike Shadow boost's log curve (which
    is extremely steep right at black and then flattens, giving a foggy look
    when a whole scene is dark), this spreads a very dark scene's tones over
    the full range so shapes keep their depth.  0 = off.

    On its own the curve saturates: everything above a modest brightness turns
    the same white, so lamps bloom and a player standing in front of a bright
    window disappears into it.  ``anti_glare`` fixes that: the exposure curve
    only fills the output up to a knee, and the top part of the output range
    is kept for highlights, which keep rising gently so bright areas stay
    brighter than whatever is in front of them.
    """
    k = min(max(exposure, 0.0), 100.0) / 100.0 * 20.0
    if k < 1e-6:
        return x

    def expo(v: float) -> float:
        return (1.0 - math.exp(-k * v)) / (1.0 - math.exp(-k))

    room = min(max(anti_glare, 0.0), 100.0) / 100.0 * ANTI_GLARE_MAX_ROOM
    if room <= 0.0:
        return expo(x)
    knee = min(0.5, 1.6 / k)          # where the exposure curve reaches ~80%
    top = 1.0 - room
    if x <= knee:
        return top * expo(x) / expo(knee)
    t = (x - knee) / (1.0 - knee)
    return top + room * math.log1p(_HIGHLIGHT_CURVE * t) / math.log1p(_HIGHLIGHT_CURVE)


def tone_curve(x: float, s: Settings) -> float:
    """Brightness transfer function shared by all three channels.

    ``x`` and the result are in 0..1.
    """
    y = exposure_curve(_clamp01(x), s.exposure, s.anti_glare)
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


@dataclass(frozen=True)
class Gradient:
    """A gradient map: brightness -> color, through a list of color stops.

    Used for false-color palettes such as the thermal look.  Every channel
    must rise (or stay level) from stop to stop, because the graphics card's
    brightness table can only go up.  ``strength`` blends with plain grey.
    """

    stops: tuple[tuple[float, tuple[float, float, float]], ...]
    strength: float = 1.0

    def color(self, y: float) -> tuple[float, float, float]:
        y = _clamp01(y)
        positions = [p for p, _c in self.stops]
        i = min(max(bisect_right(positions, y) - 1, 0), len(self.stops) - 2)
        (p0, c0), (p1, c1) = self.stops[i], self.stops[i + 1]
        t = 0.0 if p1 <= p0 else _clamp01((y - p0) / (p1 - p0))
        k = self.strength
        return tuple((1 - k) * y + k * (c0[j] + (c1[j] - c0[j]) * t) for j in range(3))


Tint = TintCurve | Gradient


def build_ramp(s: Settings, tint: Tint | None = None) -> list[list[int]]:
    """Return ``[red, green, blue]`` 256-entry tables of 16-bit values.

    ``tint`` colors the result by brightness (tint-style color filters and
    gradient maps).
    """
    s = s.clamped()
    gradient = tint if isinstance(tint, Gradient) else None
    lo, hi, power = NO_TINT if gradient else (tint or NO_TINT)
    tone = [tone_curve(i / (RAMP_SIZE - 1), s) for i in range(RAMP_SIZE)]
    colors = [gradient.color(y) for y in tone] if gradient else None
    ramp = []
    for c, gain in enumerate((s.red, s.green, s.blue)):
        channel = []
        prev = 0
        for i, y in enumerate(tone):
            if colors:
                out = colors[i][c] * gain
            else:
                out = y * gain * (lo[c] + (hi[c] - lo[c]) * y ** power)
            v = int(round(_clamp01(out) * s.white_cap * RAMP_MAX))
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


# Tint-style filters as brightness-dependent tint curves (see build_ramp).
# Tuned on dark in-game screenshots.
TINT_CURVES: dict[str, TintCurve] = {
    # Image-intensifier green: deep green shadows, pale phosphor highlights.
    "night_vision": ((0.25, 1.0, 0.30), (0.78, 1.0, 0.72), 2.0),
    # Blue and red look much darker to the eye than green, so pure blue or
    # red would hide detail.  Blue gets some green (a cyan-blue), red gets a
    # little green/blue in the shadows, and both lift toward a paler tone in
    # the highlights so a player in front of a lamp never blends into it.
    "night_vision_blue": ((0.10, 0.45, 1.00), (0.60, 0.85, 1.00), 2.0),
    "night_vision_red": ((1.45, 0.22, 0.10), (1.00, 0.70, 0.55), 2.5),
    # IR-camera / white-phosphor style: plain grey, the brightest-looking color.
    "night_vision_grey": ((1.0, 1.0, 1.0), (1.0, 1.0, 1.0), 1.0),
    "amber": ((1.0, 0.72, 0.25), (1.0, 0.72, 0.25), 1.0),
    "ice": ((0.55, 0.85, 1.0), (0.55, 0.85, 1.0), 1.0),
}

# False-color palettes (gradient maps), keyed like COLOR_FILTERS.
GRADIENTS: dict[str, Gradient] = {
    # Thermal-camera style: dark = cold (black, purple), bright = hot (red,
    # orange, yellow, white).  It colors by brightness, not real heat:
    # CatVision never reads anything from the game.  (Blue can't fall again
    # after purple, so the hot end keeps a little blue; tuned on a dark
    # in-game screenshot.)
    # The hot end keeps rising until white so a player in front of a bright
    # light never merges into it.
    "thermal": Gradient((
        (0.00, (0.00, 0.00, 0.00)),
        (0.15, (0.26, 0.00, 0.33)),
        (0.35, (0.68, 0.04, 0.35)),
        (0.55, (0.92, 0.32, 0.35)),
        (0.80, (1.00, 0.72, 0.40)),
        (1.00, (1.00, 1.00, 0.85)),
    )),
}

# Single-color approximations of the tint filters, used for color previews
# and as the starting point when copying a filter.
MONO_TINTS: dict[str, tuple[float, float, float]] = {
    "grayscale": (1.0, 1.0, 1.0),
    "night_vision": (0.30, 1.0, 0.35),
    "night_vision_blue": (0.25, 0.60, 1.00),
    "night_vision_red": (1.00, 0.35, 0.25),
    "night_vision_grey": (1.00, 1.00, 1.00),
    "thermal": (1.00, 0.55, 0.15),
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


def filter_tint(key: str, custom_tints: dict[str, tuple] | None = None) -> Tint | None:
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
    return GRADIENTS.get(key) or TINT_CURVES.get(key)


def plan_color(s: Settings, custom: dict[str, Matrix] | None = None,
               custom_tints: dict[str, tuple] | None = None,
               all_displays: bool = True) -> tuple[Tint | None, Matrix]:
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
    if isinstance(tint, Gradient):
        curve = Gradient(tint.stops, k)
    else:
        lo, hi, power = tint
        curve = (tuple(1.0 + (t - 1.0) * k for t in lo), tuple(1.0 + (t - 1.0) * k for t in hi),
                 power)
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
