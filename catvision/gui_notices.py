"""The notice area: every problem shown in one place at the bottom of the
window, each with what's wrong, what to do about it, and a button that does
it where possible."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Callable

from .theme import ACCENT, FG, MUTED, PANEL, Fonts
from .winutil import window_work_area

INFO = "#5aa9ff"
LEVELS = {
    "warn": (ACCENT, "⚠"),   # amber stripe, warning sign
    "info": (INFO, "ℹ"),     # blue stripe, info sign
}


@dataclass
class Notice:
    key: str                       # stable id (used to avoid needless redraws)
    level: str                     # "warn" or "info"
    title: str                     # what's going on, in one sentence
    fix: str                       # what to do about it
    actions: list[tuple[str, Callable[[], None]]] = field(default_factory=list)

    def signature(self) -> tuple:
        return (self.key, self.level, self.title, self.fix, tuple(a[0] for a in self.actions))


class NoticeArea:
    def __init__(self, root: tk.Misc, fonts: Fonts, px: Callable[[float], int],
                 width: int | None = None):
        self.fonts = fonts
        self.px = px
        self.frame = tk.Frame(root, bg=PANEL)
        self._shown: list[tuple] = []
        self._labels: list[tuple[tk.Label, bool]] = []
        self._wrap = (width - px(70)) if width else px(360)
        if width is None:  # fixed-width areas (the drop-down) don't re-wrap
            self.frame.bind("<Configure>", self._on_resize)

    @property
    def visible(self) -> bool:
        return bool(self._shown)

    def show(self, notices: list[Notice]) -> None:
        signature = [n.signature() for n in notices]
        if signature == self._shown:
            return
        self._shown = signature
        for child in self.frame.winfo_children():
            child.destroy()
        self._labels = []
        for n in notices:
            self._add(n)
        self.frame.configure(pady=4 if notices else 0)

    def _add(self, n: Notice) -> None:
        color, icon = LEVELS.get(n.level, LEVELS["info"])
        card = tk.Frame(self.frame, bg=PANEL)
        card.pack(fill="x", padx=8, pady=3)
        tk.Frame(card, bg=color, width=self.px(4)).pack(side="left", fill="y")
        tk.Label(card, text=icon, bg=PANEL, fg=color, font=self.fonts.big).pack(
            side="left", anchor="n", padx=(8, 6))
        if n.actions:  # buttons on the right keep each notice short
            buttons = tk.Frame(card, bg=PANEL)
            buttons.pack(side="right", anchor="center", padx=(6, 0))
            for i, (label, command) in enumerate(n.actions):
                ttk.Button(buttons, text=label, command=command,
                           style="Accent.TButton" if i == 0 else "TButton").pack(
                    fill="x", pady=1)
        body = tk.Frame(card, bg=PANEL)
        body.pack(side="left", fill="x", expand=True)
        width = self._text_width(bool(n.actions))
        title = tk.Label(body, text=n.title, bg=PANEL, fg=FG, font=self.fonts.bold,
                         justify="left", anchor="w", wraplength=width)
        title.pack(fill="x")
        fix = tk.Label(body, text=n.fix, bg=PANEL, fg=MUTED, font=self.fonts.small,
                       justify="left", anchor="w", wraplength=width)
        fix.pack(fill="x")
        self._labels.append((title, bool(n.actions)))
        self._labels.append((fix, bool(n.actions)))

    def _on_resize(self, e) -> None:
        wrap = max(120, e.width - self.px(70))
        if abs(wrap - self._wrap) > 4:
            self._wrap = wrap
            for label, has_buttons in self._labels:
                label.configure(wraplength=self._text_width(has_buttons))

    def _text_width(self, has_buttons: bool) -> int:
        return max(120, self._wrap - (self.px(170) if has_buttons else 0))


class WarningButton:
    """A warning icon in the header that only appears when something needs
    attention.  Clicking it opens a drop-down with every notice (what's wrong,
    what to do, and buttons that fix it); clicking again closes it, and it
    closes by itself once everything is resolved.  It floats over the window,
    so it never takes space from the app or shifts anything around."""

    def __init__(self, parent: tk.Misc, root: tk.Tk, fonts: Fonts,
                 px: Callable[[float], int], before: tk.Widget | None = None):
        self.root = root
        self.fonts = fonts
        self.px = px
        self.before = before
        self.icon = tk.Label(parent, text="⚠", bg=parent.cget("bg"), fg=ACCENT,
                             font=fonts.big, cursor="hand2", padx=6)
        # On release, so the icon's tooltip (hidden on press) can't sit on top.
        self.icon.bind("<ButtonRelease-1>", lambda e: self.toggle())
        self.notices: list[Notice] = []
        self.popup: tk.Toplevel | None = None
        self.area: NoticeArea | None = None
        self._watch_job = None
        self._away = 0
        root.bind("<Configure>", self._on_root_change, add="+")
        root.bind("<Unmap>", self._on_root_unmap, add="+")

    @property
    def is_open(self) -> bool:
        return self.popup is not None

    def show(self, notices: list[Notice]) -> None:
        self.notices = notices
        if not notices:
            self.close()
            self.icon.pack_forget()
            return
        count = len(notices)
        self.icon.configure(text="⚠" if count == 1 else f"⚠ {count}")
        if not self.icon.winfo_ismapped():
            opts = {"before": self.before} if self.before is not None else {}
            self.icon.pack(side="left", padx=(0, 8), **opts)
        if self.area is not None:
            self.area.show(notices)
            self._place()

    def toggle(self) -> None:
        if self.is_open:
            self.close()
        elif self.notices:
            self.open()

    def open(self) -> None:
        if self.popup is not None:
            return
        # Not transient(): on Windows a borderless transient window ends up
        # hidden behind the main window.  Topmost keeps it in front instead,
        # and _watch closes it when the user switches to another app.
        self.popup = tk.Toplevel(self.root)
        self.popup.withdraw()
        self.popup.overrideredirect(True)
        self.popup.configure(bg=ACCENT)           # 1px amber border
        inner = tk.Frame(self.popup, bg=PANEL)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.area = NoticeArea(inner, self.fonts, self.px, width=self._width())
        self.area.frame.pack(fill="x")
        self.area.show(self.notices)
        self._place()
        self.popup.deiconify()
        self.popup.attributes("-topmost", True)
        self.popup.lift()
        self._away = 0
        self._watch_job = self.root.after(400, self._watch)

    def close(self) -> None:
        if self._watch_job is not None:
            self.root.after_cancel(self._watch_job)
            self._watch_job = None
        if self.popup is not None:
            self.popup.destroy()
        self.popup = None
        self.area = None

    def _watch(self) -> None:
        """Close the drop-down once CatVision is no longer the active app, so
        it never floats over a game or another window."""
        self._watch_job = None
        if self.popup is None:
            return
        try:
            active = self.root.focus_displayof() is not None
        except (tk.TclError, KeyError):
            active = True
        self._away = 0 if active else self._away + 1
        if self._away >= 2:
            self.close()
            return
        self._watch_job = self.root.after(400, self._watch)

    def _width(self) -> int:
        return max(self.px(300), min(self.root.winfo_width() - self.px(24), self.px(520)))

    def _place(self) -> None:
        if self.popup is None:
            return
        self.popup.update_idletasks()
        w = self.popup.winfo_reqwidth()
        x = self.root.winfo_rootx() + self.root.winfo_width() - w - self.px(12)
        y = self.icon.winfo_rooty() + self.icon.winfo_height() + self.px(6)
        # Kept inside the monitor the app is on (which may sit left of or
        # above the main one, at negative coordinates).
        left, _top, right, _bottom = window_work_area(self.root)
        x = max(left, min(x, right - w))
        self.popup.geometry(f"+{x}+{y}")

    def _on_root_change(self, e) -> None:
        if e.widget is self.root:
            self._place()

    def _on_root_unmap(self, e) -> None:
        if e.widget is self.root:
            self.close()
