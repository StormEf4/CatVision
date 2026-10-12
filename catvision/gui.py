"""CatVision window (tkinter, no extra dependencies besides the optional tray)."""

from __future__ import annotations

import os
import queue
import re
import sys
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from . import __version__, curves, filters, winutil
from . import config as config_mod
from .curves import LIMITS, Settings
from .engine import Engine, emergency_reset
from .gui_filters import FiltersTab
from .gui_hotkeys import HotkeysTab
from .gui_presets import PresetsTab
from .gui_notices import Notice, WarningButton
from .hotkeys import HotkeyListener, action_label, bindings_from_config
from .presets import all_presets, builtin_by_name
from .theme import (ACCENT, BG, FG, GRID, MUTED, OFF, ON, PANEL, TEXT_SIZES, WARN, Fonts,
                    SliderRow, Tooltip, setup_style)
from .tray import Tray, TrayState

IS_WINDOWS = sys.platform == "win32"

APP_TITLE = "CatVision by StormEf4"   # shown in the window, header and tray

CURVE_SLIDERS = [
    ("shadow_boost", "Shadow boost", "int"),
    ("exposure", "Exposure (very dark games)", "int"),
    ("anti_glare", "Anti-glare (with Exposure)", "int"),
    ("gamma", "Gamma (midtones)", "x"),
    ("black_floor", "Black floor", "pct"),
    ("brightness", "Brightness", "pct"),
    ("contrast", "Contrast", "pct"),
    ("white_cap", "Glare limit (max white)", "pct"),
]
COLOR_SLIDERS = [
    ("saturation", "Saturation", "pct"),
    ("filter_strength", "Filter strength", "pct"),
    ("red", "Red", "pct"),
    ("green", "Green", "pct"),
    ("blue", "Blue", "pct"),
]

TIPS = {
    "exposure": "Amplifies dim light evenly, like night-vision goggles. Best when the "
                "whole scene is very dark: shapes keep their depth instead of looking "
                "foggy. Bright lights will bloom.",
    "anti_glare": "Stops bright lights from blooming when Exposure is on, so you're never "
                  "blinded and players in front of lamps or windows stay visible. Higher "
                  "keeps more detail in bright areas; lower keeps more in dark areas.",
    "shadow_boost": "The main control. Brightens dark areas a lot and bright areas barely "
                    "at all, so the sky doesn't turn white.",
    "gamma": "Brightens (higher) or darkens (lower) the middle tones.",
    "black_floor": "Lifts pure black to dark grey, so nothing is completely black.",
    "brightness": "Overall brightness.",
    "contrast": "Difference between dark and light. Higher cuts through fog and haze.",
    "white_cap": "Limits the brightest whites. Lower it if flashes, lamps or snow are "
                 "too bright.",
    "saturation": "Color strength. 0% is black and white, 200% is very vivid.",
    "filter_strength": "How strongly the color filter is applied.",
    "red": "Tint: more or less red.",
    "green": "Tint: more or less green.",
    "blue": "Tint: less blue is easier on tired eyes at night.",
}

BOOST_STEP = 10
KEEP_APPLIED_MS = 2000


MONITOR_EMOJI = "\U0001F5A5\uFE0F"   # desktop computer
MAIN_EMOJI = "\u2B50"                  # star marks the main display


def display_chip_text(m, index: int) -> str:
    """Checkbox label for a display: a monitor emoji and its number (a star on
    the main one).  The full name is in the checkbox's tooltip."""
    found = re.search(r"\d+", m.short or m.id or "")
    number = found.group(0) if found else str(index + 1)
    return f"{MONITOR_EMOJI} {number}" + (f" {MAIN_EMOJI}" if m.primary else "")


class Toast:
    """Tiny click-through popup used to confirm hotkey presses.

    It is created once and then only made visible/invisible, so it never
    takes keyboard focus away from the game.
    """

    def __init__(self, root: tk.Tk):
        self.root = root
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=ACCENT)  # shows as an amber stripe on the left
        self.label = tk.Label(self.win, text="", bg=PANEL, fg=FG, padx=18, pady=10,
                              font=("Segoe UI", 16, "bold"))
        self.label.pack(padx=(6, 0))
        self._hide_job = None
        self._set_alpha(0.0)
        self._offscreen()
        self.win.update_idletasks()
        self._make_click_through()

    def _set_alpha(self, value: float) -> None:
        try:
            self.win.attributes("-alpha", value)
        except tk.TclError:
            pass

    def _offscreen(self) -> None:
        self.win.geometry("+-10000+-10000")

    def _make_click_through(self) -> None:
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
            GWL_EXSTYLE = -20
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # layered | transparent (click-through) | tool window | no-activate
            style |= 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def show(self, text: str, ms: int = 1300) -> None:
        self.label.configure(text=text)
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        x = (self.root.winfo_screenwidth() - w) // 2
        self.win.geometry(f"+{x}+40")
        self._set_alpha(0.92)
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.hide)

    def hide(self) -> None:
        self._hide_job = None
        self._set_alpha(0.0)
        self._offscreen()


class CatVisionApp:
    def __init__(self, root: tk.Tk, engine: Engine, cfg: config_mod.Config,
                 start_minimized: bool = False):
        self.root = root
        self.engine = engine
        self.cfg = cfg
        self.vars: dict[str, tk.DoubleVar] = {}
        self.sliders: list[SliderRow] = []
        self.filter_key = "none"
        self._filter_keys: list[str] = ["none"]
        self._loading = False
        self._modified = False
        self._apply_job = None
        self._save_job = None
        self._actions: queue.Queue[str] = queue.Queue()
        self._rows = all_presets(cfg.custom_presets)
        self._closed = False
        self._tray_hint_shown = False

        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close_button)
        root.report_callback_exception = self._on_error
        self._set_window_icon()

        # Canvas sizes are in pixels, so scale them on high-DPI screens.
        self.px = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        self.fonts = Fonts(root, cfg.options.text_size)
        setup_style(root, self.fonts)

        for key in LIMITS:
            self.vars[key] = tk.DoubleVar(value=getattr(Settings(), key))
        self.engine.custom_filters = filters.matrices(cfg.custom_filters)
        self.engine.custom_tints = filters.tints(cfg.custom_filters)
        self.engine.displays = list(cfg.options.displays)
        self.engine.color_all_displays = cfg.options.color_everywhere

        self.hotkeys = HotkeyListener(self._actions.put)
        # Force quit runs on the tray thread and only needs to put the screen back.
        self.tray = Tray(self._actions.put, on_force_quit=self.engine.restore_now)
        self._build()

        self.toast = Toast(root)
        self.tray.start()
        self.reload_hotkeys()

        self._load_settings(cfg.current)
        self.refresh_presets()
        self._modified = self._differs_from_preset()
        self._update_title()
        if cfg.options.enable_on_start:
            self._set_enabled(True)
        else:
            self._render_status()

        if start_minimized:
            if self.tray.running:
                root.withdraw()
            else:
                root.iconify()

        root.after(50, self._poll_actions)
        root.after(KEEP_APPLIED_MS, self._keep_applied)

    def _set_window_icon(self) -> None:
        try:
            if IS_WINDOWS:
                self.root.iconbitmap(default=str(winutil.asset("catvision.ico")))
            else:
                self._icon_img = tk.PhotoImage(file=str(winutil.asset("catvision.png")))
                self.root.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass

    def px_(self, n: float) -> int:
        return int(round(n * self.px))

    # ================================================================ layout

    def _build(self) -> None:
        root = self.root
        self.status = tk.Label(root, text="", anchor="w", justify="left", bg=PANEL, fg=FG,
                               padx=10, pady=5, font=self.fonts.small, wraplength=self.px_(380))
        self.status.pack(fill="x", side="bottom")
        self.status.bind("<Configure>",
                         lambda e: self.status.configure(wraplength=max(100, e.width - 20)))

        self._build_header(root)
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(8, 6))
        home = ttk.Frame(self.notebook, padding=10)
        tune = ttk.Frame(self.notebook, padding=10)
        settings = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(home, text="Home")
        self.notebook.add(tune, text="Tune")
        self.presets_tab = PresetsTab(self.notebook, self)
        self.notebook.add(self.presets_tab.frame, text="Presets")
        self._build_home(home)
        self._build_tune(tune)
        self.filters_tab = FiltersTab(self.notebook, self)
        self.hotkeys_tab = HotkeysTab(self.notebook, self)
        self.notebook.add(self.filters_tab.frame, text="Filters")
        self.notebook.add(self.hotkeys_tab.frame, text="Hotkeys")
        self.notebook.add(settings, text="Settings")
        self._build_settings(settings)

    def _build_header(self, root: tk.Tk) -> None:
        header = tk.Frame(root, bg=BG)
        header.pack(fill="x", padx=12, pady=(10, 0))
        try:
            name = "logo_96.png" if self.px >= 1.5 else "logo_48.png"
            self._logo_img = tk.PhotoImage(file=str(winutil.asset(name)))
            tk.Label(header, image=self._logo_img, bg=BG).pack(side="left")
        except tk.TclError:
            pass
        titles = tk.Frame(header, bg=BG)
        titles.pack(side="left", padx=10)
        title_row = tk.Frame(titles, bg=BG)
        title_row.pack(anchor="w")
        tk.Label(title_row, text="CatVision", bg=BG, fg=FG, font=self.fonts.big).pack(side="left")
        tk.Label(title_row, text="by StormEf4", bg=BG, fg=ACCENT,
                 font=self.fonts.small).pack(side="left", padx=(6, 0), pady=(6, 0))
        self.header_sub = tk.Label(titles, text="", bg=BG, fg=MUTED, font=self.fonts.small)
        self.header_sub.pack(anchor="w")
        right = tk.Frame(header, bg=BG)
        right.pack(side="right")
        self.pill = tk.Label(right, text="OFF", bg=OFF, fg=FG, font=self.fonts.bold,
                             padx=16, pady=5, cursor="hand2")
        self.pill.pack(side="left")
        self.pill.bind("<Button-1>", lambda e: self._toggle())
        Tooltip(self.pill, "Click to turn CatVision on or off.")
        quit_btn = ttk.Button(right, text="Quit", width=5, command=self.quit)
        quit_btn.pack(side="left", padx=(8, 0))
        Tooltip(quit_btn, "Quit CatVision. Your screen goes back to normal.")
        # Only shown when something needs attention; click it for details + fixes.
        self.warning = WarningButton(right, root, self.fonts, self.px_, before=self.pill)
        Tooltip(self.warning.icon, "Something needs your attention. Click for details.")

    def _build_home(self, f: ttk.Frame) -> None:
        f.columnconfigure(0, weight=1)
        wrap = self.px_(380)
        self.welcome = ttk.Frame(f, style="Card.TFrame", padding=10)
        self.welcome.columnconfigure(0, weight=1)
        ttk.Label(self.welcome, style="Card.TLabel", justify="left", wraplength=wrap - 90,
                  text="Welcome! Pick a preset, turn CatVision on with the OFF/ON button at the "
                       "top, then raise Shadow boost until every dark square in the test "
                       "pattern shows. Rest the mouse on anything for help.").grid(row=0, column=0, sticky="w")
        ttk.Button(self.welcome, text="Got it", style="Accent.TButton",
                   command=self._dismiss_welcome).grid(row=0, column=1, padx=(8, 0))
        if not self.cfg.options.seen_welcome:
            self.welcome.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        ttk.Label(f, text="Preset", style="Head.TLabel").grid(row=2, column=0, sticky="w", pady=(0, 2))
        prow = ttk.Frame(f)
        prow.grid(row=3, column=0, sticky="ew")
        prow.columnconfigure(1, weight=1)
        prev = ttk.Button(prow, text="\u25c0", width=3, command=lambda: self._step_preset(-1))
        prev.grid(row=0, column=0, padx=(0, 4))
        Tooltip(prev, "Previous preset")
        self.preset_combo = ttk.Combobox(prow, state="readonly", font=self.fonts.normal, height=20)
        self.preset_combo.grid(row=0, column=1, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>",
                               lambda e: self._activate_preset(self.preset_combo.current()))
        nxt = ttk.Button(prow, text="\u25b6", width=3, command=lambda: self._step_preset(1))
        nxt.grid(row=0, column=2, padx=(4, 0))
        Tooltip(nxt, "Next preset")
        self.preset_desc = ttk.Label(f, text="", style="Muted.TLabel", justify="left",
                                     wraplength=wrap)
        self.preset_desc.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        f.bind("<Configure>", self._on_home_resize)

        boost = ttk.Frame(f)
        boost.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.sliders.append(SliderRow(boost, 0, self.vars["shadow_boost"], "Shadow boost",
                                      *LIMITS["shadow_boost"], "int", self.fonts, self._on_change,
                                      tip=TIPS["shadow_boost"]))
        self.sliders.append(SliderRow(boost, 1, self.vars["exposure"], "Exposure",
                                      *LIMITS["exposure"], "int", self.fonts, self._on_change,
                                      tip=TIPS["exposure"]))

        ttk.Label(f, text="Color filter", style="Head.TLabel").grid(row=6, column=0, sticky="w", pady=(10, 2))
        self.filter_combo = ttk.Combobox(f, state="readonly", font=self.fonts.normal, height=20)
        self.filter_combo.grid(row=7, column=0, sticky="ew")
        self.filter_combo.bind("<<ComboboxSelected>>", self._on_filter_combo)
        Tooltip(self.filter_combo, "Color looks: night vision, grayscale, color-blind assist "
                                   "and your own filters (Filters tab).")

        self._build_displays(f, row=8, wrap=wrap)

        ttk.Label(f, text="Test pattern", style="Head.TLabel").grid(row=9, column=0, sticky="w", pady=(12, 2))
        self.pattern = tk.Canvas(f, width=self.px_(300), height=self.px_(84), bg="#000000",
                                 highlightthickness=0)
        self.pattern.grid(row=10, column=0, sticky="w")
        self._draw_pattern()
        self.pattern_hint = ttk.Label(
            f, style="Muted.TLabel", justify="left", wraplength=wrap,
            text="Raise Shadow Boost and Exposure levels to your satisfaction. Above is a full "
                 "dark range for small testing. Since everyone's monitor is different, my "
                 "defaults may not be satisfactory.")
        self.pattern_hint.grid(row=11, column=0, sticky="w")
        self.home_hint = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
        self.home_hint.grid(row=12, column=0, sticky="w", pady=(10, 0))

    def _on_home_resize(self, e) -> None:
        width = max(100, e.width - 24)
        self.preset_desc.configure(wraplength=width)
        self.pattern_hint.configure(wraplength=width)

    def _dismiss_welcome(self) -> None:
        self.welcome.grid_remove()
        self.cfg.options.seen_welcome = True
        self._schedule_save()

    def _build_displays(self, f: ttk.Frame, row: int, wrap: int) -> None:
        """Pick which displays CatVision changes (only shown with 2+ displays)."""
        box = ttk.Frame(f)
        monitors = self.engine.monitors()
        chosen = set(self.engine.displays) & {m.id for m in monitors}
        self.display_vars: dict[str, tk.BooleanVar] = {}
        head = ttk.Frame(box)
        head.pack(fill="x", pady=(10, 2))
        ttk.Label(head, text="Displays", style="Head.TLabel").pack(side="left")
        ident = ttk.Button(head, text="Identify", command=self.identify_displays)
        ident.pack(side="right")
        Tooltip(ident, "Shows each display's name on that screen for a moment.")
        chips = ttk.Frame(box)
        chips.pack(fill="x")
        for i, m in enumerate(monitors):
            var = tk.BooleanVar(value=not chosen or m.id in chosen)
            self.display_vars[m.id] = var
            chip = ttk.Checkbutton(chips, text=display_chip_text(m, i), variable=var,
                                   style="Chip.TCheckbutton",
                                   command=lambda d=m.id: self._on_display_chip(d))
            chip.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0, 6), pady=2)
            Tooltip(chip, m.label)
        ttk.Label(box, text="CatVision only changes the ticked displays.",
                  style="Muted.TLabel").pack(anchor="w")
        if len(monitors) > 1:
            box.grid(row=row, column=0, sticky="ew")

    # ------------------------------------------------------- full strength

    def _strength_state(self) -> str:
        """'locked', 'restart', 'unlocked' or 'unknown' (not Windows)."""
        unlocked = winutil.full_strength_unlocked()
        if unlocked is None:
            return "unknown"
        if not unlocked:
            return "locked"
        if self.cfg.options.unlock_requested_at > winutil.boot_time():
            return "restart"
        return "unlocked"

    def _update_strength_ui(self) -> None:
        state = self._strength_state()
        settings_text = {
            "locked": "Windows limits how strongly CatVision can change brightness, which "
                      "weakens strong looks like night vision. Unlocking changes one Windows "
                      "setting (Windows will ask for permission) and needs a restart.",
            "restart": "Unlocked. Restart your PC to finish.",
            "unlocked": "Unlocked. CatVision can use its full strength.",
            "unknown": "Only available on Windows.",
        }[state]
        self.strength_label.configure(text=settings_text)
        self.unlock_btn.state(["!disabled"] if state == "locked" else ["disabled"])
        self.relock_btn.state(["!disabled"] if state in ("restart", "unlocked") else ["disabled"])


    def _unlock_full_strength(self) -> None:
        if not messagebox.askyesno(
                "Unlock full strength",
                "Windows limits how much apps can change screen brightness. CatVision will "
                "ask Windows to lift that limit by changing one setting (GdiIcmGammaRange, "
                "the same one display-calibration tools use).\n\n"
                "Windows will ask for permission. You'll need to restart your PC afterwards."
                "\n\nYou can undo this any time in Settings. Continue?"):
            return
        if not winutil.request_full_strength(True):
            self.set_status("Windows didn't allow the change, so nothing was changed.", True)
            return
        self.cfg.options.unlock_requested_at = time.time()
        self._schedule_save()
        self.root.after(1500, self._after_strength_change)

    def _undo_full_strength(self) -> None:
        if not messagebox.askyesno("Undo full strength",
                                   "Put Windows' brightness limit back to normal? "
                                   "Windows will ask for permission, then restart your PC."):
            return
        if winutil.request_full_strength(False):
            self.cfg.options.unlock_requested_at = 0.0
            self._schedule_save()
            self.root.after(1500, self._after_strength_change)

    def _after_strength_change(self) -> None:
        self._render_status()
        state = self._strength_state()
        if state == "restart":
            messagebox.showinfo("CatVision", "Done. Restart your PC to finish unlocking "
                                             "full strength.")
        elif state == "locked":
            self.set_status("Windows' brightness limit is back to normal after a restart.")

    def _chosen_displays(self) -> list[str]:
        ids = [d for d, v in self.display_vars.items() if v.get()]
        return [] if len(ids) == len(self.display_vars) else ids

    def _on_display_chip(self, display_id: str | None = None) -> None:
        if not any(v.get() for v in self.display_vars.values()):
            if display_id in self.display_vars:
                self.display_vars[display_id].set(True)
            self.set_status("At least one display has to stay ticked.", True)
            return
        self.cfg.options.displays = self._chosen_displays()
        self.engine.set_displays(self.cfg.options.displays)
        self._render_status()
        self._schedule_save()

    def _set_displays_from_tray(self, action: str) -> None:
        target = action[len("display:"):]
        if target == "all":
            for v in self.display_vars.values():
                v.set(True)
            self._on_display_chip()
        elif target in self.display_vars:
            var = self.display_vars[target]
            if not self.cfg.options.displays:  # was "all": pick just this one
                for d, v in self.display_vars.items():
                    v.set(d == target)
            else:
                var.set(not var.get())
            self._on_display_chip(target)

    def _set_color_everywhere(self, value: bool) -> None:
        """Allow (or stop) screen-wide color changes while only some displays
        are ticked."""
        self.cfg.options.color_everywhere = value
        self.engine.color_all_displays = value
        self.engine.apply()
        self._render_status()
        self._schedule_save()

    def _screen_wide_color_text(self) -> str:
        """Name the part of the current look Windows can only apply screen-wide."""
        s = self._read_settings()
        parts = []
        if (s.color_filter != "none" and s.filter_strength > 0
                and curves.filter_tint(s.color_filter, self.engine.custom_tints) is None):
            parts.append(f"the '{self.filter_combo.get().split('  (')[0]}' filter")
        if s.saturation != 1.0:
            parts.append(f"saturation ({s.saturation * 100:.0f}%)")
        return " and ".join(parts) or "this color change"

    # ------------------------------------------------------------- notices

    def _collect_notices(self) -> list[Notice]:
        notices: list[Notice] = []
        on = self.engine.enabled
        st = self.engine.status
        if self.hotkeys.failed:
            names = ", ".join(action_label(a) for a in self.hotkeys.failed)
            notices.append(Notice(
                "hotkeys", "warn", f"Another program already uses some of your hotkeys: {names}.",
                "Pick different keys for them in the Hotkeys tab.",
                [("Open Hotkeys", lambda: self.notebook.select(self.hotkeys_tab.frame))]))
        if not on:
            return notices
        if not st.gamma_ok:
            notices.append(Notice(
                "gamma_refused", "warn", "Windows refused the brightness change.",
                "Click Reset display, then turn CatVision on again. If it keeps happening, "
                "turn off HDR in Windows Settings > System > Display.",
                [("Reset display", self._reset_display)]))
        elif st.gamma_limited:
            state = self._strength_state()
            if state == "locked":
                notices.append(Notice(
                    "strength", "warn", "Windows is holding back the strength of this look.",
                    "Click Unlock: Windows asks for permission, then restart your PC.",
                    [("Unlock full strength...", self._unlock_full_strength)]))
            elif state == "restart":
                notices.append(Notice(
                    "strength", "info", "Full strength is unlocked.",
                    "Restart your PC to finish. Until then Windows keeps the old limit."))
            elif state == "unlocked":
                notices.append(Notice(
                    "strength", "info", "This look is as strong as your PC allows.",
                    "Your graphics driver has its own limit on brightness changes. Nothing "
                    "needs fixing; lower Exposure or Shadow boost if parts look flat."))
        what = self._screen_wide_color_text()
        if st.color_skipped:
            notices.append(Notice(
                "color_skipped", "warn",
                f"{what[0].upper() + what[1:]} can't be limited to the ticked displays.",
                "Windows can only apply it to every display at once, so it's off for now. "
                "Click Use it anyway, or tick all your displays.",
                [("Use it anyway", lambda: self._set_color_everywhere(True)),
                 ("Tick all displays", lambda: self._set_displays_from_tray("display:all"))]))
        elif st.color_all_displays:
            notices.append(Notice(
                "color_everywhere", "info",
                f"{what[0].upper() + what[1:]} is also changing your unticked displays.",
                "Windows can only apply it to every display at once. Click Keep it off to "
                "leave unticked displays untouched.",
                [("Keep it off", lambda: self._set_color_everywhere(False))]))
        if st.color_used and not st.color_ok and self.engine.color.available:
            notices.append(Notice(
                "color_failed", "warn", "The color filter couldn't be applied.",
                "Turn off Windows' own color filters (Windows Settings > Accessibility > "
                "Color filters), then click Try again.",
                [("Try again", self._apply_now)]))
        if st.messages:
            notices.append(Notice("preview", "info", "Preview mode",
                                  " ".join(dict.fromkeys(st.messages))))
        return notices

    def refresh_notices(self) -> None:
        self.warning.show(self._collect_notices())

    def identify_displays(self) -> None:
        """Show each display's name in big letters on that display."""
        big = self.fonts.big.copy()
        big.configure(size=self.fonts.base * 4)
        for m in self.engine.monitors():
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=ACCENT)
            tk.Label(win, text=m.short or m.label, bg=ACCENT, fg="#1a1200", font=big,
                     padx=30, pady=16).pack()
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            if m.rect:
                left, top, right, bottom = m.rect
            else:
                left, top = 0, 0
                right, bottom = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            win.geometry(f"+{(left + right - w) // 2}+{(top + bottom - h) // 2}")
            win.after(2200, win.destroy)

    def _build_tune(self, f: ttk.Frame) -> None:
        f.columnconfigure(0, weight=1)
        self.tune_title = ttk.Label(f, text="", style="Head.TLabel")
        self.tune_title.grid(row=0, column=0, sticky="w")
        ttk.Label(f, text="Changes show instantly. Click Save to keep them in this preset.",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 6))

        sl = ttk.Frame(f)
        sl.grid(row=2, column=0, sticky="ew")
        row = 0
        for key, label, kind in CURVE_SLIDERS + COLOR_SLIDERS:
            if key == "saturation":
                ttk.Separator(sl).grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
                row += 1
            self.sliders.append(SliderRow(sl, row, self.vars[key], label, *LIMITS[key], kind,
                                          self.fonts, self._on_change, tip=TIPS.get(key, "")))
            row += 1

        bottom = ttk.Frame(f)
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.graph_size = self.px_(150)
        self.graph = tk.Canvas(bottom, width=self.graph_size, height=self.graph_size, bg=PANEL,
                               highlightthickness=0)
        self.graph.grid(row=0, column=0, sticky="nw")
        btns = ttk.Frame(bottom)
        btns.grid(row=0, column=1, sticky="nw", padx=(12, 0))
        ttk.Button(btns, text="Save", command=self._save_preset).pack(fill="x")
        ttk.Button(btns, text="Save as new...", command=self._save_preset_as).pack(fill="x", pady=3)
        ttk.Button(btns, text="Undo changes", command=self._revert).pack(fill="x")
        manage = ttk.Button(btns, text="Manage presets...",
                            command=lambda: self.notebook.select(self.presets_tab.frame))
        manage.pack(fill="x", pady=(3, 0))
        Tooltip(manage, "Create, copy, rename and delete presets in the Presets tab.")

    def _build_settings(self, f: ttk.Frame) -> None:
        f.columnconfigure(1, weight=1)
        o = self.cfg.options
        self.opt_vars = {
            "keep_applied": tk.BooleanVar(value=o.keep_applied),
            "hotkeys": tk.BooleanVar(value=o.hotkeys),
            "hotkey_popup": tk.BooleanVar(value=o.hotkey_popup),
            "enable_on_start": tk.BooleanVar(value=o.enable_on_start),
            "close_to_tray": tk.BooleanVar(value=o.close_to_tray),
        }
        texts = {
            "keep_applied": "Keep applied (fixes games that reset brightness)",
            "hotkeys": "Global hotkeys",
            "hotkey_popup": "Show a popup when a hotkey is used",
            "enable_on_start": "Turn on automatically when CatVision starts",
            "close_to_tray": "Close button keeps CatVision running in the tray",
        }
        row = 0
        for key, var in self.opt_vars.items():
            cb = ttk.Checkbutton(f, text=texts[key], variable=var,
                                 command=lambda k=key: self._on_option(k))
            cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
            if key == "close_to_tray" and not self.tray.available:
                cb.state(["disabled"])
            row += 1
        self.autostart_var = tk.BooleanVar(value=winutil.autostart_enabled())
        cb = ttk.Checkbutton(f, text="Start with Windows (in the tray)", variable=self.autostart_var,
                             command=self._on_autostart)
        cb.grid(row=row, column=0, columnspan=2, sticky="w", pady=2)
        if not IS_WINDOWS:
            cb.state(["disabled"])
        row += 1

        ttk.Label(f, text="Text size").grid(row=row, column=0, sticky="w", pady=(12, 2))
        self.size_combo = ttk.Combobox(f, values=list(TEXT_SIZES), state="readonly",
                                       font=self.fonts.normal)
        self.size_combo.set(o.text_size if o.text_size in TEXT_SIZES else "Large")
        self.size_combo.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=(12, 2))
        self.size_combo.bind("<<ComboboxSelected>>", self._on_text_size)
        row += 1

        btns = ttk.Frame(f)
        btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(14, 0))
        ttk.Button(btns, text="Reset display now", command=self._reset_display).pack(side="left")
        ttk.Button(btns, text="Open settings folder", command=self._open_folder).pack(side="left", padx=6)
        row += 1

        strength = ttk.Frame(f)
        strength.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        ttk.Label(strength, text="Full strength", style="Head.TLabel").pack(anchor="w")
        self.strength_label = ttk.Label(strength, text="", style="Muted.TLabel", justify="left",
                                        wraplength=self.px_(380))
        self.strength_label.pack(anchor="w")
        sbtns = ttk.Frame(strength)
        sbtns.pack(anchor="w", pady=(4, 0))
        self.unlock_btn = ttk.Button(sbtns, text="Unlock full strength...",
                                     command=self._unlock_full_strength)
        self.unlock_btn.pack(side="left")
        self.relock_btn = ttk.Button(sbtns, text="Undo", command=self._undo_full_strength)
        self.relock_btn.pack(side="left", padx=6)
        row += 1

        about = ttk.Label(f, text=(f"{APP_TITLE}  -  version {__version__}\n"
                                   "Changes only what your monitor shows (graphics-card brightness "
                                   "table and Windows color filters). It never reads, changes or "
                                   "injects into games."),
                          style="Muted.TLabel", justify="left", wraplength=self.px_(380))
        about.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(16, 0))
        f.bind("<Configure>", lambda e: about.configure(wraplength=max(100, e.width - 24)))

    # =============================================================== presets

    def refresh_presets(self) -> None:
        self._rows = all_presets(self.cfg.custom_presets)
        names = [r.name + ("  (mine)" if not r.builtin else "  (edited)" if r.changed else "")
                 for r in self._rows]
        self.preset_combo.configure(values=names)
        idx = self._preset_index(self.cfg.active_preset)
        if idx is not None:
            self.preset_combo.current(idx)
            self.preset_desc.configure(text=self._rows[idx].description)
        else:
            self.preset_combo.set(self.cfg.active_preset)
            self.preset_desc.configure(text="")
        self._update_title()
        self.hotkeys_tab.refresh()
        self.presets_tab.refresh()
        self._sync_tray()

    def preset_names(self) -> list[str]:
        return [r.name for r in self._rows]

    def _preset_index(self, name: str | None) -> int | None:
        for i, r in enumerate(self._rows):
            if r.name == name:
                return i
        return None

    def _activate_preset(self, idx: int | None) -> None:
        if idx is None or not 0 <= idx < len(self._rows):
            return
        row = self._rows[idx]
        self.cfg.active_preset = row.name
        self.preset_combo.current(idx)
        self.preset_desc.configure(text=row.description)
        self._load_settings(row.settings)
        self._modified = False
        self._update_title()
        self._apply_now()

    def activate_preset_by_name(self, name: str) -> bool:
        idx = self._preset_index(name)
        if idx is None:
            return False
        self._activate_preset(idx)
        return True

    def _active_row(self):
        idx = self._preset_index(self.cfg.active_preset)
        return None if idx is None else self._rows[idx]

    def _differs_from_preset(self) -> bool:
        row = self._active_row()
        return row is None or row.settings.clamped() != self._read_settings()

    def _ask_name(self, title: str, prompt: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, prompt, parent=self.root, initialvalue=initial)
        name = " ".join((name or "").split())[:60]
        return name or None

    def _save_preset(self) -> None:
        row = self._active_row()
        if row is None:
            self._save_preset_as()
            return
        current = self._read_settings()
        builtin = builtin_by_name(row.name)
        if builtin and builtin.settings == current:
            self.cfg.custom_presets.pop(row.name, None)
        else:
            self.cfg.custom_presets[row.name] = current
        self._modified = False
        self.refresh_presets()
        self._schedule_save()
        self.set_status(f"Saved '{row.name}'.")

    def _save_preset_as(self) -> None:
        name = self._ask_name("Save as new preset", "Name for the new preset:")
        if not name:
            return
        if builtin_by_name(name):
            messagebox.showinfo("CatVision", "That name belongs to a built-in preset. "
                                "Use Save to change the built-in one, or pick another name.")
            return
        if name in self.cfg.custom_presets and not messagebox.askyesno(
                "CatVision", f"Replace your preset '{name}'?"):
            return
        self.cfg.custom_presets[name] = self._read_settings()
        self.cfg.active_preset = name
        self._modified = False
        self.refresh_presets()
        self._schedule_save()

    def preset_row(self, name: str | None = None):
        """The preset called ``name`` (default: the active one), or None."""
        idx = self._preset_index(name if name is not None else self.cfg.active_preset)
        return None if idx is None else self._rows[idx]

    def name_is_free(self, name: str) -> bool:
        if builtin_by_name(name) or name in self.cfg.custom_presets:
            messagebox.showinfo("CatVision", "A preset with that name already exists.")
            return False
        return True

    def add_preset(self, name: str, settings: Settings, use: bool = True) -> None:
        """Create one of the user's own presets (Presets tab: New / Copy)."""
        self.cfg.custom_presets[name] = settings.clamped()
        self.refresh_presets()
        if use:
            self.activate_preset_by_name(name)
        self._schedule_save()

    def _rename_preset(self, name: str | None = None) -> None:
        row = self.preset_row(name)
        if row is None or row.builtin:
            messagebox.showinfo("CatVision", "Built-in presets can't be renamed. "
                                "Use Copy to make your own version.")
            return
        new = self._ask_name("Rename preset", "New name:", row.name)
        if not new or new == row.name:
            return
        if not self.name_is_free(new):
            return
        self.cfg.custom_presets[new] = self.cfg.custom_presets.pop(row.name)
        old_action, new_action = "preset:" + row.name, "preset:" + new
        if old_action in self.cfg.hotkeys:
            self.cfg.hotkeys[new_action] = self.cfg.hotkeys.pop(old_action)
            self.reload_hotkeys()
        if self.cfg.active_preset == row.name:
            self.cfg.active_preset = new
        self.refresh_presets()
        self._schedule_save()

    def _delete_or_restore(self, name: str | None = None) -> None:
        row = self.preset_row(name)
        if row is None:
            return
        if row.builtin:
            if not row.changed:
                messagebox.showinfo("CatVision", "This built-in preset is already at its default.")
                return
            if messagebox.askyesno("CatVision", f"Put '{row.name}' back to its original settings?"):
                self.cfg.custom_presets.pop(row.name, None)
                self.refresh_presets()
                if self.cfg.active_preset == row.name:
                    self._activate_preset(self._preset_index(row.name))
                self._schedule_save()
            return
        if not messagebox.askyesno("CatVision", f"Delete your preset '{row.name}'?"):
            return
        was_active = self.cfg.active_preset == row.name
        del self.cfg.custom_presets[row.name]
        if self.cfg.hotkeys.pop("preset:" + row.name, None) is not None:
            self.reload_hotkeys()
        self.refresh_presets()
        if was_active:
            self._activate_preset(0)
        self._schedule_save()

    def _revert(self) -> None:
        row = self._active_row()
        if row is not None:
            self._load_settings(row.settings)
            self._modified = False
            self._update_title()
            self._apply_now()

    # ============================================================== settings

    def _load_settings(self, s: Settings) -> None:
        self._loading = True
        try:
            for key, var in self.vars.items():
                var.set(getattr(s, key))
            self.filter_key = s.color_filter
        finally:
            self._loading = False
        self.refresh_filter_choices()
        self._refresh_values()

    def _read_settings(self) -> Settings:
        data = {key: var.get() for key, var in self.vars.items()}
        data["shadow_boost"] = round(data["shadow_boost"])
        for key in data:
            if key != "shadow_boost":
                data[key] = round(data[key], 2)
        data["color_filter"] = self.filter_key
        return Settings.from_dict(data)

    def _refresh_values(self) -> None:
        for s in self.sliders:
            s.refresh()
        self._draw_graph(self._read_settings())

    def _on_change(self) -> None:
        if self._loading:
            return
        self._refresh_values()
        if not self._modified:
            self._modified = True
            self._update_title()
        self._schedule_apply()

    def refresh_filter_choices(self) -> None:
        """Rebuild the filter dropdown (after filters are added/renamed)."""
        choices = filters.all_filter_choices(self.cfg.custom_filters)
        self._filter_keys = [k for k, _ in choices]
        self.filter_combo.configure(values=[label for _, label in choices])
        if self.filter_key not in self._filter_keys:
            self.filter_key = "none"
        self.filter_combo.current(self._filter_keys.index(self.filter_key))

    def _on_filter_combo(self, _event=None) -> None:
        self.set_filter(self._filter_keys[self.filter_combo.current()])

    def set_filter(self, key: str) -> None:
        self.filter_key = key
        self.refresh_filter_choices()
        if key != "none" and self.vars["filter_strength"].get() < 0.05:
            self.vars["filter_strength"].set(1.0)
        self._on_change()

    def custom_filters_changed(self) -> None:
        """Called by the Filters tab after any edit."""
        self.engine.custom_filters = filters.matrices(self.cfg.custom_filters)
        self.engine.custom_tints = filters.tints(self.cfg.custom_filters)
        self.refresh_filter_choices()
        self._schedule_apply()
        self._schedule_save()

    def _schedule_apply(self) -> None:
        if self._apply_job:
            self.root.after_cancel(self._apply_job)
        self._apply_job = self.root.after(40, self._apply_now)

    def _apply_now(self) -> None:
        self._apply_job = None
        self.cfg.current = self._read_settings()
        self.engine.set_settings(self.cfg.current)
        self._render_status()
        self._schedule_save()

    # =============================================================== on / off

    def _toggle(self) -> None:
        self._set_enabled(not self.engine.enabled)

    def _set_enabled(self, enabled: bool) -> None:
        self.cfg.current = self._read_settings()
        self.engine.settings = self.cfg.current
        self.engine.set_enabled(enabled)
        self._render_status()

    def _render_status(self) -> None:
        on = self.engine.enabled
        self.pill.configure(text="ON" if on else "OFF", bg=ON if on else OFF)
        # The status line only says what's happening; any problem (with its
        # fix) goes to the notice area just above it, never in two places.
        if not on:
            text = "Off - your display is back to normal."
        elif self.engine.all_displays_selected():
            text = f"On - {self.cfg.active_preset}."
        else:
            names = ", ".join(m.short for m in self.engine.monitors()
                              if m.id in self.engine.displays)
            text = f"On for {names} - {self.cfg.active_preset}."
        self.set_status(text)
        self._update_strength_ui()
        self.refresh_notices()
        self._update_title()
        self._sync_tray()

    def set_status(self, text: str, warn: bool = False) -> None:
        self.status.configure(text=text, fg=WARN if warn else FG)

    def _update_title(self) -> None:
        name = self.cfg.active_preset
        changed = "  (unsaved changes)" if self._modified else ""
        self.tune_title.configure(text=f"Adjusting: {name}{changed}")
        self.root.title(f"{APP_TITLE} - {name}")
        state = "on" if self.engine.enabled else "off"
        self.header_sub.configure(text=f"{name}{' (changed)' if self._modified else ''}  \u00b7  {state}")

    def _sync_tray(self) -> None:
        choices = filters.all_filter_choices(self.cfg.custom_filters)
        self.tray.update(TrayState(
            enabled=self.engine.enabled,
            preset=self.cfg.active_preset,
            presets=self.preset_names(),
            filter_key=self.filter_key,
            filters=choices,
            boost=int(round(self.vars[self._boost_key()].get())),
            boost_label="Exposure" if self._boost_key() == "exposure" else "Shadow boost",
            displays=[(m.id, m.short or m.label) for m in self.engine.monitors()],
            chosen=[d for d in self.engine.displays
                    if d in {m.id for m in self.engine.monitors()}],
        ))

    # ================================================================ drawing

    def _draw_graph(self, s: Settings) -> None:
        c = self.graph
        c.delete("all")
        w = h = self.graph_size
        pad = self.px_(6)
        size = w - 2 * pad
        for i in range(5):
            p = pad + size * i / 4
            c.create_line(pad, p, w - pad, p, fill=GRID)
            c.create_line(p, pad, p, h - pad, fill=GRID)
        c.create_line(pad, h - pad, w - pad, pad, fill="#555c69", dash=(3, 3))
        ramp = curves.build_ramp(s)
        same = ramp[0] == ramp[1] == ramp[2]
        chans = ramp[:1] if same else ramp
        colors = ("#ffffff",) if same else ("#ff6b6b", "#5ee37a", "#5aa9ff")
        for ch, color in zip(chans, colors):
            pts = []
            for i in range(0, 256, 3):
                pts += [pad + size * i / 255, h - pad - size * ch[i] / 65535]
            pts += [pad + size, h - pad - size * ch[255] / 65535]
            c.create_line(*pts, fill=color, width=2, smooth=True)

    def _draw_pattern(self) -> None:
        c = self.pattern
        c.delete("all")
        p = self.px_
        n = 10
        cell = p(300) / n
        for i in range(n):
            v = 1 + i * 3          # very dark greys: 1..28 out of 255
            c.create_rectangle(i * cell + p(2), p(4), (i + 1) * cell - p(2), p(40),
                               fill=f"#{v:02x}{v:02x}{v:02x}", outline="")
            v = 255 - (n - 1 - i) * 3  # very bright greys: 228..255
            c.create_rectangle(i * cell + p(2), p(46), (i + 1) * cell - p(2), p(82),
                               fill=f"#{v:02x}{v:02x}{v:02x}", outline="")

    # ================================================================ options

    def _on_option(self, key: str) -> None:
        setattr(self.cfg.options, key, self.opt_vars[key].get())
        if key == "hotkeys":
            self.reload_hotkeys()
        self._schedule_save()

    def _on_autostart(self) -> None:
        want = self.autostart_var.get()
        if not winutil.set_autostart(want):
            self.autostart_var.set(not want)
            self.set_status("Couldn't change the Windows startup setting.", True)

    def _on_text_size(self, _event=None) -> None:
        self.cfg.options.text_size = self.size_combo.get()
        self.fonts.resize(self.cfg.options.text_size)
        setup_style(self.root, self.fonts)
        self._schedule_save()

    def _reset_display(self) -> None:
        self._set_enabled(False)
        emergency_reset(self.engine.gamma, _NoColor(), config_mod.app_dir())
        self.set_status("Display reset to normal.")

    def _open_folder(self) -> None:
        path = config_mod.app_dir()
        path.mkdir(parents=True, exist_ok=True)
        if IS_WINDOWS:
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            self.set_status(str(path))

    # ================================================================ hotkeys

    def reload_hotkeys(self) -> None:
        """(Re)register global hotkeys from the config."""
        if self.cfg.options.hotkeys:
            self.hotkeys.start(bindings_from_config(self.cfg.hotkeys))
        else:
            self.hotkeys.stop()
            self.hotkeys.failed = []
        self.hotkeys_tab.refresh()
        hk = self.cfg.hotkeys
        parts = []
        if hk.get("toggle"):
            parts.append(f"{hk['toggle']}: on / off")
        if hk.get("off"):
            parts.append(f"{hk['off']}: panic off")
        hint = "   ".join(parts)
        self.home_hint.configure(text=hint + "\nChange keys in the Hotkeys tab." if hint else
                                 "No hotkeys set yet. Choose your own in the Hotkeys tab.")
        self.refresh_notices()

    def pause_hotkeys(self) -> None:
        """Release hotkeys while the user records a new one."""
        self.hotkeys.stop()

    def _poll_actions(self) -> None:
        try:
            while True:
                self.handle_action(self._actions.get_nowait())
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(50, self._poll_actions)

    def handle_action(self, action: str) -> None:
        msg = None
        if action == "toggle":
            self._set_enabled(not self.engine.enabled)
            msg = f"CatVision ON - {self.cfg.active_preset}" if self.engine.enabled else "CatVision OFF"
        elif action == "off":
            self._set_enabled(False)
            msg = "CatVision OFF"
        elif action.startswith("preset:"):
            # Preset hotkeys toggle: the running preset turns off, any other
            # preset is switched to and turned on.
            name = action[len("preset:"):]
            if self.engine.enabled and self.cfg.active_preset == name:
                self._set_enabled(False)
                msg = f"{name}  OFF"
            elif self.activate_preset_by_name(name):
                self._set_enabled(True)
                msg = f"{name}  ON"
        elif action in ("next", "prev"):
            self._step_preset(1 if action == "next" else -1)
            self._set_enabled(True)
            msg = self.cfg.active_preset
        elif action in ("boost_up", "boost_down"):
            key = self._boost_key()
            var = self.vars[key]
            step = BOOST_STEP if action == "boost_up" else -BOOST_STEP
            var.set(min(100, max(0, round(var.get()) + step)))
            self._on_change()
            if not self.engine.enabled:
                self._set_enabled(True)
            msg = f"{'Exposure' if key == 'exposure' else 'Shadow boost'} {round(var.get())}"
        elif action == "filter_next":
            keys = self._filter_keys
            idx = (keys.index(self.filter_key) + 1) % len(keys) if self.filter_key in keys else 0
            self.set_filter(keys[idx])
            if not self.engine.enabled:
                self._set_enabled(True)
            msg = "Filter: " + self.filter_combo.get()
        elif action.startswith("filter:"):
            key = action[len("filter:"):]
            if key in self._filter_keys:
                self.set_filter(key)
                if key != "none" and not self.engine.enabled:
                    self._set_enabled(True)
                msg = "Filter: " + self.filter_combo.get()
        elif action.startswith("display:"):
            self._set_displays_from_tray(action)
        elif action == "identify":
            self.identify_displays()
        elif action == "reset_display":
            self._reset_display()
            msg = "Display reset to normal"
        elif action == "show_window":
            self.show_window()
        elif action == "quit":
            self.quit()
        if msg and self.cfg.options.hotkey_popup:
            self.toast.show(msg)

    def _boost_key(self) -> str:
        """The boost hotkeys adjust Exposure when the look uses it (night
        vision), otherwise Shadow boost."""
        return "exposure" if self.vars["exposure"].get() >= 0.5 else "shadow_boost"

    def _step_preset(self, step: int) -> None:
        cur = self._preset_index(self.cfg.active_preset)
        cur = -1 if cur is None else cur
        self._activate_preset((cur + step) % len(self._rows))

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    # =========================================================== housekeeping

    def _keep_applied(self) -> None:
        if self._closed:
            return
        if self.cfg.options.keep_applied:
            try:
                self.engine.check_and_reapply()
            except OSError:
                pass
        self.root.after(KEEP_APPLIED_MS, self._keep_applied)

    def _schedule_save(self) -> None:
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self._save)

    def _save(self) -> None:
        self._save_job = None
        try:
            config_mod.save(self.cfg)
        except OSError:
            pass

    def _on_error(self, exc_type, exc, tb) -> None:
        import traceback
        if sys.stderr:
            traceback.print_exception(exc_type, exc, tb)
        self.set_status(f"Error: {exc}", True)

    def on_close_button(self) -> None:
        if self.cfg.options.close_to_tray and self.tray.running:
            self.root.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.notify("CatVision is still running here. Right-click the icon to quit.")
        else:
            self.quit()

    def quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cfg.current = self._read_settings()
        self._save()
        self.hotkeys.stop()
        self.tray.stop()
        self.engine.shutdown()
        self.root.destroy()


class _NoColor:
    """Color backend stand-in for a gamma-only reset (the engine owns color)."""

    def reset(self):
        return True

    def close(self):
        pass
