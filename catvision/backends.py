"""Display backends.

Only two documented, display-level Windows APIs are used:

* ``SetDeviceGammaRamp`` (gdi32) -- loads a brightness lookup table into the
  graphics card for a monitor.  Same thing GPU control panels, f.lux and
  monitor calibration tools do.
* ``MagSetFullscreenColorEffect`` (Magnification.dll) -- Windows'
  accessibility color-filter engine.

Neither of these opens, reads, writes or hooks any other process.  The game
never knows CatVision is running; it only changes what the monitor shows.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass, field

from . import curves

IS_WINDOWS = sys.platform == "win32"


@dataclass
class Monitor:
    id: str            # device name, e.g. \\.\DISPLAY1
    label: str         # friendly name for the GUI
    primary: bool = False
    original: list[list[int]] | None = None
    short: str = ""    # e.g. "Display 1 (main)"
    rect: tuple[int, int, int, int] | None = None  # left, top, right, bottom


@dataclass
class GammaResult:
    ok: bool
    limited: bool = False          # Windows only accepted a weaker version
    strength: float = 1.0          # fraction of the requested effect applied
    failed_monitors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Gamma ramp
# ---------------------------------------------------------------------------

class NullGammaBackend:
    """Does nothing.  Used on non-Windows systems and in tests."""

    available = False
    reason = "Gamma control is only available on Windows."

    def __init__(self) -> None:
        self.last: dict[str, list[list[int]]] = {}

    def monitors(self) -> list[Monitor]:
        return [Monitor("preview", "Preview only (no display control)", True,
                        short="Preview display")]

    def set_ramp(self, monitor_id: str, ramp: list[list[int]]) -> bool:
        self.last[monitor_id] = ramp
        return True

    def get_ramp(self, monitor_id: str) -> list[list[int]] | None:
        return self.last.get(monitor_id)

    def close(self) -> None:
        pass


class WindowsGammaBackend:
    available = True
    reason = ""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)

        class DISPLAY_DEVICEW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        self._DISPLAY_DEVICEW = DISPLAY_DEVICEW
        self._RAMP = (wintypes.WORD * 256) * 3

        u, g = self._user32, self._gdi32
        u.EnumDisplayDevicesW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(DISPLAY_DEVICEW), wintypes.DWORD]
        u.EnumDisplayDevicesW.restype = wintypes.BOOL
        g.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
        g.CreateDCW.restype = wintypes.HDC
        g.DeleteDC.argtypes = [wintypes.HDC]
        g.DeleteDC.restype = wintypes.BOOL
        g.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        g.SetDeviceGammaRamp.restype = wintypes.BOOL
        g.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
        g.GetDeviceGammaRamp.restype = wintypes.BOOL

    def monitors(self) -> list[Monitor]:
        ctypes = self._ctypes
        rects = self._monitor_rects()
        result = []
        i = 0
        while True:
            dd = self._DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(dd)
            if not self._user32.EnumDisplayDevicesW(None, i, ctypes.byref(dd), 0):
                break
            i += 1
            if not dd.StateFlags & 0x1:  # DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                continue
            name = dd.DeviceName
            primary = bool(dd.StateFlags & 0x4)  # DISPLAY_DEVICE_PRIMARY_DEVICE
            mon = self._DISPLAY_DEVICEW()
            mon.cb = ctypes.sizeof(mon)
            friendly = dd.DeviceString
            if self._user32.EnumDisplayDevicesW(name, 0, ctypes.byref(mon), 0) and mon.DeviceString:
                friendly = mon.DeviceString
            number = name.replace("\\\\.\\DISPLAY", "")
            short = f"Display {number}" + (" (main)" if primary else "")
            label = f"Display {number} - {friendly}" + (" (main)" if primary else "")
            result.append(Monitor(name, label, primary, short=short, rect=rects.get(name)))
        result.sort(key=lambda m: (not m.primary, m.id))
        return result

    def _monitor_rects(self) -> dict[str, tuple[int, int, int, int]]:
        """Screen position of every display, by device name."""
        ctypes = self._ctypes
        from ctypes import wintypes

        class MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]

        rects: dict[str, tuple[int, int, int, int]] = {}
        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                       ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def callback(hmon, _hdc, _rect, _data):
            info = MONITORINFOEXW()
            info.cbSize = ctypes.sizeof(info)
            if self._user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                rects[info.szDevice] = (r.left, r.top, r.right, r.bottom)
            return True

        u = self._user32
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, proc_type, wintypes.LPARAM]
        u.EnumDisplayMonitors.restype = wintypes.BOOL
        try:
            u.EnumDisplayMonitors(None, None, proc_type(callback), 0)
        except OSError:
            pass
        return rects

    def _with_dc(self, monitor_id: str, fn):
        hdc = self._gdi32.CreateDCW(monitor_id, None, None, None)
        if not hdc:
            return None
        try:
            return fn(hdc)
        finally:
            self._gdi32.DeleteDC(hdc)

    def set_ramp(self, monitor_id: str, ramp: list[list[int]]) -> bool:
        buf = self._RAMP()
        for c in range(3):
            for i in range(256):
                buf[c][i] = ramp[c][i]
        ok = self._with_dc(
            monitor_id,
            lambda hdc: self._gdi32.SetDeviceGammaRamp(hdc, self._ctypes.byref(buf)))
        return bool(ok)

    def get_ramp(self, monitor_id: str) -> list[list[int]] | None:
        buf = self._RAMP()
        ok = self._with_dc(
            monitor_id,
            lambda hdc: self._gdi32.GetDeviceGammaRamp(hdc, self._ctypes.byref(buf)))
        if not ok:
            return None
        return [list(buf[c]) for c in range(3)]

    def close(self) -> None:
        pass


def apply_ramp_with_fallback(backend, monitor_id: str, ramp: list[list[int]]) -> GammaResult:
    """Apply a ramp, weakening it if Windows refuses the full strength.

    Windows (without the registry tweak in ``windows/``) rejects ramps that
    stray too far from a straight line.  First try the full ramp, then a
    version clamped to the usual Windows limit, then progressively weaker
    blends.
    """
    if backend.set_ramp(monitor_id, ramp):
        return GammaResult(True)
    for steps in (127, 120, 96):
        if backend.set_ramp(monitor_id, curves.limit_ramp_deviation(ramp, steps)):
            return GammaResult(True, limited=True)
    # Binary search the strongest blend toward identity that is accepted.
    lo, hi, best = 0.0, 1.0, None
    for _ in range(7):
        mid = (lo + hi) / 2
        candidate = curves.blend_ramp(ramp, mid)
        if backend.set_ramp(monitor_id, candidate):
            lo, best = mid, candidate
        else:
            hi = mid
    if best is not None:
        backend.set_ramp(monitor_id, best)
        return GammaResult(True, limited=True, strength=lo)
    return GammaResult(False, failed_monitors=[monitor_id])


# ---------------------------------------------------------------------------
# Color filter (Magnification API)
# ---------------------------------------------------------------------------

class NullColorBackend:
    available = False
    reason = "Color filters are only available on Windows 8 or newer."

    def __init__(self) -> None:
        self.last: list[float] | None = None

    def apply(self, effect: list[float]) -> bool:
        self.last = effect
        return True

    def reset(self) -> bool:
        self.last = None
        return True

    def close(self) -> None:
        pass


class WindowsColorBackend:
    available = True
    reason = ""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        if platform.architecture()[0] == "32bit" and platform.machine().endswith("64"):
            raise OSError("Color filters need 64-bit Python on 64-bit Windows.")

        class MAGCOLOREFFECT(ctypes.Structure):
            _fields_ = [("transform", ctypes.c_float * 25)]

        self._ctypes = ctypes
        self._MAGCOLOREFFECT = MAGCOLOREFFECT
        self._mag = ctypes.WinDLL("Magnification", use_last_error=True)
        self._mag.MagInitialize.restype = wintypes.BOOL
        self._mag.MagUninitialize.restype = wintypes.BOOL
        self._mag.MagSetFullscreenColorEffect.argtypes = [ctypes.POINTER(MAGCOLOREFFECT)]
        self._mag.MagSetFullscreenColorEffect.restype = wintypes.BOOL
        self._initialized = False
        self._active = False

    def _ensure_init(self) -> bool:
        if not self._initialized:
            self._initialized = bool(self._mag.MagInitialize())
        return self._initialized

    def apply(self, effect: list[float]) -> bool:
        if not self._ensure_init():
            return False
        e = self._MAGCOLOREFFECT()
        for i, v in enumerate(effect):
            e.transform[i] = v
        ok = bool(self._mag.MagSetFullscreenColorEffect(self._ctypes.byref(e)))
        self._active = self._active or ok
        return ok

    def reset(self) -> bool:
        if not self._active:
            return True
        ok = self.apply(curves.to_mag_color_effect(curves.IDENTITY3))
        self._active = False
        return ok

    def close(self) -> None:
        if self._initialized:
            self.reset()
            self._mag.MagUninitialize()
            self._initialized = False


def create_backends():
    """Return ``(gamma_backend, color_backend)`` for this machine."""
    gamma = color = None
    if IS_WINDOWS:
        try:
            gamma = WindowsGammaBackend()
        except OSError as exc:
            gamma = NullGammaBackend()
            gamma.reason = f"Gamma control unavailable: {exc}"
        try:
            color = WindowsColorBackend()
        except OSError as exc:
            color = NullColorBackend()
            color.reason = f"Color filters unavailable: {exc}"
    return gamma or NullGammaBackend(), color or NullColorBackend()
