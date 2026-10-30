"""CatVision window (tkinter, no extra dependencies besides the optional tray)."""

from __future__ import annotations

import math
import os
import queue
import re
import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from . import __version__, autolevels, curves, dialogs, filters, winutil
from . import config as config_mod
from .curves import LIMITS, Settings
from .engine import Engine, emergency_reset
from .gui_filters import FiltersTab
from .gui_hotkeys import HotkeysTab
from .gui_presets import PresetsTab
from .gui_testpattern import open_test_pattern
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
AUTO_FRONT_S = 0.25   # re-check which window is in front this often, capture or not
AUTO_STALE_S = 1.0    # no new picture for this long counts as "can't see it"


def auto_number(value, lo: float, hi: float, default):
    """Auto levels (beta): a setting read from config.json as a number inside
    lo..hi.  NaN, infinity, True/False or text give the default instead, so a
    hand-edited file can never push the controller past its own limits."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    try:
        value = float(value)
    except OverflowError:       # a hand-typed integer far too long for a float
        return default
    if not math.isfinite(value):
        return default
    return min(hi, max(lo, value))


def auto_tick_plan(now: float, fresh: bool, last_front: float,
                   last_fresh: float) -> tuple[bool, bool]:
    """Auto levels (beta): (re-check the window in front?, picture stale?).
    The window in front is re-checked on its own clock, so a stalled capture
    can't keep a display boosted after the game is gone; a picture that
    hasn't changed for AUTO_STALE_S is treated as lost."""
    recheck = fresh or now - last_front >= AUTO_FRONT_S
    stale = not fresh and now - last_fresh >= AUTO_STALE_S
    return recheck, stale


SETTING_NAMES = {"shadow_boost": "Shadow boost", "exposure": "Exposure"}
AUTO_TICK_MS = 100      # auto levels (beta) checks for new measurements this often


MONITOR_EMOJI = "\U0001F5A5\uFE0F"   # desktop computer
MAIN_EMOJI = "\u2B50"                  # star marks the main display


def display_chip_text(m, index: int) -> str:
    """Checkbox label for a display: a monitor emoji and its number (a star on
    the main one).  The full name is in the checkbox's tooltip."""
    found = re.search(r"\d+", m.short or m.id or "")
    number = found.group(0) if found else str(index + 1)
    return f"{MONITOR_EMOJI} {number}" + (f" {MAIN_EMOJI}" if m.primary else "")


class Toast:
    """Tiny click-through popup used to confirm hotkey presses, shown at the
    top of every display CatVision is changing.

    Its windows are created once and then only made visible/invisible, so it
    never takes keyboard focus away from the game.
    """

    def __init__(self, root: tk.Tk, areas: Callable[[], list[tuple[int, int, int, int]]]):
        self.root = root
        self.areas = areas            # (left, top, right, bottom) of each display to use
        self.wins: list[tk.Toplevel] = []
        self.labels: list[tk.Label] = []
        self._hide_job = None
        self._add_window()

    def _add_window(self) -> None:
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=ACCENT)  # shows as an amber stripe on the left
        label = tk.Label(win, text="", bg=PANEL, fg=FG, padx=18, pady=10,
                         font=("Segoe UI", 16, "bold"))
        label.pack(padx=(6, 0))
        self._set_alpha(win, 0.0)
        win.geometry("+-10000+-10000")
        win.update_idletasks()
        self._make_click_through(win)
        self.wins.append(win)
        self.labels.append(label)

    @staticmethod
    def _set_alpha(win: tk.Toplevel, value: float) -> None:
        try:
            win.attributes("-alpha", value)
        except tk.TclError:
            pass

    @staticmethod
    def _make_click_through(win: tk.Toplevel) -> None:
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
            GWL_EXSTYLE = -20
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            # layered | transparent (click-through) | tool window | no-activate
            style |= 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        except Exception:
            pass

    def show(self, text: str, ms: int = 1300) -> None:
        areas = self.areas() or [winutil.window_work_area(self.root)]
        while len(self.wins) < len(areas):
            self._add_window()
        for i, win in enumerate(self.wins):
            if i >= len(areas):
                self._hide_one(win)
                continue
            self.labels[i].configure(text=text)
            win.update_idletasks()
            w = win.winfo_reqwidth()
            left, top, right, _bottom = areas[i]   # top middle of that display
            win.geometry(f"+{left + (right - left - w) // 2}+{top + 40}")
            self._set_alpha(win, 0.92)
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.hide)

    def _hide_one(self, win: tk.Toplevel) -> None:
        self._set_alpha(win, 0.0)
        win.geometry("+-10000+-10000")

    def hide(self) -> None:
        self._hide_job = None
        for win in self.wins:
            self._hide_one(win)


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
        # Auto levels (beta): measures the ticked displays on its own thread.
        self.auto_watch = autolevels.Watcher()
        self._auto_ctrl: dict[str, autolevels.Controller] = {}
        self._auto_key = ""
        self._auto_seq = -1
        self._auto_seq_at = 0.0     # when a new measurement last arrived
        self._auto_front_at = 0.0   # when the window in front was last checked
        self._auto_winding_down = False
        self._auto_gamma_state = (True, False)
        self._auto_job = None
        self._auto_fader = autolevels.Fader(self._auto_ctrl, self._fader_apply)
        self._auto_shown: dict = {}
        self._auto_watched: set[str] = set()
        self._auto_game = ""       # title of the full-screen game in front, if any
        # Force quit runs on the tray thread and only needs to put the screen back.
        self.tray = Tray(self._actions.put, on_force_quit=self.engine.restore_now)
        self._build()

        self.toast = Toast(root, self._toast_areas)
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
        self._build_auto_levels(f, row=9, wrap=wrap)


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

    # ------------------------------------------------- auto levels (beta)

    def _build_auto_levels(self, f: ttk.Frame, row: int, wrap: int) -> None:
        o = self.cfg.options
        box = ttk.Frame(f)
        box.grid(row=row, column=0, sticky="ew", pady=(14, 0))
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 2))
        ttk.Label(head, text="Auto levels", style="Head.TLabel").pack(side="left")
        badge = tk.Label(head, text="BETA", bg=ACCENT, fg="#1a1200", font=self.fonts.small,
                         padx=5)
        badge.pack(side="left", padx=(6, 0))
        Tooltip(badge, "New and still being tested. Tell StormEf4 how it works for you.")
        self.auto_keep_btn = ttk.Button(head, text="Keep this look", command=self._keep_auto_look)
        self.auto_keep_btn.pack(side="right")
        self.auto_forget_btn = ttk.Button(head, text="Forget", width=7,
                                          command=self._forget_game_look)
        Tooltip(self.auto_forget_btn, "Beta. Stop using the look you kept for this game; "
                                      "it goes back to the normal Aim for.")
        Tooltip(self.auto_keep_btn,
                "Beta. Happy with how the game looks right now? Click this and auto levels "
                "aims for this look from now on (it sets Aim for to match). Brighter or "
                "darker scenes are then adjusted from there. Each game remembers its own "
                "look (by its window title) and gets it back next time.")
        self.auto_var = tk.BooleanVar(value=o.auto_levels)
        chk = ttk.Checkbutton(box, text="Adjust to the game automatically", variable=self.auto_var,
                              command=lambda: self.set_auto_levels(self.auto_var.get()))
        chk.grid(row=1, column=0, sticky="w")
        Tooltip(chk, "Beta. Watches how dark the picture on your ticked displays is and raises "
                     "Shadow boost (or Exposure) in dark scenes and lowers it in bright ones. "
                     "It only measures brightness from a tiny copy of the screen, like "
                     "recording apps do: it never touches the game, and nothing is saved or "
                     "sent. Works best with games in borderless windowed mode.")
        sl = ttk.Frame(box)
        sl.grid(row=2, column=0, sticky="ew", pady=(2, 0))
        self._clean_auto_options()
        self.auto_vars = {
            "auto_strength": tk.DoubleVar(value=o.auto_strength),
            "auto_target": tk.DoubleVar(value=self._auto_aim()),
        }
        self.auto_rows = [
            SliderRow(sl, 0, self.auto_vars["auto_strength"], "How much",
                      *autolevels.STRENGTH_LIMITS, "int", self.fonts, self._on_auto_slider,
                      tip="Beta. How far auto levels may move Shadow boost (or Exposure) up "
                          "or down from your preset."),
            SliderRow(sl, 1, self.auto_vars["auto_target"], "Aim for",
                      *autolevels.TARGET_LIMITS, "pct", self.fonts, self._on_auto_slider,
                      tip="Beta. How bright it tries to make scenes. Higher brightens dark "
                          "scenes more; lower keeps more of the game's own darkness."),
        ]
        speed_row = ttk.Frame(box)
        speed_row.grid(row=3, column=0, sticky="w", pady=(2, 0))
        speed_label = ttk.Label(speed_row, text="Speed")
        speed_label.pack(side="left", padx=(0, 10))
        Tooltip(speed_label, "Beta. How quickly it reacts when a scene gets darker or brighter. "
                             "Flashes are always toned down much faster than other changes.")
        self.auto_speed_var = tk.StringVar(value=o.auto_speed if o.auto_speed in
                                           autolevels.SPEEDS else "normal")
        controls = [chk, self.auto_keep_btn]
        for value in autolevels.SPEEDS:
            rb = ttk.Radiobutton(speed_row, text=value.capitalize(), value=value,
                                 variable=self.auto_speed_var, command=self._on_auto_option)
            rb.pack(side="left", padx=(0, 8))
            controls.append(rb)
        self.auto_flag_vars = {
            "auto_game_only": tk.BooleanVar(value=o.auto_game_only),
            "auto_dim": tk.BooleanVar(value=o.auto_dim),
        }
        flags = (
            ("auto_game_only", "Only while a game fills the screen",
             "Beta. Pauses on the desktop and in windowed apps and goes back to your preset. "
             "Games in fullscreen or borderless windowed mode count, and so do full-screen "
             "videos. Only the position, style and title of the window in front are checked, "
             "nothing inside it. A normal maximized window with a title bar doesn't count."),
            ("auto_dim", "Also tone down bright scenes",
             "Beta. Lowers the boost in bright scenes and flashes so you're never blinded. "
             "Untick it to only ever brighten."),
        )
        for i, (key, text, tip) in enumerate(flags):
            cb = ttk.Checkbutton(box, text=text, variable=self.auto_flag_vars[key],
                                 command=self._on_auto_option)
            cb.grid(row=4 + i, column=0, sticky="w")
            Tooltip(cb, tip)
            controls.append(cb)
        self.auto_info = ttk.Label(box, text="", style="Muted.TLabel", justify="left",
                                   wraplength=wrap)
        self.auto_info.grid(row=6, column=0, sticky="ew", pady=(2, 0))
        box.bind("<Configure>", lambda e: self.auto_info.configure(
            wraplength=max(100, e.width - 4)))
        if not self.auto_watch.available:
            for c in controls:
                c.state(["disabled"])
            for r in self.auto_rows:
                r.set_enabled(False)

    def _on_auto_option(self) -> None:
        o = self.cfg.options
        o.auto_speed = self.auto_speed_var.get()
        for key, var in self.auto_flag_vars.items():
            setattr(o, key, var.get())
        self._schedule_save()
        self._render_auto_info(self._auto_shown)

    def _on_auto_slider(self) -> None:
        o = self.cfg.options
        for r in self.auto_rows:
            r.refresh()
        o.auto_strength = round(self.auto_vars["auto_strength"].get(), 3)
        aim = round(self.auto_vars["auto_target"].get(), 3)
        if self._auto_game in o.auto_game_looks:   # editing this game's own look
            o.auto_game_looks.pop(self._auto_game, None)   # newest last (see MAX_GAME_LOOKS)
            o.auto_game_looks[self._auto_game] = aim
        else:
            o.auto_target = aim
        self._schedule_save()

    def _clean_auto_options(self) -> None:
        """Make the saved auto levels settings valid (a hand-edited or damaged
        config.json), so the sliders, the controller and the next save agree."""
        o = self.cfg.options
        d = config_mod.Options()
        o.auto_strength = auto_number(o.auto_strength, *autolevels.STRENGTH_LIMITS,
                                      d.auto_strength)
        o.auto_target = auto_number(o.auto_target, *autolevels.TARGET_LIMITS, d.auto_target)
        if o.auto_speed not in autolevels.SPEEDS:
            o.auto_speed = d.auto_speed
        looks = {}
        if isinstance(o.auto_game_looks, dict):
            for title, aim in o.auto_game_looks.items():
                title = autolevels.clean_title(str(title))
                aim = auto_number(aim, *autolevels.TARGET_LIMITS, None)
                if title and aim is not None:   # "" means no game: never a look
                    looks[title] = aim
        o.auto_game_looks = looks

    def _auto_strength(self) -> float:
        """How much, always inside the slider's limits (options may have been
        changed elsewhere since the window was built)."""
        o = self.cfg.options
        return auto_number(o.auto_strength, *autolevels.STRENGTH_LIMITS,
                           config_mod.Options().auto_strength)

    def _auto_aim(self) -> float:
        """Aim for: the game's own look if it has one, otherwise the default.
        Always inside the slider's limits."""
        o = self.cfg.options
        lo, hi = autolevels.TARGET_LIMITS
        default = auto_number(o.auto_target, lo, hi, config_mod.Options().auto_target)
        return auto_number(o.auto_game_looks.get(self._auto_game), lo, hi, default)

    def _set_auto_game(self, title: str) -> None:
        """A different game (or none) is in front: show its Aim for."""
        if title == self._auto_game:
            return
        self._auto_game = title
        self.auto_vars["auto_target"].set(self._auto_aim())
        for r in self.auto_rows:
            r.refresh()

    def set_auto_levels(self, on: bool) -> None:
        self.cfg.options.auto_levels = on
        self.auto_var.set(on)
        self._schedule_save()
        self._render_status()

    def _auto_active(self) -> bool:
        return (self.cfg.options.auto_levels and self.engine.enabled
                and self.auto_watch.available)

    def _auto_sync(self) -> None:
        """Start or stop watching to match the toggle, on/off and displays."""
        if self._auto_active():
            self._auto_winding_down = False
            rects = {m.id: m.rect for m in self.engine.target_monitors() if m.rect}
            for did in list(self._auto_ctrl):
                if did not in rects:
                    del self._auto_ctrl[did]
            if not self._auto_fader.running:
                # This run's updates carry its own generation; once auto
                # levels stops, anything still in flight from it is dropped.
                gen = self.engine.auto_generation
                self._auto_fader.apply = lambda pos, gen=gen: self._fader_apply(pos, gen)
            self._auto_fader.start()
            if self._auto_job is None and not self._closed:
                self._auto_job = self.root.after(AUTO_TICK_MS, self._auto_tick)
        else:
            self.auto_watch.stop()
            if self._auto_job is not None:
                self.root.after_cancel(self._auto_job)
                self._auto_job = None
            self._auto_seq = -1
            self._auto_watched = set()
            self._auto_shown = {}
            # No game is being watched now: Aim for shows (and edits) the
            # normal value again, not the last game's kept look.
            self._set_auto_game("")
            if self.engine.enabled and any(abs(c.offset) > 0.05 for c in self._auto_ctrl.values()):
                # Auto levels was turned off while CatVision stays on: glide
                # back to the preset instead of jumping.
                for ctrl in self._auto_ctrl.values():
                    ctrl.release()
                self._auto_fader.wake()
                if not self._auto_winding_down:
                    self._auto_winding_down = True
                    self.root.after(100, self._auto_wind_down)
            else:
                self._auto_finish()
        self._render_auto_info(self._auto_shown)

    def _auto_wind_down(self) -> None:
        if not self._auto_winding_down or self._closed:
            return
        if self.engine.enabled and any(abs(c.offset) > 0.05 for c in self._auto_ctrl.values()):
            self._auto_check_gamma()
            self.root.after(100, self._auto_wind_down)
        else:
            self._auto_finish()
            self.refresh_notices()

    def _auto_finish(self) -> None:
        self._auto_winding_down = False
        self._auto_fader.stop()   # before clearing, so nothing moves the displays again
        self._auto_ctrl.clear()
        self.engine.retire_auto_offsets()
        # Remember what the displays show now, so the next run's check compares
        # against the real state (callers refresh the notices themselves).
        self._auto_gamma_state = (self.engine.status.gamma_ok, self.engine.status.gamma_limited)

    def _auto_check_gamma(self) -> None:
        """Keep the warning icon in step with whether Windows is holding the
        strength back (fades change that as they go)."""
        gamma_state = (self.engine.status.gamma_ok, self.engine.status.gamma_limited)
        if gamma_state != self._auto_gamma_state:
            self._auto_gamma_state = gamma_state
            self.refresh_notices()

    def _auto_tick(self) -> None:
        """Decide where each display should go as new measurements arrive
        (five a second).  The fader thread does the actual moving, 60 times a
        second, so the picture glides instead of stepping."""
        self._auto_job = None
        if self._closed or not self._auto_active():
            return
        o = self.cfg.options
        key = self._auto_setting()
        # The preset may switch between Shadow boost and Exposure: the
        # controllers keep their offsets and split_offset hands them to the
        # new setting, so nothing jumps.
        self._auto_key = key
        self._auto_fader.speed = autolevels.SPEEDS.get(o.auto_speed, 1.0)
        rects = {m.id: m.rect for m in self.engine.target_monitors() if m.rect}
        seq, latest = self.auto_watch.latest()
        fresh = seq != self._auto_seq
        now = time.monotonic()
        if fresh:
            self._auto_seq, self._auto_seq_at = seq, now
        recheck, stale = auto_tick_plan(now, fresh, self._auto_front_at, self._auto_seq_at)
        if recheck or not self._auto_shown:
            self._auto_front_at = now
            # Only adjust displays a game fills; elsewhere drift back to the preset.
            in_front, title = autolevels.window_in_front(rects)
            if title != autolevels.OWN_WINDOW:   # settings open: keep the game's look
                self._set_auto_game(title if in_front else "")
            if not o.auto_game_only:
                in_front = None
            self._auto_watched = (set(rects) if in_front is None else set(rects) & in_front)
            self.auto_watch.watch({d: rects[d] for d in self._auto_watched})
        shown: dict[str, tuple[str, object]] = {}
        goals = {did: c.want for did, c in self._auto_ctrl.items()}
        strength, aim = self._auto_strength(), self._auto_aim()
        for did in rects:
            ctrl = self._auto_ctrl.setdefault(did, autolevels.Controller())
            if did not in self._auto_watched:
                ctrl.release()
                shown[did] = ("waiting", None)
            elif stale and did in latest:
                # The capture has stalled: let the controller's lost-picture
                # logic hold, then ease back to the preset.
                ctrl.observe(None, strength, aim, o.auto_dim)
                shown[did] = ("stalled", latest[did])
            elif did in latest:
                if fresh:
                    ctrl.observe(latest[did], strength, aim, o.auto_dim)
                shown[did] = ("watching", latest[did])
            else:
                shown[did] = ("starting", None)
        for did in list(self._auto_ctrl):
            if did not in rects:
                del self._auto_ctrl[did]
        if any(c.want != goals.get(did) for did, c in self._auto_ctrl.items()):
            self._auto_fader.wake()   # a new goal (a flash, say): start moving now
        # Fades change whether Windows is holding the strength back: keep the
        # warning icon in step with what the displays really show.
        self._auto_check_gamma()
        # Redraw on new pictures and state changes, and keep redrawing while
        # a display eases back, so the readout never shows a stale boost.
        easing = any(state in ("waiting", "stalled") and abs(self._auto_ctrl[did].offset) > 0.05
                     for did, (state, _m) in shown.items() if did in self._auto_ctrl)
        if fresh or stale or easing or shown != self._auto_shown:
            self._auto_shown = shown
            self._render_auto_info(shown)
        self._auto_job = self.root.after(AUTO_TICK_MS, self._auto_tick)

    def _fader_apply(self, positions: dict[str, float], generation: int | None = None) -> None:
        """Runs on the fader thread: share each display's offset between
        Shadow boost and Exposure and pass it to the engine (which is
        thread-safe) only when it actually changed."""
        key = self._auto_key or self._auto_setting()
        settings = self.engine.settings
        base = {k: getattr(settings, k) for k in ("shadow_boost", "exposure")}
        offsets: dict[str, dict[str, float]] = {}
        for did, offset in positions.items():
            amounts = {k: round(v, 2) for k, v in
                       autolevels.split_offset(key, offset, base).items()}
            amounts = {k: v for k, v in amounts.items() if v}
            if amounts or did in self.engine.auto_offsets:
                offsets[did] = amounts
        if offsets != self.engine.auto_offsets:
            self.engine.set_auto_offsets(offsets, generation=generation)

    def _keep_auto_look(self, quiet: bool = False) -> str:
        """Make auto levels aim for how the game looks right now."""
        o = self.cfg.options
        if not o.auto_levels:
            msg = "Auto levels (beta) is off: turn it on first."
        elif not self.engine.enabled:
            msg = "Auto levels (beta): turn CatVision on first."
        else:
            msg = "Auto levels (beta): no game picture yet, so there's no look to keep."
        if self._auto_active():
            # Only a picture that is live right now is "this look": not one
            # that stopped arriving (the display has eased back) or one that
            # isn't changing (paused, or exclusive fullscreen).
            shown = self._auto_shown
            if time.monotonic() - self._auto_seq_at >= AUTO_STALE_S:
                msg = ("Auto levels (beta): can't see the picture right now, "
                       "so there's no look to keep.")
                shown = {}
            for did, (state, m) in shown.items():
                ctrl = self._auto_ctrl.get(did)
                if state != "watching" or m is None or ctrl is None:
                    continue
                if m.black:
                    msg = ("Auto levels (beta): the screen is black, "
                           "so there's no look to keep.")
                    continue
                if ctrl.frozen:
                    msg = ("Auto levels (beta): the picture isn't changing, "
                           "so there's no look to keep.")
                    continue
                lo, hi = autolevels.TARGET_LIMITS
                aim = autolevels.aim_for_current_look(m.key, ctrl.offset, self._auto_strength())
                aim = round(min(hi, max(lo, aim)), 3)
                if self._auto_game:     # remembered for this game only
                    o.auto_game_looks.pop(self._auto_game, None)   # newest last
                    o.auto_game_looks[self._auto_game] = aim
                    where = f" for {self._auto_game}"
                else:
                    o.auto_target = aim
                    where = ""
                self.auto_vars["auto_target"].set(aim)
                for r in self.auto_rows:
                    r.refresh()
                self._schedule_save()
                self._render_auto_info(self._auto_shown)
                msg = f"Auto levels (beta): keeping this look{where} (Aim for {aim * 100:.0f}%)"
                break
        if not quiet:
            self.set_status(msg)
        return msg

    def _nudge_auto_aim(self, brighter: bool) -> str:
        """Aim brighter / darker from a hotkey, without leaving the game.
        Changes the game's own kept look if it has one."""
        o = self.cfg.options
        aim = autolevels.nudge_aim(self._auto_aim(), brighter)
        if self._auto_game in o.auto_game_looks:
            o.auto_game_looks.pop(self._auto_game, None)   # newest last
            o.auto_game_looks[self._auto_game] = aim
        else:
            o.auto_target = aim
        self.auto_vars["auto_target"].set(aim)
        for r in self.auto_rows:
            r.refresh()
        self._schedule_save()
        word = "brighter" if brighter else "darker"
        note = "" if o.auto_levels else " (auto levels is off)"
        return f"Auto levels (beta): aim {word} ({aim * 100:.0f}%){note}"

    def _forget_game_look(self) -> None:
        o = self.cfg.options
        if o.auto_game_looks.pop(self._auto_game, None) is not None:
            self.auto_vars["auto_target"].set(self._auto_aim())
            for r in self.auto_rows:
                r.refresh()
            self._schedule_save()
            self.set_status(f"Auto levels (beta): {self._auto_game} uses the normal Aim for again.")
        self._render_auto_info(self._auto_shown)

    def _auto_setting(self) -> str:
        """Auto levels moves whichever of Shadow boost / Exposure the preset
        leans on more (Exposure for night vision, Shadow boost otherwise)."""
        s = self.engine.settings
        return "exposure" if s.exposure > s.shadow_boost else "shadow_boost"

    def _render_auto_info(self, shown: dict) -> None:
        if not hasattr(self, "auto_info"):
            return
        o = self.cfg.options
        kept = self._auto_game in o.auto_game_looks
        if kept and self._auto_active():
            self.auto_forget_btn.pack(side="right", padx=(0, 6))
        else:
            self.auto_forget_btn.pack_forget()
        if not self.auto_watch.available:
            text = self.auto_watch.reason
        elif not o.auto_levels:
            text = "Off."
        elif not self.engine.enabled:
            text = "Starts when CatVision is on."
        elif not shown:
            text = "Watching..."
        else:
            key = self._auto_setting()
            names = {m.id: m.short or m.label for m in self.engine.monitors()}
            lines = []
            if self._auto_game:
                lines.append(f"Game: {self._auto_game}"
                             + ("  \u00b7  its own kept look" if kept else ""))
            for did, (state, m) in shown.items():
                name = names.get(did, did)
                base = getattr(self.engine.settings, key)
                now = getattr(self.engine.settings_for(did), key)
                change = f"{SETTING_NAMES[key]} {base:.0f} \u2192 {now:.0f}"
                spilled = [k for k, v in self.engine.auto_offsets.get(did, {}).items()
                           if k != key and v]
                if spilled:  # the main slider is at its limit: say where the rest went
                    eff = self.engine.settings_for(did)
                    change = ", ".join(f"{SETTING_NAMES[k]} {getattr(eff, k):.0f}"
                                       for k in [key] + spilled)
                back = "" if abs(now - base) < 0.5 else f"  \u00b7  easing back to {base:.0f}"
                if state == "waiting":
                    lines.append(f"{name}: waiting for a full-screen game{back}")
                elif state == "stalled":
                    lines.append(f"{name}: can't see the picture right now{back}")
                elif state == "starting":
                    lines.append(f"{name}: starting...")
                elif m is None:
                    lines.append(f"{name}: can't see the picture. Try borderless windowed mode.")
                elif self._auto_ctrl.get(did) and self._auto_ctrl[did].frozen:
                    gone = ("back to your preset" if self._auto_ctrl[did].want == 0.0
                            else "holding for now")
                    lines.append(f"{name}: the picture isn't changing (paused, or exclusive "
                                 f"fullscreen?)  \u00b7  {gone}")
                else:
                    held = ""
                    if m.black:
                        ctrl = self._auto_ctrl.get(did)
                        held = ("  (black for a while: back to your preset)"
                                if ctrl is not None and ctrl.black_too_long
                                else "  (black screen: holding)")
                    lines.append(f"{name}: {m.key * 100:.0f}% bright  \u00b7  {change}{held}")
            text = "\n".join(lines)
        self.auto_info.configure(text=text)

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
        if not dialogs.ask_yes_no(self.root, 
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
        if not dialogs.ask_yes_no(self.root, "Undo full strength",
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
            dialogs.show_info(self.root, "CatVision", "Done. Restart your PC to finish unlocking "
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
        pattern = ttk.Button(f, text="Test pattern...", style="Accent.TButton",
                             command=lambda: open_test_pattern(self))
        pattern.grid(row=0, column=1, sticky="e")
        Tooltip(pattern, "An image to test your monitors levels. Raise Shadow Boost and Exposure "
                         "levels to your satisfaction. Since everyone's monitor is different, my "
                         "defaults may not be satisfactory.")
        ttk.Label(f, text="Changes show instantly. Click Save to keep them in this preset.",
                  style="Muted.TLabel").grid(row=1, column=0, columnspan=2, sticky="w",
                                             pady=(2, 6))

        sl = ttk.Frame(f)
        sl.grid(row=2, column=0, columnspan=2, sticky="ew")
        row = 0
        for key, label, kind in CURVE_SLIDERS + COLOR_SLIDERS:
            if key == "saturation":
                ttk.Separator(sl).grid(row=row, column=0, columnspan=3, sticky="ew", pady=6)
                row += 1
            self.sliders.append(SliderRow(sl, row, self.vars[key], label, *LIMITS[key], kind,
                                          self.fonts, self._on_change, tip=TIPS.get(key, "")))
            row += 1

        bottom = ttk.Frame(f)
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
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
        else:
            self.preset_combo.set(self.cfg.active_preset)
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
        name = dialogs.ask_string(self.root, title, prompt, initial)
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
            dialogs.show_info(self.root, "CatVision", "That name belongs to a built-in preset. "
                                "Use Save to change the built-in one, or pick another name.")
            return
        if name in self.cfg.custom_presets and not dialogs.ask_yes_no(self.root, 
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
            dialogs.show_info(self.root, "CatVision", "A preset with that name already exists.")
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
            dialogs.show_info(self.root, "CatVision", "Built-in presets can't be renamed. "
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
                dialogs.show_info(self.root, "CatVision", "This built-in preset is already at its default.")
                return
            if dialogs.ask_yes_no(self.root, "CatVision", f"Put '{row.name}' back to its original settings?"):
                self.cfg.custom_presets.pop(row.name, None)
                self.refresh_presets()
                if self.cfg.active_preset == row.name:
                    self._activate_preset(self._preset_index(row.name))
                self._schedule_save()
            return
        if not dialogs.ask_yes_no(self.root, "CatVision", f"Delete your preset '{row.name}'?"):
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
        if on and self._auto_active():
            text += " Auto levels (beta) is adjusting."
        self.set_status(text)
        self._auto_sync()
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
        if self.engine.enabled and self._auto_active():
            state += "  \u00b7  auto (beta)"
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
            auto_levels=self.cfg.options.auto_levels,
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
        elif action == "auto_levels":
            self.set_auto_levels(not self.cfg.options.auto_levels)
            if self.cfg.options.auto_levels and not self.engine.enabled:
                self._set_enabled(True)
            msg = f"Auto levels (beta) {'ON' if self.cfg.options.auto_levels else 'OFF'}"
        elif action == "auto_keep":
            msg = self._keep_auto_look(quiet=True)
        elif action in ("auto_brighter", "auto_darker"):
            msg = self._nudge_auto_aim(action == "auto_brighter")
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

    def _toast_areas(self) -> list[tuple[int, int, int, int]]:
        """Where the hotkey popup goes: every display CatVision is changing.
        (Unlike other pop-ups, which open on the app's own display.)"""
        rects = [m.rect for m in self.engine.target_monitors()]
        if not rects or any(r is None for r in rects):
            return [winutil.window_work_area(self.root)]
        return rects

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
        self.auto_watch.stop()
        self._auto_fader.stop()
        self.engine.shutdown()
        self._cancel_after_jobs()
        self.root.destroy()

    def _cancel_after_jobs(self) -> None:
        """Drop every pending Tk timer (auto levels tick, save, polling...)
        so nothing runs against the destroyed window."""
        self._auto_job = self._save_job = self._apply_job = None
        try:
            jobs = self.root.tk.splitlist(self.root.tk.call("after", "info"))
        except tk.TclError:
            return
        for job in jobs:
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass


class _NoColor:
    """Color backend stand-in for a gamma-only reset (the engine owns color)."""

    def reset(self):
        return True

    def close(self):
        pass
