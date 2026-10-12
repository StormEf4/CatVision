"""Colors, fonts and small shared widgets for the CatVision window."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk
from typing import Callable

BG = "#121317"
PANEL = "#1d1f25"
RAISED = "#262932"
FG = "#f4f4f4"
MUTED = "#a9b0bc"
ACCENT = "#f5a524"      # cat-eye amber, matches the logo
ACCENT_DARK = "#c47d0e"
ON = "#2fbf71"
OFF = "#3a3f4b"
WARN = "#ffcc66"
GRID = "#2c313b"

TEXT_SIZES = {"Normal": 10, "Large": 12, "Extra large": 15}


def fmt(kind: str, value: float) -> str:
    if kind == "int":
        return f"{value:.0f}"
    if kind == "pct":
        return f"{value * 100:.0f}%"
    return f"{value:.2f}"


class Fonts:
    """Named fonts that can be resized live (text size option)."""

    def __init__(self, root: tk.Misc, size_name: str):
        self.normal = tkfont.nametofont("TkDefaultFont")
        self.bold = self.normal.copy()
        self.big = self.normal.copy()
        self.small = self.normal.copy()
        self.resize(size_name)

    def resize(self, size_name: str) -> None:
        base = TEXT_SIZES.get(size_name, 12)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(size=base)
            except tk.TclError:
                pass
        self.bold.configure(size=base, weight="bold")
        self.big.configure(size=int(base * 1.4), weight="bold")
        self.small.configure(size=max(8, base - 2))
        self.base = base


def setup_style(root: tk.Tk, fonts: Fonts) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL,
                    bordercolor=OFF, lightcolor=PANEL, darkcolor=PANEL)
    style.configure("TFrame", background=BG)
    style.configure("TLabelframe", background=BG, bordercolor=OFF)
    style.configure("TLabelframe.Label", background=BG, foreground=ACCENT, font=fonts.bold)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=fonts.small)
    style.configure("Head.TLabel", background=BG, foreground=FG, font=fonts.bold)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.map("TCheckbutton", background=[("active", BG)])
    style.configure("TRadiobutton", background=BG, foreground=FG)
    style.map("TRadiobutton", background=[("active", BG)])
    style.configure("TButton", background=OFF, foreground=FG, padding=(8, 4))
    style.map("TButton", background=[("active", "#4a5160"), ("disabled", BG)],
              foreground=[("disabled", "#5d6370")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#1a1200", font=fonts.bold)
    style.map("Accent.TButton", background=[("active", "#ffb940")])
    style.configure("Chip.TCheckbutton", background=RAISED, foreground=FG, padding=(8, 4))
    style.map("Chip.TCheckbutton", background=[("selected", ACCENT_DARK), ("active", OFF)],
              foreground=[("selected", "#ffffff")])
    style.configure("Card.TFrame", background=PANEL)
    style.configure("Card.TLabel", background=PANEL, foreground=FG)
    style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED, font=fonts.small)
    style.configure("Warn.TLabel", background=BG, foreground=WARN, font=fonts.small)
    style.configure("TCombobox", fieldbackground=PANEL, background=OFF, foreground=FG,
                    arrowcolor=FG)
    style.map("TCombobox", fieldbackground=[("readonly", PANEL)],
              foreground=[("readonly", FG)])
    style.configure("TSpinbox", fieldbackground=PANEL, background=OFF, foreground=FG,
                    arrowcolor=FG)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=OFF, foreground=MUTED, padding=(12, 5))
    style.map("TNotebook.Tab", background=[("selected", PANEL)],
              foreground=[("selected", FG)])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    rowheight=int(fonts.base * 2.4), borderwidth=0)
    style.map("Treeview", background=[("selected", ACCENT)], foreground=[("selected", "#000000")])
    style.configure("Treeview.Heading", background=OFF, foreground=FG, font=fonts.bold)
    style.configure("Horizontal.TScale", background=BG, troughcolor=PANEL)
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#000000")


def listbox(parent, fonts: Fonts, height: int = 8) -> tk.Listbox:
    return tk.Listbox(parent, height=height, activestyle="none", exportselection=False,
                      bg=PANEL, fg=FG, selectbackground=ACCENT, selectforeground="#000000",
                      highlightthickness=0, bd=0, font=fonts.normal)


class Tooltip:
    """Small explanation that appears when the mouse rests on a widget."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay_ms
        self._job = None
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._job:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self._win or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._win = tw = tk.Toplevel(self.widget)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=self.text, bg=RAISED, fg=FG, justify="left", wraplength=320,
                 padx=10, pady=6, bd=1, relief="solid",
                 font=tkfont.nametofont("TkDefaultFont")).pack()
        tw.geometry(f"+{x}+{y}")

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._win:
            self._win.destroy()
            self._win = None


class SliderRow:
    """Label + slider + value readout on one grid row."""

    def __init__(self, parent, row: int, var: tk.DoubleVar, label: str, lo: float, hi: float,
                 kind: str, fonts: Fonts, on_change: Callable[[], None], tip: str = ""):
        self.var = var
        self.kind = kind
        lab = ttk.Label(parent, text=label)
        lab.grid(row=row, column=0, sticky="w", pady=2)
        if tip:
            Tooltip(lab, tip)
        self.scale = ttk.Scale(parent, from_=lo, to=hi, variable=var, orient="horizontal",
                               command=lambda _v: on_change())
        self.scale.grid(row=row, column=1, sticky="ew", padx=8)
        if tip:
            Tooltip(self.scale, tip)
        parent.columnconfigure(1, weight=1)
        self.value = tk.Label(parent, width=5, anchor="e", bg=BG, fg=ACCENT, font=fonts.bold)
        self.value.grid(row=row, column=2, sticky="e")
        self.refresh()

    def refresh(self) -> None:
        self.value.configure(text=fmt(self.kind, self.var.get()))

    def set_enabled(self, enabled: bool) -> None:
        self.scale.state(["!disabled"] if enabled else ["disabled"])
