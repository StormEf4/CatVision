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
RISE_FAST_S = 0.5               # ...but catches up quicker after a big change (into a cave)
FALL_S = 0.3                    # dimming is fast, so flashes never blind you
STOPS_FOR_FULL = 3.0            # 3 stops darker/brighter than the target = full strength
DEAD_ZONE = 0.08                # ignore changes smaller than this share of How much
CENTER_WEIGHT = 2.0             # the middle of the screen counts up to 3x the edges
SHADOW_SHARE = 0.35             # how much the darkest quarter of the picture counts
SPEEDS = {"slow": 0.5, "normal": 1.0, "fast": 2.0}
SETTLE_S = 1.0                  # only brighten once it has stayed dark this long (no pumping)
FLASH_RATIO = 2.0               # a picture twice as bright as the last one is a flash...
LOST_S = 2.0                    # ...and a picture lost this long eases back to the preset

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
    With the picture's width, the middle of the screen counts extra."""
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
    return Measurement(key, average, shadows, dark, bright, max(lum) == 0)


def target_offset(key: float, strength: float, target: float, dim: bool = True) -> float:
    """How far to move the slider for a scene this bright: up to +strength
    in dark scenes, down to -strength in bright ones (only if dim), 0 at the
    target."""
    stops = math.log2(target / max(key, 1 / 255))
    amount = strength * max(-1.0, min(1.0, stops / STOPS_FOR_FULL))
    return amount if dim else max(0.0, amount)


def aim_for_current_look(key: float, offset: float, strength: float) -> float:
    """The Aim for value that makes a scene this bright keep its current
    adjustment ("Keep this look")."""
    if strength <= 0:
        return key
    stops = max(-1.0, min(1.0, offset / strength)) * STOPS_FOR_FULL
    return key * 2 ** stops


@dataclass
class Controller:
    """Smoothly follows target_offset for one display.

    observe() takes each new measurement and decides where to go; advance()
    moves there a little at a time (many times a second, so changes fade
    instead of stepping)."""
    offset: float = 0.0
    want: float = 0.0
    strength: float = 0.0
    clock: float = 0.0
    _recent: list = field(default_factory=list)   # (time, key) of the last SETTLE_S
    _last_key: float = 0.0
    _missing_since: float | None = None

    def observe(self, m: Measurement | None, strength: float, target: float,
                dim: bool = True) -> None:
        self.strength = strength
        if m is None:  # couldn't capture the picture: hold, then ease back to the preset
            if self._missing_since is None:
                self._missing_since = self.clock
            elif self.clock - self._missing_since >= LOST_S:
                self.want = 0.0
            return
        self._missing_since = None
        if m.black:  # loading screen, or capture blocked: hold
            return
        self._recent = [(t, k) for t, k in self._recent if self.clock - t < SETTLE_S]
        self._recent.append((self.clock, m.key))
        # Steer by the brightest recent picture: dimming reacts at once, but
        # brightening waits until it has stayed dark, so flickering lights,
        # muzzle flashes and lightning don't make the picture pump.
        self.want = target_offset(max(k for _t, k in self._recent), strength, target, dim)
        if self._last_key and m.key >= self._last_key * FLASH_RATIO and self.want < self.offset:
            self.offset = self.want  # flash or explosion: dim at once, never blind you
        self._last_key = m.key

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
        self.want = 0.0
        self._recent.clear()
        self._last_key = 0.0
        return self.advance(dt, speed)

    def follow(self, want: float, strength: float, dt: float, speed: float = 1.0) -> float:
        gap = want - self.offset
        if abs(gap) < max(0.5, strength * DEAD_ZONE):
            return self.offset      # close enough: don't keep "breathing"
        if gap > 0:
            tau = RISE_FAST_S if strength and gap > strength * 0.5 else RISE_S
        else:
            tau = FALL_S
        self.offset += gap * (1 - math.exp(-max(dt, 0.0) * max(speed, 0.1) / tau))
        return self.offset


# --------------------------------------------------------------------------
# Is a game in front?  (only the foreground window's position is looked at)
# --------------------------------------------------------------------------

_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def full_screen_displays(rects: dict[str, Rect]) -> set[str] | None:
    """Displays that the window in front fills (a game in fullscreen or
    borderless mode, a full-screen video...).  None if it can't be told
    (not Windows), meaning "don't hold anything back"."""
    if not IS_WINDOWS:
        return None
    try:
        import ctypes
        import os
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.GetForegroundWindow.restype = wintypes.HWND
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return set()
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return set()            # CatVision's own windows (e.g. the test pattern)
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        if cls.value in _SHELL_CLASSES:
            return set()            # the desktop or taskbar
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return set()
    except Exception:
        return None
    slack = 2
    return {did for did, (left, top, right, bottom) in rects.items()
            if r.left <= left + slack and r.top <= top + slack
            and r.right >= right - slack and r.bottom >= bottom - slack}


# --------------------------------------------------------------------------
# Screen capture (Windows GDI): a shrunken copy of one display
# --------------------------------------------------------------------------

def gdi_capture(rect: Rect, w: int = THUMB_W, h: int = THUMB_H) -> bytes | None:
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

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    vp = ctypes.c_void_p
    user32.GetDC.restype = vp
    user32.GetDC.argtypes = [vp]
    user32.ReleaseDC.argtypes = [vp, vp]
    gdi32.CreateCompatibleDC.restype = vp
    gdi32.CreateCompatibleDC.argtypes = [vp]
    gdi32.CreateCompatibleBitmap.restype = vp
    gdi32.CreateCompatibleBitmap.argtypes = [vp, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = vp
    gdi32.SelectObject.argtypes = [vp, vp]
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.SetStretchBltMode.argtypes = [vp, ctypes.c_int]
    gdi32.SetBrushOrgEx.argtypes = [vp, ctypes.c_int, ctypes.c_int, vp]
    gdi32.StretchBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [vp, vp, wintypes.UINT, wintypes.UINT, vp, vp, wintypes.UINT]

    left, top, right, bottom = rect
    screen = user32.GetDC(None)
    if not screen:
        return None
    mem = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    try:
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
        if bmp:
            gdi32.DeleteObject(bmp)
        if mem:
            gdi32.DeleteDC(mem)
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
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="catvision-auto-levels",
                                            daemon=True)
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

    def _run(self) -> None:
        while not self._stop.is_set():
            results = self.measure_once()
            with self._lock:
                if not self._stop.is_set():
                    self._latest = results
                    self._seq += 1
            self._stop.wait(INTERVAL_S)
