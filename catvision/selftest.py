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


def _screenshots(root, app, report_path: str) -> list[str]:
    """Save pictures of the window and of display checkboxes (to check that
    the monitor emoji renders on real Windows).  Best effort."""
    import tkinter as tk
    from tkinter import ttk
    saved = []
    try:
        from PIL import ImageGrab
        from .backends import Monitor
        from .gui import display_chip_text
        win = tk.Toplevel(root)
        win.configure(bg="#121317", padx=12, pady=12)
        for i, primary in enumerate((True, False)):
            m = Monitor(f"\\\\.\\DISPLAY{i + 1}", "test", primary, short=f"Display {i + 1}")
            var = tk.BooleanVar(value=primary)
            ttk.Checkbutton(win, text=display_chip_text(m, i), variable=var,
                            style="Chip.TCheckbutton").pack(side="left", padx=4)
        win.geometry("+40+40")
        for target, suffix in ((win, ".chips.png"), (root, ".window.png")):
            target.update()
            target.lift()
            target.update()
            x, y = target.winfo_rootx(), target.winfo_rooty()
            box = (x, y, x + target.winfo_width(), y + target.winfo_height())
            path = report_path.rsplit(".", 1)[0] + suffix
            ImageGrab.grab(bbox=box, all_screens=True).save(path)
            saved.append(path)
        win.destroy()
    except Exception as exc:  # screenshots are only a diagnostic
        saved.append(f"failed: {exc}")
    return saved


def _top_window_at(x: int, y: int):
    """Top-level window handle that is actually visible at screen point x, y."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = wintypes.HWND
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    h = u.WindowFromPoint(wintypes.POINT(x, y))
    return u.GetAncestor(h, 2) if h else None  # GA_ROOT


def _real_click(widget) -> None:
    """Click the middle of a widget with the real mouse (Windows input path)."""
    import ctypes
    import time
    widget.update()
    x = widget.winfo_rootx() + widget.winfo_width() // 2
    y = widget.winfo_rooty() + widget.winfo_height() // 2
    u = ctypes.windll.user32
    u.SetCursorPos(x, y)
    time.sleep(0.05)
    u.mouse_event(0x0002, 0, 0, 0, 0)   # left down
    u.mouse_event(0x0004, 0, 0, 0, 0)   # left up


def _popup_state(root, get_popup) -> dict:
    """Let Tk handle pending input, then report whether the window that
    get_popup() returns is really visible on top of everything."""
    import ctypes
    import time
    for _ in range(6):
        root.update()
        time.sleep(0.05)
    popup = get_popup()
    if popup is None or not popup.winfo_exists():
        return {"exists": False}
    popup.update_idletasks()
    cx = popup.winfo_rootx() + popup.winfo_width() // 2
    cy = popup.winfo_rooty() + popup.winfo_height() // 2
    ctypes.windll.user32.GetAncestor.restype = ctypes.c_void_p
    own = ctypes.windll.user32.GetAncestor(ctypes.c_void_p(popup.winfo_id()), 2)
    return {"exists": True, "viewable": bool(popup.winfo_viewable()),
            "geometry": popup.winfo_geometry(), "root_geometry": root.winfo_geometry(),
            "visible_on_top": _top_window_at(cx, cy) == own}


def _warning_popup_check(root, app, report_path: str) -> dict:
    """Show a warning, click the warning icon like a user would, and check the
    drop-down really appears on top (it didn't on a user's Windows PC)."""
    out: dict = {}
    if sys.platform != "win32":
        return {"skipped": "not Windows"}
    try:
        import tkinter as tk
        app.hotkeys.failed = ["toggle"]          # a harmless warning to show
        app.refresh_notices()
        root.deiconify()
        root.lift()
        root.focus_force()
        root.update()
        out["icon_mapped"] = bool(app.warning.icon.winfo_ismapped())
        out["app_active"] = root.focus_displayof() is not None
        _real_click(app.warning.icon)
        out["after_click"] = _popup_state(root, lambda: app.warning.popup)
        try:
            from PIL import ImageGrab
            path = report_path.rsplit(".", 1)[0] + ".popup.png"
            ImageGrab.grab(all_screens=True).save(path)
            out["screenshot"] = path
        except Exception as exc:
            out["screenshot"] = f"failed: {exc}"
        _real_click(app.warning.icon)
        out["after_second_click"] = _popup_state(root, lambda: app.warning.popup)
        app.warning.close()
        # The same drop-down made a different way, for comparison.
        for name, transient, topmost in (("no_transient", False, False),
                                         ("topmost", False, True),
                                         ("transient_topmost", True, True)):
            p = tk.Toplevel(root)
            p.overrideredirect(True)
            if transient:
                p.transient(root)
            if topmost:
                p.attributes("-topmost", True)
            tk.Label(p, text="test drop-down", padx=40, pady=30).pack()
            p.geometry(f"+{root.winfo_rootx() + 60}+{root.winfo_rooty() + 120}")
            out[name] = _popup_state(root, lambda: p)
            p.destroy()
        app.hotkeys.failed = []
        app.refresh_notices()
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def _test_pattern_check(root, app, report_path: str) -> dict:
    """Open the test pattern window, try full screen and save a picture."""
    from .gui_testpattern import open_test_pattern
    out: dict = {}
    try:
        from .winutil import window_work_area
        area = window_work_area(root)
        out["work_area"] = list(area)

        def inside(win) -> bool:
            x, y = win.winfo_rootx(), win.winfo_rooty()
            return area[0] <= x < area[2] and area[1] <= y < area[3]
        # The hotkey popup goes on every display CatVision is changing.
        targets = app._toast_areas()
        app.toast.show("CatVision test", ms=400)
        root.update()
        shown = [(w.winfo_rootx(), w.winfo_rooty()) for w in app.toast.wins[:len(targets)]]
        out["toast_targets"] = [list(t) for t in targets]
        out["toast_on_each_target"] = len(shown) == len(targets) and all(
            a[0] <= x < a[2] and a[1] <= y < a[3] for (x, y), a in zip(shown, targets))
        app.toast.hide()
        tp = open_test_pattern(app)
        root.update()
        out["pattern_on_app_monitor"] = inside(tp.win)
        out["size"] = [tp.win.winfo_width(), tp.win.winfo_height()]
        out["items_drawn"] = len(tp.canvas.find_all())
        try:
            from PIL import ImageGrab
            x, y = tp.win.winfo_rootx(), tp.win.winfo_rooty()
            path = report_path.rsplit(".", 1)[0] + ".pattern.png"
            ImageGrab.grab(bbox=(x, y, x + tp.win.winfo_width(), y + tp.win.winfo_height()),
                           all_screens=True).save(path)
            out["screenshot"] = path
        except Exception as exc:
            out["screenshot"] = f"failed: {exc}"
        tp.toggle_full()
        root.update()
        out["full_screen"] = bool(tp.win.attributes("-fullscreen"))
        out["full_size"] = [tp.win.winfo_width(), tp.win.winfo_height()]
        tp.close()
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def _dialog_check(root) -> dict:
    """Open a yes/no dialog and check it appears over the app's window."""
    from . import dialogs
    out: dict = {}

    def answer():
        found = [w for w in root.winfo_children() if isinstance(w, dialogs.Modal)]
        if not found:
            out["opened"] = False
            return
        d = found[0]
        d.update()
        x, y = d.winfo_rootx(), d.winfo_rooty()
        out.update(opened=True, position=[x, y],
                   over_app=root.winfo_rootx() <= x <= root.winfo_rootx() + root.winfo_width()
                   and root.winfo_rooty() <= y <= root.winfo_rooty() + root.winfo_height())
        d.event_generate("<Return>")
    try:
        root.after(400, answer)
        out["answer"] = dialogs.ask_yes_no(root, "CatVision", "Self-test question")
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def _auto_levels_app_check(root, app) -> dict:
    """Turn on auto levels in the real window for a moment."""
    import time
    out: dict = {}
    try:
        app.handle_action("auto_levels")
        out["watching"] = app.auto_watch.running
        end = time.time() + 2.0
        while time.time() < end:
            root.update()
            time.sleep(0.02)
        out["offsets"] = {k: list(v) for k, v in app.engine.auto_offsets.items()}
        out["info"] = app.auto_info.cget("text")
        out["capture_ms"] = round(app.auto_watch.capture_ms, 1)
        app.handle_action("auto_levels")
        app.handle_action("off")
        root.update()
        out["stopped"] = not app.auto_watch.running
    except Exception as exc:
        out["error"] = repr(exc)
    return out


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
        from .tray import Tray, TrayState
        t = Tray(lambda a: None)
        out = {"available": t.available}
        if t.available:
            t.state = TrayState(enabled=True, preset="Dark Areas", presets=["Dark Areas"],
                                filters=[("none", "None")], displays=[("a", "A"), ("b", "B")])
            out["menu_items"] = [str(item) for item in t.build_menu().items]
        return out

    def displays():
        out = []
        mons = gamma.monitors()
        for m in mons:
            out.append({"id": m.id, "short": m.short, "rect": m.rect})
        if mons:
            from .engine import Engine
            e = Engine(gamma, color, None)
            e.displays = [mons[0].id]
            e.settings = curves.Settings(shadow_boost=40, color_filter="night_vision")
            st = e.set_enabled(True)
            out.append({"per_display_apply": st.gamma_ok, "screen_wide_color": st.color_all_displays})
            e.restore_now()
        return out

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
        info["screenshots"] = _screenshots(root, app, report_path)

        def popup_check_then_quit():  # after the steps, so their toasts are gone
            info["warning_popup"] = _warning_popup_check(root, app, report_path)
            info["test_pattern"] = _test_pattern_check(root, app, report_path)
            info["dialog"] = _dialog_check(root)
            info["auto_levels"] = _auto_levels_app_check(root, app)
            app.quit()
        root.after(300 + 150 * len(steps) + 1800, popup_check_then_quit)
        root.mainloop()
        info["enabled_after"] = engine.enabled
        return info

    def auto_levels():
        """Screen capture for auto levels (beta): speed, and whether the
        captured picture includes CatVision's own brightness change."""
        import time
        from .autolevels import THUMB_W, Watcher, full_screen_displays, gdi_capture, measure
        mons = [m for m in gamma.monitors() if m.rect]
        if not mons:
            return {"skipped": "no display positions"}
        m = mons[0]
        start = time.perf_counter()
        pixels = gdi_capture(m.rect)
        out = {"monitor": m.short, "captured": pixels is not None,
               "capture_ms": round((time.perf_counter() - start) * 1000, 1)}
        if pixels is None:
            return out
        before = measure(pixels, THUMB_W)
        out["key_before"] = round(before.key, 4)
        original = gamma.get_ramp(m.id)
        if original is not None and gamma.set_ramp(
                m.id, curves.build_ramp(curves.Settings(shadow_boost=100))):
            time.sleep(0.3)
            after = gdi_capture(m.rect)
            gamma.set_ramp(m.id, original)
            if after:
                out["key_with_boost"] = round(measure(after, THUMB_W).key, 4)
                out["capture_sees_boost"] = abs(out["key_with_boost"] - out["key_before"]) > 0.02
        found = full_screen_displays({x.id: x.rect for x in mons})
        out["full_screen_now"] = None if found is None else sorted(found)
        w = Watcher()
        w.watch({m.id: m.rect})
        time.sleep(1.0)
        seq, latest = w.latest()
        w.stop()
        out["watcher_measurements"] = seq
        out["watcher_stopped"] = not w.running
        return out

    check("gamma", gamma_roundtrip)
    check("auto_levels", auto_levels)
    check("color_filter", color_filter)
    check("hotkeys", hotkeys)
    def full_strength():
        from . import winutil
        # Read-only: never asks for elevation during the self-test.
        return {"unlocked": winutil.full_strength_unlocked(),
                "boot_time_ok": winutil.boot_time() > 0 if winutil.IS_WINDOWS else None}

    check("tray", tray)
    check("full_strength", full_strength)
    check("displays", displays)
    check("window", window)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    return 1 if report["errors"] else 0
