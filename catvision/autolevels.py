"""Auto levels (beta): watch how bright the picture is and adjust Shadow boost
or Exposure to match, the way your eyes adapt.

How it stays safe: it only takes a tiny, low-resolution copy of what Windows
is already showing on the ticked displays (the same screen-capture route
recording and streaming apps use).  It never touches the game, never saves
or sends the pictures, and only measures overall brightness: it does not
look for anything in the picture.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .winutil import IS_WINDOWS

THUMB_W, THUMB_H = 96, 54       # size of the copy that is measured
INTERVAL_S = 0.2                # five measurements a second
RISE_S = 1.2                    # brightening is slow and smooth...
RISE_FAST_S = 0.4               # ...but catches up quicker after a big change (into a cave)
FALL_S = 0.3                    # dimming is fast, so flashes never blind you
RUSH_S = 0.12                   # a flash fades down in about a tenth of a second
# Speed limits in slider points per second: with 60 updates a second (more
# on faster screens) even the fastest normal fade moves well under one point
# per update, which the eye can't see as a step.
MAX_RISE = 25.0
MAX_RISE_FAST = 55.0
MAX_FALL = 50.0
MAX_RUSH = 240.0
# Whatever speed is chosen, a fade never moves more than this per second
# (about 1.3 points per update outside a flash, 4 in one), so "fast" is
# quicker but still below the step the eye notices.
CEILING = 80.0
CEILING_RUSH = 240.0
FADER_HZ = 60                   # how often the displays are moved (a 60 Hz screen)...
FADER_MAX_HZ = 120              # ...and on faster screens up to this, so each step is smaller
IDLE_S = 0.1                    # how often the fader checks for a new goal while at rest
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
FLASH_RATIO = 2.0               # a picture twice as bright as the last one is a flash...
LOST_S = 2.0                    # ...and a picture lost this long eases back to the preset
BLACK_HOLD_S = 30.0             # a black screen holds the look this long (loading screens)
NOISE_KEY = 2.0 / 255           # a wobble this small (about a level each way) is only noise
FROZEN_S = 3.0                  # a picture that hasn't changed at all this long is stale
AIM_STEP = 1.25                 # the "aim brighter / darker" hotkeys change Aim for this much
# When the main slider runs out of room, the rest goes to the other one
# (Exposure works harder per point, so it gets half as much).
SPILL = {"shadow_boost": ("exposure", 0.5), "exposure": ("shadow_boost", 1.0)}
SLIDER_RANGE = (0.0, 100.0)

DEFAULT_STRENGTH = 30.0         # how far (slider points) it may move Shadow boost / Exposure
DEFAULT_TARGET = 0.12           # the scene brightness it aims for (log-average, 0-1)
STRENGTH_LIMITS = (0.0, 60.0)
TARGET_LIMITS = (0.04, 0.30)

_LOG = [math.log(max(i, 1) / 255.0) for i in range(256)]

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


def measure(bgra: bytes, width: int | None = None) -> Measurement:
    """Brightness of a 32-bit BGRA picture (as Windows hands it over).
    With the picture's width (a real capture), the middle of the screen
    counts extra and a stale, unchanging picture can be noticed."""
    n = len(bgra) // 4
    if n == 0:
        return Measurement(0.0, 0.0, 0.0, 1.0, 0.0, True)
    lum = [(54 * r + 183 * g + 19 * b) >> 8
           for b, g, r in zip(bgra[0::4], bgra[1::4], bgra[2::4])]
    if width and n % width == 0:
        weights = _center_weights(width, n // width)
    else:
        weights = [1.0] * n
    total = sum(weights)
    average = math.exp(sum(w * _LOG[v] for w, v in zip(weights, lum)) / total)
    hist = [0.0] * 256
    for w, v in zip(weights, lum):
        hist[v] += w
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
    return Measurement(key, average, shadows, dark, bright, max(lum) == 0, signature)


def target_offset(key: float, strength: float, target: float, dim: bool = True) -> float:
    """How far to move the slider for a scene this bright: up to +strength
    in dark scenes, down to -strength in bright ones (only if dim), 0 at the
    target."""
    stops = math.log2(target / max(key, 1 / 255))
    amount = strength * max(-1.0, min(1.0, stops / STOPS_FOR_FULL))
    return amount if dim else max(0.0, amount)


def split_offset(key: str, offset: float, base: dict[str, float]) -> dict[str, float]:
    """Share an adjustment between Shadow boost and Exposure: the preset's
    main slider first, and whatever doesn't fit (it's already near the top or
    bottom) goes to the other one, so auto levels keeps its full range."""
    lo, hi = SLIDER_RANGE
    main = max(lo - base[key], min(hi - base[key], offset))
    out = {key: main}
    rest = offset - main
    if abs(rest) > 0.05:
        other, ratio = SPILL[key]
        extra = max(lo - base[other], min(hi - base[other], rest * ratio))
        if abs(extra) > 0.05:
            out[other] = extra
    return out


def clean_title(title: str) -> str:
    # rstrip after the cut: a cut ending on a space would not survive
    # config.py cleaning the saved key again (this must be idempotent).
    return " ".join((title or "").split())[:60].rstrip()


def nudge_aim(aim: float, brighter: bool) -> float:
    """One press of "aim brighter" or "aim darker"."""
    lo, hi = TARGET_LIMITS
    aim = aim * AIM_STEP if brighter else aim / AIM_STEP
    return round(min(hi, max(lo, aim)), 3)


def aim_for_current_look(key: float, offset: float, strength: float) -> float:
    """The Aim for value that makes a scene this bright keep its current
    adjustment ("Keep this look")."""
    if strength <= 0:
        return key
    stops = max(-1.0, min(1.0, offset / strength)) * STOPS_FOR_FULL
    return key * 2 ** stops


@dataclass
class Controller:
    """Decides where one display should go and glides it there.

    observe() takes each new measurement (five a second) and sets the goal;
    advance() moves toward it in tiny steps (60-120 a second, on the fader
    thread).  The motion eases in and out like a spring and has a speed
    limit, so there is never a visible jump."""
    offset: float = 0.0
    want: float = 0.0
    strength: float = 0.0
    clock: float = 0.0
    velocity: float = 0.0
    rush: bool = False                             # a flash: fade down extra fast
    frozen: bool = False                           # the picture has stopped changing
    _catch_up: bool = False                        # a big brightening: quick pace to the end
    _recent: list = field(default_factory=list)   # (time, key) of the last SETTLE_S
    _last_key: float = 0.0
    _jump_at: float = -CALM_S                      # when a light last went on (or a flash)
    _missing_since: float | None = None
    _signature: int = 0
    _same_since: float = 0.0
    _black_since: float | None = None
    _settings: tuple = ()                          # (strength, target, dim) last seen

    def observe(self, m: Measurement | None, strength: float, target: float,
                dim: bool = True) -> None:
        self.strength = strength
        if m is not None and m.black:
            # Loading screen, or capture blocked: hold the look.  A black
            # screen is pixel-identical every time, so it must not count as
            # a frozen picture either; only a very long one eases back.
            self._missing_since = None
            self._signature, self._same_since = m.signature, self.clock
            self.frozen = False
            if self._black_since is None:
                self._black_since = self.clock
            elif self.clock - self._black_since >= BLACK_HOLD_S:
                self.want, self.rush = 0.0, False
            return
        if m is not None:
            self._black_since = None
        if m is not None and m.signature:
            # Some games in exclusive fullscreen leave Windows with an old,
            # frozen picture.  One that hasn't changed at all for a few
            # seconds is treated as not seen, so it can't steer anything.
            if m.signature != self._signature:
                self._signature, self._same_since = m.signature, self.clock
            self.frozen = self.clock - self._same_since >= FROZEN_S
            if self.frozen:
                m = None
        if m is None:  # couldn't capture the picture: hold, then ease back to the preset
            if self._missing_since is None:
                self._missing_since = self.clock
            elif self.clock - self._missing_since >= LOST_S:
                self.want = 0.0
            return
        self._missing_since = None
        now = self.clock
        self._recent = [(t, k) for t, k in self._recent if now - t < SETTLE_S]
        self._recent.append((now, m.key))
        if self._last_key and m.key > self._last_key * STEADY_RATIO:
            self._jump_at = now
        # Steer by the brightest recent picture: dimming reacts at once, but
        # brightening waits until it has stayed dark, so flickering lights,
        # muzzle flashes and lightning don't make the picture pump.
        calm = now - self._jump_at >= CALM_S
        lately = [(t, k) for t, k in self._recent if now - t < SETTLE_FAST_S]
        steady = [k for _t, k in lately]
        newest = [k for _t, k in self._recent[-DARKENING_COUNT:]]
        early = None
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
        if early is not None:
            # Brightening granted early is kept: the older, brighter pictures
            # can't take it back (that would pump), only a brighter new one can.
            self._recent = [(t, min(k, early)) for t, k in self._recent]
        key = max(k for _t, k in self._recent)
        want = target_offset(key, strength, target, dim)
        flash = bool(self._last_key) and m.key >= self._last_key * FLASH_RATIO
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
        noise = settings == before and near <= self.want <= far
        if not noise and abs(want - self.want) >= max(0.5, strength * DEAD_ZONE) or flash:
            # A flash fades down fast; so does a picture that keeps getting
            # brighter while that fast fade is still going (walking out into
            # daylight takes several measurements, not one).
            brighter = self.rush and want < self.want
            self.want = want
            self.rush = (flash or brighter) and want < self.offset

    @property
    def black_too_long(self) -> bool:
        """A black screen has lasted longer than BLACK_HOLD_S: back to the preset."""
        return self._black_since is not None and self.clock - self._black_since >= BLACK_HOLD_S

    def release(self) -> None:
        """Head back to the preset (no game in front)."""
        self.want = 0.0
        self.rush = False
        self._recent.clear()
        self._last_key = 0.0
        self._black_since = None
        self._jump_at = self.clock - CALM_S

    def advance(self, dt: float, speed: float = 1.0) -> float:
        self.clock += max(dt, 0.0)
        return self.follow(self.want, self.strength, dt, speed)

    def step(self, m: Measurement | None, strength: float, target: float, dt: float,
             speed: float = 1.0, dim: bool = True) -> float:
        """observe() then advance(): one measurement, one move."""
        self.observe(m, strength, target, dim)
        return self.advance(dt, speed)

    def relax(self, dt: float, speed: float = 1.0) -> float:
        """Drift back to the preset (no game in front)."""
        self.release()
        return self.advance(dt, speed)

    def follow(self, want: float, strength: float, dt: float, speed: float = 1.0) -> float:
        """Glide toward want: a critically damped spring (smooth start and
        stop, no overshoot) with a speed limit, never past want."""
        dt = max(dt, 0.0)
        gap = want - self.offset
        if abs(gap) < 0.01 and abs(self.velocity) < 0.05:
            self.offset, self.velocity, self._catch_up = want, 0.0, False
            return self.offset
        if gap < 0:
            self._catch_up = False
        speed = max(speed, 0.1)
        if gap > 0:
            # A big change (into a cave) catches up quicker, all the way:
            # dropping to the slow pace for the last stretch would leave the
            # picture too dark for seconds.
            if strength and gap > strength * 0.5:
                self._catch_up = True
            tau, vmax = (RISE_FAST_S, MAX_RISE_FAST) if self._catch_up else (RISE_S, MAX_RISE)
        elif self.rush:
            tau, vmax = RUSH_S, MAX_RUSH
        else:
            tau, vmax = FALL_S, MAX_FALL
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
        limit = min(vmax * speed, CEILING_RUSH if vmax == MAX_RUSH else CEILING)
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
    """Moves every display toward where auto levels wants it, FADER_HZ times
    a second on its own thread, so fades are smooth and never wait on the
    window.  apply() gets {display id: offset} whenever something moved."""

    def __init__(self, controllers: dict[str, Controller],
                 apply: Callable[[dict[str, float]], None], hz: int = FADER_HZ):
        self.controllers = controllers
        self.apply = apply
        self.speed = 1.0
        self.interval = 1.0 / hz     # may be changed while running (set_rate)
        self._stop = threading.Event()
        self._wake = threading.Event()   # a new goal: stop resting at once
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self._applied: dict[str, float] = {}

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

    def settled(self) -> bool:
        """Every display is where it wants to be and has stopped moving."""
        return all(abs(c.want - c.offset) <= 0.001 and abs(c.velocity) < 1e-3
                   for c in list(self.controllers.values()))

    def tick(self, dt: float, motion_dt: float | None = None, force: bool = False,
             apply: Callable[[dict[str, float]], None] | None = None) -> bool:
        """Move every display one step.  apply() is called only when some
        position moved noticeably (or force), and always once more when a
        display comes to rest, so its exact final value is applied.  Returns
        True if apply() was called."""
        motion = dt if motion_dt is None else min(motion_dt, dt)
        # One snapshot: the window may add or remove displays meanwhile.
        items = list(self.controllers.items())
        positions, wants = {}, {}
        for did, ctrl in items:
            if dt > motion:
                ctrl.clock += dt - motion   # time passes even while resting
            positions[did] = ctrl.advance(motion, self.speed)
            wants[did] = ctrl.want
        last = self._applied
        changed = force or positions.keys() != last.keys()
        if not changed:
            for did, pos in positions.items():
                gap = abs(pos - last[did])
                if gap > 0.001 or (gap > 0 and wants[did] == pos):
                    changed = True
                    break
        if changed:
            (apply or self.apply)(positions)
            self._applied = positions
        return changed

    def _run(self, stop: threading.Event,
             apply: Callable[[dict[str, float]], None] | None = None) -> None:
        last = due = time.perf_counter()
        idle = False
        self._applied = {}
        while not stop.is_set():
            now = time.perf_counter()
            dt, last = min(now - last, 0.25), now
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
            try:
                pixels = self._capture(rect)
            except Exception:
                pixels = None
            out[did] = measure(pixels, THUMB_W) if pixels else None
        self.capture_ms = (time.perf_counter() - start) * 1000
        return out

    def _run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            results = self.measure_once()
            with self._lock:
                if not stop.is_set():
                    # watch() may have changed the displays while this ran.
                    self._latest = {k: v for k, v in results.items() if k in self._rects}
                    self._seq += 1
            stop.wait(INTERVAL_S)
