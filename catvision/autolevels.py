"""Auto levels (beta): watch how bright the picture is and adjust Shadow boost
or Exposure to match, the way your eyes adapt.

How it stays safe: it only takes a tiny, low-resolution copy of what Windows
is already showing on the ticked displays (the same screen-capture route
recording and streaming apps use).  It never touches the game, never saves
or sends the pictures, and only measures brightness (overall, and in a few
large blocks to tell turning to a new view from a light going on or off):
it does not look for anything in the picture.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field, fields, replace
from itertools import compress
from typing import Callable

from . import curves
from .winutil import IS_WINDOWS

THUMB_W, THUMB_H = 96, 54       # size of the copy that is measured
INTERVAL_S = 0.2                # five measurements a second...
BURST_S = 0.1                   # ...and ten while the brightness is changing a lot
RISE_S = 1.2                    # brightening is slow and smooth...
RISE_FAST_S = 0.4               # ...but catches up quicker after a big change (into a cave)
FALL_S = 0.3                    # dimming is fast, so flashes never blind you
RUSH_S = 0.12                   # a flash fades down in about a tenth of a second...
LEAVING_GAP = 0.2               # ...and so does walking out of the dark: a picture STEADY_RATIO
                                # brighter while a fade down is under way (or a different,
                                # brighter view) and the display is still this share of
                                # How much too bright
LEAVING_UNDERWAY = 0.5          # (under way: its goal is at least this many points below)
RUSH_VIEW_S = 0.06              # turning or walking out to a brighter view fades at the speed
                                # limit from the start (the view change hides the fade); a
                                # light flashing on keeps RUSH_S's gentler start
# Speed limits in slider points per second: with 60 updates a second (more
# on faster screens) even the fastest normal fade moves well under one point
# per update, which the eye can't see as a step.
MAX_RISE = 25.0
MAX_RISE_FAST = 55.0
MAX_FALL = 50.0
MAX_RUSH = 240.0
# Whatever speed is chosen, a fade never moves more than this per second
# (about 1.3 points per update outside a flash, 4 in one), so "fast" is
# quicker but still below the step the eye notices.  (The quick fade into a
# new darker view is capped per update instead: see CEILING_STEP.)
CEILING = 80.0
CEILING_RUSH = 240.0
FADER_HZ = 60                   # how often the displays are moved (a 60 Hz screen)...
FADER_MAX_HZ = 120              # ...and on faster screens up to this, so each step is smaller
# Not 240 on a 240 Hz screen: the eye can't follow brightness changes much
# faster than 60-90 a second (why flicker above that looks steady), so
# 120 steps a second already blend into one glide, even in a 0.1 s flash
# fade.  Every step costs a Windows call per display (and the game needs
# the CPU), so 240 would double what a fade costs for nothing visible.
IDLE_S = 0.1                    # how often the fader checks for a new goal while at rest
# The fader passes a move on once some display has gone this far (slider
# points) since the last move it passed on.  The slow end of a glide moves a
# few thousandths of a point per step.  Even the most visible point (Exposure
# near 0 with strong contrast, about 8 L* of lightness per point) then
# changes the picture by under 0.2 L*, far below what the eye notices
# (about 0.4 at mid-grey), so passing on each of those would only cost CPU.
# A display coming to rest always gets its exact final value.
FADER_MIN_STEP = 0.02
# (Python's thread switch interval is left at its 5 ms default: on Windows a
# 1 ms interval made fader steps no more even, measured by the self-test's
# "fader_load" with the Watcher running and with a thread measuring back to
# back, so a process-wide change wasn't worth it.)
STOPS_FOR_FULL = 3.0            # 3 stops darker/brighter than the target = full strength
DEAD_ZONE = 0.08                # ignore changes smaller than this share of How much
CENTER_WEIGHT = 2.0             # the middle of the screen counts up to 3x the edges
SHADOW_SHARE = 0.35             # how much the darkest quarter of the picture counts
SPEEDS = {"slow": 0.5, "normal": 1.0, "fast": 2.0}
SETTLE_S = 1.0                  # only brighten once it has stayed dark this long (no pumping)...
SETTLE_FAST_S = 0.85            # ...but a steadily dark scene (walked into a cave) waits only this
STEADY_RATIO = 1.3              # (steady: measurements this close to each other,
STEADY_S = 0.15                 #  at least two, this far apart)
CALM_S = 2.0                    # unless a light went on (STEADY_RATIO brighter) this recently
DARKENING_COUNT = 3             # this many measurements in a row, each darker than the last...
DARKER = 0.9                    # ...by this much, start brightening early (on the way in)
# Turning or running into the dark (into a building, away from the sky): a
# different view is told from a light going off by how unevenly the parts of
# the picture changed and by its changed layout, so it can be followed after
# two measurements instead of waiting out SETTLE_FAST_S (a light going off
# still waits: it may flicker).
REGION_COLS, REGION_ROWS = 8, 4  # the coarse grid each picture's parts are measured on
VIEW_SPREAD = 0.6               # parts changing this unevenly (stops) = a different view...
VIEW_LAYOUT = 0.6               # ...if its layout is less alike than this (a light going off
                                # keeps where the bright and dark parts are, even when a
                                # window it doesn't light stays the same and the parts
                                # change unevenly)
VIEW_MOVED = 0.65               # ...and if at least this share of the parts changed by more
VIEW_STILL = 0.25               # than this (stops): a light that reaches only part of the
                                # view (a lamp in a corner, a muzzle flash lighting the middle,
                                # lightning through a window) leaves the rest as it was, though
                                # the parts it lights change unevenly and its layout changes;
                                # turning changes nearly every part (all but a static HUD's
                                # few, which are the same in every view)
VIEW_SETTLED_S = 1.0            # brightness held this long is settled (not a flash or a glance)
VIEW_STEADY_S = 0.08            # a new view seen this long (two measurements) is followed...
VIEW_TURN_S = 2.0               # ...if it came this soon after the settled one (a turn, a walk in)
# Back from a quick look at a brighter, different view (glancing out of a
# door, a door opened and shut, a menu opened for a moment): the settled
# view it left is followed again once it has been back a little while, at
# the new-view pace, instead of waiting out SETTLE_S as if a light had
# flashed (a light flashing keeps the layout, so it still waits).
RETURN_HOLD_S = 0.6             # back this long (the dark spells running past windows are
                                # shorter: shorter holds pump there)...
RETURN_LAYOUT = 0.8             # ...laid out this much like the settled view (the same view)
ALTERNATE_S = 2.0               # a brighter view back this soon after = turning back and forth...
ALTERNATE_HOLD_S = 4.0          # ...so new views wait the usual way until that stops this long
RISE_VIEW_S = 0.15              # a new darker view is caught up with quicker still (too dark
                                # hides far more than a moment too bright, and the view
                                # change hides the fade)...
MAX_RISE_VIEW = 160.0           # ...up to this many points a second (120 updates a second)...
CEILING_STEP = 4 / 3            # ...but never more than this per update (80 a second at 60 Hz,
                                # under the 2 points of a flash's fade at 120 Hz; per update
                                # the display really gets, see Fader.set_rates)
TURN_BRAKE = 8000.0             # still moving away from a goal that turned round (a light
                                # came up while brightening): slow down by this many points
                                # a second, every second, so even the quickest rise (160 a
                                # second) stops within 0.02 s and a slow one within a frame,
                                # instead of carrying on for over a tenth of a second (2-3
                                # points past the new goal, then back)
# A flashbang (or a blast right in front) whites the whole picture out and
# then fades back to the view it hit.  Lightning, muzzle flashes and strobes
# light the view instead (its dark parts stay darker than this), for less
# than WHITE_OUT_S, and snap straight back off.  A white-out fades back
# (each picture no brighter than the one before, at least one in between
# the white and the view) and shows the view it hit coming back through the
# white, so it is followed as it goes, instead of waiting out CALM_S as if
# a light had flashed: the room can be seen again as soon as the white has
# gone.  One that snaps straight back, or fades into another view, still
# waits.
WHITE_OUT = 0.6                 # every part of the picture at least this bright (0-1) = a white-out...
WHITE_OUT_SKIP = 4              # (but for this many parts: a HUD drawn over the white and
                                # corners a vignette keeps darker)
WHITE_OUT_S = 0.2               # ...lasting this long (white pictures this far apart; a lightning
                                # stroke is shorter)
FADE_S = 2.0                    # its fade back is followed for up to this long after the last white picture
FLASH_RATIO = 2.0               # a picture twice as bright as the last one is a flash...
LOST_S = 2.0                    # ...and a picture lost this long eases back to the preset
BLACK_HOLD_S = 30.0             # a black screen holds the look this long (loading screens)
NOISE_KEY = 2.5 / 255           # a wobble this small (about a level each way, with margin) is only noise
FROZEN_S = 3.0                  # a picture that hasn't changed at all this long is stale
AIM_STEP = 1.25                 # the "aim brighter / darker" hotkeys change Aim for this much
# When the main slider runs out of room, the rest goes to the other one
# (Exposure works harder per point, so it gets half as much).
SPILL = {"shadow_boost": ("exposure", 0.5), "exposure": ("shadow_boost", 1.0)}
SLIDER_RANGE = (0.0, 100.0)
# Anti-glare only works while Exposure is above 0, and then at once at full
# strength: Exposure 0.01 with Anti-glare is a different picture from 0 (up
# to about 22 L* of lightness, a visible jump in one fade step).  So with
# Anti-glare on, auto levels keeps Exposure at least this high when the
# preset has it on (0.01 survives the rounding to 2 decimals), and leaves it
# off when the preset has it off.
EXPOSURE_FLOOR = 0.01

# Sharing the range with bright areas (beta): a window, an open door or a
# lamp seen from a dark room gets extra Outside detail (the highlight_room
# setting, see curves.HIGHLIGHT_SLOPE) in proportion to how much of the view
# it fills, so the lifted room doesn't push it to white.  Only brightness is
# counted (the share of bright pixels), never where they are.
HIGHS_LEVEL = 96                # brightness (0-255) that counts as a bright area
ROOM_EXTRA = 40.0               # the most Outside detail points it adds...
ROOM_FULL = 0.25                # ...once this share of the view is bright, and at least this
                                #  much is not (in proportion to the square root below that,
                                #  so a small window gets some; see room_for_highs)
ROOM_MIN_SHARE = 0.005          # less than this is specks and HUD: no change
HIGHS_SPARSE = 0.10             # with the picture's grid (see REGION_COLS), bright pixels count
HIGHS_DENSE = 0.30              #  only in parts at least this bright (fully from DENSE): a
                                #  window or lamp fills its part, HUD text and specks only dot it
ROOM_RISE_S = 0.6               # a window coming into view gets its room in about a second
                                #  (the preset's own Outside detail covers the first moments)...
ROOM_FALL_S = 1.5               # ...and gives it back slowly (it has left the view by then)
ROOM_STEP = 0.25                # the fader passes it on when it moved this much (an
                                #  invisible change: under 1/1000 of the range)...
ROOM_SNAP = 0.5                 # ...and this close it goes straight there (under half a level)
ROOM_DEADBAND = 2.0             # a moving picture changes the bright share a little all the
                                #  time: smaller changes than this (about a level) are ignored,
                                #  so a steady view doesn't redo the tables on every picture
ROOM_KEEP_LEVEL = 40            # the room comes first: the extra never reaches this input
                                #  (a dark room's brightest walls) or anything darker, so with
                                #  a strong lift (a high boost, Pitch Black, Night Vision) it
                                #  gets less, or none (see curves.highlight_room_cap)
ROOM_STRENGTH_FULL = 10.0       # How much below this many points scales the extra down with
                                #  it: 0 turns auto levels' window response off too

DEFAULT_STRENGTH = 30.0         # how far (slider points) it may move Shadow boost / Exposure
DEFAULT_TARGET = 0.12           # the scene brightness it aims for (log-average, 0-1)
STRENGTH_LIMITS = (0.0, 60.0)
TARGET_LIMITS = (0.04, 0.30)

_LOG = [math.log(max(i, 1) / 255.0) for i in range(256)]
_LN2 = math.log(2.0)

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class Measurement:
    key: float      # what auto levels steers by: overall brightness with the
                    # middle of the screen and the dark areas counting extra (0-1)
    average: float  # center-weighted log-average brightness (0-1)
    shadows: float  # how bright the darkest quarter of the picture is (0-1)
    dark: float     # share of the picture that is near-black
    bright: float   # share that is near-white
    black: bool     # completely black: probably can't see the picture
    signature: int = 0  # changes whenever the picture changes (0 = not tracked)
    # Average brightness (log2) of each part of a coarse REGION_COLS x
    # REGION_ROWS grid, left to right, top to bottom, for telling a turn to
    # another view from a light changing (see view_spread).  Only brightness:
    # nothing is looked for in the parts.  Empty if not known.
    regions: tuple = ()
    # When it was captured (time.perf_counter(); 0 = not known).  The
    # controller's own clock only moves in IDLE_S steps while the fader
    # rests, so how far apart two pictures were is taken from these.
    at: float = 0.0
    # Center-weighted share of the picture at HIGHS_LEVEL or brighter (a
    # window, a lamp, the sky): how much output range they get.  With the
    # grid, only bright pixels in parts they partly fill (see _dense_highs).
    highs: float = 0.0


_weights_cache: dict[tuple[int, int], list[float]] = {}


def _center_weights(width: int, height: int) -> list[float]:
    """Center-weighted metering, like a camera: what you look at counts
    most, the HUD and screen edges least."""
    key = (width, height)
    if key not in _weights_cache:
        out = []
        for y in range(height):
            dy = (y + 0.5) / height - 0.5
            for x in range(width):
                dx = (x + 0.5) / width - 0.5
                out.append(1.0 + CENTER_WEIGHT * math.exp(-(dx * dx + dy * dy) / 0.06))
        _weights_cache[key] = out
    return _weights_cache[key]


def _regions(lum: list[int], width: int, height: int) -> tuple:
    """Log2 average brightness of each grid part (see Measurement.regions)."""
    logs = list(map(_LOG.__getitem__, lum))
    cols = [c * width // REGION_COLS for c in range(REGION_COLS + 1)]
    sums = [0.0] * (REGION_COLS * REGION_ROWS)
    counts = [0] * (REGION_COLS * REGION_ROWS)
    for y in range(height):
        row, first = y * width, (y * REGION_ROWS // height) * REGION_COLS
        for c in range(REGION_COLS):
            a, b = row + cols[c], row + cols[c + 1]
            sums[first + c] += sum(logs[a:b])
            counts[first + c] += b - a
    return tuple(s / n / _LN2 for s, n in zip(sums, counts))


_part_areas: dict[tuple[int, int], list[float]] = {}


def _dense_highs(lum: list[int], weights: list[float], width: int, height: int) -> float:
    """The weighted amount of bright pixels (HIGHS_LEVEL or brighter) that
    sit in grid parts they fill at least HIGHS_SPARSE of (in full from
    HIGHS_DENSE, in proportion between).  A window, an open door or a lamp
    fills the parts it is in; bright HUD text, a crosshair or specks only
    dot theirs, so they don't count as something to keep visible.  (The
    parts are the fixed grid of Measurement.regions: nothing is located.)"""
    cols = [c * width // REGION_COLS for c in range(REGION_COLS + 1)]
    areas = _part_areas.get((width, height))
    if areas is None:
        areas = [0.0] * (REGION_COLS * REGION_ROWS)
        for y in range(height):
            row, first = y * width, (y * REGION_ROWS // height) * REGION_COLS
            for c in range(REGION_COLS):
                areas[first + c] += sum(weights[row + cols[c]:row + cols[c + 1]])
        _part_areas[(width, height)] = areas
    mass = [0.0] * (REGION_COLS * REGION_ROWS)
    bright = HIGHS_LEVEL.__le__
    for y in range(height):
        row, first = y * width, (y * REGION_ROWS // height) * REGION_COLS
        for c in range(REGION_COLS):
            a, b = row + cols[c], row + cols[c + 1]
            seg = lum[a:b]
            if max(seg) >= HIGHS_LEVEL:      # (most of a dark room has none: skipped)
                mass[first + c] += sum(compress(weights[a:b], map(bright, seg)))
    out, span = 0.0, HIGHS_DENSE - HIGHS_SPARSE
    for m, area in zip(mass, areas):
        if m:
            out += m * min(1.0, max(0.0, (m / area - HIGHS_SPARSE) / span))
    return out


def view_spread(a: Measurement, b: Measurement) -> float | None:
    """How unevenly the picture changed from a to b, in stops (the spread of
    the change across the grid parts).  A light going on or off, or the
    sun going behind a cloud, changes every part of the view much alike;
    turning to look somewhere else (into a doorway, out of a cave) changes
    each part differently.  None if either picture's parts aren't known."""
    ra, rb = a.regions, b.regions
    if not ra or len(ra) != len(rb):
        return None
    change = [y - x for x, y in zip(ra, rb)]
    mean = sum(change) / len(change)
    return math.sqrt(sum((c - mean) ** 2 for c in change) / len(change))


def view_moved(a: Measurement, b: Measurement) -> float | None:
    """The share of the grid parts whose brightness changed by more than
    VIEW_STILL stops from a to b.  Turning to look somewhere else changes
    nearly every part; a light that reaches only part of the view (a lamp
    in a corner, a muzzle flash lighting the middle) leaves the rest as it
    was.  None if either picture's parts aren't known."""
    ra, rb = a.regions, b.regions
    if not ra or len(ra) != len(rb):
        return None
    return sum(1 for x, y in zip(ra, rb) if abs(y - x) > VIEW_STILL) / len(ra)


def _other_view(a: Measurement, b: Measurement) -> bool:
    """b is a different view from a: its parts changed unevenly, its layout
    changed and nearly all of it changed (see VIEW_SPREAD, VIEW_LAYOUT and
    VIEW_MOVED), not a light going on or off in all or part of the view."""
    spread, layout, moved = view_spread(a, b), view_layout(a, b), view_moved(a, b)
    return (spread is not None and spread >= VIEW_SPREAD
            and layout is not None and layout < VIEW_LAYOUT
            and moved is not None and moved >= VIEW_MOVED)


def _blend_layout(m: Measurement, a: Measurement, b: Measurement) -> float:
    """How well m is laid out like a blend of a and b (0-1: the multiple
    correlation of m's parts with theirs, both counting positively).  Half
    way through a fade from one to the other, a picture is laid out much
    like neither, but like the two together."""
    r1, r2, r12 = view_layout(a, m), view_layout(b, m), view_layout(a, b)
    if r1 is None or r2 is None or r12 is None:
        return 0.0
    if 1 - r12 * r12 < 1e-6:
        return max(r1, r2)
    if r1 - r2 * r12 < 0 or r2 - r1 * r12 < 0:
        return 0.0                  # one of the two counts against it: not a blend
    fit = (r1 * r1 + r2 * r2 - 2 * r1 * r2 * r12) / (1 - r12 * r12)
    return math.sqrt(max(0.0, min(1.0, fit)))


def _white_out(m: Measurement) -> bool:
    """Every part of m but WHITE_OUT_SKIP is at least WHITE_OUT bright."""
    regions = m.regions
    return bool(regions) and (sorted(regions)[min(WHITE_OUT_SKIP, len(regions) - 1)]
                              >= math.log2(WHITE_OUT))


def view_layout(a: Measurement, b: Measurement) -> float | None:
    """How alike the layout of a and b is: where the bright and dark parts
    are, as the correlation of their grid parts' brightness (1 = the same
    layout, 0 = unrelated, below 0 = swapped round).  A light going off
    keeps the layout even where it lights the parts unevenly (a lamp-lit
    room with a window); turning to look somewhere else changes it.  A
    picture that is even all over has no layout and counts as alike (1).
    None if either picture's parts aren't known."""
    ra, rb = a.regions, b.regions
    if not ra or len(ra) != len(rb):
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    da, db = [x - ma for x in ra], [y - mb for y in rb]
    norm = math.sqrt(sum(x * x for x in da) * sum(y * y for y in db))
    if norm < 1e-9:
        return 1.0
    return sum(x * y for x, y in zip(da, db)) / norm


def measure(bgra: bytes, width: int | None = None) -> Measurement:
    """Brightness of a 32-bit BGRA picture (as Windows hands it over).
    With the picture's width (a real capture), the middle of the screen
    counts extra and a stale, unchanging picture can be noticed."""
    n = len(bgra) // 4
    if n == 0:
        return Measurement(0.0, 0.0, 0.0, 1.0, 0.0, True)
    lum = [(54 * r + 183 * g + 19 * b) >> 8
           for b, g, r in zip(bgra[0::4], bgra[1::4], bgra[2::4])]
    regions: tuple = ()
    if width and n % width == 0:
        weights = _center_weights(width, n // width)
        if width >= REGION_COLS and n // width >= REGION_ROWS:
            regions = _regions(lum, width, n // width)
    else:
        weights = [1.0] * n
    total = sum(weights)
    average = math.exp(sum(w * _LOG[v] for w, v in zip(weights, lum)) / total)
    hist = [0.0] * 256
    for w, v in zip(weights, lum):
        hist[v] += w
    highs = sum(hist[HIGHS_LEVEL:])
    if highs and regions:
        highs = _dense_highs(lum, weights, width, n // width)
    highs /= total
    seen, shadow_level = 0.0, 0
    for level, w in enumerate(hist):          # weighted 25th percentile
        seen += w
        if seen >= total * 0.25:
            shadow_level = level
            break
    shadows = max(shadow_level, 1) / 255
    key = math.exp((1 - SHADOW_SHARE) * math.log(average) + SHADOW_SHARE * math.log(shadows))
    dark = sum(1 for v in lum if v < 26) / n
    bright = sum(1 for v in lum if v > 230) / n
    signature = (hash(bgra) or 1) if width else 0
    return Measurement(key, average, shadows, dark, bright, max(lum) == 0, signature, regions,
                       highs=highs)


def capture_interval(prev: Measurement | None, m: Measurement | None) -> float:
    """Seconds until the next measurement: BURST_S right after the
    brightness changed a lot (so a new view is confirmed, and followed, a
    tenth of a second sooner), else INTERVAL_S."""
    if (prev is not None and m is not None and not prev.black and not m.black
            and max(prev.key, m.key) > min(prev.key, m.key) * STEADY_RATIO):
        return BURST_S
    return INTERVAL_S


def target_offset(key: float, strength: float, target: float, dim: bool = True) -> float:
    """How far to move the slider for a scene this bright: up to +strength
    in dark scenes, down to -strength in bright ones (only if dim), 0 at the
    target."""
    stops = math.log2(target / max(key, 1 / 255))
    amount = strength * max(-1.0, min(1.0, stops / STOPS_FOR_FULL))
    return amount if dim else max(0.0, amount)


def _slider_range(name: str, base: dict[str, float]) -> tuple[float, float]:
    """Where auto levels may take this slider (see EXPOSURE_FLOOR)."""
    lo, hi = SLIDER_RANGE
    if name == "exposure" and base.get("anti_glare", 0.0) > 0:
        start = base[name]
        if start <= 0:
            return start, start   # stays off: switching it on would jump
        lo = min(start, EXPOSURE_FLOOR)
    return lo, hi


def split_offset(key: str, offset: float, base: dict[str, float]) -> dict[str, float]:
    """Share an adjustment between Shadow boost and Exposure: the preset's
    main slider first, and whatever doesn't fit (it's already near the top or
    bottom) goes to the other one, so auto levels keeps its full range.
    With the preset's "anti_glare" in ``base``, Exposure never crosses 0
    (see EXPOSURE_FLOOR)."""
    lo, hi = _slider_range(key, base)
    main = max(lo - base[key], min(hi - base[key], offset))
    out = {key: main}
    rest = offset - main
    if abs(rest) > 0.05:
        other, ratio = SPILL[key]
        lo, hi = _slider_range(other, base)
        extra = max(lo - base[other], min(hi - base[other], rest * ratio))
        if abs(extra) > 0.05:
            out[other] = extra
    return out


def room_for_highs(highs: float, strength: float = DEFAULT_STRENGTH) -> float:
    """Extra Outside detail (points) for this share of bright pixels: in
    proportion to the square root of the share, up to ROOM_EXTRA.  It shares
    the range between a dark room and bright areas, so it needs both: a view
    that is almost all bright (a flash whiting out the screen, the open sky)
    has no room to share with and gets as little as the same share of dark
    would.  How much scales it below ROOM_STRENGTH_FULL (0: none)."""
    share = min(highs, 1.0 - highs)
    if share <= ROOM_MIN_SHARE or strength <= 0:
        return 0.0
    scale = min(1.0, strength / ROOM_STRENGTH_FULL)
    return ROOM_EXTRA * scale * min(1.0, math.sqrt((share - ROOM_MIN_SHARE) / ROOM_FULL))


def fader_amounts(key: str, offset: float, base: dict[str, float],
                  controller: "Controller | None" = None) -> dict[str, float]:
    """What one display gets on top of the preset: the offset shared out by
    split_offset, plus the controller's Outside detail for bright areas in
    view (see room_for_highs), as much as leaves the room's tones alone
    (ROOM_KEEP_LEVEL).  ``base``: the look's settings (every field, so the
    room's tones can be worked out; at least the ones split_offset moves)."""
    out = split_offset(key, offset, base)
    if controller is not None and controller.room > 0:
        extra = min(controller.room, _room_left(base, out))
        if extra > 0:
            out["highlight_room"] = extra
    return out


_SETTING_NAMES = tuple(f.name for f in fields(curves.Settings))
# What the room's tones depend on (see curves._lifted), plus the preset's
# own Outside detail.
_ROOM_FIELDS = ("exposure", "anti_glare", "gamma", "shadow_boost", "brightness", "contrast",
                "highlight_room")
# _room_left's answers for the last few looks: while the Outside detail
# glides in or out the offset is mostly at rest, so it is the same look on
# every step.  Emptied when it holds this many.
_ROOM_LEFT: dict[tuple, float] = {}
_ROOM_LEFT_MAX = 64


def _room_left(base: dict, amounts: dict[str, float]) -> float:
    """How much Outside detail may be added to the look ``base`` plus
    ``amounts`` before it reaches ROOM_KEEP_LEVEL (see
    curves.highlight_room_cap; can be below 0)."""
    cache_key = (tuple(base.get(k) for k in _ROOM_FIELDS), tuple(sorted(amounts.items())))
    left = _ROOM_LEFT.get(cache_key)
    if left is None:
        known = {k: v for k, v in base.items() if k in _SETTING_NAMES}
        look = curves.Settings(**known).clamped(
            {k: base.get(k, 0.0) + v for k, v in amounts.items()})
        left = curves.highlight_room_cap(look, ROOM_KEEP_LEVEL / 255.0) - look.highlight_room
        if len(_ROOM_LEFT) >= _ROOM_LEFT_MAX:
            _ROOM_LEFT.clear()
        _ROOM_LEFT[cache_key] = left
    return left


def look_values(s: curves.Settings) -> dict:
    """A look as fader_amounts' ``base``: every setting by name."""
    return {k: getattr(s, k) for k in _SETTING_NAMES}


def clean_title(title: str) -> str:
    # rstrip after the cut: a cut ending on a space would not survive
    # config.py cleaning the saved key again (this must be idempotent).
    return " ".join((title or "").split())[:60].rstrip()


def nudge_aim(aim: float, brighter: bool) -> float:
    """One press of "aim brighter" or "aim darker"."""
    lo, hi = TARGET_LIMITS
    aim = aim * AIM_STEP if brighter else aim / AIM_STEP
    return round(min(hi, max(lo, aim)), 3)


def aim_for_current_look(key: float, offset: float, strength: float,
                         current: float | None = None, dim: bool = True) -> float:
    """The Aim for value that makes a scene this bright keep its current
    adjustment ("Keep this look").  ``current`` (the Aim for in use) is kept
    if it already gives this look: at full boost, say, many values do, and
    moving it would change every other scene."""
    if current is not None and abs(target_offset(key, strength, current, dim) - offset) < 0.5:
        return current
    if strength <= 0:
        return key
    stops = max(-1.0, min(1.0, offset / strength)) * STOPS_FOR_FULL
    return key * 2 ** stops


def _apart(a: tuple, b: tuple) -> float:
    """Seconds between two (clock time, Measurement) entries: by when the
    pictures were captured if both are known, else by the controller's
    clock."""
    if a[1].at and b[1].at:
        return b[1].at - a[1].at
    return b[0] - a[0]


@dataclass
class Controller:
    """Decides where one display should go and glides it there.

    observe() takes each new measurement (five a second, ten while the
    brightness changes a lot) and sets the goal; advance() moves toward it
    in tiny steps (60-120 a second, on the fader thread).  The motion eases
    in and out like a spring and has a speed limit, so there is never a
    visible jump."""
    offset: float = 0.0
    want: float = 0.0
    strength: float = 0.0
    clock: float = 0.0
    velocity: float = 0.0
    rush: bool = False                             # a flash (or walking out of the dark): fade down extra fast
    frozen: bool = False                           # the picture has stopped changing
    key: float = 0.0                               # the brightness the goal follows
    _catch_up: bool = False                        # a big brightening: quick pace to the end
    _recent: list = field(default_factory=list)   # (time, key) of the last SETTLE_S
    _last_key: float = 0.0
    _jump_at: float = -CALM_S                      # when a light last went on (or a flash)
    _missing_since: float | None = None
    _signature: int = 0
    _same_since: float = 0.0
    _black_since: float | None = None
    _settings: tuple = ()                          # (strength, target, dim) last seen
    _stretch: list = field(default_factory=list)  # (time, Measurement) since the last big change
    _settled: tuple | None = None                  # (since, until, newest) of the last settled stretch
    _away: Measurement | None = None               # the brightest view looked at since (2+ pictures)
    _quick_at: float = -ALTERNATE_S                # when the last view followed early began
    _alternate_until: float = 0.0                  # turning back and forth: no early start till then
    _view_rise: bool = False                       # brightening for a new darker view: quickest pace
    _estimate: bool = False                        # the goal is a halfway guess (into a cave)
    _rush_view: bool = False                       # the fast fade-down is for a new, brighter view
    _fade: float = 0.0                             # newest key of a white-out or its fade back (0 = none)
    _fade_since: float = 0.0                       # when the white-out began
    _fade_at: float = 0.0                          # when its last white picture came
    _fading: bool = False                          # its fade back has begun (a picture in between)
    _fade_from: Measurement | None = None          # the settled view the white-out hit
    _white: Measurement | None = None              # the white picture it began with
    room: float = 0.0                              # extra Outside detail now (see room_for_highs)...
    room_want: float = 0.0                         # ...and where it is heading
    _room_back: bool = False                       # released: hand it back at the quick pace
    _highs: float = 0.0                            # the last picture's bright share

    def observe(self, m: Measurement | None, strength: float, target: float,
                dim: bool = True, now: float | None = None) -> None:
        """``now``: the clock as it is right now (Fader.now_for); by default
        the clock as of the fader's last step."""
        self.strength = strength
        clock = self.clock if now is None else max(now, self.clock)
        if m is not None:
            # (A lost picture keeps it; a black one has no bright areas.)
            self._highs = m.highs
            self._aim_room(strength)
            self._room_back = False
        # A lower How much or an unticked "tone down" brings the goal inside
        # its new limits at once, even while a black or lost picture holds.
        self.want = min(strength, max(-strength if dim else 0.0, self.want))
        if m is not None and m.black:
            # Loading screen, or capture blocked: hold the look.  A black
            # screen is pixel-identical every time, so it must not count as
            # a frozen picture either; only a very long one eases back.
            self._missing_since = None
            self._signature, self._same_since = m.signature, clock
            self.frozen = False
            self._stretch = []
            if self._black_since is None:
                self._black_since = clock
            elif clock - self._black_since >= BLACK_HOLD_S:
                self.want, self.rush = 0.0, False
            return
        if m is not None:
            self._black_since = None
        if m is not None and m.signature:
            # Some games in exclusive fullscreen leave Windows with an old,
            # frozen picture.  One that hasn't changed at all for a few
            # seconds is treated as not seen, so it can't steer anything.
            if m.signature != self._signature:
                self._signature, self._same_since = m.signature, clock
            self.frozen = clock - self._same_since >= FROZEN_S
            if self.frozen:
                m = None
        if m is None:  # couldn't capture the picture: hold, then ease back to the preset
            self._stretch = []
            if self._missing_since is None:
                self._missing_since = clock
            elif clock - self._missing_since >= LOST_S:
                self.want = 0.0
            return
        self._missing_since = None
        now = clock
        self._recent = [(t, k) for t, k in self._recent if now - t < SETTLE_S]
        self._recent.append((now, m.key))
        if self._last_key and m.key > self._last_key * STEADY_RATIO:
            self._jump_at = now
            if now - self._quick_at < ALTERNATE_S or now < self._alternate_until:
                # Back to a brighter view soon after a darker one was followed
                # early (running past windows, in and out of a doorway):
                # following every turn would pump, so new views wait the
                # usual way until the turning has stopped for a while.
                self._alternate_until = now + ALTERNATE_HOLD_S
        last = self._stretch[-1][1] if self._stretch else None
        fade_key = self._fading_back(now, m)
        self._track_stretch(now, m)
        # Steer by the brightest recent picture: dimming reacts at once, but
        # brightening waits until it has stayed dark, so flickering lights,
        # muzzle flashes and lightning don't make the picture pump.
        # (Turning back and forth between a bright and a dark view counts as
        # a light flashing too: see ALTERNATE_HOLD_S.)
        calm = now - self._jump_at >= CALM_S and now >= self._alternate_until
        lately = [(t, k) for t, k in self._recent if now - t < SETTLE_FAST_S]
        steady = [k for _t, k in lately]
        newest = [k for _t, k in self._recent[-DARKENING_COUNT:]]
        early, halfway = None, False
        if (calm and now - lately[0][0] >= STEADY_S
                and max(steady) <= min(steady) * STEADY_RATIO):
            # Steadily dark with no light flashing lately (walked into a
            # building): only wait SETTLE_FAST_S, so it isn't left too dark.
            early = max(steady)
        elif (calm and len(newest) == DARKENING_COUNT
              and all(b < a * DARKER for a, b in zip(newest, newest[1:]))):
            # Getting darker and darker (walking into a cave): start
            # brightening gently on the way, halfway (in stops) to the newest picture.
            early = math.sqrt(max(k for _t, k in self._recent) * newest[-1])
            halfway = True
        view_key = self._new_darker_view(now, m)
        if view_key is None:
            view_key = self._back_to_settled_view(now, m)
        if view_key is None:
            view_key = fade_key
        if view_key is not None and (early is None or view_key < early):
            # Turned or ran into a darker view, or back to the one it was
            # settled in after a quick look out (or a white-out fading back
            # to it): follow it now.
            early, halfway = view_key, False
            self._quick_at = self._stretch[0][0]
        if early is not None:
            # Brightening granted early is kept: the older, brighter pictures
            # can't take it back (that would pump), only a brighter new one can.
            self._recent = [(t, min(k, early)) for t, k in self._recent]
        key = self.key = max(k for _t, k in self._recent)
        want = target_offset(key, strength, target, dim)
        flash = bool(self._last_key) and m.key >= self._last_key * FLASH_RATIO
        # Coming out of the dark (a cave mouth, a door swinging open onto
        # daylight) the picture brightens over several measurements, each
        # less than a flash.  Brightening again while the display is still
        # fading down, and still boosted far too much, is that walk out: the
        # rest fades at the flash pace, so the way out doesn't glare.  One
        # brighter picture on its own (a light flickering on) still dims at
        # the normal pace, so flicker never dims more than it used to.
        # Unless it is a different view: a brighter picture whose parts
        # changed unevenly and whose layout changed (turning toward the sky,
        # the first step out of a cave mouth) is the way out too, so it
        # fades at the flash pace from the first picture.  A light coming
        # on keeps the layout, so it still dims at the normal pace.  Walking
        # out over half a second or so, each picture is laid out much like
        # the one before; it is told by comparing with the dark view it was
        # settled in a moment ago instead.
        new_view = self._new_brighter_view(last, m) or self._left_for_brighter_view(now, m)
        glaring = self.offset - want > strength * LEAVING_GAP
        leaving = glaring and (new_view or (
            bool(self._last_key) and m.key >= self._last_key * STEADY_RATIO
            and self.want < self.offset - LEAVING_UNDERWAY))
        self._last_key = m.key
        # Small changes don't set a new goal, so the picture doesn't keep
        # "breathing"; the glide itself never stops halfway.  Near black a
        # one-level wobble (video compression, dithering) is a big change in
        # stops, so the goal only moves if it is clear of that noise too.
        near = target_offset(key + NOISE_KEY, strength, target, dim)
        far = target_offset(max(key - NOISE_KEY, 0.0), strength, target, dim)
        # (A changed How much / Aim for / dim always applies at once.)
        settings, before = (strength, target, dim), self._settings
        self._settings = settings
        # A halfway guess was never measured, so it isn't held by the noise
        # filter: near black that band is wide enough to leave the cave
        # several points too dark for good.
        noise = settings == before and not self._estimate and near <= self.want <= far
        if (settings != before or flash
                or not noise and abs(want - self.want) >= max(0.5, strength * DEAD_ZONE)):
            # A flash fades down fast; so does a picture that keeps getting
            # brighter while that fast fade is still going (walking out into
            # daylight takes several measurements, not one).
            brighter = self.rush and want < self.want
            if want < self.want:
                self._view_rise = False
            elif (view_key is not None and early == view_key) or (
                    halfway and self._estimate and self._left_settled_view(m)):
                # A new darker view is caught up with at the quickest pace;
                # so is walking into one, once it has kept getting darker
                # for a second halfway step (the first may be a quick turn
                # that is about to end, or to come back).
                self._view_rise = True
            self.want = want
            rush = (flash or brighter or leaving) and want < self.offset
            # A new view (or a fade for one that keeps getting brighter)
            # fades at the speed limit from the start: see RUSH_VIEW_S.  Set
            # before rush, so the fader thread never pairs a new fast fade
            # with the pace of an old one.
            self._rush_view = rush and (new_view or (brighter and self._rush_view))
            self.rush = rush
            self._estimate = halfway

    def _track_stretch(self, now: float, m: Measurement) -> None:
        """Keep track of how long the brightness has held: pictures in a row
        within STEADY_RATIO of each other (the camera may move) are one
        stretch, and one that lasted VIEW_SETTLED_S is settled."""
        stretch = self._stretch
        prev = stretch[-1][1] if stretch else None
        if prev is not None and max(prev.key, m.key) <= min(prev.key, m.key) * STEADY_RATIO:
            stretch.append((now, m))
            del stretch[1:-2]       # only its start and newest two matter
        else:
            # A view seen for two pictures in a row (not a single flash) since
            # the settled stretch ended is remembered if it is the brightest
            # one looked at (see _back_to_settled_view).
            if (stretch and _apart(stretch[0], stretch[-1]) >= VIEW_STEADY_S
                    and (self._settled is None or stretch[0][0] > self._settled[1])):
                top = max(stretch[0][1], stretch[-1][1], key=lambda x: x.key)
                if self._away is None or top.key > self._away.key:
                    self._away = top
            self._stretch = stretch = [(now, m)]
        if now - stretch[0][0] >= VIEW_SETTLED_S:
            self._settled, self._away = (stretch[0][0], now, m), None

    @staticmethod
    def _new_brighter_view(last: Measurement | None, m: Measurement) -> bool:
        """m is at least STEADY_RATIO brighter than the picture just before
        it and a different view: its parts changed unevenly and its layout
        changed (turned toward the sky or a window, stepped out of a cave
        mouth), not a light going on, which brightens the view much alike
        and keeps where the bright and dark parts are."""
        if last is None or m.key < last.key * STEADY_RATIO:
            return False
        spread, layout = view_spread(last, m), view_layout(last, m)
        return (spread is not None and spread >= VIEW_SPREAD
                and layout is not None and layout < VIEW_LAYOUT)

    def _left_for_brighter_view(self, now: float, m: Measurement) -> bool:
        """m is a different, much brighter view than the settled stretch
        that ended within VIEW_TURN_S (see _new_brighter_view): walking out
        of a cave mouth or a doorway, where each picture is only a little
        brighter than the one before and laid out much like it."""
        if self._settled is None:
            return False
        _since, until, seen = self._settled
        return now - until <= VIEW_TURN_S and self._new_brighter_view(seen, m)

    def _new_darker_view(self, now: float, m: Measurement) -> float | None:
        """The newest two pictures, VIEW_STEADY_S apart, show the same new
        view (alike, and within STEADY_RATIO), much darker than the settled
        stretch it came right after and unlike it, both in how unevenly the
        parts changed and in layout (turned or ran into the dark, not a
        light going off), with no light going on since: the
        brightness to steer by now (the brighter of the two).  None
        otherwise."""
        ref, stretch = self._settled, self._stretch
        if ref is None or len(stretch) < 2 or now < self._alternate_until:
            return None
        since, until, seen = ref
        if (since >= stretch[0][0] or _apart(stretch[-2], (now, m)) < VIEW_STEADY_S
                or stretch[0][0] - until > VIEW_TURN_S or self._jump_at > until):
            return None
        prev = stretch[-2][1]
        key = max(prev.key, m.key)
        alike = view_spread(prev, m)
        if (alike is None or alike >= VIEW_SPREAD or seen.key <= key * STEADY_RATIO
                or not _other_view(seen, m)):
            return None
        return key

    def _left_settled_view(self, m: Measurement) -> bool:
        """m is a much darker, different view than the settled stretch (laid
        out unlike it and changed unevenly: walking into a cave, not a light
        dimming), with no light going on since."""
        if self._settled is None:
            return False
        _since, until, seen = self._settled
        # (Not VIEW_MOVED: walking in, the view of the cave mouth still
        # fills the edges of the picture for a while, and pictures that
        # keep getting darker for three in a row are no flickering light.)
        spread, layout = view_spread(seen, m), view_layout(seen, m)
        return (self._jump_at <= until and seen.key > m.key * STEADY_RATIO
                and spread is not None and spread >= VIEW_SPREAD
                and layout is not None and layout < VIEW_LAYOUT)

    def _back_to_settled_view(self, now: float, m: Measurement) -> float | None:
        """Back in the settled view after a quick look at a much brighter,
        different one (glanced out of a doorway, a door opened and shut, a
        menu opened for a moment): the newest two pictures are alike, as
        dark as the settled view and laid out like it, and have been back
        RETURN_HOLD_S.  A light flashing in the same view never counts (its
        bright pictures keep the layout), nor does a white-out (lightning
        whiting the screen out has a layout of its own under a HUD), and
        the hold outlasts the dark spells of running past windows, so none
        of them can pump.  The brightness to steer by now (never boosting
        more than the settled view had).  None otherwise."""
        ref, stretch, away = self._settled, self._stretch, self._away
        if ref is None or away is None or len(stretch) < 2 or now < self._alternate_until:
            return None
        since, until, seen = ref
        start = stretch[0][0]
        if (since >= start or start - until > VIEW_TURN_S
                or _apart(stretch[0], (now, m)) < RETURN_HOLD_S):
            return None
        prev = stretch[-2][1]
        key = max(prev.key, m.key, seen.key)
        if (away.key <= key * STEADY_RATIO or _white_out(away)
                or max(seen.key, m.key) > min(seen.key, m.key) * STEADY_RATIO):
            return None
        # (Not VIEW_MOVED for the look away: a window filling the middle of
        # the view leaves the rest of the room as it was, and a light
        # reaching only part of the view would have to stay on for two
        # pictures and then off for RETURN_HOLD_S, with ALTERNATE_S
        # stopping any repeat.)
        out, out_layout = view_spread(seen, away), view_layout(seen, away)
        back, back_layout = view_spread(seen, m), view_layout(seen, m)
        alike = view_spread(prev, m)
        if (out is None or out < VIEW_SPREAD or out_layout is None or out_layout >= VIEW_LAYOUT
                or back is None or back >= VIEW_SPREAD or back_layout is None
                or back_layout < RETURN_LAYOUT or alike is None or alike >= VIEW_SPREAD):
            return None
        return key

    def _fading_back(self, now: float, m: Measurement) -> float | None:
        """A white-out (see WHITE_OUT) that lasted WHITE_OUT_S fading back
        to the settled view it hit: m is no brighter than the picture before
        it, within FADE_S of the last white picture, laid out like that
        settled view (the view showing through the white, not another one),
        and the fade has shown a picture between the two (still brighter
        than the view: lightning snaps straight back instead).  In a game
        the white has a layout of its own (a HUD drawn over it, a vignette
        darkening its corners), so a picture laid out like the white and
        within FLASH_RATIO of it still counts as white (as a plain white
        fading would, every part still at least WHITE_OUT), and one laid out
        like a blend of the white and the view (RETURN_LAYOUT, see
        _blend_layout) is a picture in between: the fade is followed on
        through those.  The brightness to steer by now (never boosting more
        than the settled view had).  None otherwise.  Called with every
        picture, before _track_stretch."""
        last, self._fade = self._fade, 0.0
        regions, white = m.regions, self._white
        white_layout = view_layout(white, m) if last and white is not None else None
        if regions and (
                _white_out(m)
                or not self._fading and white_layout is not None and white_layout >= RETURN_LAYOUT
                and m.key * FLASH_RATIO >= white.key):
            if not last:
                # Remember the view it hit: a long white-out would
                # otherwise become the settled view itself.
                ref = self._settled
                self._fade_from = (ref[2] if ref is not None and now - ref[1] <= VIEW_TURN_S
                                   else None)
            if not last or self._fading:
                self._fade_since = now
            self._fade, self._fade_at, self._fading = m.key, now, False
            if white_layout is None:
                self._white = m
            return None
        seen = self._fade_from
        if (not last or seen is None or now - self._fade_at > FADE_S
                or self._fade_at - self._fade_since < WHITE_OUT_S or m.key > last + NOISE_KEY):
            return None
        layout = view_layout(seen, m)
        if layout is not None and layout < RETURN_LAYOUT and m.key > seen.key * STEADY_RATIO:
            if white is not None and _blend_layout(m, seen, white) >= RETURN_LAYOUT:
                # A picture in between, but the view can't be told
                # through the white yet.
                self._fade, self._fading = m.key, True
            return None
        if (layout is None or layout < RETURN_LAYOUT
                or not self._fading and m.key <= seen.key * STEADY_RATIO):
            return None
        self._fade, self._fading = m.key, True
        return max(m.key, seen.key)

    def hold(self, strength: float, target: float, dim: bool = True) -> None:
        """No new game picture (CatVision's own window is in front): keep the
        goal, but show a changed How much, Aim for or "tone down" on the last
        picture at once."""
        self.strength = strength
        self.want = min(strength, max(-strength if dim else 0.0, self.want))
        settings = (strength, target, dim)
        if (settings != self._settings and self.key
                and self._missing_since is None and self._black_since is None):
            # (Not once a lost or black picture has eased back to the preset.)
            self._settings = settings
            self.want = target_offset(self.key, strength, target, dim)
            self.rush = False
            self._aim_room(strength)

    @property
    def black_too_long(self) -> bool:
        """A black screen has lasted longer than BLACK_HOLD_S: back to the preset."""
        return self._black_since is not None and self.clock - self._black_since >= BLACK_HOLD_S

    def release(self) -> None:
        """Head back to the preset (no game in front)."""
        self.want = 0.0
        self.room_want, self._room_back, self._highs = 0.0, True, 0.0
        self.rush = self._rush_view = False
        self._recent.clear()
        self._last_key = 0.0
        self._black_since = None
        self._jump_at = self.clock - CALM_S
        self._stretch, self._settled, self._estimate = [], None, False
        self._away = self._fade_from = self._white = None
        self._fade, self._fading = 0.0, False
        self._view_rise = False     # back to the preset at the usual pace

    def advance(self, dt: float, speed: float = 1.0, frame: float | None = None) -> float:
        """``frame``: how often this display is really updated, when that is
        less often than every step (see Fader.set_rates)."""
        self.clock += max(dt, 0.0)
        self._ease_room(dt)
        return self.follow(self.want, self.strength, dt, speed, frame)

    def _aim_room(self, strength: float) -> None:
        """Where the Outside detail heads for the last picture's bright share."""
        room = room_for_highs(self._highs, strength)
        if not room or abs(room - self.room_want) >= ROOM_DEADBAND:
            self.room_want = room

    def _ease_room(self, dt: float) -> None:
        """Glide the Outside detail toward room_want: quicker up than down
        (an even exponential approach, so no step is ever visible)."""
        gap = self.room_want - self.room
        if not gap:
            return
        if abs(gap) < ROOM_SNAP:
            self.room = self.room_want
            return
        # (Back to the preset, as auto levels stops, at the quick pace.)
        pace = ROOM_RISE_S if gap > 0 or self._room_back else ROOM_FALL_S
        self.room += gap * (1.0 - math.exp(-max(dt, 0.0) / pace))

    def step(self, m: Measurement | None, strength: float, target: float, dt: float,
             speed: float = 1.0, dim: bool = True) -> float:
        """observe() then advance(): one measurement, one move."""
        self.observe(m, strength, target, dim)
        return self.advance(dt, speed)

    def relax(self, dt: float, speed: float = 1.0) -> float:
        """Drift back to the preset (no game in front)."""
        self.release()
        return self.advance(dt, speed)

    def follow(self, want: float, strength: float, dt: float, speed: float = 1.0,
               frame: float | None = None) -> float:
        """Glide toward want: a critically damped spring (smooth start and
        stop, no overshoot) with a speed limit, never past want.  ``frame``:
        see advance()."""
        dt = max(dt, 0.0)
        gap = want - self.offset
        if abs(gap) < 0.01 and abs(self.velocity) < 0.05:
            self.offset, self.velocity, self._catch_up = want, 0.0, False
            self._view_rise = False
            return self.offset
        if gap < 0:
            self._catch_up = False
            self._view_rise = False
        speed = max(speed, 0.1)
        if self.rush and gap < 0 < self.velocity:
            # A flash while still brightening: stop rising at once (the
            # picture doesn't jump, it just turns round without first
            # carrying on up into the glare).
            self.velocity = 0.0
        if gap * self.velocity < 0:
            # Still moving away from a goal that turned round without a
            # flash (a light came up while brightening for the dark): the
            # spring below would carry on that way for a tenth of a second
            # or so, a couple of points past the new goal and back.  Brake
            # evenly instead (no pull toward the goal yet, so no sudden
            # fade back either): the speed eases off to nothing within
            # a few hundredths of a second, with no step and no dead stop,
            # and from rest the usual glide takes over.
            v0 = self.velocity
            stop = abs(v0) / TURN_BRAKE
            if dt < stop:
                v1 = v0 - math.copysign(TURN_BRAKE * dt, v0)
                self.offset += (v0 + v1) / 2 * dt
                self.velocity = v1
                return self.offset
            self.offset += v0 * stop / 2
            self.velocity, dt = 0.0, dt - stop
            gap = want - self.offset
        if gap > 0:
            # A big change (into a cave) catches up quicker, all the way:
            # dropping to the slow pace for the last stretch would leave the
            # picture too dark for seconds.
            if strength and gap > strength * 0.5:
                self._catch_up = True
            tau, vmax = (RISE_FAST_S, MAX_RISE_FAST) if self._catch_up else (RISE_S, MAX_RISE)
            per_update = self._view_rise
            if per_update:
                # Turned into a darker view (see observe): the player needs
                # to see in there now, and a view change hides the fade.
                tau, vmax = RISE_VIEW_S, MAX_RISE_VIEW
        elif self.rush:
            tau = RUSH_VIEW_S if self._rush_view else RUSH_S
            vmax, per_update = MAX_RUSH, False
        else:
            tau, vmax, per_update = FALL_S, MAX_FALL, False
        w = 2.0 * speed / tau
        c1 = -gap                               # offset - want
        velocity = self.velocity
        if velocity * gap > 0 and abs(velocity) > w * abs(gap):
            # Still carrying speed from the speed limit or an earlier goal,
            # more than this spring could stop in time: brake with a spring
            # just stiff enough for that speed.  The glide carries on at its
            # current speed and eases into the goal (c2 becomes 0: a plain
            # exponential approach), instead of stopping dead or sliding past.
            w = abs(velocity) / abs(gap)
        c2 = velocity + w * c1
        e = math.exp(-w * dt)
        new = want + (c1 + c2 * dt) * e
        vel = (c2 - w * (c1 + c2 * dt)) * e
        if per_update:
            # Its cap is per update, so a 60 Hz screen fades at 80 points a
            # second and only faster screens (smaller steps) go quicker; a
            # stalled update can't make a bigger step.  A screen updated
            # only every few steps (a 60 Hz one next to a 144 Hz one) counts
            # its own updates, or it would show steps twice the cap.
            ceiling = CEILING_STEP / max(dt, frame or 1.0 / FADER_MAX_HZ)
        else:
            ceiling = CEILING_RUSH if vmax == MAX_RUSH else CEILING
        limit = min(vmax * speed, ceiling)
        if abs(new - self.offset) > limit * dt:  # speed limit: never a visible jump
            new = self.offset + math.copysign(limit * dt, new - self.offset)
            vel = math.copysign(min(abs(vel), limit), vel)
        if (new - want) * (self.offset - want) < 0:  # never past the goal
            new, vel = want, 0.0
        self.offset, self.velocity = new, vel
        if self.rush and self.offset <= self.want + 0.01:
            self.rush = False
        return self.offset


def fader_rate(refresh_rates) -> int:
    """Fader updates a second for these displays: their fastest refresh
    rate, from FADER_HZ up to FADER_MAX_HZ."""
    return min(FADER_MAX_HZ, max([FADER_HZ] + [int(hz) for hz in refresh_rates if hz]))


class Fader:
    """Moves every display toward where auto levels wants it, at the
    displays' refresh rate (see fader_rate; each display is passed on at its
    own, see set_rates) on its own thread, so fades are smooth and never
    wait on the window.  apply() gets {display id: offset} whenever
    something moved."""

    def __init__(self, controllers: dict[str, Controller],
                 apply: Callable[[dict[str, float]], None], hz: int = FADER_HZ):
        self.controllers = controllers
        self.apply = apply
        self.speed = 1.0
        self.interval = 1.0 / hz     # may be changed while running (set_rate)
        # Per display: its own updates a second (see set_rates).  A display
        # not listed is passed on at the fader's own pace.
        self.rates: dict[str, int] = {}
        self._since: dict[str, float] = {}   # per display: time since it was last passed on
        self._stop = threading.Event()
        self._wake = threading.Event()   # a new goal: stop resting at once
        self._thread: threading.Thread | None = None
        self._ticked_at: float | None = None   # real time the clocks were last moved to
        self.last_error = ""
        self._applied: dict[str, float] = {}
        self._rooms: dict[str, float] = {}     # each display's Outside detail when last applied

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        # Each run gets its own stop signal, so a thread from an earlier run
        # that is still finishing can never be brought back to life.
        # The run also keeps the apply() it started with, so a thread that
        # is still finishing reports through its own (outdated) callback.
        stop = self._stop = threading.Event()
        self.last_error = ""
        self._ticked_at = None   # (an earlier run's last step says nothing about this one)
        self._thread = threading.Thread(target=self._run, args=(stop, self.apply),
                                        name="catvision-auto-fader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def wake(self) -> None:
        """A goal changed: start moving now instead of after the rest pause."""
        self._wake.set()

    def set_rate(self, hz: float) -> None:
        """Updates a second (the displays' refresh rate; see fader_rate)."""
        self.interval = 1.0 / max(1.0, hz)

    def set_rates(self, refresh_rates: dict[str, float]) -> None:
        """Each display's refresh rate ({display id: Hz}).  The fader steps
        at the fastest of them (fader_rate) and every controller moves on
        every step, but each display is passed on only at its own pace
        (fader_rate of its own rate): a 60 Hz screen next to a 144 Hz one
        shows only 60 pictures a second, so 120 updates would cost twice
        the Windows calls for nothing visible."""
        self.rates = {did: fader_rate([hz]) for did, hz in refresh_rates.items()}
        self.set_rate(fader_rate(refresh_rates.values()))

    def now_for(self, ctrl: Controller) -> float:
        """ctrl's clock as it is right now.  The clock moves only when the
        fader steps, which at rest is only every IDLE_S, so on its own it
        can be a whole measurement behind when a picture arrives."""
        at = self._ticked_at
        if at is None or not self.running:
            return ctrl.clock
        return ctrl.clock + max(0.0, min(time.perf_counter() - at, 0.25))

    def settled(self) -> bool:
        """Every display is where it wants to be and has stopped moving."""
        return all(abs(c.want - c.offset) <= 0.001 and abs(c.velocity) < 1e-3
                   and c.room == c.room_want
                   for c in list(self.controllers.values()))

    def tick(self, dt: float, motion_dt: float | None = None, force: bool = False,
             apply: Callable[[dict[str, float]], None] | None = None) -> bool:
        """Move every display one step.  A display's new position is passed
        on only once it has moved FADER_MIN_STEP since it was last passed on,
        at most at its own pace (see set_rates), and always once more when it
        comes to rest, so its exact final value is applied; with force, every
        display's.  apply() gets every display's position as last passed on,
        and is called only when one of them changed (or force).  Returns True
        if apply() was called."""
        motion = dt if motion_dt is None else min(motion_dt, dt)
        # One snapshot: the window may add or remove displays meanwhile.
        items = list(self.controllers.items())
        rates, step = self.rates, self.interval
        last, shown, since_all = self._applied, {}, {}
        for did, ctrl in items:
            if dt > motion:
                ctrl.clock += dt - motion   # time passes even while resting
            # Every controller moves on every step, whatever its display's
            # pace, so the motion is the same on every screen; only the
            # quickest fade's cap per update counts a slower screen's own
            # updates (see CEILING_STEP), so its steps stay as small as on
            # its own.
            pace = 1.0 / rates[did] if did in rates else step
            pos = ctrl.advance(motion, self.speed, pace if pace > step else None)
            prev = last.get(did)
            since = self._since.get(did, 0.0) + dt
            if force or prev is None:
                new, since = True, 0.0
            elif pos == prev:
                new = False
            elif ctrl.want == pos:
                new = True                  # came to rest: its exact final value, at once
            else:
                # A slower screen than the fastest is passed on only every
                # few steps (the nearest step to its own beat).
                due = pace <= step or since >= pace - step / 2
                new = due and abs(pos - prev) >= FADER_MIN_STEP
            if new:
                shown[did] = pos
                if since:
                    # Keep its beat (a 75 Hz screen gets 75 a second, not
                    # 60), but never hurry to catch up after a stall.
                    since = min(since - pace, step / 2)
            else:
                shown[did] = prev
                since = min(since, pace)
            since_all[did] = since
        self._since = since_all
        changed = force or shown != last
        # The Outside detail moves on its own (see Controller.room).
        rooms = {did: (ctrl.room, ctrl.room_want) for did, ctrl in items}
        if not changed:
            for did, (room, room_want) in rooms.items():
                gap = abs(room - self._rooms.get(did, 0.0))
                if gap >= ROOM_STEP or (gap > 0 and room == room_want):
                    changed = True
                    break
        if changed:
            (apply or self.apply)(shown)
            self._applied = shown
            self._rooms = {did: room for did, (room, _want) in rooms.items()}
        return changed

    def _run(self, stop: threading.Event,
             apply: Callable[[dict[str, float]], None] | None = None) -> None:
        last = due = time.perf_counter()
        idle = False
        self._applied, self._since, self._rooms = {}, {}, {}
        while not stop.is_set():
            now = time.perf_counter()
            dt, last = min(now - last, 0.25), now
            # Set before the clocks move (now_for adds the time since this
            # to a clock): a picture arriving in between is stamped one step
            # early at worst, never with that step counted twice.
            self._ticked_at = now
            try:
                # Coming out of a rest, the first step is a normal 60 Hz step,
                # so a fade starts exactly as it would have anyway.  At rest it
                # still refreshes apply() a few times a second (cheap, and a
                # changed preset gets shared out again).
                self.tick(dt, self.interval if idle else None, force=idle, apply=apply)
                idle = self.settled()
            except Exception as exc:  # keep fading; the window shows nothing breaks
                self.last_error = repr(exc)
                idle = False
            if idle:
                # Nothing to move: rest until a new goal (wake()) or a while.
                self._wake.wait(IDLE_S)
                self._wake.clear()
                due = time.perf_counter()
                continue
            # The next step on a steady beat.  time.sleep is precise on
            # Windows (Python 3.11+ uses a high-resolution timer); waiting on
            # an Event snaps to Windows' 15.6 ms clock tick, which would turn
            # 60 updates a second into about 32 (each step twice as big).
            interval = self.interval
            due += interval
            now = time.perf_counter()
            if due < now - interval:   # fell well behind (a stall): restart the beat, no rush to catch up
                due = now
            if due > now:
                time.sleep(due - now)


# --------------------------------------------------------------------------
# Is a game in front?  (only the foreground window's position, style and title are looked at)
# --------------------------------------------------------------------------

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
# Title reported while CatVision's own window is in front (the player is
# changing settings): the game behind it stays the current game.
OWN_WINDOW = "\x00catvision"


_window_api = None


def _window_functions():
    """user32/dwmapi functions with their signatures declared once, on
    private handles (window handles are 64-bit; without argtypes ctypes would
    squeeze them into 32 bits)."""
    global _window_api
    if _window_api is None:
        import ctypes
        from ctypes import wintypes
        hwnd = wintypes.HWND
        u = ctypes.WinDLL("user32")
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = hwnd
        u.GetWindowThreadProcessId.argtypes = [hwnd, ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowThreadProcessId.restype = wintypes.DWORD
        u.GetClassNameW.argtypes = [hwnd, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        for name in ("IsIconic", "IsZoomed"):
            getattr(u, name).argtypes = [hwnd]
            getattr(u, name).restype = wintypes.BOOL
        u.GetWindowLongW.argtypes = [hwnd, ctypes.c_int]
        u.GetWindowLongW.restype = ctypes.c_long
        u.GetWindowRect.argtypes = [hwnd, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.GetWindowTextW.argtypes = [hwnd, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.restype = ctypes.c_int
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = [hwnd, wintypes.DWORD, ctypes.c_void_p,
                                                  wintypes.DWORD]
            dwm.DwmGetWindowAttribute.restype = ctypes.c_long   # HRESULT
        except (OSError, AttributeError):
            dwm = None
        _window_api = (ctypes, wintypes, u, dwm)
    return _window_api


def window_in_front(rects: dict[str, Rect]) -> tuple[set[str] | None, str]:
    """Which displays the window in front fills (a game in fullscreen or
    borderless mode, a full-screen video...), and that window's title, which
    is used to remember each game's look.  Only the window's position, style
    and title are read, never anything inside it.  (None, "") if it can't be
    told (not Windows), meaning "don't hold anything back"; if a Windows call
    fails it holds back instead (set(), "")."""
    if not IS_WINDOWS:
        return None, ""
    try:
        import os
        ctypes, wintypes, u, dwm = _window_functions()
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return set(), ""
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return set(), OWN_WINDOW  # CatVision's own windows (e.g. the test pattern)
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        if cls.value in _SHELL_CLASSES:
            return set(), ""        # the desktop or taskbar
        if u.IsIconic(hwnd):
            return set(), ""        # minimized
        if dwm is not None:
            cloaked = wintypes.DWORD(0)
            if (dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked),   # DWMWA_CLOAKED
                                          ctypes.sizeof(cloaked)) == 0 and cloaked.value):
                return set(), ""    # hidden by Windows (another virtual desktop, a suspended app)
        # A normal maximized window (with a title bar) can cover the whole
        # monitor when the taskbar hides itself; games in fullscreen or
        # borderless mode have no title bar.
        style = u.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF     # GWL_STYLE
        if (style & 0x00C00000) == 0x00C00000 and u.IsZoomed(hwnd):  # WS_CAPTION
            return set(), ""
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return set(), ""
        slack = 2
        filled = {did for did, (left, top, right, bottom) in rects.items()
                  if r.left <= left + slack and r.top <= top + slack
                  and r.right >= right - slack and r.bottom >= bottom - slack}
        if not filled:
            return filled, ""
        title = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(hwnd, title, 256)
        return filled, clean_title(title.value)
    except Exception:
        return set(), ""            # can't tell on Windows: hold back, never adjust everything


def full_screen_displays(rects: dict[str, Rect]) -> set[str] | None:
    """Displays that the window in front fills (see window_in_front)."""
    return window_in_front(rects)[0]


# --------------------------------------------------------------------------
# Screen capture (Windows GDI): a shrunken copy of one display
# --------------------------------------------------------------------------

_gdi = None   # (ctypes, user32, gdi32, BITMAPINFO, BITMAPINFOHEADER), set up once


def _gdi_setup():
    """Declare the GDI functions and structures once (not on every capture)."""
    global _gdi
    if _gdi is not None:
        return _gdi
    import ctypes
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    # Own WinDLL handles, so these declarations never clash with other code
    # that uses ctypes.windll.user32 / gdi32.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    vp = ctypes.c_void_p
    user32.GetDC.restype = vp
    user32.GetDC.argtypes = [vp]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [vp, vp]
    gdi32.CreateCompatibleDC.restype = vp
    gdi32.CreateCompatibleDC.argtypes = [vp]
    gdi32.CreateCompatibleBitmap.restype = vp
    gdi32.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = vp
    gdi32.SelectObject.argtypes = [vp, vp]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.SetStretchBltMode.restype = ctypes.c_int
    gdi32.SetStretchBltMode.argtypes = [vp, ctypes.c_int]
    gdi32.SetBrushOrgEx.restype = wintypes.BOOL
    gdi32.SetBrushOrgEx.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp]
    gdi32.StretchBlt.restype = wintypes.BOOL
    gdi32.StretchBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.DWORD]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [vp, vp, wintypes.UINT, wintypes.UINT, vp, vp, wintypes.UINT]
    _gdi = (ctypes, user32, gdi32, BITMAPINFO, BITMAPINFOHEADER)
    return _gdi


def gdi_capture(rect: Rect, w: int = THUMB_W, h: int = THUMB_H) -> bytes | None:
    ctypes, user32, gdi32, BITMAPINFO, BITMAPINFOHEADER = _gdi_setup()
    left, top, right, bottom = rect
    screen = user32.GetDC(None)
    if not screen:
        return None
    mem = bmp = None
    try:
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        if not mem or not bmp:
            return None
        old = gdi32.SelectObject(mem, bmp)
        gdi32.SetStretchBltMode(mem, 4)          # HALFTONE: averages, so tiny lights don't flicker
        gdi32.SetBrushOrgEx(mem, 0, 0, None)
        ok = gdi32.StretchBlt(mem, 0, 0, w, h, screen, left, top, right - left, bottom - top,
                              0x00CC0020)        # SRCCOPY
        gdi32.SelectObject(mem, old)             # GetDIBits needs the bitmap deselected
        if not ok:
            return None
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = w
        info.bmiHeader.biHeight = -h             # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buf = ctypes.create_string_buffer(w * h * 4)
        lines = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(info), 0)
        return buf.raw if lines == h else None
    finally:
        if mem:
            gdi32.DeleteDC(mem)                  # first, so the bitmap is never still selected
        if bmp:
            gdi32.DeleteObject(bmp)
        user32.ReleaseDC(None, screen)


class Watcher:
    """Measures the chosen displays on a background thread.  The window reads
    the latest results with latest() and does the adjusting itself."""

    def __init__(self, capture: Callable[[Rect], bytes | None] | None = None):
        self.available = capture is not None or IS_WINDOWS
        self.reason = "" if self.available else "Auto levels needs Windows."
        self._capture = capture or gdi_capture
        self._lock = threading.Lock()
        self._rects: dict[str, Rect] = {}
        self._latest: dict[str, Measurement | None] = {}
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.capture_ms = 0.0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def watch(self, rects: dict[str, Rect]) -> None:
        """Measure these displays (starting the thread if needed).  An empty
        set just idles; stop() ends it."""
        with self._lock:
            self._rects = dict(rects)
            self._latest = {k: v for k, v in self._latest.items() if k in rects}
        if self.available and not self.running:
            # A fresh stop signal per run (see Fader.start).
            stop = self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(stop,),
                                            name="catvision-auto-levels", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._latest = {}

    def latest(self) -> tuple[int, dict[str, Measurement | None]]:
        with self._lock:
            return self._seq, dict(self._latest)

    def measure_once(self) -> dict[str, Measurement | None]:
        with self._lock:
            rects = dict(self._rects)
        out: dict[str, Measurement | None] = {}
        start = time.perf_counter()
        for did, rect in rects.items():
            taken = time.perf_counter()
            try:
                pixels = self._capture(rect)
            except Exception:
                pixels = None
            out[did] = replace(measure(pixels, THUMB_W), at=taken) if pixels else None
        self.capture_ms = (time.perf_counter() - start) * 1000
        return out

    def _run(self, stop: threading.Event) -> None:
        due = time.perf_counter()
        before: dict[str, Measurement | None] = {}
        while not stop.is_set():
            results = self.measure_once()
            with self._lock:
                if not stop.is_set():
                    # watch() may have changed the displays while this ran.
                    self._latest = {k: v for k, v in results.items() if k in self._rects}
                    self._seq += 1
            # The next measurement on a steady beat (the capture's own time
            # doesn't add to the gap), sooner while the brightness changes.
            gap = min([capture_interval(before.get(did), m) for did, m in results.items()],
                      default=INTERVAL_S)
            before = results
            due += gap
            now = time.perf_counter()
            if due <= now:      # a slow capture: start the beat again, never back to back
                due = now + gap
            stop.wait(due - now)
