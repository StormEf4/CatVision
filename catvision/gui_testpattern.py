"""Test pattern window: a big picture of very dark and very bright greys to
tune Shadow boost, Exposure and the other sliders against.  It stays open
next to the main window, so the picture changes as you move the sliders."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .theme import BG

HINT = ("Raise Shadow Boost and Exposure levels to your satisfaction. Above is a full dark "
        "range for small testing. Since everyone's monitor is different, my defaults may not "
        "be satisfactory.")
LABEL = "#8a909c"                                   # captions drawn on the black picture
DARK_ROWS = (list(range(1, 17)), list(range(18, 50, 2)))
BRIGHT = list(range(210, 256, 3))
HIDDEN = (2, 4, 7, 10, 14, 20)                      # levels of the figures in the dark box


def grey(v: int) -> str:
    return f"#{v:02x}{v:02x}{v:02x}"


class TestPattern:
    """Opened from the Tune tab.  Only one at a time; opening it again just
    brings the existing window to the front."""

    def __init__(self, app):
        self.app = app
        root = app.root
        self.win = w = tk.Toplevel(root)
        w.title("CatVision test pattern")
        w.configure(bg=BG)
        w.minsize(app.px_(480), app.px_(360))
        self.canvas = tk.Canvas(w, bg="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        bottom = ttk.Frame(w, padding=(12, 8))
        bottom.pack(fill="x")
        bottom.columnconfigure(0, weight=1)
        hint = HINT
        if not app.engine.all_displays_selected():
            hint += " Drag this window onto a ticked display to see the effect."
        self.hint = ttk.Label(bottom, text=hint, style="Muted.TLabel", justify="left")
        self.hint.grid(row=0, column=0, sticky="ew")
        btns = ttk.Frame(bottom)
        btns.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.full_btn = ttk.Button(btns, text="Full screen", command=self.toggle_full)
        self.full_btn.pack(fill="x")
        ttk.Button(btns, text="Close", command=self.close).pack(fill="x", pady=(3, 0))
        bottom.bind("<Configure>", lambda e: self.hint.configure(
            wraplength=max(120, e.width - btns.winfo_width() - 40)))

        sw, sh = w.winfo_screenwidth(), w.winfo_screenheight()
        width = int(min(sw * 0.8, app.px_(1100)))
        height = int(min(sh * 0.8, width * 0.7))
        w.geometry(f"{width}x{height}+{(sw - width) // 2}+{max(0, (sh - height) // 3)}")
        self.canvas.bind("<Configure>", lambda e: self.draw())
        w.bind("<Escape>", lambda e: self.toggle_full() if self.full else self.close())
        w.bind("<F11>", lambda e: self.toggle_full())
        w.protocol("WM_DELETE_WINDOW", self.close)
        self.full = False
        self.closed = False

    # ------------------------------------------------------------- window

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        self.closed = True
        self.win.destroy()

    def toggle_full(self) -> None:
        self.full = not self.full
        self.win.attributes("-fullscreen", self.full)
        self.full_btn.configure(text="Exit full screen" if self.full else "Full screen")

    # ------------------------------------------------------------ picture

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 50 or H < 50:
            return
        # Heights in "units"; the whole picture is 100 units tall.
        unit = H / 100
        m = max(8, W * 0.025)
        cw = W - 2 * m
        size = max(8, int(unit * 2.4))
        font = ("Segoe UI", size, "bold")
        small = ("Segoe UI", max(7, int(unit * 1.9)))
        y = unit * 2

        def caption(text: str) -> None:
            nonlocal y
            c.create_text(m, y, text=text, fill=LABEL, font=font, anchor="nw")
            y += unit * 5

        def steps(levels: list[int], height: float) -> None:
            nonlocal y
            n = len(levels)
            cell = cw / n
            gap = max(2, cell * 0.06)
            for i, v in enumerate(levels):
                x0 = m + i * cell
                c.create_rectangle(x0 + gap / 2, y, x0 + cell - gap / 2, y + height,
                                   fill=grey(v), outline="")
                c.create_text(x0 + cell / 2, y + height + unit * 0.4, text=str(v),
                              fill=LABEL, font=small, anchor="n")
            y += height + unit * 4

        caption("Dark steps (brightness out of 255): try to see every square")
        steps(DARK_ROWS[0], unit * 13)
        steps(DARK_ROWS[1], unit * 13)

        caption("Hidden in the dark: how many figures can you see?")
        box_h = unit * 20
        n = len(HIDDEN)
        slot = cw / n
        for i, v in enumerate(HIDDEN):
            cx = m + slot * (i + 0.5)
            head = box_h * 0.13
            top = y + box_h * 0.06
            c.create_oval(cx - head, top, cx + head, top + head * 2, fill=grey(v), outline="")
            body_w = head * 1.6
            c.create_rectangle(cx - body_w, top + head * 2.2, cx + body_w, y + box_h * 0.86,
                               fill=grey(v), outline="")
            c.create_text(cx, y + box_h * 0.9, text=str(v), fill=LABEL, font=small, anchor="n")
        y += box_h + unit * 4

        caption("Bright steps: these should stay apart, not turn into one white block")
        steps(BRIGHT, unit * 9)

        # Full range: a smooth ramp from black to white.
        bar_h = max(unit * 5, H - y - unit * 2)
        for i in range(256):
            x0 = m + cw * i / 256
            c.create_rectangle(x0, y, m + cw * (i + 1) / 256 + 1, y + bar_h,
                               fill=grey(i), outline="")


def open_test_pattern(app) -> TestPattern:
    """Open the test pattern, or bring it to the front if it's already open."""
    current = getattr(app, "test_pattern", None)
    if current is None or current.closed or not current.win.winfo_exists():
        current = app.test_pattern = TestPattern(app)
    current.show()
    return current
