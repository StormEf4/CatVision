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
        out["fader_running"] = app._auto_fader.running
        end = time.time() + 2.0
        while time.time() < end:
            root.update()
            time.sleep(0.02)
        out["offsets"] = {k: dict(v) for k, v in app.engine.auto_offsets.items()}
        out["info"] = app.auto_info.cget("text")
        out["capture_ms"] = round(app.auto_watch.capture_ms, 1)
        out["fader_error"] = app._auto_fader.last_error or None
        app.handle_action("auto_levels")
        app.handle_action("off")
        root.update()
        out["stopped"] = not app.auto_watch.running
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def _thumbnail() -> bytes:
    """A picture like one auto levels captures (96x54 BGRA): a dim scene
    with a brighter middle and some texture, so measure() does its full work."""
    from .autolevels import THUMB_H, THUMB_W
    out = bytearray()
    for y in range(THUMB_H):
        for x in range(THUMB_W):
            v = 20 + (x * 7 + y * 13) % 40 + (60 if abs(x - THUMB_W // 2) < 20 else 0)
            out += bytes((v, v + 5, v + 10, 255))
    return bytes(out)


def _gap_stats(stamps: list[float], hz: float) -> dict:
    """How evenly spaced the fader's steps were (gaps in ms)."""
    gaps = sorted((b - a) * 1000 for a, b in zip(stamps, stamps[1:]))
    if not gaps:
        return {"steps": len(stamps)}
    beat = 1000 / hz

    def pick(share):
        return round(gaps[min(len(gaps) - 1, int(len(gaps) * share))], 2)
    return {"steps": len(stamps), "hz": round(len(gaps) / (sum(gaps) / 1000), 1),
            "median_ms": pick(0.5), "p95_ms": pick(0.95), "max_ms": round(gaps[-1], 2),
            "late_steps": sum(1 for g in gaps if g > beat * 1.5)}


def fader_steps_under_load(seconds: float, load: str, hz: float | None = None) -> dict:
    """Auto levels (beta): how evenly the fader steps while other threads
    run Python.  ``load``: "watcher" (the real Watcher measuring a captured
    picture five times a second, plus window-like work every 50 ms) or
    "busy" (a thread measuring pictures back to back: the worst case).
    "switch_ms" is Python's thread switch interval (CatVision leaves it as
    it is).  Each step builds a real brightness table (its usual cost) but
    no display is changed.  Never raises: problems go in "error"."""
    import threading
    import time
    from . import autolevels, curves
    hz = hz or autolevels.FADER_MAX_HZ
    out: dict = {"load": load, "switch_ms": round(sys.getswitchinterval() * 1000, 2)}
    stop = threading.Event()
    threads: list = []
    watcher = fader = None
    measure_ms: list[float] = []
    try:
        thumb = _thumbnail()

        def busy():
            while not stop.is_set():
                start = time.perf_counter()
                autolevels.measure(thumb, autolevels.THUMB_W)
                measure_ms.append((time.perf_counter() - start) * 1000)

        def window_like():   # the window's 50 ms tick: a little Python work
            ctrl = autolevels.Controller()
            m = autolevels.measure(thumb, autolevels.THUMB_W)
            while not stop.is_set():
                for _ in range(20):
                    ctrl.observe(m, 30.0, 0.12)
                    autolevels.split_offset("shadow_boost", 12.5, {"shadow_boost": 50.0})
                stop.wait(0.05)

        if load == "busy":
            threads.append(threading.Thread(target=busy, daemon=True))
        else:
            watcher = autolevels.Watcher(capture=lambda rect: thumb)
            watcher.watch({"test": (0, 0, autolevels.THUMB_W, autolevels.THUMB_H)})
            threads.append(threading.Thread(target=window_like, daemon=True))
        for t in threads:
            t.start()
        stamps: list[float] = []

        def apply(positions):
            curves.build_ramp(curves.Settings(shadow_boost=20 + len(stamps) % 40 * 0.25))
            stamps.append(time.perf_counter())   # about when the display would change

        fader = autolevels.Fader({"test": autolevels.Controller(want=1e6, strength=1e6)},
                                 apply)
        fader.set_rate(hz)
        fader.start()
        time.sleep(seconds)
        fader.stop()
        out.update(_gap_stats(stamps, hz))
        if watcher is not None:
            out["measurements"] = watcher.latest()[0]
        if measure_ms:
            out["measure_ms"] = round(sorted(measure_ms)[len(measure_ms) // 2], 2)
        if fader.last_error:
            out["error"] = fader.last_error
    except Exception as exc:
        out["error"] = repr(exc)
    finally:
        stop.set()
        for stopper in (fader, watcher):
            try:
                if stopper is not None:
                    stopper.stop()
            except Exception as exc:
                out.setdefault("error", repr(exc))
        for t in threads:
            t.join(timeout=1.0)
    return out


def gamma_call_cost(gamma, calls: int = 200) -> dict:
    """Auto levels (beta): where one brightness change's time goes on this
    PC (every fade step makes one per display, two when Windows limits the
    strength).  ``dc_ms``: opening and closing the display's device context
    (set_ramp does that on every call); ``set_only_ms``: SetDeviceGammaRamp
    on a context kept open; ``set_ramp_ms``: the whole set_ramp; the
    ``*_cpu_ms`` values are CPU time (Windows counts it in 15.6 ms ticks,
    hence the many calls); ``refused_ms``: a call Windows turns down;
    ``set_ramp_quick_ms``: the way fading updates set it (a kept context),
    and ``quick_readback_max_diff``: a slight look set that way and read
    back through a fresh context (0, or driver rounding: it reached the
    display); ``layout_reread_keeps_dc``: reading the refresh rate again
    (nothing changed) keeps that context.  The timed ramps differ from the
    display's own by one 16-bit unit in one entry, which nobody can see,
    and the display gets its own ramp back.
    Information only: never raises, problems go in "error"."""
    out: dict = {}
    if not hasattr(gamma, "_gdi32") or not hasattr(gamma, "_with_dc"):
        return {"skipped": "not the Windows gamma backend"}
    original = None
    monitor = None
    try:   # (imports too: a problem here must never fail the self-test)
        import statistics
        import time
        from . import curves
        from .backends import fill_ramp_buffer
        monitors = gamma.monitors()
        if not monitors:
            return {"skipped": "no displays"}
        monitor = monitors[0].id
        original = gamma.get_ramp(monitor)
        if original is None:
            return {"skipped": "the display's ramp can't be read"}
        nudged = [list(ch) for ch in original]
        green = nudged[1]
        if len(green) > 129:   # one unit up or down, still never going down
            if green[128] < green[129]:
                green[128] += 1
            elif green[128] > green[127]:
                green[128] -= 1
        ramps = (nudged, original)

        def timed(fn, n: int = calls) -> tuple[float, float]:
            """Median wall ms per call, and CPU ms per call over all of them."""
            walls = []
            cpu = time.thread_time()
            for i in range(n):
                start = time.perf_counter()
                fn(i)
                walls.append(time.perf_counter() - start)
            cpu = time.thread_time() - cpu
            return (round(statistics.median(walls) * 1000, 3), round(cpu / n * 1000, 3))

        out["calls"] = calls
        out["dc_ms"], out["dc_cpu_ms"] = timed(lambda i: gamma._with_dc(monitor, lambda hdc: 1))
        out["set_ramp_ms"], out["set_ramp_cpu_ms"] = timed(
            lambda i: gamma.set_ramp(monitor, ramps[i % 2]))
        buffers = []
        for ramp in ramps:
            buf = gamma._RAMP()
            fill_ramp_buffer(buf, ramp)
            buffers.append(buf)
        gdi, byref = gamma._gdi32, gamma._ctypes.byref
        hdc = gdi.CreateDCW(monitor, None, None, None)
        if hdc:
            try:
                accepted = []
                out["set_only_ms"], out["set_only_cpu_ms"] = timed(
                    lambda i: accepted.append(bool(gdi.SetDeviceGammaRamp(
                        hdc, byref(buffers[i % 2])))))
                out["set_only_accepted"] = sum(accepted)
            finally:
                gdi.DeleteDC(hdc)
        quick = getattr(gamma, "set_ramp_quick", None)
        if quick is not None:
            # Fading updates reuse the display's device context (see
            # backends.DC_REUSE_S): what that costs, and whether a ramp set
            # through a kept one is really what the display holds (read back
            # through a fresh one: a slight look, then the display's own).
            from .backends import DC_REUSE_S
            out["set_ramp_quick_ms"], out["set_ramp_quick_cpu_ms"] = timed(
                lambda i: quick(monitor, ramps[i % 2]))
            look = curves.build_ramp(curves.Settings(shadow_boost=3))
            out["quick_ok"] = bool(quick(monitor, look))
            back = gamma.get_ramp(monitor)
            gamma.set_ramp(monitor, original)
            if back is not None:   # 0 (or driver rounding): it reached the display
                out["quick_readback_max_diff"] = max(
                    abs(a - b) for x, y in zip(back, look) for a, b in zip(x, y))
                out["quick_look_vs_own_max_diff"] = max(
                    abs(a - b) for x, y in zip(original, look) for a, b in zip(x, y))
            time.sleep(DC_REUSE_S + 0.05)
            out["quick_renewed_ok"] = bool(quick(monitor, original))
            # Reading the refresh rate again with nothing changed keeps the
            # kept context (a different reading is a display change, which
            # closes it: see backends' _note_layout).
            rate = getattr(gamma, "_refresh_rate", None)
            if rate is not None:
                # (Only when a context is kept to begin with: a CreateDC
                # failure would otherwise read as a display change.)
                kept = monitor in getattr(gamma, "_dcs", {})
                out["refresh_hz"] = rate(monitor)
                rate(monitor)
                if kept:
                    out["layout_reread_keeps_dc"] = monitor in getattr(gamma, "_dcs", {})
        # A ramp far from a straight line: Windows refuses it unless full
        # strength is unlocked.  Timed again only while it is refused (an
        # accepted one would flash on screen).
        strong = curves.build_ramp(curves.Settings(shadow_boost=100, gamma=3.0))
        out["strong_accepted"] = bool(gamma.set_ramp(monitor, strong))
        if out["strong_accepted"]:
            gamma.set_ramp(monitor, original)
        else:
            out["refused_ms"], _cpu = timed(lambda i: gamma.set_ramp(monitor, strong), 20)
    except Exception as exc:
        out["error"] = repr(exc)
    finally:
        try:
            if original is not None and monitor is not None:
                out["restored"] = bool(gamma.set_ramp(monitor, original))
        except Exception as exc:
            out.setdefault("error", repr(exc))
        try:   # the contexts set_ramp_quick kept (the backend still works)
            if getattr(gamma, "set_ramp_quick", None) is not None:
                gamma.close()
        except Exception as exc:
            out.setdefault("error", repr(exc))
    return out


def _median_ms(fn, n: int) -> float:
    """Median time of ``fn(i)`` for i in range(n), in ms."""
    import statistics
    import time
    times = []
    for i in range(n):
        start = time.perf_counter()
        fn(i)
        times.append(time.perf_counter() - start)
    return round(statistics.median(times) * 1000, 3)


def capture_costs(gamma) -> dict:
    """See run()'s capture_cost."""
    import ctypes
    from .autolevels import gdi_capture
    mons = [m for m in gamma.monitors() if m.rect]
    if not mons:
        return {"skipped": "no display positions"}
    rect = mons[0].rect
    left, top, right, bottom = rect
    out: dict = {"size": [right - left, bottom - top]}
    gdi_capture(rect)   # the first one sets things up
    out["capture_ms"] = _median_ms(lambda i: gdi_capture(rect), 8)
    # The same display copied at full size, without HALFTONE averaging.
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    vp, cint = ctypes.c_void_p, ctypes.c_int
    user32.GetDC.restype, user32.GetDC.argtypes = vp, [vp]
    user32.ReleaseDC.argtypes = [vp, vp]
    gdi32.CreateCompatibleDC.restype, gdi32.CreateCompatibleDC.argtypes = vp, [vp]
    gdi32.CreateCompatibleBitmap.restype = vp
    gdi32.CreateCompatibleBitmap.argtypes = [vp, cint, cint]
    gdi32.SelectObject.restype, gdi32.SelectObject.argtypes = vp, [vp, vp]
    gdi32.DeleteObject.argtypes = [vp]
    gdi32.DeleteDC.argtypes = [vp]
    gdi32.BitBlt.argtypes = [vp, cint, cint, cint, cint, vp, cint, cint, ctypes.c_uint32]
    w, h = right - left, bottom - top
    screen = user32.GetDC(None)
    if not screen:
        return out
    mem = bmp = None
    try:
        mem = gdi32.CreateCompatibleDC(screen)
        bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
        if mem and bmp:
            old = gdi32.SelectObject(mem, bmp)
            out["full_copy_ms"] = _median_ms(
                lambda i: gdi32.BitBlt(mem, 0, 0, w, h, screen, left, top, 0x00CC0020), 8)
            gdi32.SelectObject(mem, old)
    finally:
        if mem:
            gdi32.DeleteDC(mem)
        if bmp:
            gdi32.DeleteObject(bmp)
        user32.ReleaseDC(None, screen)
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
                try:   # does the driver hold what was set?  (information only)
                    back = gamma.get_ramp(m.id)
                    entry["readback_max_diff"] = None if back is None or not result.ok else max(
                        abs(a - b) for x, y in zip(back, result.applied) for a, b in zip(x, y))
                except Exception as exc:
                    entry["readback_error"] = repr(exc)
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
            # Re-reading the layout right away finds nothing changed.
            layout = gamma.layout() if hasattr(gamma, "layout") else {}
            out.append({"layout": {k: [list(r), hz] for k, (r, hz) in layout.items()},
                        "layout_changed": e.refresh_layout()})
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
        if original is not None:
            # How long one brightness change takes (the fader makes up to 120 a second).
            ramps = [curves.build_ramp(curves.Settings(shadow_boost=40 + i % 2)) for i in range(2)]
            start = time.perf_counter()
            for i in range(20):
                gamma.set_ramp(m.id, ramps[i % 2])
            out["set_ramp_ms"] = round((time.perf_counter() - start) / 20 * 1000, 2)
            gamma.set_ramp(m.id, original)
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

    def fader_timing():
        """Auto levels (beta): how many updates a second the fader really
        makes on this PC, the old way of waiting for comparison, and each
        display's refresh rate.  Information only: never fails the test."""
        import threading
        import time
        from . import autolevels
        out = {"refresh_hz": {m.short or m.id: m.hz for m in gamma.monitors()}}
        try:
            for hz in (autolevels.FADER_HZ, autolevels.FADER_MAX_HZ):
                stamps = []
                ctrl = autolevels.Controller(want=1e6, strength=1e6)   # always moving
                fader = autolevels.Fader({"test": ctrl}, lambda pos: stamps.append(time.perf_counter()))
                fader.set_rate(hz)
                fader.start()
                time.sleep(0.6)
                fader.stop()
                gaps = [b - a for a, b in zip(stamps, stamps[1:])]
                out[f"fader_{hz}_hz"] = round(len(gaps) / sum(gaps), 1) if gaps else 0.0
                out[f"fader_{hz}_max_gap_ms"] = round(max(gaps) * 1000, 1) if gaps else None
            stop, n, start = threading.Event(), 0, time.perf_counter()
            while time.perf_counter() - start < 0.5:    # how 1.14 waited between updates
                stop.wait(1 / 60)
                n += 1
            out["old_wait_hz"] = round(n / (time.perf_counter() - start), 1)
        except Exception as exc:
            out["error"] = repr(exc)
        return out

    def update_cost():
        """Auto levels (beta): CPU milliseconds for one fade step on this PC
        (medians; the fader makes up to 120 a second per display).  Uses a
        stand-in display, so nothing on screen changes.  Information only."""
        import statistics
        import time
        out = {}

        def median_ms(fn, n=60):
            times = []
            for i in range(n):
                start = time.perf_counter()
                fn(i)
                times.append(time.perf_counter() - start)
            return round(statistics.median(times) * 1000, 3)
        try:   # (imports too: a problem here must never fail the self-test)
            import ctypes
            from .backends import NullColorBackend, NullGammaBackend, fill_ramp_buffer
            from .engine import Engine
            look = curves.Settings(shadow_boost=40, exposure=6, anti_glare=100, gamma=1.1)
            out["build_ramp_ms"] = median_ms(
                lambda i: curves.build_ramp(curves.Settings(shadow_boost=40 + i * 0.37)))
            ramp = curves.build_ramp(curves.Settings(shadow_boost=100, gamma=2))
            out["limit_ramp_ms"] = median_ms(lambda i: curves.limit_ramp_deviation(ramp, 96))
            buffer_type = (ctypes.c_ushort * 256) * 3
            out["copy_ramp_ms"] = median_ms(lambda i: fill_ramp_buffer(buffer_type(), ramp))
            engine = Engine(NullGammaBackend(), NullColorBackend())
            engine.set_settings(look)
            engine.set_enabled(True)
            did = engine.monitors()[0].id
            out["engine_update_ms"] = median_ms(
                lambda i: engine.set_auto_offsets({did: {"shadow_boost": 5 + i * 0.37}}))
            # Night vision: auto levels moves Exposure, three tinted channels.
            engine.set_settings(curves.Settings(
                exposure=40, anti_glare=100, gamma=1.3, contrast=1.1, black_floor=0.015,
                white_cap=0.88, color_filter="night_vision"))
            out["engine_update_night_vision_ms"] = median_ms(
                lambda i: engine.set_auto_offsets({did: {"exposure": 5 + i * 0.37}}))
        except Exception as exc:
            out["error"] = repr(exc)
        return out

    def fader_load():
        """Auto levels (beta): how evenly the fader steps at 120 a second
        while the Watcher measures and the window works, and with a thread
        measuring back to back (the worst case).  Information only: never
        fails the test."""
        out: dict = {}
        try:
            for name, seconds in (("watcher", 0.6), ("busy", 0.3)):
                out[name] = fader_steps_under_load(seconds, name)
        except Exception as exc:
            out["error"] = repr(exc)
        return out

    def capture_cost():
        """Auto levels (beta): what one picture capture costs (median ms of
        several, after the first), and how much of it is copying the whole
        display without shrinking it.  Information only."""
        out: dict = {}
        try:
            out.update(capture_costs(gamma))
        except Exception as exc:
            out["error"] = repr(exc)
        return out

    check("gamma", gamma_roundtrip)
    check("auto_levels", auto_levels)
    check("fader", fader_timing)
    check("update_cost", update_cost)
    check("gamma_calls", lambda: gamma_call_cost(gamma))
    check("capture_cost", capture_cost)
    check("fader_load", fader_load)
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
