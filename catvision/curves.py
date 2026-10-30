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
import operator
from bisect import bisect_right
from dataclasses import asdict, dataclass, fields
from itertools import accumulate, islice

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
    "highlight_room": (0.0, 100.0),
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
    highlight_room: float = 0.0    # 0..100, keeps bright areas (a window from a dark room) apart
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

    def clamped(self, changes: dict | None = None) -> "Settings":
        """A copy with every value inside its limits.  ``changes``: values
        to use instead of this object's (``dataclasses.replace`` and then
        clamped, in one pass: auto levels does this on every update)."""
        # The fields straight into a dict: what asdict gives for these plain
        # values, without its deep copy (this runs several times per auto
        # levels update).
        data = {name: getattr(self, name) for name in _SETTINGS_FIELDS}
        if changes:
            data.update(changes)
        for name, (lo, hi) in LIMITS.items():
            data[name] = min(hi, max(lo, float(data[name])))
        if not is_valid_filter_key(data["color_filter"]):
            data["color_filter"] = "none"
        return Settings(**data)

    def to_dict(self) -> dict:
        return asdict(self)


_SETTINGS_FIELDS = tuple(f.name for f in fields(Settings))


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


# Outside detail (highlight_room): a ceiling line through white,
# 1 - slope * (1 - x), that the lifted tones may not cross.  A strong lift
# pushes everything above the dark room close to white, and contrast then
# turns the whole view out of a window into one white block; under the line
# the bright inputs from where the curve meets it up to white keep at least
# ``slope`` of their own range, so sky, walls and trees stay apart.  The
# line is a function of brightness only (the gamma table can't know where
# a pixel is): the dark room sits below where the curve meets the line and
# is not touched.  Input 255 still reaches white, and nothing before it.
HIGHLIGHT_SLOPE = 0.6        # the line's slope at 100 (0.3 at 50)
HIGHLIGHT_SOFT = 0.06        # width of the rounded corner where the curve meets the line...
HIGHLIGHT_SOFT_TOP = 0.2     # ...narrowing to a sharp one over the top this much of the inputs


def highlight_slope(highlight_room: float) -> float:
    """The ceiling line's slope for the 0..100 slider (0 = off)."""
    return HIGHLIGHT_SLOPE * (min(max(highlight_room, 0.0), 100.0) / 100.0)


def _under_ceiling(z: float, top: float, width: float) -> float:
    """z kept under the line value ``top``, with a rounded corner ``width``
    wide (a quadratic smooth minimum: it never rises above either, and it
    rises whenever both do)."""
    if z <= top - width:
        return z
    if z >= top + width:
        return top
    gap = z - top
    h = width - abs(gap)
    return (z if gap < 0 else top) - h * h / (4.0 * width)


def _lifted(x: float, s: Settings) -> float:
    """tone_curve's stages before Outside detail and the clamp (``x`` is
    already in 0..1; the result can be outside it)."""
    y = exposure_curve(x, s.exposure, s.anti_glare)
    if s.gamma != 1.0:
        y = y ** (1.0 / s.gamma)
    a = shadow_boost_factor(s.shadow_boost)
    if a > 0:
        # Logarithmic lift: huge boost near black, almost none near white,
        # so dark corners open up without blowing out the sky.
        y = math.log1p(a * y) / math.log1p(a)
    y *= s.brightness
    return (y - 0.5) * s.contrast + 0.5


def highlight_room_cap(s: Settings, x: float) -> float:
    """The most Outside detail (0..100) that leaves input ``x`` and
    everything darker untouched: the ceiling line's rounded corner just
    clears the tone there.  A strong lift (a high boost, Pitch Black, the
    Night Vision looks) puts the room's own tones close to white, so it
    allows less; 0 when even a little would reach them."""
    x = _clamp01(x)
    if x >= 1.0:
        return 100.0
    # The line may come down to the tone plus the corner's width there.
    headroom = 1.0 - HIGHLIGHT_SOFT * min(1.0, (1.0 - x) / HIGHLIGHT_SOFT_TOP) - _lifted(x, s)
    return min(100.0, max(0.0, headroom / (1.0 - x) / HIGHLIGHT_SLOPE * 100.0))


def tone_curve(x: float, s: Settings) -> float:
    """Brightness transfer function shared by all three channels.

    ``x`` and the result are in 0..1.
    """
    x = _clamp01(x)
    y = _lifted(x, s)
    slope = highlight_slope(s.highlight_room)
    if slope > 0:
        y = _under_ceiling(y, 1.0 - slope * (1.0 - x),
                           HIGHLIGHT_SOFT * min(1.0, (1.0 - x) / HIGHLIGHT_SOFT_TOP))
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


# The inputs of the brightness table (0..1), worked out once.
_RAMP_XS = tuple(i / (RAMP_SIZE - 1) for i in range(RAMP_SIZE))


def _tone_table(s: Settings) -> list[float]:
    """``[tone_curve(x, s) for x in _RAMP_XS]`` for clamped settings, fast.

    Auto levels rebuilds the brightness table up to 120 times a second while
    the game needs the CPU, so this works out every constant once instead of
    256 times.  Each value goes through exactly the same arithmetic, in the
    same order, as tone_curve and exposure_curve, so the results are
    bit-for-bit the same (the tests compare them over thousands of looks).
    """
    ys = _exposure_tones(s.exposure, s.anti_glare, s.gamma)
    a = shadow_boost_factor(s.shadow_boost)
    gain, contrast, floor = s.brightness, s.contrast, s.black_floor
    rest = 1.0 - floor
    slope = highlight_slope(s.highlight_room)
    if slope > 0:
        # Outside detail: the same stages, then the ceiling line (worked out
        # once per slope; see _under_ceiling) before the clamp, in one pass.
        # (Most entries are well under the line: one comparison each.)  The
        # clamp is only written where it can change anything: under the
        # corner a tone can be below 0 but never above 1 (the line is at most
        # 1), and the line and its corner stay between 1 - HIGHLIGHT_SLOPE
        # minus a quarter of the corner's width and 1, always inside.
        ceiling = _ceiling(slope)
        if ceiling is None:
            # A slope seen for the first time (the Outside detail itself is
            # fading): work the line out only where a tone can reach it (see
            # _CEILING_PARTS) instead of building a table that is used once.
            if a > 0:
                log1p, scale = math.log1p, math.log1p(a)
                return [floor + rest * ((0.0 if z < 0.0 else z)
                                        if (z := (log1p(a * y) / scale * gain - 0.5)
                                            * contrast + 0.5) <= safe
                                        else z if z <= (t := 1.0 - slope * d) - w
                                        else t if z >= t + w
                                        else (z if (g := z - t) < 0 else t)
                                        - (h := w - (g if g >= 0 else -g)) * h / q)
                        for y, (safe, d, w, q) in zip(ys, _CEILING_PARTS)]
            return [floor + rest * ((0.0 if z < 0.0 else z)
                                    if (z := (y * gain - 0.5) * contrast + 0.5) <= safe
                                    else z if z <= (t := 1.0 - slope * d) - w
                                    else t if z >= t + w
                                    else (z if (g := z - t) < 0 else t)
                                    - (h := w - (g if g >= 0 else -g)) * h / q)
                    for y, (safe, d, w, q) in zip(ys, _CEILING_PARTS)]
        if a > 0:
            log1p, scale = math.log1p, math.log1p(a)
            return [floor + rest * ((0.0 if z < 0.0 else z)
                                    if (z := (log1p(a * y) / scale * gain - 0.5) * contrast
                                        + 0.5) <= lo
                                    else t if z >= hi
                                    else (z if (g := z - t) < 0 else t)
                                    - (h := w - (g if g >= 0 else -g)) * h / q)
                    for y, (lo, hi, t, w, q) in zip(ys, ceiling)]
        return [floor + rest * ((0.0 if z < 0.0 else z)
                                if (z := (y * gain - 0.5) * contrast + 0.5) <= lo
                                else t if z >= hi
                                else (z if (g := z - t) < 0 else t)
                                - (h := w - (g if g >= 0 else -g)) * h / q)
                for y, (lo, hi, t, w, q) in zip(ys, ceiling)]
    # The last stages in one pass (z: the value before it is clamped).
    if a > 0:
        log1p, scale = math.log1p, math.log1p(a)
        return [floor + rest * (0.0 if (z := (log1p(a * y) / scale * gain - 0.5) * contrast
                                        + 0.5) < 0.0 else 1.0 if z > 1.0 else z)
                for y in ys]
    return [floor + rest * (0.0 if (z := (y * gain - 0.5) * contrast + 0.5) < 0.0
                            else 1.0 if z > 1.0 else z)
            for y in ys]


# For each entry, per slope: where the rounded corner starts and ends, the
# ceiling line, the corner's width and 4 x the width (tone_curve's arithmetic
# exactly).  A table is only built for a slope asked for a second time (the
# boost fading under a steady Outside detail); a slope that is itself fading
# is new on every step and is worked out inline.  Emptied when it holds this
# many.
_CEILINGS: dict[float, tuple] = {}
_CEILINGS_MAX = 16
_CEILINGS_SEEN: set[float] = set()   # slopes asked for once (emptied the same way)


# What doesn't depend on the slope, per entry: the lowest the corner can
# start at any slope (a tone at or below it is never touched: one comparison
# for the dark entries), 1 - x, the corner's width and 4 x the width.  (Rounding
# never turns a smaller product or difference into a larger one, so a tone
# at or below the first is at or below the corner's start for every slope
# up to HIGHLIGHT_SLOPE.)
_CEILING_PARTS = tuple(
    ((1.0 - HIGHLIGHT_SLOPE * d) - w, d, w, 4.0 * w)
    for x in _RAMP_XS
    for d in (1.0 - x,)
    for w in (HIGHLIGHT_SOFT * min(1.0, d / HIGHLIGHT_SOFT_TOP),))


def _ceiling(slope: float) -> tuple | None:
    """The per-entry table for ``slope``, or None the first time it is asked
    for (see _CEILINGS)."""
    found = _CEILINGS.get(slope)
    if found is None:
        if slope not in _CEILINGS_SEEN:
            if len(_CEILINGS_SEEN) >= _CEILINGS_MAX:
                _CEILINGS_SEEN.clear()
            _CEILINGS_SEEN.add(slope)
            return None
        if len(_CEILINGS) >= _CEILINGS_MAX:
            _CEILINGS.clear()
        found = _CEILINGS[slope] = tuple(
            (t - w, t + w, t, w, q)
            for _safe, d, w, q in _CEILING_PARTS for t in (1.0 - slope * d,))
    return found


# _exposure_tones' results for the last few (exposure, anti_glare, gamma).
# Auto levels mostly moves only Shadow boost, which comes after these
# stages, so they come out the same on every step of a fade.  Emptied when
# it holds this many (about 8 KB each).
_EXPOSURE_TONES: dict[tuple, tuple] = {}
_EXPOSURE_TONES_MAX = 16


def _exposure_tones(exposure: float, anti_glare: float, gamma: float) -> tuple:
    """The exposure and gamma stages of _tone_table: one value per entry.

    Kept in _EXPOSURE_TONES (read-only tuples).  Values that compare equal
    give the same table (5 and 5.0, 0.0 and -0.0 both do), so they may share
    an entry."""
    key = (exposure, anti_glare, gamma)
    ys = _EXPOSURE_TONES.get(key)
    if ys is not None:
        return ys
    k = min(max(exposure, 0.0), 100.0) / 100.0 * 20.0
    if k < 1e-6:
        ys = _RAMP_XS
    else:
        nk, exp, full = -k, math.exp, 1.0 - math.exp(-k)
        room = min(max(anti_glare, 0.0), 100.0) / 100.0 * ANTI_GLARE_MAX_ROOM
        if room <= 0.0:
            ys = [(1.0 - exp(nk * x)) / full for x in _RAMP_XS]
        else:
            knee = min(0.5, 1.6 / k)
            top = 1.0 - room
            at_knee = (1.0 - exp(nk * knee)) / full
            span, log1p, scale = 1.0 - knee, math.log1p, math.log1p(_HIGHLIGHT_CURVE)
            ys = [top * ((1.0 - exp(nk * x)) / full) / at_knee if x <= knee
                  else top + room * log1p(_HIGHLIGHT_CURVE * ((x - knee) / span)) / scale
                  for x in _RAMP_XS]
    if gamma != 1.0:
        inv = 1.0 / gamma
        ys = [y ** inv for y in ys]
    if len(_EXPOSURE_TONES) >= _EXPOSURE_TONES_MAX:
        _EXPOSURE_TONES.clear()
    ys = _EXPOSURE_TONES[key] = tuple(ys)
    return ys


def _gradient_columns(gradient: Gradient, tone: list[float]) -> list[list[float]]:
    """``[gradient.color(y)[c] for y in tone]`` for each channel c, with the
    stops looked at once instead of for every entry (same arithmetic)."""
    stops = gradient.stops
    if len(stops) < 2:   # not a real gradient: leave it to color()
        colors = [gradient.color(y) for y in tone]
        return [[col[c] for col in colors] for c in range(3)]
    positions = [p for p, _c in stops]
    last = len(stops) - 2
    segments = []
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        segments.append((p0, p1 <= p0, p1 - p0, c0[0], c0[1], c0[2],
                         c1[0] - c0[0], c1[1] - c0[1], c1[2] - c0[2]))
    k = gradient.strength
    keep = 1 - k
    reds, greens, blues = [], [], []
    add_red, add_green, add_blue = reds.append, greens.append, blues.append
    for y in tone:   # (the clamps written out: a function call per entry costs more)
        y = 0.0 if y < 0.0 else 1.0 if y > 1.0 else y
        i = bisect_right(positions, y) - 1
        i = 0 if i < 0 else last if i > last else i
        p0, flat, span, r0, g0, b0, dr, dg, db = segments[i]
        if flat:
            t = 0.0
        else:
            t = (y - p0) / span
            t = 0.0 if t < 0.0 else 1.0 if t > 1.0 else t
        grey = keep * y
        add_red(grey + k * (r0 + dr * t))
        add_green(grey + k * (g0 + dg * t))
        add_blue(grey + k * (b0 + db * t))
    return [reds, greens, blues]


def _ramp_channel(outs, white_cap: float) -> list[int]:
    """Brightness levels (0..1) -> 16-bit table entries that never go down."""
    scaled = [(0.0 if o < 0.0 else 1.0 if o > 1.0 else o) * white_cap * RAMP_MAX for o in outs]
    # round() on a float is float.__round__ (the same rounding, the same
    # errors for nan and inf), but mapping it straight is about a fifth
    # cheaper than calling round() per entry: this runs for every channel
    # of every auto levels update.  (A float white cap makes every value a
    # float.)
    values = list(map(_ROUND_FLOAT if type(white_cap) is float else round, scaled))
    # Graphics drivers expect non-decreasing ramps.  The table almost always
    # is already, and checking is cheaper than raising each entry to the
    # one before (which then changes nothing).
    if values and values[0] >= 0 and all(map(operator.le, values, islice(values, 1, None))):
        return values
    channel = list(accumulate(values, max, initial=0))
    del channel[0]
    return channel


_ROUND_FLOAT = float.__round__

# Tint powers for which ``y ** power`` is a finite number for every tone
# value (0..1, give or take a rounding unit), so an even tint's term
# (hi - lo) * y ** power is exactly 0 and needs no powers worked out.
_EVEN_POWERS = (0.0, 64.0)


def build_ramp(s: Settings, tint: Tint | None = None) -> list[list[int]]:
    """Return ``[red, green, blue]`` 256-entry tables of 16-bit values.

    ``tint`` colors the result by brightness (tint-style color filters and
    gradient maps).
    """
    s = s.clamped()
    gradient = tint if isinstance(tint, Gradient) else None
    lo, hi, power = NO_TINT if gradient else (tint or NO_TINT)
    tone = _tone_table(s)
    columns = _gradient_columns(gradient, tone) if gradient else None
    ramp: list[list[int]] = []
    done: list[tuple] = []   # (what decides a channel, its table)
    # y ** 1.0 is y (tone values are finite): no need to raise anything.
    unit_power = type(power) in (float, int) and power == 1.0
    # Night vision's green channel is even (lo == hi) under a power of 2:
    # with y ** power finite its tint term is a constant too.
    tame_power = unit_power or (type(power) in (float, int)
                                and _EVEN_POWERS[0] <= power <= _EVEN_POWERS[1])
    lifts = None   # y ** power for each entry, the same for every channel
    for c, gain in enumerate((s.red, s.green, s.blue)):
        # Channels with the same gain and tint come out the same (a plain
        # grey look, the usual case): work the table out once and copy it.
        key = (gain, columns[c]) if columns else (gain, lo[c], hi[c])
        same = next((table for k, table in done if k == key), None)
        if same is not None:
            ramp.append(list(same))
            continue
        if columns:
            col = columns[c]
            outs = col if gain == 1.0 else [v * gain for v in col]
        elif hi[c] == lo[c] and tame_power and math.isfinite(lo[c]):
            # An even tint: (hi - lo) is 0 and y ** power is finite, so the
            # tint term is the constant lo + 0.0 (and * 1.0 changes nothing).
            even = lo[c] + 0.0
            if even == 1.0:
                outs = tone if gain == 1.0 else [y * gain for y in tone]
            else:
                outs = [y * gain * even for y in tone]
        else:
            if lifts is None:
                lifts = tone if unit_power else [y ** power for y in tone]
            low, rise = lo[c], hi[c] - lo[c]
            scaled = tone if gain == 1.0 else [y * gain for y in tone]
            outs = [g * (low + rise * z) for g, z in zip(scaled, lifts)]
        table = _ramp_channel(outs, s.white_cap)
        done.append((key, table))
        ramp.append(table)
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
    out: list[list[int]] = []
    done: list[tuple] = []   # (channel, its result): identical channels clamp the same
    for channel in ramp:
        same = next((new for old, new in done if old == channel), None)
        if same is not None:
            out.append(list(same))
            continue
        lows, highs = _limit_bounds(len(channel), max_steps)
        # Entry by entry: min(hi, max(lo, v)), then never below the one before.
        new = list(accumulate(map(min, highs, map(max, lows, channel)), max, initial=0))
        del new[0]
        done.append((channel, new))
        out.append(new)
    return out


# limit_ramp_deviation's bounds per (length, max_steps): the fades ask for the
# same few limits over and over.  Emptied when it holds this many (~18 KB each).
_LIMIT_BOUNDS: dict[tuple, tuple[list, list]] = {}
_LIMIT_BOUNDS_MAX = 64


def _limit_bounds(length: int, max_steps) -> tuple[list, list]:
    """The lowest and highest value allowed for each entry (16-bit)."""
    key = (length, max_steps, type(max_steps))   # 96 and 96.0 give different types
    bounds = _LIMIT_BOUNDS.get(key)
    if bounds is None:
        if len(_LIMIT_BOUNDS) >= _LIMIT_BOUNDS_MAX:
            _LIMIT_BOUNDS.clear()
        bounds = _LIMIT_BOUNDS[key] = (
            [max(0, i - max_steps) * 257 for i in range(length)],
            [min(255, i + max_steps) * 257 for i in range(length)])
    return bounds


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
    curve = _tint_curve(s.color_filter, s.filter_strength, custom_tints)
    if curve is None:
        return None, color_matrix(s, custom)
    k = s.filter_strength
    m = saturation_matrix(s.saturation)
    if all_displays:
        m = mat_mul(mat_lerp(IDENTITY3, filter_matrix("grayscale"), k), m)
    return curve, m


def plan_tint(s: Settings, custom_tints: dict[str, tuple] | None = None) -> Tint | None:
    """Just the tint curve of ``plan_color`` (the same on all or some
    displays), without working out the color matrix: auto levels needs only
    this on every update.  Only the two settings it uses are clamped, the
    way clamped() does it (that would copy every setting)."""
    lo, hi = LIMITS["filter_strength"]
    key = s.color_filter if is_valid_filter_key(s.color_filter) else "none"
    return _tint_curve(key, min(hi, max(lo, float(s.filter_strength))), custom_tints)


def _tint_curve(key: str, k: float, custom_tints: dict[str, tuple] | None) -> Tint | None:
    """The tint curve of a (valid) filter key, scaled by the (clamped)
    filter strength ``k``."""
    tint = filter_tint(key, custom_tints)
    if tint is None or k <= 0:
        return None
    if isinstance(tint, Gradient):
        return Gradient(tint.stops, k)
    lo, hi, power = tint
    # (lists, not generators, inside tuple(): quicker, on every auto levels update)
    return (tuple([1.0 + (t - 1.0) * k for t in lo]), tuple([1.0 + (t - 1.0) * k for t in hi]),
            power)


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
