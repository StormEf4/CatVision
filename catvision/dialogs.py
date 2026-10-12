"""CatVision's own message, yes/no and name dialogs.

They replace tkinter's messagebox and simpledialog, which Windows opens on the
main monitor even when CatVision is on another one.  These always open in the
middle of CatVision's window, kept inside the monitor it is on."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .theme import BG, FG
from .winutil import window_work_area


def place_over(win: tk.Toplevel, owner: tk.Misc) -> None:
    """Center win over owner's window, kept inside the monitor owner is on."""
    root = owner.winfo_toplevel()
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    left, top, right, bottom = window_work_area(root)
    if root.winfo_viewable():
        x = root.winfo_rootx() + (root.winfo_width() - w) // 2
        y = root.winfo_rooty() + (root.winfo_height() - h) // 3
    else:  # minimized or in the tray: middle of its monitor
        x = left + (right - left - w) // 2
        y = top + (bottom - top - h) // 3
    x = max(left, min(x, right - w))
    y = max(top, min(y, bottom - h))
    win.geometry(f"+{x}+{y}")


class Modal(tk.Toplevel):
    """A small dark dialog that blocks the window until it's closed."""

    def __init__(self, owner: tk.Misc, title: str):
        root = owner.winfo_toplevel()
        super().__init__(root)
        self.owner = root
        self.result = None
        self.withdraw()  # placed before it's shown, so it never flashes elsewhere
        self.title(title)
        self.configure(bg=BG, padx=20, pady=16)
        self.resizable(False, False)
        if root.winfo_viewable():
            self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def run(self) -> None:
        place_over(self, self.owner)
        self.deiconify()
        self.lift()
        self.grab_set()
        self.focus_force()
        self.wait_window()


class _Message(Modal):
    def __init__(self, owner: tk.Misc, title: str, text: str, buttons: list[tuple[str, object]],
                 entry: str | None = None):
        super().__init__(owner, title)
        tk.Label(self, text=text, bg=BG, fg=FG, justify="left", wraplength=420,
                 font="TkDefaultFont").pack(anchor="w")
        self.entry = None
        if entry is not None:
            self.var = tk.StringVar(value=entry)
            self.entry = ttk.Entry(self, textvariable=self.var, width=40, font="TkDefaultFont")
            self.entry.pack(fill="x", pady=(8, 0))
            self.entry.select_range(0, "end")
        row = ttk.Frame(self)
        row.pack(fill="x", pady=(14, 0))
        # The first button is the main one (yellow, Enter); the last one is Esc.
        for i, (label, value) in reversed(list(enumerate(buttons))):
            ttk.Button(row, text=label, style="Accent.TButton" if i == 0 else "TButton",
                       command=lambda v=value: self._done(v)).pack(side="right", padx=(6, 0))
        self.bind("<Return>", lambda e: self._done(buttons[0][1]))
        self.bind("<Escape>", lambda e: self._done(buttons[-1][1]))

    def run(self) -> None:
        if self.entry is not None:
            self.after_idle(self.entry.focus_set)
        super().run()

    def _done(self, value) -> None:
        if value is True and self.entry is not None:
            value = self.var.get()
        self.result = value
        self.destroy()


def ask_yes_no(owner: tk.Misc, title: str, text: str) -> bool:
    d = _Message(owner, title, text, [("Yes", True), ("No", False)])
    d.run()
    return bool(d.result)


def show_info(owner: tk.Misc, title: str, text: str) -> None:
    _Message(owner, title, text, [("OK", None)]).run()


def ask_string(owner: tk.Misc, title: str, prompt: str, initial: str = "") -> str | None:
    d = _Message(owner, title, prompt, [("OK", True), ("Cancel", None)], entry=initial)
    d.run()
    return d.result if isinstance(d.result, str) else None
