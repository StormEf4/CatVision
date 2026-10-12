"""The notice area: every problem shown in one place at the bottom of the
window, each with what's wrong, what to do about it, and a button that does
it where possible."""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Callable

from .theme import ACCENT, FG, MUTED, PANEL, Fonts

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
    def __init__(self, root: tk.Misc, fonts: Fonts, px: Callable[[float], int]):
        self.fonts = fonts
        self.px = px
        self.frame = tk.Frame(root, bg=PANEL)
        self._shown: list[tuple] = []
        self._labels: list[tk.Label] = []
        self._wrap = px(360)
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
