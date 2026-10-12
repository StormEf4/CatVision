"""Entry point: ``python -m catvision`` (or ``pythonw -m catvision``)."""

from __future__ import annotations

import argparse
import atexit
import sys

from . import __version__
from . import config as config_mod
from .backends import IS_WINDOWS, create_backends
from .engine import Engine, emergency_reset


def _enable_dpi_awareness() -> None:
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


def _single_instance() -> object | None:
    """Return a handle if we are the only CatVision running, else None."""
    if not IS_WINDOWS:
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\CatVisionSingleInstance")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catvision", description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="put every display back to normal brightness and exit")
    parser.add_argument("--version", action="version", version=f"CatVision {__version__}")
    args = parser.parse_args(argv)

    state_dir = config_mod.app_dir()

    if args.reset:
        gamma, color = create_backends()
        n = emergency_reset(gamma, color, state_dir)
        print(f"Reset {n} display(s) to normal.")
        return 0

    if _single_instance() is None:
        _message("CatVision is already running. Look for its window on the taskbar.")
        return 1

    _enable_dpi_awareness()
    import tkinter as tk
    from .gui import CatVisionApp

    cfg = config_mod.load()
    gamma, color = create_backends()
    engine = Engine(gamma, color, state_dir)
    atexit.register(engine.shutdown)  # restore the display no matter what

    root = tk.Tk()
    CatVisionApp(root, engine, cfg)
    try:
        root.mainloop()
    finally:
        engine.shutdown()
    return 0


def _message(text: str) -> None:
    if IS_WINDOWS:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "CatVision", 0x40)
    else:
        print(text, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
