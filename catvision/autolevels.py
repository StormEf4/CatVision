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

from . import curves, dxgicapture
from .winutil import IS_WINDOWS

THUMB_W, THUMB_H = 96, 54       # size of the copy that is measured
INTERVAL_S = 0.2                # five measurements a second...
BURST_S = 0.1                   # ...and ten while the brightness is changing a lot
# A capture (with its measuring, every ticked display) this cheap measures
# twice as often, so a new darker view is followed about a tenth of a
# second sooner.  A GPU copy takes about 1-3 ms and measuring it 2, so ten
# a second is about 5% of one CPU core, and even at this limit less than
# the GDI route's 20-40 ms five times a second; that slow route keeps the
# pace above, so it costs no more CPU than it did.
QUICK_CAPTURE_MS = 8.0
QUICK_LEAVE_MS = 10.0           # (once quick, until a capture takes this long: no flapping;
                                #  ten a second at this is still no more than GDI's 20 ms five)
INTERVAL_QUICK_S = 0.1          # ten measurements a second at rest...
BURST_QUICK_S = 0.05            # ...and twenty while the brightness is changing a lot
# Measuring twice as often would catch twice as many lightning strokes and
# muzzle flashes (under a tenth of a second, gone by the next picture), and
# each one caught dims the picture for a second or more: stormy nights
# would be left too dark.  So at the quick pace (pictures under
# QUICK_GAP_S apart, all of the last SETTLE_S) a picture STEADY_RATIO
# brighter than the last one only counts once a picture at least
# FLASH_CONFIRM_S later still is: then both count, and it dims as before.
# The next picture is taken FLASH_AGAIN_S after a brightening for that.  So
# a flash under about a tenth of a second is caught less often than at the
# usual pace (five a second), and one that lasts (a flashbang, a flare lit
# near you, walking out into daylight) still dims about as soon on average
# and never later (seen within a tenth of a second, then again
# FLASH_AGAIN_S later, against within a fifth plus the slow capture's own
# time).
QUICK_GAP_S = (INTERVAL_S + INTERVAL_QUICK_S) / 2
FLASH_CONFIRM_S = 0.06
FLASH_AGAIN_S = 0.07            # (a little more: without capture times the clock steps by a frame)
# A brighter picture that is a different view all over (see _brighter_view:
# turned toward the sky or a window, stepped out into daylight) is looked at
# again BURST_QUICK_S later instead and counts once seen again this long
# after: a glance at it as short as a flash is caught about as often as at
# the usual pace (half the time for a tenth of a second), and one that lasts
# dims about as soon (within a tenth of a second on average).
VIEW_CONFIRM_S = 0.04
VIEW_CONFIRM_MOVED = 0.9        # (all over: at least this share of the parts changed, see
                                # VIEW_MOVED; lightning lighting one side or the sky of a
                                # night scene, a muzzle flash or a flare light less of it)
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
VIEW_STEADY_S = 0.08            # a new view seen this long (two measurements; three at the
                                # quick pace, see QUICK_CAPTURE_MS) is followed...
VIEW_TURN_S = 2.0               # ...if it came this soon after the settled one (a turn, a walk in)
STRETCH_KEEP = 3                # (the newest pictures of a stretch kept: VIEW_STEADY_S back
                                #  at the quickest capture pace, BURST_QUICK_S)
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

# Cut through haze (beta): fog, smoke, dust or rain lift the darkest parts
# of the view well above black and squeeze every tone together; a levels
# black point (curves.haze_cut) takes part of that veil off.  Only from
# brightness statistics (see haze_for_view), never for a dark view (night
# fog is auto levels' shadow lift's to handle) or a flat one (a white menu,
# open sky, a loading screen, a plain wall), and held low while auto levels
# lifts the view.  It eases in and out on its own, like the room.
HAZE_LOW_SHARE = 0.03           # "the darkest few percent": this share of the view
HAZE_LOW = (0.12, 0.24)         # the darkest few percent this bright: a veil starts / is full
HAZE_LOW_TOP = (0.42, 0.58)     # ...but this bright and more: the whole view is bright
                                #  (a white menu, the open sky, a snow white-out): less, then none
HAZE_SPREAD = (0.48, 0.30)      # tones spread this widely (5th-95th percentile): no veil /
                                #  squeezed by one (a clear desert or sky view spreads ~0.5)
HAZE_FLAT = (0.05, 0.12)        # narrower than this is a flat picture (a menu, a loading
                                #  screen, a wall), not haze: none, full from the second
HAZE_DARK = (0.07, 0.12)        # views darker than this (key) are the shadow lift's: none, full
                                #  from the second (night fog, a dark room)
# A game's view under haze is rarely veiled all over: the HUD, the player's
# own character or a near wall stay dark and the sun or an explosion spread
# the tones, so the whole picture's levels say "clear".  So the veil is also
# measured part by part on the middle rows of the coarse grid (the top row is
# mostly sky, the bottom one the HUD and the ground at the player's feet):
# a part is veiled when its own darkest few percent are well above black and
# its tones are squeezed together, and the view counts as hazy when at least
# HAZE_PART_SHARE of those parts are (one sun, one dark figure don't matter).
# Only each part's brightness levels: nothing is looked for in it.
HAZE_PART_LOW = (0.25, 0.35)    # a part's 5th percentile this bright: veiled from / fully
HAZE_PART_TOP = (0.64, 0.80)    # ...but this bright and more: open sky or a white wall: less, none
HAZE_PART_SPREAD = (0.30, 0.22) # a part's 5th-95th percentile spread: not veiled / fully...
HAZE_PART_FLAT = (0.02, 0.05)   # ...but under the first, a flat part (a loading screen, a
                                #  menu panel): none, full from the second
HAZE_PART_SHARE = 0.5           # this share of the middle parts veiled: a hazy view...
HAZE_PART_DEPTH = (0.04, 0.08)  # ...when their black levels differ this much (none / full):
                                #  haze thickens with distance, so a veiled view's parts are
                                #  veiled unevenly; a wall up close is alike all over
HAZE_PART_KEY_TOP = (0.50, 0.60)  # a view this bright (key) is all bright (open sky, a snow
                                #  field, a white menu with a HUD): less, then none
HAZE_SHARE = 0.6                # take this share of the veil off (the rest stays: it's air)
HAZE_FRAMES = 5                 # the middle of the last this many pictures counts (one smoke
                                #  puff or muzzle flash moves nothing)...
HAZE_GAP_S = INTERVAL_S - INTERVAL_QUICK_S / 2  # ...at the usual pace: pictures closer than
                                #  this (the quick pace, bursts) share a place, the newest
                                #  counting, so the window stays about a second at any pace
HAZE_RISE_S = 0.5               # eases in over about 1.5 s...
HAZE_FALL_S = 0.7               # ...and out over about 2 s
HAZE_STEP = 0.5                 # the fader passes it on when it moved this much (points)...
HAZE_SNAP = 0.5                 # ...and this close it goes straight there
HAZE_DEADBAND = 5.0             # a still view's small changes leave the goal where it is
# Light haze (a snowfall veil, misty distance) leaves the black level alone
# but squeezes much of the middle of the view into a narrow band of mid or
# bright, mostly colourless tones: curves.haze_band steepens the curve
# across that band and takes the room from above it, never darkening
# anything (see band_for_view).  Eased, faded and given way like the cut.
BAND_ROWS = (1, 3)              # the band is looked for in these rows of the grid (not the HUD's)
BAND_SHARE = 0.5                # the band: the narrowest range of levels holding this share...
BAND_GREY = 0.25                # ...of the pixels, those with at most this colour (max-min)/max
BAND_COLOUR_WEIGHT = 0.15       #  counting fully, more colourful ones this much
BAND_WIDTH = (0.34, 0.18)       # a band this wide: no haze / fully squeezed...
BAND_FLAT = (0.03, 0.06)        # ...but narrower than this: one flat surface (a wall), not haze
BAND_LOW = (0.30, 0.45)         # the band's bottom this bright: washed out from / fully
BAND_TOP = (0.80, 0.90)         # ...but this bright: a white menu, snow or sky up close: less, none
BAND_KEY = (0.38, 0.46)         # views darker than this (key) are not washed out: none, full
BAND_KEY_TOP = (0.62, 0.72)     # ...and this bright: all bright (open sky, a white wall): less, 0
BAND_SPREAD = (0.20, 0.30)      # the whole view's 5th-95th percentile spread: flat (sky, a wall) to
                                #  full (a game view's HUD and near objects keep it wider)
BAND_EDGE_SNAP = 0.004          # the band's edges go straight there this close
BAND_EDGE_DEADBAND = 0.03       # ...and follow the view when it moves them this far (levels 0-1)
BAND_DEADBAND = 10.0            # the band's HAZE_DEADBAND: its score is twitchier (a still,
                                #  noisy view swings it by 10 points), each step is small
BAND_STEP = 0.5 / 255           # the fader passes the band on when it moved about this much
                                #  (output levels, see band_moved): half a level
HAZE_LIFT_OFF = 15.0            # auto levels' lift (points of offset) that turns it fully
                                #  off: the dark room's lift comes first

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
    # For "Cut through haze": how bright the darkest few percent are (the
    # weighted HAZE_LOW_SHARE percentile, 0-1: well above black under fog or
    # smoke) and the spread of the tones (weighted 5th to 95th percentile,
    # 0-1: narrow when a veil squeezes them together).  Only brightness.
    lows: float = 0.0
    spread: float = 0.0
    # ...and how veiled the middle of the grid is (0-1: how veiled at least
    # HAZE_PART_SHARE of the middle rows' parts are, see _veiled).  0 if the
    # grid isn't known.
    veiled: float = 0.0
    # ...and the band the middle of the view's tones are squeezed into (input
    # levels 0-1, see BAND_SHARE; 0, 0 if not known).
    band_lo: float = 0.0
    band_hi: float = 0.0
    # "Normal display in menus" (beta): how much the whole picture looks like
    # one of the game's own menu or inventory screens (see menu_score;
    # MENU_SCORE and up).  Only coarse brightness: where the picture has
    # sharp steps row by row and how flat it is; nothing is looked for in it.
    menu: float = 0.0


def _band(bgra: bytes, lum: list[int], width: int, height: int) -> tuple[float, float]:
    """The narrowest range of levels that holds BAND_SHARE of the middle
    rows' pixels (BAND_ROWS), mostly colourless ones counting fully (see
    BAND_GREY): where a veil squeezes the view's tones.  Only brightness and
    how colourful each pixel is."""
    hist = [0.0] * 256
    first, last = (r * height // REGION_ROWS for r in BAND_ROWS)
    a, b = first * width, last * width
    for v, bl, g, r in zip(lum[a:b], bgra[4 * a::4], bgra[4 * a + 1::4], bgra[4 * a + 2::4]):
        top = r if r > g else g
        top = top if top > bl else bl
        low = r if r < g else g
        low = low if low < bl else bl
        hist[v] += 1.0 if top - low <= BAND_GREY * top + 6 else BAND_COLOUR_WEIGHT
    total = sum(hist)
    if total <= 0:
        return 0.0, 0.0
    need = total * BAND_SHARE
    best, lo, seen = (256, 0, 255), 0, 0.0
    for hi in range(256):            # two pointers: the narrowest window holding `need`
        seen += hist[hi]
        while lo < hi and seen - hist[lo] >= need:
            seen -= hist[lo]
            lo += 1
        if seen >= need and hi - lo < best[0]:
            best = (hi - lo, lo, hi)
    return best[1] / 255, best[2] / 255


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


def _veiled(lum: list[int], width: int, height: int) -> float:
    """How veiled the middle rows of the grid are (see Measurement.veiled):
    each part's 5th and 95th percentile brightness, scored by HAZE_PART_LOW,
    HAZE_PART_TOP, HAZE_PART_SPREAD and HAZE_PART_FLAT; the score at least
    HAZE_PART_SHARE of the parts reach, as far as those parts' black levels
    differ (HAZE_PART_DEPTH)."""
    cols = [c * width // REGION_COLS for c in range(REGION_COLS + 1)]
    parts = []
    for r in range(1, REGION_ROWS - 1):
        rows = range(r * height // REGION_ROWS, (r + 1) * height // REGION_ROWS)
        for c in range(REGION_COLS):
            part = sorted(v for y in rows for v in lum[y * width + cols[c]:y * width + cols[c + 1]])
            if not part:
                continue
            n = len(part)
            lo, hi = part[n * 5 // 100] / 255, part[min(n - 1, n * 95 // 100)] / 255
            parts.append((_ramp(lo, *HAZE_PART_LOW) * (1.0 - _ramp(lo, *HAZE_PART_TOP))
                          * _ramp(hi - lo, *HAZE_PART_SPREAD) * _ramp(hi - lo, *HAZE_PART_FLAT),
                          lo))
    if not parts:
        return 0.0
    parts.sort(reverse=True)
    score = parts[max(0, math.ceil(len(parts) * HAZE_PART_SHARE) - 1)][0]
    if score <= 0:
        return 0.0
    lows = [lo for s, lo in parts if s >= score]
    return score * _ramp(max(lows) - min(lows), *HAZE_PART_DEPTH)


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


def _brighter_view(last: Measurement | None, m: Measurement) -> bool:
    """m is a different, brighter view than last (see
    Controller._new_brighter_view) all over (see VIEW_CONFIRM_MOVED:
    lightning lighting one side or the sky of a night scene, a muzzle flash
    or a flare lights only part of it, though its layout changes) and not a
    white-out (lightning whiting the screen out has a layout of its own
    under a HUD)."""
    if _white_out(m) or not Controller._new_brighter_view(last, m):
        return False
    moved = view_moved(last, m)
    return moved is not None and moved >= VIEW_CONFIRM_MOVED


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
    veiled = band_lo = band_hi = menu = 0.0
    if width and n % width == 0:
        weights = _center_weights(width, n // width)
        if width >= REGION_COLS and n // width >= REGION_ROWS:
            regions = _regions(lum, width, n // width)
            veiled = _veiled(lum, width, n // width)
            band_lo, band_hi = _band(bgra, lum, width, n // width)
            menu = menu_score(lum, width, n // width, bgra)
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
    # Weighted percentiles: the darkest few percent, the 5th, the 25th
    # (the shadows) and the 95th.
    low_level, p5, shadow_level, p95 = _levels(hist, total, (HAZE_LOW_SHARE, 0.05, 0.25, 0.95))
    shadows = max(shadow_level, 1) / 255
    key = math.exp((1 - SHADOW_SHARE) * math.log(average) + SHADOW_SHARE * math.log(shadows))
    dark = sum(1 for v in lum if v < 26) / n
    bright = sum(1 for v in lum if v > 230) / n
    signature = (hash(bgra) or 1) if width else 0
    return Measurement(key, average, shadows, dark, bright, max(lum) == 0, signature, regions,
                       highs=highs, lows=low_level / 255, spread=(p95 - p5) / 255,
                       veiled=veiled, band_lo=band_lo, band_hi=band_hi, menu=menu)


# "Normal display in menus" (beta).  The game's menu and inventory screens
# end in a full-width line of small hints just above the bottom edge (keys to
# press) with nothing below it and, where the hints are, nothing just above
# it either; many have a full-width menu bar along the top, and the in-raid
# inventory sits on a dark navy background, often flat.  Gameplay keeps its
# HUD in the bottom corners (bars and text stacked several rows tall) and a
# narrow compass at the top centre, and a busy scene reaches right down to
# the last row.  Only how many of the picture's MENU_SEGMENTS columns have a
# sharp step on a row, how flat coarse blocks are and the picture's average
# colour are measured: nothing in the picture is looked for.
# Tuned on about 350 real menu and gameplay screenshots at the 96x54
# thumbnail size; a picture needs the hint line AND a second, independent
# cue (menu bar, dark flat or dark navy background), so a missed menu is
# preferred to a gameplay picture ever counting.
MENU_EDGE = 40                  # neighbouring pixels this different (0-255) = a sharp step
MENU_SEGMENTS = 8               # the rows are split into this many columns
MENU_LINE = 5                   # a hint line this strong (see menu_score)...
MENU_QUIET = 3                  # ...with this many of its columns clear above and below...
MENU_SCORE = 6.0                # ...and a score this high (see menu_score) = a menu
MENU_BAR = 2                    # a menu bar counts beyond this (see menu_score)
MENU_DARK_FLAT = 2.0            # what a dark, flat picture adds to the score
MENU_NAVY = 1.0                 # what a dark navy picture adds (if not flat)
MENU_NAVY_TINT = 3.0            # (navy: average blue above average red by this, 0-255)
MENU_FLAT = 0.25                # this share of flat blocks (a menu's plain background)...
MENU_FLAT_STD = 6.0             # (a block flat: brightness spread under this, 0-255)
MENU_DARK = 60                  # ...on a dark picture (average under this, 0-255)
MENU_ON_S = 0.3                 # a menu looks like one this long (MENU_ON_COUNT pictures)...
MENU_ON_COUNT = 3
MENU_OFF_S = 0.2                # ...and gameplay like gameplay this long before switching back
MENU_OFF_COUNT = 2
MENU_FADE_S = 0.2               # the display glides to normal and back in this long
MENU_LOST_S = 1.0               # no picture this long in a menu: back to the full look


def _row_steps(lum: list[int], width: int, y: int) -> set[int]:
    """Which of the MENU_SEGMENTS columns have a sharp step on row y."""
    row = lum[y * width:(y + 1) * width]
    seg = max(1, width // MENU_SEGMENTS)
    return {x // seg for x in range(width - 1) if abs(row[x + 1] - row[x]) > MENU_EDGE}


def _menu_lines(lum: list[int], width: int, height: int) -> tuple[int, int, int]:
    """(bottom line, quiet columns, top bar) of a picture, see menu_score."""
    if width < MENU_SEGMENTS * 2 or height < 8:
        return 0, 0, 0
    top0, top1, top2, above1, above2, hint1, hint2, last = (
        _row_steps(lum, width, y) for y in (0, 1, 2, height - 6, height - 5,
                                             height - 3, height - 2, height - 1))
    bottom = len(hint1) + len(hint2) - 2 * len(last)
    quiet = len((hint1 | hint2) - last - above1 - above2)
    return bottom, quiet, len(top1) - max(len(top0), len(top2))


def _tint(bgra: bytes | None) -> float:
    """Average blue less average red of a BGRA picture (0 without one)."""
    n = len(bgra) // 4 if bgra else 0
    return (sum(bgra[0::4]) - sum(bgra[2::4])) / n if n else 0.0


def menu_parts(lum: list[int], width: int, height: int,
               bgra: bytes | None = None) -> tuple[int, int, int, float, float, float]:
    """(bottom line, quiet columns, top bar, flat share, average brightness,
    blue tint) of a picture, see menu_score."""
    bottom, quiet, top = _menu_lines(lum, width, height)
    if width < MENU_SEGMENTS * 2 or height < 8:
        return bottom, quiet, top, 0.0, 0.0, 0.0
    return (bottom, quiet, top, _flat_share(lum, width, height), sum(lum) / len(lum),
            _tint(bgra))


def _flat_share(lum: list[int], width: int, height: int) -> float:
    """Share of an 8 x 6 grid of blocks whose brightness hardly varies."""
    bw, bh = width // 8, height // 6
    flat = 0
    for by in range(6):
        for bx in range(8):
            vals = [lum[(by * bh + y) * width + bx * bw + x] for y in range(bh) for x in range(bw)]
            mean = sum(vals) / len(vals)
            if sum((v - mean) ** 2 for v in vals) / len(vals) < MENU_FLAT_STD ** 2:
                flat += 1
    return flat / 48


def menu_score(lum: list[int], width: int, height: int, bgra: bytes | None = None) -> float:
    """How much a picture looks like one of the game's own menu screens.

    The bottom hint line: columns with a sharp step on the third and on the
    second row from the bottom, less twice those on the last row (small text
    is two rows tall here, and a busy scene reaches the last row too).  It
    must be at least MENU_LINE, and at least MENU_QUIET of its columns must
    have no step on the last row nor on the two rows just above the hints
    (the HUD's bars and text in gameplay are stacked several rows tall).
    Then a second, independent cue is needed: how far a thin line along the
    top beats the rows beside it beyond MENU_BAR (a menu bar), or
    MENU_DARK_FLAT for a dark, flat picture, or else MENU_NAVY for a dark
    navy one (the inventory).  Score = hint line + second cue, 0 without
    both.  MENU_SCORE and up = a menu."""
    bottom, quiet, top = _menu_lines(lum, width, height)
    if bottom < MENU_LINE or quiet < MENU_QUIET:
        return 0.0     # (most pictures: the rest isn't worked out)
    second = float(max(0, top - MENU_BAR))
    if sum(lum) / len(lum) < MENU_DARK:
        if _flat_share(lum, width, height) >= MENU_FLAT:
            second += MENU_DARK_FLAT
        elif _tint(bgra) >= MENU_NAVY_TINT:
            second += MENU_NAVY
    return float(bottom) + second if second > 0 else 0.0


class MenuWatch:
    """Whether the game is showing one of its menus, from the pictures'
    menu scores, with a delay each way so a single picture never switches:
    a menu must look like one for MENU_ON_S (and MENU_ON_COUNT pictures),
    gameplay like gameplay for MENU_OFF_S (and MENU_OFF_COUNT).  A black
    picture (loading) counts as gameplay: auto levels' black hold keeps the
    look there.  A missing picture changes nothing for a moment, but no
    picture for MENU_LOST_S (the capture failing, say as a raid starts in
    exclusive full screen) ends menu mode: when in doubt, the full look."""

    def __init__(self):
        self.active = False
        self.looks = False      # the newest picture looks like a menu
        self._since: float | None = None
        self._count = 0
        self._seen: float | None = None   # when the last picture came

    def reset(self) -> None:
        self.active, self.looks, self._since, self._count = False, False, None, 0
        self._seen: float | None = None

    def update(self, m: Measurement | None, now: float) -> bool:
        if m is None:
            if self.active and self._seen is not None and now - self._seen >= MENU_LOST_S:
                self.reset()   # can't see the game any more: back to the full look
            return self.active
        self._seen = now
        looks = self.looks = not m.black and m.menu >= MENU_SCORE
        if looks == self.active:
            self._since, self._count = None, 0
            return self.active
        if self._since is None:
            self._since = now
        self._count += 1
        wait, count = (MENU_ON_S, MENU_ON_COUNT) if looks else (MENU_OFF_S, MENU_OFF_COUNT)
        if self._count >= count and now - self._since >= wait - 1e-9:
            self.active, self._since, self._count = looks, None, 0
        return self.active


def _levels(hist: list[float], total: float, shares: tuple) -> list[int]:
    """The levels below which these shares (rising) of the weighted
    histogram lie: one walk up the histogram."""
    out, seen, i = [], 0.0, 0
    for level, w in enumerate(hist):
        seen += w
        while i < len(shares) and seen >= total * shares[i]:
            out.append(level)
            i += 1
        if i == len(shares):
            break
    return out + [255] * (len(shares) - len(out))


def _changed(a: Measurement | None, b: Measurement | None) -> bool:
    return (a is not None and b is not None and not a.black and not b.black
            and max(a.key, b.key) > min(a.key, b.key) * STEADY_RATIO)


def quick_pace(cost_ms: float, quick: bool) -> bool:
    """Whether a capture (and its measuring) taking ``cost_ms`` measures at
    the quick pace (see QUICK_CAPTURE_MS), given whether the last one did:
    once quick, it stays quick up to QUICK_LEAVE_MS."""
    return cost_ms <= (QUICK_LEAVE_MS if quick else QUICK_CAPTURE_MS)


def capture_interval(prev: Measurement | None, m: Measurement | None,
                     cost_ms: float | None = None, older: Measurement | None = None) -> float:
    """Seconds until the next measurement: BURST_S right after the
    brightness changed a lot (so a new view is confirmed, and followed, a
    tenth of a second sooner), else INTERVAL_S.  ``cost_ms``: what the last
    capture (and its measuring) took; when that was at most
    QUICK_CAPTURE_MS, the quick pace (INTERVAL_QUICK_S, BURST_QUICK_S),
    which keeps bursting one picture longer (``older``: the picture before
    prev), so a new view is still seen VIEW_STEADY_S after it first was a
    tenth of a second later, not at the next picture at rest.  Right after
    a brightening, the quick pace looks again FLASH_AGAIN_S later instead
    (a flash then counts only if it is still there: see FLASH_CONFIRM_S),
    or BURST_QUICK_S later for a different view (see VIEW_CONFIRM_S)."""
    if cost_ms is not None and cost_ms <= QUICK_CAPTURE_MS:
        if _changed(prev, m) and m.key > prev.key:
            # (A different view sooner: see VIEW_CONFIRM_S.)
            return BURST_QUICK_S if _brighter_view(prev, m) else FLASH_AGAIN_S
        return BURST_QUICK_S if _changed(prev, m) or _changed(older, prev) else INTERVAL_QUICK_S
    return BURST_S if _changed(prev, m) else INTERVAL_S


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


def main_setting(s: curves.Settings) -> str:
    """The slider auto levels (and Peek) moves first: whichever of Shadow
    boost / Exposure the preset leans on more (Exposure for night vision,
    Shadow boost otherwise)."""
    return "exposure" if s.exposure > s.shadow_boost else "shadow_boost"


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


def _ramp(x: float, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi (either way round), straight between."""
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))


def haze_for_view(m: Measurement) -> float:
    """How much to cut through haze (curves.haze_cut points, 0-100) for one
    picture: a lifted black level (the darkest few percent well above black)
    and a squeezed tone range, over the whole view or in most of the middle
    of it (Measurement.veiled), in a view that is neither dark (night fog:
    the shadow lift's) nor flat or all bright (a white menu, the open sky).
    Enough to take HAZE_SHARE of the veil off, as far as curves.HAZE_FLOOR_MAX
    allows, and never more than HAZE_SHARE of the whole view's darkest few
    percent: what stays dark under the haze (the HUD, a figure close by)
    is never cut to black."""
    if m.black or not m.spread:
        return 0.0
    veil = max(_ramp(m.lows, *HAZE_LOW) * _ramp(m.spread, *HAZE_SPREAD),
               m.veiled * (1.0 - _ramp(m.key, *HAZE_PART_KEY_TOP)))
    score = (veil * (1.0 - _ramp(m.lows, *HAZE_LOW_TOP)) * _ramp(m.spread, *HAZE_FLAT)
             * _ramp(m.key, *HAZE_DARK))
    if score <= 0:
        return 0.0
    return 100.0 * score * min(1.0, HAZE_SHARE * m.lows / curves.HAZE_FLOOR_MAX)


def band_moved(a: tuple, b: tuple) -> float:
    """About how far (output levels 0-1, at most) the haze band's bump
    moves between two (amount, lo, hi): its height (gain x width / 2, see
    curves.haze_band_shape) and its edges (its steepest slope is the gain).
    So a drifting veil rewrites the table when the picture would show it,
    not 0.5 points of amount at a time (a fraction of a level)."""
    ga, gb = (curves.HAZE_BAND_GAIN * min(max(v[0], 0.0), 100.0) / 100.0 for v in (a, b))
    return (abs(ga * (a[2] - a[1]) - gb * (b[2] - b[1])) / 2.0
            + max(ga, gb) * max(abs(a[1] - b[1]), abs(a[2] - b[2])))


def band_for_view(m: Measurement) -> float:
    """How much more contrast to give the band of hazy tones (curves.haze_band
    points, 0-100) for one picture: how squeezed the middle of the view's
    tones are (Measurement.band_lo..band_hi, BAND_WIDTH) and how washed out
    (BAND_LOW), smoothly from light to heavy haze, in a view that is
    neither dark (night: the shadow lift's), nor flat or all bright (a white
    menu, the open sky, a wall, a loading screen)."""
    if m.black or not m.spread or m.band_hi <= m.band_lo:
        return 0.0
    width = m.band_hi - m.band_lo
    score = (_ramp(width, *BAND_WIDTH) * _ramp(width, *BAND_FLAT) * _ramp(m.band_lo, *BAND_LOW)
             * (1.0 - _ramp(m.band_lo, *BAND_TOP)) * _ramp(m.key, *BAND_KEY)
             * (1.0 - _ramp(m.key, *BAND_KEY_TOP)) * _ramp(m.spread, *BAND_SPREAD))
    return 100.0 * score


def fader_amounts(key: str, offset: float, base: dict[str, float],
                  controller: "Controller | None" = None,
                  standalone: bool = False) -> dict[str, float]:
    """What one display gets on top of the preset: the offset shared out by
    split_offset, plus the controller's Outside detail for bright areas in
    view (see room_for_highs), as much as leaves the room's tones alone
    (ROOM_KEEP_LEVEL).  ``base``: the look's settings (every field, so the
    room's tones can be worked out; at least the ones split_offset moves)."""
    give = 1.0
    if controller is not None:
        # The haze cut and band give way to the lift (a dark room's comes
        # first); in auto mode only to its lift past the daylight one, over
        # the same share of its wider range.
        lo, off = ((auto_day_lift(controller.strength), HAZE_LIFT_OFF * AUTO_LIFT_PER_STRENGTH)
                   if standalone else (0.0, HAZE_LIFT_OFF))
        give = 1.0 - _ramp(offset, lo, lo + off)
    # (Auto mode, ``standalone``: auto_amounts on the neutral look.)
    if standalone:
        out = auto_amounts(_daylight_under_haze(offset, controller, give), base)
    else:
        out = split_offset(key, offset, base)
    if controller is not None and controller.room > 0:
        extra = min(controller.room, _room_left(base, out))
        if extra > 0:
            out["highlight_room"] = extra
    if controller is not None and controller.haze > 0:
        # (After the room: it never changes the room's tones' lift.)
        haze = controller.haze * give
        if haze > 0:
            out["haze_cut"] = haze
    if controller is not None and controller.band > 0:
        # The band gives way to the lift the same way.
        band = controller.band * give
        if band > 0:
            out["haze_band"] = band
            out["haze_band_lo"], out["haze_band_hi"] = controller.band_lo, controller.band_hi
    return out


def _daylight_under_haze(offset: float, controller: "Controller | None",
                         give: float) -> float:
    """Auto mode: the offset with its daylight lift (auto_day_lift) given
    up while the haze cut or band works (``give``: how much of them is
    left, see fader_amounts; AUTO_DAY_HAZE): both brighten a bright view,
    and stacked they push its highlights to white.  So a hazy daylight view
    is toned down a little instead, as before the daylight lift (with "tone
    down" ticked).  A dark view's lift stays (the haze gives way to it)."""
    if controller is None:
        return offset
    veil = _ramp(max(controller.haze, controller.band) * give, 0.0, AUTO_DAY_HAZE)
    if veil <= 0:
        return offset
    dim = controller._settings[2] if controller._settings else True
    return max(offset - auto_day_lift(controller.strength) * veil,
               min(offset, 0.0) if not dim else -controller.strength)


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


# Auto mode (beta): auto levels as a mode of its own.  No preset underneath:
# it starts from a neutral look (AUTO_BASE: no boost, no tint, contrast 1,
# gamma 1) and moves the settings itself.  The controller still works in
# "points" (so the anti-pumping and fade machinery is the same), but here
# they mean: above 0, Shadow boost (spilling into Exposure past 100: only
# pitch-dark scenes get there); below 0, toning a bright scene down
# (auto_amounts).  How much caps the lift at AUTO_LIFT_PER_STRENGTH times
# its value (30 -> 120 points, 60 -> 240), since there is no preset's own
# boost underneath any more.
AUTO_BASE = curves.Settings()
AUTO_NAME = "Auto (beta)"
AUTO_LIFT_PER_STRENGTH = 4.0
# A scene as dark as Aim for or darker is lifted until it is SHOWN at about
# AUTO_SHOWN times Aim for (inverting the tone curve: the lift that shows its
# measured brightness there, so a night raid is shown at about a third of
# full brightness, as Dark Areas plus auto levels did).  Brighter scenes get
# less, none from AUTO_RAMP_STOPS above Aim for, and only scenes brighter
# than that are toned down.
AUTO_SHOWN = 3.2
AUTO_RAMP_STOPS = 1.5
# Daylight keeps a moderate lift: AUTO_DAY_SHARE of the cap (Shadow boost
# 30 at How much 30), for the shadows of a bright view (doorways, under
# cover, bushes): Shadow boost lifts the dark tones most and barely moves
# the highlights, which Outside detail keeps from glaring.  It fades out
# over AUTO_DAY_STOPS more (an all-bright or washed-out view: snow, sky,
# haze), and only views brighter still are toned down.
AUTO_DAY_SHARE = 0.25
AUTO_DAY_STOPS = 1.0
AUTO_DAY_HAZE = 20.0            # haze cut or band points that take the daylight lift off fully
# Toning a bright scene down: each point takes this much off Brightness
# (overall gain), down to AUTO_TONE_FLOOR, with just enough Shadow boost to
# keep the slope at black where it was: the glare (highlights) comes down,
# the dark tones keep their level, nothing is crushed or clipped.
AUTO_TONE_PER_POINT = 0.003
AUTO_TONE_FLOOR = 0.75
# Auto mode's fades (see follow).  Its points span AUTO_LIFT_PER_STRENGTH
# times a preset's range, so its speed limits (points a second) are that
# much higher: a switch across the range takes as long as before.  Each
# step is still capped at what a preset's fade step showed, in lightness on
# screen: the ceilings are in the presets' points, worth up to
# AUTO_PACE_POINT L* each (Dark Areas, the everyday preset: its contrast
# makes a point count most), and auto_pace turns them into auto mode's
# points (worth less: about 0.7 L* lifting, under 0.3 toning down).
AUTO_PACE_POINT = 0.95
AUTO_PACE_MAX = 4.0             # (at most this many auto points per preset point)
# Right after the view itself changed all over (turned or ran into a new
# darker view, or out into a brighter one: see _new_darker_view and
# _brighter_view), the change on screen hides the fade's steps, so for
# AUTO_CUT_S they may be AUTO_CUT_STEP times as large (auto mode's day and
# night looks are further apart than a preset's).  Lights, flashes,
# lightning, a white-out fading back and coming back from a glance keep
# the usual steps.
AUTO_CUT_S = 0.5
AUTO_CUT_STEP = 1.5
AUTO_CUT_EASE = 2.0             # (and the glide eases into its goal this much quicker)
# Auto mode's neutral look has no Outside detail of its own, where a
# preset's (presets.HIGHLIGHT_ROOM, as much as ROOM_EXTRA) lies under auto
# levels' extra: so its extra for bright areas is this much larger (still
# never reaching the room's tones, see _room_left), or a window seen from
# a dark room at night would show brighter than with Dark Areas.
AUTO_ROOM = 2.0
_AUTO_LIFTS: dict[tuple, float] = {}
_AUTO_PACES: dict[int, float] = {}


def auto_cap(strength: float) -> float:
    """Auto mode: the most points the controller may move for How much."""
    return max(0.0, strength) * AUTO_LIFT_PER_STRENGTH


def _boost_for_slope(slope: float) -> float:
    """The Shadow boost whose slope at black (a / log1p(a)) is ``slope``."""
    if slope <= 1.0:
        return 0.0
    lo, hi = 0.0, 200.0
    for _ in range(40):
        a = (lo + hi) / 2
        if a / math.log1p(a) < slope:
            lo = a
        else:
            hi = a
    a = (lo + hi) / 2
    return 100.0 * math.log1p(a) / math.log(201.0)


def auto_amounts(offset: float, base: dict[str, float] | None = None) -> dict[str, float]:
    """Auto mode: what ``offset`` points add to the neutral look: a lift
    (Shadow boost, spilling into Exposure) above 0, a gentle tone-down
    (Brightness down, its dark tones kept by a little Shadow boost) below."""
    base = look_values(AUTO_BASE) if base is None else base
    if offset >= 0:
        return split_offset("shadow_boost", offset, base)
    gain = max(AUTO_TONE_FLOOR, 1.0 - AUTO_TONE_PER_POINT * -offset)
    out = {"brightness": gain - 1.0}
    boost = _boost_for_slope(1.0 / gain)
    if boost > 0.05:
        out["shadow_boost"] = boost
    return out


def auto_look(offset: float) -> curves.Settings:
    """The neutral look with ``offset`` points of auto mode applied."""
    base = look_values(AUTO_BASE)
    return AUTO_BASE.clamped({k: base[k] + v for k, v in auto_amounts(offset, base).items()})


def _lightness(y: float) -> float:
    """CIE L* (0-100) of a display output level 0-1 (display gamma 2.2)."""
    lum = max(y, 0.0) ** 2.2
    return 116.0 * lum ** (1 / 3) - 16.0 if lum > 0.008856 else 903.3 * lum


def auto_pace(offset: float) -> float:
    """Auto mode: how many of its points change the picture as much as a
    preset's point (AUTO_PACE_POINT L* at any level), around ``offset``:
    from the largest change of any of the 256 levels over one point there
    (worked out once per point, then kept: a fraction of a millisecond on
    the fader thread, the first time a fade passes there)."""
    i = int(math.floor(offset + 0.5))
    pace = _AUTO_PACES.get(i)
    if pace is None:
        # (curves._tone_table: tone_curve at the 256 levels, the fast way.)
        worst = max(abs(_lightness(a) - _lightness(b))
                    for a, b in zip(curves._tone_table(auto_look(i - 0.5)),
                                    curves._tone_table(auto_look(i + 0.5))))
        pace = min(AUTO_PACE_MAX, max(1.0, AUTO_PACE_POINT / max(worst, 1e-6)))
        _AUTO_PACES[i] = pace
    return pace


def shown_key(key: float, offset: float) -> float:
    """How bright a scene of brightness ``key`` is shown with ``offset``."""
    return curves.tone_curve(key, auto_look(offset))


def _lift_to_show(key: float, goal: float, cap: float) -> float:
    """The smallest lift (points, at most ``cap``) that shows ``key`` at
    ``goal``: the tone curve inverted (it rises with the lift)."""
    ck = (round(key, 5), round(goal, 4), round(cap, 3))
    lift = _AUTO_LIFTS.get(ck)
    if lift is not None:
        return lift
    if cap <= 0 or shown_key(key, 0.0) >= goal:
        lift = 0.0
    elif shown_key(key, cap) <= goal:
        lift = cap
    else:
        lo, hi = 0.0, cap
        for _ in range(24):
            mid = (lo + hi) / 2
            if shown_key(key, mid) < goal:
                lo = mid
            else:
                hi = mid
        lift = (lo + hi) / 2
    if len(_AUTO_LIFTS) >= 4096:
        _AUTO_LIFTS.clear()
    _AUTO_LIFTS[ck] = lift
    return lift


def auto_day_lift(strength: float) -> float:
    """Auto mode: the daylight lift (points) for ``strength`` (auto_cap of
    How much), see AUTO_DAY_SHARE."""
    return max(0.0, strength) * AUTO_DAY_SHARE


def auto_offset(key: float, strength: float, target: float, dim: bool = True) -> float:
    """Auto mode's goal (points, like target_offset; ``strength`` is already
    auto_cap of How much): a scene darker than Aim for is lifted so it is
    shown at about AUTO_SHOWN x Aim for (in full from AUTO_RAMP_STOPS below
    it), a brighter one keeps the daylight lift (auto_day_lift, fading out
    over AUTO_DAY_STOPS), and only one brighter still is toned down (only
    if dim) like target_offset."""
    key = max(key, 1 / 255)
    stops = math.log2(target / key) + AUTO_RAMP_STOPS
    day = auto_day_lift(strength)
    if stops > 0:
        full = _lift_to_show(key, target * AUTO_SHOWN, strength)
        return max(day, full * min(1.0, stops / AUTO_RAMP_STOPS))
    stops += AUTO_DAY_STOPS
    if stops > 0:
        return day * stops / AUTO_DAY_STOPS
    if not dim:
        return 0.0
    return strength * max(-1.0, stops / STOPS_FOR_FULL)


def aim_for_auto_look(key: float, offset: float, strength: float,
                      current: float | None = None, dim: bool = True) -> float:
    """aim_for_current_look for auto mode (``strength``: auto_cap of How
    much): the Aim for (searched: the goal rises with it) giving this look."""
    if current is not None and abs(auto_offset(key, strength, current, dim) - offset) < 0.5:
        return current
    if strength <= 0:
        return key
    lo, hi = 1e-3, 1.0
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        if auto_offset(key, strength, mid, dim) < offset:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


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
    brightness changes a lot, twice that when capturing is cheap: see
    QUICK_CAPTURE_MS) and sets the goal; advance() moves toward it
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
    _settled_before: Measurement | None = None     # (and the picture before its newest)
    _away: Measurement | None = None               # the brightest view looked at since (2+ pictures)
    _quick_at: float = -ALTERNATE_S                # when the last view followed early began
    _alternate_until: float = 0.0                  # turning back and forth: no early start till then
    _view_rise: bool = False                       # brightening for a new darker view: quickest pace
    _estimate: bool = False                        # the goal is a halfway guess (into a cave)
    _rush_view: bool = False                       # the fast fade-down is for a new, brighter view
    _cut_at: float = -1e9                          # auto mode: when the view last changed all over
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
    haze_on: bool = True                           # "Cut through haze (beta)" is ticked
    haze: float = 0.0                              # haze cut now (curves.haze_cut points)...
    haze_want: float = 0.0                         # ...and where it is heading
    _hazes: list = field(default_factory=list)    # haze_for_view of the last HAZE_FRAMES places
    band: float = 0.0                              # the haze band now (curves.haze_band points)...
    band_want: float = 0.0                         # ...where it is heading
    band_lo: float = 0.0                           # ...its input levels now...
    band_hi: float = 0.0
    band_edges: tuple = (0.0, 0.0)                 # ...and where they are heading
    standalone: bool = False                       # auto mode: goals from auto_offset
    _seen: list = field(default_factory=list)     # (time, Measurement): every picture of the last SETTLE_S
    _unconfirmed: tuple | None = None              # (time, Measurement): a brighter picture not yet counted
    _unconfirmed_view: bool = False                # (and it is a different view: see VIEW_CONFIRM_S)

    def _goal(self, key: float, strength: float, target: float, dim: bool) -> float:
        """target_offset, or auto_offset in auto mode (``standalone``)."""
        if self.standalone:
            return auto_offset(key, strength, target, dim)
        return target_offset(key, strength, target, dim)

    def observe(self, m: Measurement | None, strength: float, target: float,
                dim: bool = True, now: float | None = None) -> None:
        """``now``: the clock as it is right now (Fader.now_for); by default
        the clock as of the fader's last step."""
        self.strength = strength
        clock = self.clock if now is None else max(now, self.clock)
        # A lower How much or an unticked "tone down" brings the goal inside
        # its new limits at once, even while a black or lost picture holds.
        self.want = min(strength, max(-strength if dim else 0.0, self.want))
        if m is not None and not m.black and self._not_yet(clock, m):
            return      # a flash at the quick pace: see FLASH_CONFIRM_S
        if m is not None:
            # (A lost picture keeps it; a black one has no bright areas.)
            self._highs = m.highs
            self._aim_room(strength)
            self._room_back = False
        if m is not None and m.black:
            # Loading screen, or capture blocked: hold the look.  A black
            # screen is pixel-identical every time, so it must not count as
            # a frozen picture either; only a very long one eases back.
            self._missing_since = None
            self._signature, self._same_since = m.signature, clock
            self.frozen = False
            self._stretch = []
            self._aim_haze(m, clock)     # (none for a black picture)
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
                # (A frozen picture's haze is no guide.)
                self.haze_want, self.band_want, self._hazes = 0.0, 0.0, []
            return
        self._missing_since = None
        self._aim_haze(m, clock)       # (a held-back flash never gets here)
        held, self._unconfirmed = self._unconfirmed, None
        if held is not None:
            # Seen again (see FLASH_CONFIRM_S): the first picture counts too,
            # when it came, then this one.
            self._count(held[0], held[1], strength, target, dim)
        self._count(clock, m, strength, target, dim)

    def _not_yet(self, now: float, m: Measurement) -> bool:
        """At the quick capture pace, m is much brighter than the last
        picture counted and not yet seen again FLASH_CONFIRM_S later (a
        lightning stroke, a muzzle flash): hold it back, as if not seen.
        The first one is kept to count once it is seen again; one gone by
        the next picture is dropped."""
        # (How far apart by when they were captured: see _apart.)
        entry = (now, m)
        seen = self._seen = [e for e in self._seen if now - e[0] < SETTLE_S]
        quick = len(seen) >= 2 and all(_apart(a, b) < QUICK_GAP_S
                                       for a, b in zip(seen, seen[1:] + [entry]))
        seen.append(entry)
        if not self._last_key or m.key <= self._last_key * STEADY_RATIO:
            self._unconfirmed = None
            return False
        if self._unconfirmed is None or m.key * STEADY_RATIO < self._unconfirmed[1].key:
            # (Already much dimmer than the flash, a lightning stroke's
            # afterglow: it starts again from this one.)
            if not quick and self._unconfirmed is None:
                return False
            self._unconfirmed = entry
            # A different view (turned toward the sky or a window, stepped
            # out into daylight) is confirmed by the next picture, taken
            # BURST_QUICK_S later (see capture_interval): a glance at it as
            # short as a flash is still caught no more often than at the
            # usual pace, and one that lasts dims as soon.  Lightning
            # lights the same view, or only part of it (see
            # VIEW_CONFIRM_MOVED),
            # or whites it out, and still waits.
            # (Against the last picture counted, as capture_interval does.)
            self._unconfirmed_view = _brighter_view(
                self._stretch[-1][1] if self._stretch else None, m)
            return True
        wait = VIEW_CONFIRM_S if self._unconfirmed_view else FLASH_CONFIRM_S
        return _apart(self._unconfirmed, entry) < wait

    def _count(self, now: float, m: Measurement, strength: float, target: float,
               dim: bool) -> None:
        """observe() for a picture that counts (seen, not black, not lost)."""
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
        newest = self._darkening()
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
        new_dark = view_key
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
        want = self._goal(key, strength, target, dim)
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
        near = self._goal(key + NOISE_KEY, strength, target, dim)
        far = self._goal(max(key - NOISE_KEY, 0.0), strength, target, dim)
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
                # (Bigger steps only for a view that changed all over.)
                self._cut_at = now if new_dark is not None and early == new_dark else -1e9
            self.want = want
            rush = (flash or brighter or leaving) and want < self.offset
            # A new view (or a fade for one that keeps getting brighter)
            # fades at the speed limit from the start: see RUSH_VIEW_S.  Set
            # before rush, so the fader thread never pairs a new fast fade
            # with the pace of an old one.
            self._rush_view = rush and (new_view or (brighter and self._rush_view))
            if rush and new_view and not brighter:
                self._cut_at = now
            self.rush = rush
            self._estimate = halfway

    def _cut_open(self) -> bool:
        """Auto mode: a fade for a view that changed all over, within
        AUTO_CUT_S of the change (see AUTO_CUT_STEP)."""
        return (self.standalone and self.clock - self._cut_at <= AUTO_CUT_S
                and (self._view_rise or (self.rush and self._rush_view)))

    def _darkening(self) -> list:
        """The newest DARKENING_COUNT brightnesses (oldest first), at the
        usual pace: at the quick capture pace (see QUICK_CAPTURE_MS) only
        every other picture counts, so a light fading off over a fifth of a
        second is no more "darker and darker" than it was.  (Every other
        one even when the change is gentle: pictures a fifth of a second
        apart, checked ten times a second, start brightening while a light
        swelling and fading every second or two fades, at night or in
        drifting fog, and the picture pumps.)"""
        keys, last = [], None
        for t, k in reversed(self._recent):
            if last is None or last - t >= BURST_S - BURST_QUICK_S / 2:
                keys.append(k)
                last = t
                if len(keys) == DARKENING_COUNT:
                    break
        return keys[::-1]

    def _track_stretch(self, now: float, m: Measurement) -> None:
        """Keep track of how long the brightness has held: pictures in a row
        within STEADY_RATIO of each other (the camera may move) are one
        stretch, and one that lasted VIEW_SETTLED_S is settled."""
        stretch = self._stretch
        prev = stretch[-1][1] if stretch else None
        if prev is not None and max(prev.key, m.key) <= min(prev.key, m.key) * STEADY_RATIO:
            stretch.append((now, m))
            # Only its start and newest few matter (the newest picture at
            # least VIEW_STEADY_S before this one, at the quickest capture
            # pace too: see _steady_before).
            del stretch[1:-STRETCH_KEEP]
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
            self._settled_before = stretch[-2][1]

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
        earlier = self._steady_before(now, m)
        if (since >= stretch[0][0] or earlier is None
                or stretch[0][0] - until > VIEW_TURN_S or self._jump_at > until):
            return None
        prev = earlier[1]
        key = max(prev.key, m.key)
        alike = view_spread(prev, m)
        if alike is None or alike >= VIEW_SPREAD:
            return None
        # The settled view's newest picture, or the one before it: at the
        # quick capture pace (see QUICK_CAPTURE_MS) the newest may already
        # be the first moment of a quick fade into the dark (still within
        # STEADY_RATIO, so part of the stretch), half the new view already.
        for seen in (seen, self._settled_before):
            if seen is not None and seen.key > key * STEADY_RATIO and _other_view(seen, m):
                return key
        return None

    def _steady_before(self, now: float, m: Measurement) -> tuple | None:
        """The newest earlier picture of the current stretch (m is its
        newest) at least VIEW_STEADY_S before m: two measurements apart at
        the usual pace, three at the quick one (see QUICK_CAPTURE_MS), so a
        new view is seen for as long either way.  None if there is none."""
        for entry in reversed(self._stretch[:-1]):
            if _apart(entry, (now, m)) >= VIEW_STEADY_S:
                return entry
        return None

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
            self.want = self._goal(self.key, strength, target, dim)
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
        self.haze_want, self.band_want, self._hazes = 0.0, 0.0, []
        self.rush = self._rush_view = False
        self._recent.clear()
        self._last_key = 0.0
        self._seen, self._unconfirmed = [], None
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
        self._ease_haze(dt)
        return self.follow(self.want, self.strength, dt, speed, frame)

    def _aim_room(self, strength: float) -> None:
        """Where the Outside detail heads for the last picture's bright share
        (auto mode: see AUTO_ROOM)."""
        if self.standalone:
            room = room_for_highs(self._highs, strength / AUTO_LIFT_PER_STRENGTH) * AUTO_ROOM
        else:
            room = room_for_highs(self._highs, strength)
        if not room or abs(room - self.room_want) >= ROOM_DEADBAND:
            self.room_want = room

    def _ease_room(self, dt: float) -> None:
        """Glide the Outside detail toward room_want: quicker up than down
        (an even exponential approach, so no step is ever visible)."""
        want = self.room_want      # (read once: the window thread may change it)
        gap = want - self.room
        if not gap:
            return
        if abs(gap) < ROOM_SNAP:
            self.room = want
            return
        # (Back to the preset, as auto levels stops, at the quick pace.)
        pace = ROOM_RISE_S if gap > 0 or self._room_back else ROOM_FALL_S
        self.room += gap * (1.0 - math.exp(-max(dt, 0.0) / pace))

    def set_haze(self, on: bool) -> None:
        """Tick or untick "Cut through haze": unticked, it heads back off at
        once (not at the next picture) and forgets the pictures it saw, so
        ticking it again starts from a clear view."""
        if not on:
            self.haze_want, self.band_want, self._hazes = 0.0, 0.0, []
        self.haze_on = on

    def _aim_haze(self, m: Measurement, now: float) -> None:
        """Where the haze cut heads: the middle of the last HAZE_FRAMES
        pictures' haze_for_view (none while unticked; pictures not seen yet
        count as clear, so it takes more than half of them), moved only when
        that is HAZE_DEADBAND away (or none), so a still view never keeps
        rewriting the brightness table.  Pictures less than HAZE_GAP_S after
        the first of their place (the quick capture pace, see
        QUICK_CAPTURE_MS) take that place over instead of a new one."""
        hazes = self._hazes      # [((time, Measurement) the place began, haze, band)]
        entry = (now, m)
        haze = haze_for_view(m) if self.haze_on else 0.0
        band = (band_for_view(m), m.band_lo, m.band_hi) if self.haze_on else (0.0, 0.0, 0.0)
        if hazes and _apart(hazes[-1][0], entry) < HAZE_GAP_S:
            hazes[-1] = (hazes[-1][0], haze, band)
        else:
            hazes.append((entry, haze, band))
        del hazes[:-HAZE_FRAMES]
        missing = [0.0] * (HAZE_FRAMES - len(hazes))
        haze = (sorted(missing + [h for _e, h, _b in hazes])[HAZE_FRAMES // 2]
                if self.haze_on else 0.0)
        if not haze or abs(haze - self.haze_want) >= HAZE_DEADBAND:
            self.haze_want = haze
        # The band the same way.  Its edges: the middle of each edge over the
        # places that call for it (at least three when the middle one does),
        # and they follow the view on their own when it turns or the veil
        # moves (BAND_EDGE_DEADBAND), even while the amount stays (a heavy
        # veil's is 100 wherever you look), or the contrast would go to
        # tones the view no longer has.
        bands = sorted([(0.0, 0.0, 0.0)] * len(missing) + [b for _e, _h, b in hazes])
        amount = bands[HAZE_FRAMES // 2][0] if self.haze_on else 0.0
        if amount:
            lit = [b for b in bands if b[0] > 0]
            lo = sorted(b[1] for b in lit)[len(lit) // 2]
            hi = sorted(b[2] for b in lit)[len(lit) // 2]
        if not amount or abs(amount - self.band_want) >= BAND_DEADBAND:
            self.band_want = amount
            if amount:
                self.band_edges = (lo, hi)
                if not self.band:
                    self.band_lo, self.band_hi = lo, hi   # (from off: no glide needed)
        elif (self.band_want and max(abs(lo - self.band_edges[0]),
                                     abs(hi - self.band_edges[1])) >= BAND_EDGE_DEADBAND):
            self.band_edges = (lo, hi)

    def _ease_haze(self, dt: float) -> None:
        """Glide the haze cut toward haze_want, and the band toward band_want
        and band_edges (an even exponential approach, a little slower out
        than in)."""
        self._ease_band(dt)
        want = self.haze_want      # (once: the window's thread may change it meanwhile)
        gap = want - self.haze
        if not gap:
            return
        if abs(gap) < HAZE_SNAP:
            self.haze = want
            return
        pace = HAZE_RISE_S if gap > 0 else HAZE_FALL_S
        self.haze += gap * (1.0 - math.exp(-max(dt, 0.0) / pace))

    def _ease_band(self, dt: float) -> None:
        want, (lo, hi) = self.band_want, self.band_edges
        gap = want - self.band
        if gap:
            if abs(gap) < HAZE_SNAP:
                self.band = want
            else:
                pace = HAZE_RISE_S if gap > 0 else HAZE_FALL_S
                self.band += gap * (1.0 - math.exp(-max(dt, 0.0) / pace))
        if (lo, hi) != (self.band_lo, self.band_hi):
            if max(abs(lo - self.band_lo), abs(hi - self.band_hi)) < BAND_EDGE_SNAP:
                self.band_lo, self.band_hi = lo, hi
            else:
                k = 1.0 - math.exp(-max(dt, 0.0) / HAZE_RISE_S)
                self.band_lo += (lo - self.band_lo) * k
                self.band_hi += (hi - self.band_hi) * k

    @property
    def band_resting(self) -> bool:
        """The band is where it is heading (amount and edges)."""
        return (self.band == self.band_want
                and (self.band_lo, self.band_hi) == self.band_edges)

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
            big = strength * 0.5
            if self.standalone:
                # (Auto mode: half How much in a preset's points, on screen.)
                big *= auto_pace(self.offset) / AUTO_LIFT_PER_STRENGTH
            if strength and gap > big:
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
        cut = self._cut_open()
        if cut:
            # (Auto mode, a view changed all over: see AUTO_CUT_STEP.  The
            # glide's ease into the goal is quicker too.)
            tau /= AUTO_CUT_EASE
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
        if self.standalone:
            # Auto mode: the same share of its (wider) range a second, each
            # step no more visible than a preset's (see AUTO_PACE_POINT).
            # (Where the step lands too: from toning down into a lift.)
            pace = min(auto_pace(self.offset), auto_pace(new))
            if cut:
                pace *= AUTO_CUT_STEP
            limit = min(vmax * speed * AUTO_LIFT_PER_STRENGTH, ceiling * pace)
        else:
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
        self._hazes: dict[str, float] = {}     # ...and its haze cut
        self._bands: dict[str, tuple] = {}     # ...and its haze band (amount, edges)

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
                   and c.room == c.room_want and c.haze == c.haze_want and c.band_resting
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
            # (Passed on every one or two steps when its pace isn't a whole
            # number of them, 75 Hz next to 120: the cap counts the longer
            # gap, so no update it shows moves more than CEILING_STEP.)
            frame = math.ceil(pace / step - 1e-6) * step if pace > step else None
            pos = ctrl.advance(motion, self.speed, frame)
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
        # So does the haze cut.
        hazes = {did: (ctrl.haze, ctrl.haze_want) for did, ctrl in items}
        # And the haze band (its amount and edges: one table between them).
        bands = {did: ((ctrl.band, ctrl.band_lo, ctrl.band_hi), ctrl.band_resting)
                 for did, ctrl in items}
        if not changed:
            for did, (room, room_want) in rooms.items():
                gap = abs(room - self._rooms.get(did, 0.0))
                if gap >= ROOM_STEP or (gap > 0 and room == room_want):
                    changed = True
                    break
        if not changed:
            for did, (haze, haze_want) in hazes.items():
                gap = abs(haze - self._hazes.get(did, 0.0))
                if gap >= HAZE_STEP or (gap > 0 and haze == haze_want):
                    changed = True
                    break
        if not changed:
            for did, ((band, lo, hi), resting) in bands.items():
                gap = band_moved(self._bands.get(did, (0.0, 0.0, 0.0)), (band, lo, hi))
                if gap >= BAND_STEP or (gap > 0 and resting):
                    changed = True
                    break
        if changed:
            (apply or self.apply)(shown)
            self._applied = shown
            self._rooms = {did: room for did, (room, _want) in rooms.items()}
            self._hazes = {did: haze for did, (haze, _want) in hazes.items()}
            self._bands = {did: shape for did, (shape, _resting) in bands.items()}
        return changed

    def _run(self, stop: threading.Event,
             apply: Callable[[dict[str, float]], None] | None = None) -> None:
        last = due = time.perf_counter()
        idle = False
        self._applied, self._since, self._rooms, self._hazes = {}, {}, {}, {}
        self._bands = {}
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
    """The display as a w x h BGRA thumbnail, made exactly like the DXGI
    route's (dxgicapture), so both measure the same brightness: HALFTONE
    shrinks the display to the size of the mip level the graphics card
    would read back (an area average, like the mip chain's 2 x 2 boxes),
    then the same Python box average (dxgicapture.resample) makes the
    thumbnail.  (Shrinking straight to w x h used HALFTONE's own pixel
    grid and weights; the CI self-test measured the routes 13% apart.)
    The rect is in physical pixels (the app is per-monitor DPI aware), so
    it is the size of the desktop picture DXGI hands over."""
    left, top, right, bottom = rect
    _, mw, mh = dxgicapture.choose_mip(right - left, bottom - top, w, h)
    pixels = gdi_shrink(rect, mw, mh)
    if pixels is None or (mw, mh) == (w, h):
        return pixels
    return dxgicapture.resample(pixels, mw * 4, mw, mh, w, h)


def gdi_shrink(rect: Rect, w: int, h: int) -> bytes | None:
    """The display shrunk by GDI (StretchBlt, HALFTONE) straight to w x h
    BGRA (alpha 0), top-down rows."""
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

    def __init__(self, capture: Callable[[Rect], bytes | None] | None = None,
                 fast: bool = True):
        self.available = capture is not None or IS_WINDOWS
        self.reason = "" if self.available else "Auto levels needs Windows."
        self._capture = capture or gdi_capture
        # Faster screen capture (DXGI Desktop Duplication, see dxgicapture):
        # only with the real GDI capture, never with a stand-in one (tests).
        # The window sets fast_capture from the option; the thread reads it.
        self.fast_capture = fast
        self._use_fast = capture is None
        self._local = threading.local()     # each thread's own FastCapture (DXGI is per thread)
        self.routes: dict[str, str] = {}    # display id -> "dxgi" / "gdi" (last capture)
        self.route_reasons: dict[str, str] = {}   # display id -> why DXGI isn't used
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
            self._thread = threading.Thread(target=self._run_then_release, args=(stop,),
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

    def _fast(self) -> dxgicapture.FastCapture | None:
        """This thread's DXGI-or-GDI chooser (None with a stand-in capture)."""
        if not self._use_fast:
            return None
        fast = getattr(self._local, "fast", None)
        if fast is None:
            fast = self._local.fast = dxgicapture.FastCapture(self._capture)
        fast.enabled = self.fast_capture
        return fast

    def release_capture(self) -> None:
        """Let go of this thread's DXGI objects (they belong to the thread
        that made them, so the capture thread calls this as it ends)."""
        fast = getattr(self._local, "fast", None)
        self._local.fast = None
        if fast is not None:
            fast.close()

    def measure_once(self) -> dict[str, Measurement | None]:
        with self._lock:
            rects = dict(self._rects)
        out: dict[str, Measurement | None] = {}
        fast = self._fast()
        if fast is not None:
            fast.keep_only(rects)
        start = time.perf_counter()
        for did, rect in rects.items():
            taken = time.perf_counter()
            try:
                pixels = fast.capture(did, rect) if fast is not None else self._capture(rect)
            except Exception:
                pixels = None
            out[did] = replace(measure(pixels, THUMB_W), at=taken) if pixels else None
        self.capture_ms = (time.perf_counter() - start) * 1000
        if fast is not None:
            self.routes = {did: fast.routes.get(did, "gdi") for did in rects}
            self.route_reasons = {did: fast.reasons[did] for did in rects if did in fast.reasons}
        return out

    def _run_then_release(self, stop: threading.Event) -> None:
        try:
            self._run(stop)
        finally:
            self.release_capture()

    def _run(self, stop: threading.Event) -> None:
        due = time.perf_counter()
        before: dict[str, Measurement | None] = {}
        older: dict[str, Measurement | None] = {}
        last_cost, quick = math.inf, False
        while not stop.is_set():
            results = self.measure_once()
            with self._lock:
                if not stop.is_set():
                    # watch() may have changed the displays while this ran.
                    self._latest = {k: v for k, v in results.items() if k in self._rects}
                    self._seq += 1
            # The next measurement on a steady beat (the capture's own time
            # doesn't add to the gap), sooner while the brightness changes,
            # and twice as often when capturing is cheap.  (The slower of
            # the last two captures counts, so one quick capture among slow
            # ones doesn't change the pace; once quick, it stays quick up to
            # QUICK_LEAVE_MS, so captures taking about QUICK_CAPTURE_MS, two
            # displays say, don't flap between the two paces.)
            cost, last_cost = max(self.capture_ms, last_cost), self.capture_ms
            quick = quick_pace(cost, quick)
            pace = min(cost, QUICK_CAPTURE_MS) if quick else cost
            gap = min([capture_interval(before.get(did), m, pace, older.get(did))
                       for did, m in results.items()], default=INTERVAL_S)
            older, before = before, results
            due += gap
            now = time.perf_counter()
            if due <= now:      # a slow capture: start the beat again, never back to back
                due = now + gap
            stop.wait(due - now)
