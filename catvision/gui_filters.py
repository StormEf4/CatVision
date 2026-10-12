"""Filters tab: create and edit your own color filters."""

from __future__ import annotations

import tkinter as tk
from tkinter import colorchooser, ttk

from . import curves, dialogs, filters
from .curves import COLOR_FILTERS
from .filters import CustomFilter
from .theme import PANEL, SliderRow, listbox

PREVIEW_COLORS = [
    (0.90, 0.20, 0.20), (0.95, 0.55, 0.10), (0.95, 0.90, 0.20), (0.20, 0.75, 0.30),
    (0.20, 0.80, 0.85), (0.20, 0.35, 0.90), (0.60, 0.30, 0.80), (0.85, 0.65, 0.50),
    (0.50, 0.50, 0.50),
]
CHANNELS = ("Red", "Green", "Blue")


def _hex(rgb) -> str:
    return "#" + "".join(f"{int(round(max(0.0, min(1.0, v)) * 255)):02x}" for v in rgb)


class FiltersTab:
    def __init__(self, notebook: ttk.Notebook, app):
        self.app = app
        self.cfg = app.cfg
        self.fonts = app.fonts
        self.frame = f = ttk.Frame(notebook, padding=10)
        self._loading = False
        self.keys: list[str] = []
        f.columnconfigure(0, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.list = listbox(top, self.fonts, height=7)
        self.list.grid(row=0, column=0, sticky="nsew")
        self.list.bind("<<ListboxSelect>>", lambda e: self._on_select())
        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, sticky="n", padx=(8, 0))
        ttk.Button(btns, text="New...", command=self._new).pack(fill="x")
        ttk.Button(btns, text="Copy...", command=self._copy).pack(fill="x", pady=3)
        self.rename_btn = ttk.Button(btns, text="Rename...", command=self._rename)
        self.rename_btn.pack(fill="x")
        self.delete_btn = ttk.Button(btns, text="Delete", command=self._delete)
        self.delete_btn.pack(fill="x", pady=3)
        ttk.Button(btns, text="Use it", style="Accent.TButton", command=self._use).pack(fill="x")

        self.info = ttk.Label(f, text="", style="Muted.TLabel", justify="left",
                              wraplength=app.px_(380))
        self.info.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        f.bind("<Configure>", lambda e: self.info.configure(wraplength=max(100, e.width - 24)))

        kind_row = ttk.Frame(f)
        kind_row.grid(row=2, column=0, sticky="w")
        self.kind = tk.StringVar(value="tint")
        self.kind_buttons = [
            ttk.Radiobutton(kind_row, text="Monochrome tint", value="tint", variable=self.kind,
                            command=self._on_kind),
            ttk.Radiobutton(kind_row, text="Channel mixer", value="mixer", variable=self.kind,
                            command=self._on_kind),
        ]
        for b in self.kind_buttons:
            b.pack(side="left", padx=(0, 14))

        self.editor = ttk.Frame(f)
        self.editor.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.editor.columnconfigure(0, weight=1)

        # tint editor
        self.tint_frame = ttk.Frame(self.editor)
        self.tint_vars = [tk.DoubleVar(value=1.0) for _ in range(3)]
        self.tint_sliders = [
            SliderRow(self.tint_frame, i, self.tint_vars[i], f"Tint {CHANNELS[i].lower()}",
                      *filters.TINT_RANGE, "pct", self.fonts, self._on_edit)
            for i in range(3)
        ]
        pick = ttk.Frame(self.tint_frame)
        pick.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.pick_btn = ttk.Button(pick, text="Pick a color...", command=self._pick_color)
        self.pick_btn.pack(side="left")
        self.swatch = tk.Label(pick, width=4, bg="#ffffff")
        self.swatch.pack(side="left", padx=8)

        # channel mixer editor
        self.mixer_frame = ttk.Frame(self.editor)
        ttk.Label(self.mixer_frame, text="Each output color is a mix of the input colors:",
                  style="Muted.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        for j, ch in enumerate(CHANNELS):
            ttk.Label(self.mixer_frame, text=f"from {ch}").grid(row=1, column=j + 1, padx=4)
        self.mix_vars: list[list[tk.StringVar]] = []
        self.mix_boxes: list[ttk.Spinbox] = []
        for i, ch in enumerate(CHANNELS):
            ttk.Label(self.mixer_frame, text=f"{ch} out =").grid(row=i + 2, column=0, sticky="w")
            row_vars = []
            for j in range(3):
                var = tk.StringVar(value="1.0" if i == j else "0.0")
                var.trace_add("write", lambda *_a: self._on_edit())
                box = ttk.Spinbox(self.mixer_frame, from_=filters.MIXER_RANGE[0],
                                  to=filters.MIXER_RANGE[1], increment=0.05, width=6,
                                  textvariable=var, font=self.fonts.normal)
                box.grid(row=i + 2, column=j + 1, padx=4, pady=2)
                row_vars.append(var)
                self.mix_boxes.append(box)
            self.mix_vars.append(row_vars)

        ttk.Label(f, text="Preview (before / after)", style="Head.TLabel").grid(
            row=4, column=0, sticky="w", pady=(10, 2))
        self.px = app.px_
        self.preview = tk.Canvas(f, width=self.px(300), height=self.px(64), bg=PANEL,
                                 highlightthickness=0)
        self.preview.grid(row=5, column=0, sticky="w")

        self.refresh(select=None)

    # ----------------------------------------------------------------- list

    def refresh(self, select: str | None = None) -> None:
        choices = [(k, v) for k, v in filters.all_filter_choices(self.cfg.custom_filters)
                   if k != "none"]
        self.keys = [k for k, _ in choices]
        self.list.delete(0, "end")
        for _k, label in choices:
            self.list.insert("end", label)
        if select is None:
            select = self.app.filter_key if self.app.filter_key in self.keys else self.keys[0]
        if select in self.keys:
            idx = self.keys.index(select)
            self.list.selection_clear(0, "end")
            self.list.selection_set(idx)
            self.list.see(idx)
        self._on_select()

    def _selected(self) -> str | None:
        sel = self.list.curselection()
        return self.keys[sel[0]] if sel else None

    def _custom_name(self) -> str | None:
        key = self._selected()
        if key and key.startswith(curves.CUSTOM_PREFIX):
            return filters.name_from_key(key)
        return None

    def _on_select(self) -> None:
        key = self._selected()
        if key is None:
            return
        name = self._custom_name()
        if name is not None:
            f = self.cfg.custom_filters[name]
            self.info.configure(text="Your filter. Changes are saved and shown right away while "
                                     "this filter is in use.")
        else:
            f = filters.copy_of_builtin(key)
            self.info.configure(text=f"'{COLOR_FILTERS[key]}' is built in and can't be changed. "
                                     "Click Copy to make your own version you can edit.")
        self._show(f, editable=name is not None)

    def _show(self, f: CustomFilter, editable: bool) -> None:
        self._loading = True
        try:
            self.kind.set(f.kind)
            for var, v in zip(self.tint_vars, f.tint):
                var.set(v)
            for i in range(3):
                for j in range(3):
                    self.mix_vars[i][j].set(f"{f.matrix[i][j]:.2f}")
        finally:
            self._loading = False
        state = ["!disabled"] if editable else ["disabled"]
        for b in self.kind_buttons + self.mix_boxes + [self.pick_btn, self.rename_btn, self.delete_btn]:
            b.state(state)
        for s in self.tint_sliders:
            s.set_enabled(editable)
            s.refresh()
        self._show_kind()
        self._draw_preview(f.to_matrix())

    def _show_kind(self) -> None:
        if self.kind.get() == "tint":
            self.mixer_frame.grid_forget()
            self.tint_frame.grid(row=0, column=0, sticky="ew")
        else:
            self.tint_frame.grid_forget()
            self.mixer_frame.grid(row=0, column=0, sticky="w")

    # --------------------------------------------------------------- editing

    def _read_editor(self, base: CustomFilter) -> CustomFilter:
        f = CustomFilter(self.kind.get(), [v.get() for v in self.tint_vars],
                         [row[:] for row in base.matrix])
        for i in range(3):
            for j in range(3):
                try:
                    f.matrix[i][j] = float(self.mix_vars[i][j].get())
                except ValueError:
                    pass  # half-typed number: keep the previous value
        return CustomFilter.from_dict(f.to_dict())  # clamp to valid ranges

    def _on_kind(self) -> None:
        name = self._custom_name()
        if name is None or self._loading:
            return
        current = self.cfg.custom_filters[name]
        if self.kind.get() == "mixer" and current.kind == "tint":
            # Start the mixer from what the tint looked like.
            current.matrix = [[round(v, 3) for v in row] for row in current.to_matrix()]
            self._show(CustomFilter("mixer", current.tint, current.matrix), editable=True)
        self._show_kind()
        self._on_edit()

    def _on_edit(self) -> None:
        if self._loading:
            return
        name = self._custom_name()
        if name is None:
            return
        f = self._read_editor(self.cfg.custom_filters[name])
        self.cfg.custom_filters[name] = f
        for s in self.tint_sliders:
            s.refresh()
        self.swatch.configure(bg=_hex([min(1.0, v) for v in f.tint]))
        self._draw_preview(f.to_matrix())
        self.app.custom_filters_changed()

    def _pick_color(self) -> None:
        start = _hex([min(1.0, v.get()) for v in self.tint_vars])
        rgb, _hexval = colorchooser.askcolor(color=start, parent=self.frame,
                                            title="Tint color")
        if rgb is None:
            return
        self._loading = True
        try:
            for var, v in zip(self.tint_vars, rgb):
                var.set(round(v / 255, 3))
        finally:
            self._loading = False
        self._on_edit()

    def _draw_preview(self, m) -> None:
        c = self.preview
        c.delete("all")
        n = len(PREVIEW_COLORS)
        cell = self.px(300) / n
        for i, rgb in enumerate(PREVIEW_COLORS):
            x0, x1 = i * cell + self.px(2), (i + 1) * cell - self.px(2)
            c.create_rectangle(x0, self.px(2), x1, self.px(30), fill=_hex(rgb), outline="")
            after = curves.apply_matrix(m, rgb)
            c.create_rectangle(x0, self.px(34), x1, self.px(62), fill=_hex(after), outline="")

    # -------------------------------------------------------------- actions

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = dialogs.ask_string(self.app.root, title, "Filter name:", initial)
        name = filters.clean_name(name or "")
        if not name:
            return None
        if name in self.cfg.custom_filters:
            dialogs.show_info(self.app.root, "CatVision", "You already have a filter with that name.")
            return None
        return name

    def _add(self, name: str, f: CustomFilter) -> None:
        self.cfg.custom_filters[name] = f
        key = filters.key_for(name)
        self.app.custom_filters_changed()
        self.app.set_filter(key)  # use it straight away so edits show live
        self.refresh(select=key)
        if not self.app.engine.enabled:
            self.app.set_status("Turn CatVision on (Home tab) to see your filter on screen.")

    def _new(self) -> None:
        name = self._ask_name("New filter")
        if name:
            self._add(name, CustomFilter("tint", [1.0, 0.9, 0.7]))

    def _copy(self) -> None:
        key = self._selected()
        if key is None:
            return
        base = (self.cfg.custom_filters[filters.name_from_key(key)]
                if key.startswith(curves.CUSTOM_PREFIX) else filters.copy_of_builtin(key))
        label = filters.name_from_key(key) if key.startswith(curves.CUSTOM_PREFIX) else COLOR_FILTERS[key]
        name = self._ask_name("Copy filter", f"{label.split(' (')[0]} (mine)")
        if name:
            self._add(name, CustomFilter.from_dict(base.to_dict()))

    def _rename(self) -> None:
        old = self._custom_name()
        if old is None:
            return
        new = self._ask_name("Rename filter", old)
        if not new:
            return
        self.cfg.custom_filters[new] = self.cfg.custom_filters.pop(old)
        old_key, new_key = filters.key_for(old), filters.key_for(new)
        for name, s in list(self.cfg.custom_presets.items()):
            if s.color_filter == old_key:
                s.color_filter = new_key
        if self.app.filter_key == old_key:
            self.app.filter_key = new_key
        self.app.custom_filters_changed()
        self.app.refresh_presets()
        self.refresh(select=new_key)

    def _delete(self) -> None:
        name = self._custom_name()
        if name is None:
            return
        if not dialogs.ask_yes_no(self.app.root, "CatVision", f"Delete your filter '{name}'?"):
            return
        del self.cfg.custom_filters[name]
        if self.app.filter_key == filters.key_for(name):
            self.app.set_filter("none")
        self.app.custom_filters_changed()
        self.refresh()

    def _use(self) -> None:
        key = self._selected()
        if key:
            self.app.set_filter(key)
            self.app.set_status(f"Color filter set to '{self.list.get(self.list.curselection()[0])}'."
                                + ("" if self.app.engine.enabled else " Turn CatVision on to see it."))
