"""``CatVision --selftest report.json``: exercise the real Windows APIs.

Used by the automated Windows build to make sure the .exe actually works:
display enumeration, reading/writing the gamma ramp (then restoring it),
the color-filter API, hotkey registration, the tray icon and the window.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback


def run(report_path: str) -> int:
    report: dict = {"platform": sys.platform, "frozen": bool(getattr(sys, "frozen", False)),
                    "checks": {}, "errors": []}
    checks = report["checks"]

    def check(name, fn):
        try:
            checks[name] = fn()
        except Exception:
            report["errors"].append(f"{name}: {traceback.format_exc()}")
            checks[name] = "EXCEPTION"

    os.environ["CATVISION_CONFIG_DIR"] = tempfile.mkdtemp(prefix="catvision-selftest-")

    from . import curves
    from .backends import apply_ramp_with_fallback, create_backends
    gamma, color = create_backends()
    checks["gamma_backend"] = type(gamma).__name__
    checks["color_backend"] = type(color).__name__

    def gamma_roundtrip():
        out = []
        for m in gamma.monitors():
            original = gamma.get_ramp(m.id)
            entry = {"monitor": m.label, "read": original is not None}
            if original is not None:
                result = apply_ramp_with_fallback(
                    gamma, m.id, curves.build_ramp(curves.Settings(shadow_boost=60)))
                entry["apply_ok"] = result.ok
                entry["limited"] = result.limited
                entry["restored"] = gamma.set_ramp(m.id, original)
            out.append(entry)
        return out

    def color_filter():
        ok = color.apply(curves.to_mag_color_effect(curves.filter_matrix("grayscale")))
        reset = color.reset()
        color.close()
        return {"apply": ok, "reset": reset}

    def hotkeys():
        import time
        from .hotkeys import HotkeyListener, bindings_from_config
        listener = HotkeyListener(lambda a: None)
        listener.start(bindings_from_config({"toggle": "Ctrl+Alt+Shift+F12"}))
        time.sleep(0.2)
        failed = list(listener.failed)
        listener.stop()
        return {"supported": listener.supported, "failed": failed}

    def tray():
        from .tray import Tray
        t = Tray(lambda a: None)
        return {"available": t.available}

    def window():
        import tkinter as tk
        from . import config as config_mod
        from .engine import Engine
        from .gui import CatVisionApp
        g, c = create_backends()
        engine = Engine(g, c, config_mod.app_dir())
        root = tk.Tk()
        app = CatVisionApp(root, engine, config_mod.load())
        steps = ["preset:Night Vision", "boost_up", "filter_next", "next", "toggle", "off"]
        for i, action in enumerate(steps):
            root.after(300 + 150 * i, lambda a=action: app.handle_action(a))
        info = {"tabs": len(app.notebook.tabs()), "presets": len(app.preset_names()),
                "tray_running": app.tray.running}
        root.after(300 + 150 * len(steps) + 300, app.quit)
        root.mainloop()
        info["enabled_after"] = engine.enabled
        return info

    check("gamma", gamma_roundtrip)
    check("color_filter", color_filter)
    check("hotkeys", hotkeys)
    check("tray", tray)
    check("window", window)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 1 if report["errors"] else 0
