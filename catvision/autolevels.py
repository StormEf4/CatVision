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
from dataclasses import dataclass
from typing import Callable

from .winutil import IS_WINDOWS

THUMB_W, THUMB_H = 96, 54       # size of the copy that is measured
INTERVAL_S = 0.2                # five measurements a second
RISE_S = 1.2                    # brightening is slow and smooth...
FALL_S = 0.3                    # ...dimming is fast, so flashes never blind you
STOPS_FOR_FULL = 3.0            # 3 stops darker/brighter than the target = full strength

DEFAULT_STRENGTH = 30.0         # how far (slider points) it may move Shadow boost / Exposure
DEFAULT_TARGET = 0.12           # the scene brightness it aims for (log-average, 0-1)
STRENGTH_LIMITS = (0.0, 60.0)
TARGET_LIMITS = (0.04, 0.30)

_LOG = [math.log(max(i, 1) / 255.0) for i in range(256)]

Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class Measurement:
    key: float      # log-average brightness 0-1 (dark areas count fully)
    dark: float     # share of the picture that is near-black
    bright: float   # share that is near-white
    black: bool     # completely black: probably can't see the picture


def measure(bgra: bytes) -> Measurement:
    """Brightness of a 32-bit BGRA picture (as Windows hands it over)."""
    n = len(bgra) // 4
    if n == 0:
        return Measurement(0.0, 1.0, 0.0, True)
    lum = [(54 * r + 183 * g + 19 * b) >> 8
           for b, g, r in zip(bgra[0::4], bgra[1::4], bgra[2::4])]
    key = math.exp(sum(_LOG[v] for v in lum) / n)
    dark = sum(1 for v in lum if v < 26) / n
    bright = sum(1 for v in lum if v > 230) / n
    return Measurement(key, dark, bright, max(lum) == 0)


def target_offset(key: float, strength: float, target: float) -> float:
    """How far to move the slider for a scene this bright: up to +strength
    in dark scenes, down to -strength in bright ones, 0 at the target."""
    stops = math.log2(target / max(key, 1 / 255))
    return strength * max(-1.0, min(1.0, stops / STOPS_FOR_FULL))


@dataclass
class Controller:
    """Smoothly follows target_offset for one display."""
    offset: float = 0.0

    def step(self, m: Measurement, strength: float, target: float, dt: float) -> float:
        if m.black:  # nothing to see (loading screen, or capture blocked): hold
            return self.offset
        want = target_offset(m.key, strength, target)
        tau = RISE_S if want > self.offset else FALL_S
        self.offset += (want - self.offset) * (1 - math.exp(-max(dt, 0.0) / tau))
        return self.offset


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
        """Start (or keep) measuring these displays; empty stops."""
        with self._lock:
            self._rects = dict(rects)
            self._latest = {k: v for k, v in self._latest.items() if k in rects}
        if not rects:
            self.stop()
        elif self.available and not self.running:
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
            out[did] = measure(pixels) if pixels else None
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
