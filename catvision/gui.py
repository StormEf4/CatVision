"""CatVision window (tkinter, no extra dependencies)."""

from __future__ import annotations

import queue
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, simpledialog, ttk

from . import config as config_mod
from . import curves
from .curves import COLOR_FILTERS, LIMITS, Settings
from .engine import Engine
from .hotkeys import HOTKEY_HELP, HotkeyListener, default_hotkeys
from .presets import BUILTIN_PRESETS

IS_WINDOWS = sys.platform == "win32"

BG = "#14161b"
PANEL = "#1f232b"
FG = "#f4f4f4"
MUTED = "#a9b0bc"
ACCENT = "#4fc3f7"
ON = "#2fbf71"
OFF = "#3a3f4b"
WARN = "#ffcc66"

TEXT_SIZES = {"Normal": 10, "Large": 12, "Extra large": 15}

CURVE_SLIDERS = [
    ("shadow_boost", "Shadow boost", "int"),
    ("gamma", "Gamma (midtones)", "x"),
    ("black_floor", "Black floor", "pct"),
    ("brightness", "Brightness", "pct"),
    ("contrast", "Contrast", "pct"),
    ("white_cap", "Max brightness (glare limit)", "pct"),
]
COLOR_SLIDERS = [
    ("saturation", "Color saturation", "pct"),
    ("filter_strength", "Filter strength", "pct"),
    ("red", "Red", "pct"),
    ("green", "Green", "pct"),
    ("blue", "Blue", "pct"),
]

BOOST_STEP = 10
KEEP_APPLIED_MS = 2000


def _fmt(kind: str, value: float) -> str:
    if kind == "int":
        return f"{value:.0f}"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    return f"{value:.2f}"


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
        self.win.configure(bg=PANEL)
        self.label = tk.Label(self.win, text="", bg=PANEL, fg=FG, padx=18, pady=10,
                              font=("Segoe UI", 16, "bold"))
        self.label.pack()
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
    def __init__(self, root: tk.Tk, engine: Engine, cfg: config_mod.Config):
        self.root = root
        self.engine = engine
        self.cfg = cfg
        self.vars: dict[str, tk.DoubleVar] = {}
        self.value_labels: dict[str, tk.Label] = {}
        self._kinds: dict[str, str] = {}
        self._loading = False
        self._modified = False
        self._apply_job = None
        self._save_job = None
        self._actions: queue.Queue[str] = queue.Queue()
        self._preset_rows: list[tuple[str, Settings, bool]] = []
        self._closed = False

        root.title("CatVision - see in the dark")
        root.configure(bg=BG)
        root.minsize(900, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self._on_error

        # Canvas sizes are in pixels, so scale them on high-DPI screens.
        self.px = max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))
        self._setup_fonts(cfg.options.text_size)
        self._setup_style()
        self._build()

        self.toast = Toast(root)
        self.hotkeys = HotkeyListener(default_hotkeys(), self._actions.put)
        if cfg.options.hotkeys:
            self._start_hotkeys()

        engine.monitor_choice = cfg.options.monitor
        self._refresh_presets(select=cfg.active_preset)
        self._load_settings(cfg.current)
        self._modified = self._differs_from_preset()
        self._update_title()
        if cfg.options.enable_on_start:
            self._set_enabled(True)
        else:
            self._render_status()

        root.after(50, self._poll_actions)
        root.after(KEEP_APPLIED_MS, self._keep_applied)

    # ------------------------------------------------------------------ setup

    def _setup_fonts(self, size_name: str) -> None:
        base = TEXT_SIZES.get(size_name, 12)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=base)
            except tk.TclError:
                pass
        self.font = tkfont.nametofont("TkDefaultFont")
        self.font_bold = self.font.copy()
        self.font_bold.configure(weight="bold")
        self.font_big = self.font.copy()
        self.font_big.configure(size=int(base * 1.4), weight="bold")
        self.font_small = self.font.copy()
        self.font_small.configure(size=max(8, base - 2))
        self.base_size = base

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabelframe", background=BG, foreground=ACCENT, bordercolor=OFF)
        style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=self.font_bold)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=self.font_small)
        style.configure("TCheckbutton", background=BG, foreground=FG)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TButton", background=OFF, foreground=FG, padding=6)
        style.map("TButton", background=[("active", "#4a5160")])
        style.configure("TCombobox", fieldbackground=PANEL, background=OFF, foreground=FG,
                        arrowcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
                  foreground=[("readonly", FG)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.font", self.font)
        style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)

    def _build(self) -> None:
        root = self.root
        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        self.toggle_btn = tk.Button(
            top, command=self._toggle, font=self.font_big, relief="flat", bd=0,
            padx=18, pady=8, cursor="hand2", activeforeground=FG)
        self.toggle_btn.pack(side="left")
        self.title_label = ttk.Label(top, text="", font=self.font_big)
        self.title_label.pack(side="left", padx=16)

        body = ttk.Frame(root, padding=(12, 0, 12, 6))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # --- presets -------------------------------------------------------
        left = ttk.LabelFrame(body, text="Presets", padding=8)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        self.preset_list = tk.Listbox(
            left, width=28, activestyle="none", exportselection=False,
            bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="#000000",
            highlightthickness=0, bd=0, font=self.font)
        self.preset_list.pack(fill="both", expand=True)
        self.preset_list.bind("<<ListboxSelect>>", self._on_preset_select)
        self.preset_desc = ttk.Label(left, text="", style="Muted.TLabel",
                                     wraplength=self.base_size * 22, justify="left")
        self.preset_desc.pack(fill="x", pady=(6, 6))
        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="Save as...", command=self._save_preset).pack(side="left")
        ttk.Button(btns, text="Delete", command=self._delete_preset).pack(side="left", padx=4)
        ttk.Button(btns, text="Undo changes", command=self._revert).pack(side="left")

        # --- sliders -------------------------------------------------------
        mid = ttk.Frame(body)
        mid.grid(row=0, column=1, sticky="nsew")
        mid.columnconfigure(0, weight=1)
        curve_box = ttk.LabelFrame(mid, text="Brightness", padding=8)
        curve_box.grid(row=0, column=0, sticky="ew")
        for row, (key, label, kind) in enumerate(CURVE_SLIDERS):
            self._slider(curve_box, row, key, label, kind)

        color_box = ttk.LabelFrame(mid, text="Color", padding=8)
        color_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(color_box, text="Color filter").grid(row=0, column=0, sticky="w", pady=3)
        self.filter_var = tk.StringVar(value=COLOR_FILTERS["none"])
        self.filter_combo = ttk.Combobox(
            color_box, textvariable=self.filter_var, state="readonly",
            values=list(COLOR_FILTERS.values()), font=self.font)
        self.filter_combo.grid(row=0, column=1, columnspan=2, sticky="ew", padx=8)
        self.filter_combo.bind("<<ComboboxSelected>>", lambda e: self._on_change())
        for row, (key, label, kind) in enumerate(COLOR_SLIDERS, start=1):
            self._slider(color_box, row, key, label, kind)

        # --- preview -------------------------------------------------------
        right = ttk.Frame(body)
        right.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        graph_box = ttk.LabelFrame(right, text="Brightness curve", padding=8)
        graph_box.pack(fill="x")
        self.graph = tk.Canvas(graph_box, width=self._px(230), height=self._px(230), bg=PANEL,
                               highlightthickness=0)
        self.graph.pack()
        ttk.Label(graph_box, text="Left = dark input, up = brighter output",
                  style="Muted.TLabel").pack(anchor="w")

        test_box = ttk.LabelFrame(right, text="Test pattern", padding=8)
        test_box.pack(fill="x", pady=(8, 0))
        self.pattern = tk.Canvas(test_box, width=self._px(230), height=self._px(96), bg="#000000",
                                 highlightthickness=0)
        self.pattern.pack()
        ttk.Label(test_box, text="Turn CatVision on and raise Shadow\nboost until every dark square shows.",
                  style="Muted.TLabel", justify="left").pack(anchor="w")
        self._draw_pattern()

        # --- options -------------------------------------------------------
        opts = ttk.LabelFrame(root, text="Options", padding=8)
        opts.pack(fill="x", padx=12, pady=(0, 6))
        o = self.cfg.options
        self.opt_vars = {
            "keep_applied": tk.BooleanVar(value=o.keep_applied),
            "hotkeys": tk.BooleanVar(value=o.hotkeys),
            "hotkey_popup": tk.BooleanVar(value=o.hotkey_popup),
            "enable_on_start": tk.BooleanVar(value=o.enable_on_start),
        }
        texts = {
            "keep_applied": "Keep applied (fixes games that reset brightness)",
            "hotkeys": "Global hotkeys",
            "hotkey_popup": "Show popup on hotkey",
            "enable_on_start": "Turn on when CatVision starts",
        }
        for i, (key, var) in enumerate(self.opt_vars.items()):
            ttk.Checkbutton(opts, text=texts[key], variable=var,
                            command=lambda k=key: self._on_option(k)).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 20), pady=2)

        ttk.Label(opts, text="Display").grid(row=0, column=2, sticky="e", padx=(10, 6))
        self._monitor_ids = ["all"] + [m.id for m in self.engine.monitors()]
        labels = ["All displays"] + [m.label for m in self.engine.monitors()]
        self.monitor_combo = ttk.Combobox(opts, values=labels, state="readonly", width=30, font=self.font)
        choice = o.monitor if o.monitor in self._monitor_ids else "all"
        self.monitor_combo.current(self._monitor_ids.index(choice))
        self.monitor_combo.grid(row=0, column=3, sticky="w")
        self.monitor_combo.bind("<<ComboboxSelected>>", self._on_monitor)

        ttk.Label(opts, text="Text size").grid(row=1, column=2, sticky="e", padx=(10, 6))
        self.size_combo = ttk.Combobox(opts, values=list(TEXT_SIZES), state="readonly", width=12,
                                       font=self.font)
        self.size_combo.set(o.text_size if o.text_size in TEXT_SIZES else "Large")
        self.size_combo.grid(row=1, column=3, sticky="w")
        self.size_combo.bind("<<ComboboxSelected>>", self._on_text_size)

        help_text = "    ".join(f"{k}: {v}" for k, v in HOTKEY_HELP)
        self.hotkey_label = ttk.Label(root, text="Hotkeys -  " + help_text, style="Muted.TLabel",
                                      wraplength=1100, justify="left")
        self.hotkey_label.pack(fill="x", padx=12)

        self.status = tk.Label(root, text="", anchor="w", bg=PANEL, fg=FG, padx=12, pady=6,
                               font=self.font)
        self.status.pack(fill="x", side="bottom")

    def _slider(self, parent, row: int, key: str, label: str, kind: str) -> None:
        lo, hi = LIMITS[key]
        var = tk.DoubleVar(value=getattr(Settings(), key))
        self.vars[key] = var
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        scale = ttk.Scale(parent, from_=lo, to=hi, variable=var, orient="horizontal",
                          command=lambda _v, k=key: self._on_slider(k))
        scale.grid(row=row, column=1, sticky="ew", padx=8)
        parent.columnconfigure(1, weight=1)
        val = tk.Label(parent, text="", width=6, anchor="e", bg=BG, fg=ACCENT, font=self.font_bold)
        val.grid(row=row, column=2, sticky="e")
        self.value_labels[key] = val
        self._kinds[key] = kind

    def _px(self, n: float) -> int:
        return int(round(n * self.px))

    # --------------------------------------------------------------- presets

    def _all_presets(self) -> list[tuple[str, Settings, bool]]:
        rows = [(p.name, p.settings, True) for p in BUILTIN_PRESETS]
        rows += [(name, s, False) for name, s in sorted(self.cfg.custom_presets.items())]
        return rows

    def _refresh_presets(self, select: str | None = None) -> None:
        self._preset_rows = self._all_presets()
        self.preset_list.delete(0, "end")
        for i, (name, _s, builtin) in enumerate(self._preset_rows):
            prefix = f"{i + 1}  " if i < 9 else "    "
            self.preset_list.insert("end", prefix + name + ("" if builtin else "  (mine)"))
        idx = self._preset_index(select)
        if idx is not None:
            self.preset_list.selection_clear(0, "end")
            self.preset_list.selection_set(idx)
            self.preset_list.see(idx)
            self._show_description(idx)

    def _preset_index(self, name: str | None) -> int | None:
        for i, (n, _s, _b) in enumerate(self._preset_rows):
            if n == name:
                return i
        return None

    def _show_description(self, idx: int) -> None:
        name, _s, builtin = self._preset_rows[idx]
        desc = "Your saved preset."
        if builtin:
            desc = next(p.description for p in BUILTIN_PRESETS if p.name == name)
        self.preset_desc.configure(text=desc)

    def _on_preset_select(self, _event=None) -> None:
        sel = self.preset_list.curselection()
        if sel:
            self._activate_preset(sel[0], from_list=True)

    def _activate_preset(self, idx: int, from_list: bool = False) -> None:
        if not 0 <= idx < len(self._preset_rows):
            return
        name, settings, _builtin = self._preset_rows[idx]
        if not from_list:
            self.preset_list.selection_clear(0, "end")
            self.preset_list.selection_set(idx)
            self.preset_list.see(idx)
        self._show_description(idx)
        self.cfg.active_preset = name
        self._load_settings(settings)
        self._modified = False
        self._update_title()
        self._apply_now()

    def _active_preset_settings(self) -> Settings | None:
        idx = self._preset_index(self.cfg.active_preset)
        return None if idx is None else self._preset_rows[idx][1]

    def _differs_from_preset(self) -> bool:
        base = self._active_preset_settings()
        return base is None or base.clamped() != self._read_settings()

    def _save_preset(self) -> None:
        name = simpledialog.askstring("Save preset", "Name for this preset:", parent=self.root)
        if not name:
            return
        name = name.strip()[:60]
        if not name:
            return
        if any(p.name == name for p in BUILTIN_PRESETS):
            messagebox.showinfo("CatVision", "That name is used by a built-in preset. Pick another.")
            return
        if name in self.cfg.custom_presets and not messagebox.askyesno(
                "CatVision", f"Replace your preset '{name}'?"):
            return
        self.cfg.custom_presets[name] = self._read_settings()
        self.cfg.active_preset = name
        self._modified = False
        self._refresh_presets(select=name)
        self._update_title()
        self._schedule_save()

    def _delete_preset(self) -> None:
        sel = self.preset_list.curselection()
        if not sel:
            return
        name, _s, builtin = self._preset_rows[sel[0]]
        if builtin:
            messagebox.showinfo("CatVision", "Built-in presets can't be deleted.")
            return
        if messagebox.askyesno("CatVision", f"Delete your preset '{name}'?"):
            del self.cfg.custom_presets[name]
            self._refresh_presets(select=BUILTIN_PRESETS[0].name)
            self._schedule_save()

    def _revert(self) -> None:
        base = self._active_preset_settings()
        if base is not None:
            self._load_settings(base)
            self._modified = False
            self._update_title()
            self._apply_now()

    # -------------------------------------------------------------- settings

    def _load_settings(self, s: Settings) -> None:
        self._loading = True
        try:
            for key, var in self.vars.items():
                var.set(getattr(s, key))
            self.filter_var.set(COLOR_FILTERS.get(s.color_filter, COLOR_FILTERS["none"]))
        finally:
            self._loading = False
        self._refresh_values()

    def _read_settings(self) -> Settings:
        data = {key: var.get() for key, var in self.vars.items()}
        data["shadow_boost"] = round(data["shadow_boost"])
        for key in data:
            if key != "shadow_boost":
                data[key] = round(data[key], 2)
        label = self.filter_var.get()
        data["color_filter"] = next(
            (k for k, v in COLOR_FILTERS.items() if v == label), "none")
        return Settings.from_dict(data)

    def _refresh_values(self) -> None:
        s = self._read_settings()
        for key, lab in self.value_labels.items():
            lab.configure(text=_fmt(self._kinds[key], getattr(s, key)))
        self._draw_graph(s)

    def _on_slider(self, _key: str) -> None:
        if not self._loading:
            self._on_change()

    def _on_change(self) -> None:
        self._refresh_values()
        if not self._modified:
            self._modified = True
            self._update_title()
        self._schedule_apply()

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

    # --------------------------------------------------------------- on / off

    def _toggle(self) -> None:
        self._set_enabled(not self.engine.enabled)

    def _set_enabled(self, enabled: bool) -> None:
        self.cfg.current = self._read_settings()
        self.engine.settings = self.cfg.current
        self.engine.set_enabled(enabled)
        self._render_status()

    def _render_status(self) -> None:
        on = self.engine.enabled
        self.toggle_btn.configure(
            text="  ON  " if on else "  OFF  ",
            bg=ON if on else OFF, activebackground=ON if on else OFF, fg=FG)
        st = self.engine.status
        warn = on and (not st.gamma_ok or st.gamma_limited or (st.color_used and not st.color_ok)
                       or bool(st.messages))
        self.status.configure(text=st.summary(), fg=WARN if warn else FG)

    def _update_title(self) -> None:
        text = self.cfg.active_preset + ("  (changed)" if self._modified else "")
        self.title_label.configure(text=text)

    # ---------------------------------------------------------------- drawing

    def _draw_graph(self, s: Settings) -> None:
        c = self.graph
        c.delete("all")
        w = h = self._px(230)
        pad = self._px(8)
        size = w - 2 * pad
        for i in range(5):
            p = pad + size * i / 4
            c.create_line(pad, p, w - pad, p, fill="#2c313b")
            c.create_line(p, pad, p, h - pad, fill="#2c313b")
        c.create_line(pad, h - pad, w - pad, pad, fill="#555c69", dash=(3, 3))
        ramp = curves.build_ramp(s)
        colors = ("#ff6b6b", "#5ee37a", "#5aa9ff")
        same = ramp[0] == ramp[1] == ramp[2]
        for ch, color in zip(ramp if not same else ramp[:1], colors if not same else ("#ffffff",)):
            pts = []
            for i in range(0, 256, 3):
                pts += [pad + size * i / 255, h - pad - size * ch[i] / 65535]
            pts += [pad + size, h - pad - size * ch[255] / 65535]
            c.create_line(*pts, fill=color, width=2, smooth=True)

    def _draw_pattern(self) -> None:
        c = self.pattern
        c.delete("all")
        n = 10
        p = self._px
        cell = p(230) / n
        for i in range(n):
            v = 1 + i * 3          # very dark greys: 1..28 out of 255
            c.create_rectangle(i * cell + p(2), p(4), (i + 1) * cell - p(2), p(44),
                               fill=f"#{v:02x}{v:02x}{v:02x}", outline="")
            v = 255 - (n - 1 - i) * 3  # very bright greys: 228..255
            c.create_rectangle(i * cell + p(2), p(52), (i + 1) * cell - p(2), p(92),
                               fill=f"#{v:02x}{v:02x}{v:02x}", outline="")

    # ---------------------------------------------------------------- options

    def _on_option(self, key: str) -> None:
        value = self.opt_vars[key].get()
        setattr(self.cfg.options, key, value)
        if key == "hotkeys":
            if value:
                self._start_hotkeys()
            else:
                self.hotkeys.stop()
        self._schedule_save()

    def _start_hotkeys(self) -> None:
        if not self.hotkeys.supported:
            return
        self.hotkeys.start()
        if self.hotkeys.failed:
            self.root.after(500, lambda: self.status.configure(
                text="Some hotkeys are used by another program: " + ", ".join(self.hotkeys.failed),
                fg=WARN))

    def _on_monitor(self, _event=None) -> None:
        choice = self._monitor_ids[self.monitor_combo.current()]
        self.cfg.options.monitor = choice
        self.engine.set_monitor_choice(choice)
        self._render_status()
        self._schedule_save()

    def _on_text_size(self, _event=None) -> None:
        self.cfg.options.text_size = self.size_combo.get()
        self._schedule_save()
        messagebox.showinfo("CatVision", "The new text size will be used next time CatVision starts.")

    # ---------------------------------------------------------------- hotkeys

    def _poll_actions(self) -> None:
        try:
            while True:
                self._handle_action(self._actions.get_nowait())
        except queue.Empty:
            pass
        if not self._closed:
            self.root.after(50, self._poll_actions)

    def _handle_action(self, action: str) -> None:
        msg = None
        if action == "toggle":
            self._set_enabled(not self.engine.enabled)
            msg = f"CatVision ON - {self.cfg.active_preset}" if self.engine.enabled else "CatVision OFF"
        elif action == "off":
            self._set_enabled(False)
            msg = "CatVision OFF"
        elif action.startswith("preset:"):
            idx = int(action.split(":")[1]) - 1
            if idx < len(self._preset_rows):
                self._activate_preset(idx)
                self._set_enabled(True)
                msg = f"{idx + 1}  {self.cfg.active_preset}"
        elif action in ("next", "prev"):
            cur = self._preset_index(self.cfg.active_preset)
            cur = -1 if cur is None else cur
            step = 1 if action == "next" else -1
            idx = (cur + step) % len(self._preset_rows)
            self._activate_preset(idx)
            self._set_enabled(True)
            msg = self.cfg.active_preset
        elif action in ("boost_up", "boost_down"):
            var = self.vars["shadow_boost"]
            step = BOOST_STEP if action == "boost_up" else -BOOST_STEP
            var.set(min(100, max(0, round(var.get()) + step)))
            self._on_change()
            if not self.engine.enabled:
                self._set_enabled(True)
            msg = f"Shadow boost {round(var.get())}"
        if msg and self.cfg.options.hotkey_popup:
            self.toast.show(msg)

    # ---------------------------------------------------------- housekeeping

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
        traceback.print_exception(exc_type, exc, tb)
        self.status.configure(text=f"Error: {exc}", fg=WARN)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.cfg.current = self._read_settings()
        self._save()
        self.hotkeys.stop()
        self.engine.shutdown()
        self.root.destroy()
