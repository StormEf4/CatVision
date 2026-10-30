"""Entry point: ``CatVision.exe``, ``python -m catvision`` or ``pythonw -m catvision``."""

from __future__ import annotations

import argparse
import atexit
import sys

from . import __version__
from . import config as config_mod
from .backends import create_backends
from .engine import Engine, emergency_reset
from .winutil import SingleInstance, enable_dpi_awareness, message_box


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catvision", description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="put every display back to normal brightness and exit")
    parser.add_argument("--minimized", action="store_true",
                        help="start hidden in the system tray")
    parser.add_argument("--selftest", metavar="REPORT.json",
                        help="check the Windows APIs and the window, write a report, exit")
    parser.add_argument("--version", action="version", version=f"CatVision {__version__}")
    args = parser.parse_args(argv)

    state_dir = config_mod.app_dir()

    if args.reset:
        gamma, color = create_backends()
        n = emergency_reset(gamma, color, state_dir)
        text = f"Reset {n} display(s) to normal."
        if sys.stdout:
            print(text)
        else:
            message_box(text)
        return 0

    # Auto levels (beta): the fader thread gets its turn within about a
    # millisecond while another thread is busy (see THREAD_SWITCH_S).
    from .autolevels import quick_thread_switches
    quick_thread_switches()

    if args.selftest:
        from .selftest import run
        return run(args.selftest)

    instance = SingleInstance()
    if instance.already_running:
        return 0  # the running copy has been asked to show its window

    enable_dpi_awareness()
    import tkinter as tk
    from .gui import CatVisionApp

    cfg = config_mod.load()
    gamma, color = create_backends()
    engine = Engine(gamma, color, state_dir)
    atexit.register(engine.shutdown)  # restore the display no matter what

    root = tk.Tk()
    app = CatVisionApp(root, engine, cfg, start_minimized=args.minimized)
    instance.watch(lambda: app._actions.put("show_window"))
    try:
        root.mainloop()
    finally:
        instance.close()
        engine.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
