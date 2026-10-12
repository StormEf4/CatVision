"""Small Windows helpers: single instance, start with Windows, assets."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

IS_WINDOWS = sys.platform == "win32"
_EVENT_NAME = "Local\\CatVisionShowWindow"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "CatVision"


def asset(name: str) -> Path:
    """Path of a bundled file (works from source and from the .exe)."""
    return Path(__file__).resolve().parent / "assets" / name


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def launch_command(minimized: bool = True) -> str:
    """Command line that starts CatVision (used for start-with-Windows)."""
    flag = " --minimized" if minimized else ""
    if is_frozen():
        return f'"{sys.executable}"{flag}'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else exe
    script = Path(__file__).resolve().parent.parent / "run_catvision.py"
    return f'"{runner}" "{script}"{flag}'


def autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled: bool) -> bool:
    """Add/remove CatVision from the current user's startup programs."""
    if not IS_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


class SingleInstance:
    """Lets a second launch ask the running CatVision to show its window."""

    def __init__(self) -> None:
        self.already_running = False
        self._handle = None
        self._stop = False
        if not IS_WINDOWS:
            return
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        self._k32 = k32
        self._handle = k32.CreateEventW(None, False, False, _EVENT_NAME)
        if self._handle and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            self.already_running = True
            k32.SetEvent(self._handle)  # wake the running copy

    def watch(self, on_show: Callable[[], None]) -> None:
        if not self._handle or self.already_running:
            return

        def run():
            while not self._stop:
                if self._k32.WaitForSingleObject(self._handle, 500) == 0 and not self._stop:
                    on_show()

        threading.Thread(target=run, name="catvision-instance", daemon=True).start()

    def close(self) -> None:
        self._stop = True


def enable_dpi_awareness() -> None:
    """Sharp text on high-DPI screens instead of blurry upscaling."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def message_box(text: str, title: str = "CatVision") -> None:
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    elif sys.stderr:
        print(text, file=sys.stderr)
