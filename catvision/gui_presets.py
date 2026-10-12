"""Presets tab: see every preset and create, copy, rename, delete and use them
(works like the Filters tab)."""

from __future__ import annotations

from tkinter import simpledialog, ttk

from .curves import COLOR_FILTERS, Settings
from .filters import name_from_key
from .theme import Tooltip, listbox


def summary(s: Settings) -> str:
    """One readable line describing a preset's main settings."""
    parts = []
    if s.shadow_boost:
        parts.append(f"Shadow boost {s.shadow_boost:.0f}")
    if s.exposure:
        parts.append(f"Exposure {s.exposure:.0f}")
    if s.exposure and s.anti_glare:
        parts.append(f"Anti-glare {s.anti_glare:.0f}")
    if s.gamma != 1.0:
        parts.append(f"Gamma {s.gamma:.2f}")
    if s.contrast != 1.0:
        parts.append(f"Contrast {s.contrast * 100:.0f}%")
    if s.saturation != 1.0:
        parts.append(f"Saturation {s.saturation * 100:.0f}%")
    if s.color_filter != "none":
        label = COLOR_FILTERS.get(s.color_filter) or f"{name_from_key(s.color_filter)} (mine)"
        parts.append(f"Filter: {label}")
    return "  ·  ".join(parts) or "No changes (normal picture)"


class PresetsTab:
    def __init__(self, notebook: ttk.Notebook, app):
        self.app = app
        self.fonts = app.fonts
        self.frame = f = ttk.Frame(notebook, padding=10)
        self.names: list[str] = []
        f.columnconfigure(0, weight=1)

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.list = listbox(top, self.fonts, height=10)
        self.list.grid(row=0, column=0, sticky="nsew")
        self.list.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.list.bind("<Double-1>", lambda e: self._use())
        btns = ttk.Frame(top)
        btns.grid(row=0, column=1, sticky="n", padx=(8, 0))
        self.use_btn = ttk.Button(btns, text="Use it", style="Accent.TButton", command=self._use)
        self.use_btn.pack(fill="x")
        new = ttk.Button(btns, text="New...", command=self._new)
        new.pack(fill="x", pady=(6, 3))
        Tooltip(new, "Make a new preset from what's on screen now (your current settings).")
        ttk.Button(btns, text="Copy...", command=self._copy).pack(fill="x")
        self.rename_btn = ttk.Button(btns, text="Rename...", command=self._rename)
        self.rename_btn.pack(fill="x", pady=3)
        self.delete_btn = ttk.Button(btns, text="Delete", command=self._delete)
        self.delete_btn.pack(fill="x")

        self.title = ttk.Label(f, text="", style="Head.TLabel")
        self.title.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.desc = ttk.Label(f, text="", style="Muted.TLabel", justify="left",
                              wraplength=app.px_(380))
        self.desc.grid(row=2, column=0, sticky="ew")
        self.values = ttk.Label(f, text="", justify="left", wraplength=app.px_(380))
        self.values.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.hint = ttk.Label(f, text="", style="Muted.TLabel", justify="left",
                              wraplength=app.px_(380))
        self.hint.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        f.bind("<Configure>", self._on_resize)

    def _on_resize(self, e) -> None:
        for label in (self.desc, self.values, self.hint):
            label.configure(wraplength=max(100, e.width - 24))

    # ----------------------------------------------------------------- list

    def refresh(self, select: str | None = None) -> None:
        if not hasattr(self.app, "_rows"):
            return
        keep = select or self._selected() or self.app.cfg.active_preset
        rows = self.app._rows
        self.names = [r.name for r in rows]
        self.list.delete(0, "end")
        for r in rows:
            tag = "  (mine)" if not r.builtin else "  (edited)" if r.changed else ""
            active = "▶ " if r.name == self.app.cfg.active_preset else "   "
            self.list.insert("end", f"{active}{r.name}{tag}")
        if keep in self.names:
            idx = self.names.index(keep)
            self.list.selection_set(idx)
            self.list.see(idx)
        self._on_select()

    def _selected(self) -> str | None:
        sel = self.list.curselection()
        return self.names[sel[0]] if sel and sel[0] < len(self.names) else None

    def _on_select(self) -> None:
        row = self.app.preset_row(self._selected()) if self._selected() else None
        if row is None:
            for w in (self.title, self.desc, self.values, self.hint):
                w.configure(text="")
            return
        kind = "Built-in preset" if row.builtin else "Your preset"
        if row.builtin and row.changed:
            kind += " (you've saved changes to it)"
        self.title.configure(text=row.name)
        self.desc.configure(text=f"{kind}. {row.description}" if row.builtin else kind + ".")
        self.values.configure(text=summary(row.settings))
        if row.builtin:
            self.hint.configure(text="Built-in presets can't be renamed or deleted. Use Copy to "
                                     "make your own version, or adjust it in Tune and Save.")
        else:
            self.hint.configure(text="Adjust it in the Tune tab and click Save to change it.")
        self.rename_btn.state(["disabled"] if row.builtin else ["!disabled"])
        if row.builtin:
            self.delete_btn.configure(text="Restore default")
            self.delete_btn.state(["!disabled"] if row.changed else ["disabled"])
        else:
            self.delete_btn.configure(text="Delete")
            self.delete_btn.state(["!disabled"])

    # -------------------------------------------------------------- actions

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = simpledialog.askstring(title, "Preset name:", parent=self.frame,
                                      initialvalue=initial)
        name = " ".join((name or "").split())[:60]
        if not name or not self.app.name_is_free(name):
            return None
        return name

    def _use(self) -> None:
        name = self._selected()
        if name:
            self.app.activate_preset_by_name(name)
            self.app.set_status(f"Using '{name}'."
                                + ("" if self.app.engine.enabled else " Turn CatVision on to see it."))
            self.refresh(select=name)

    def _new(self) -> None:
        name = self._ask_name("New preset")
        if name:
            self.app.add_preset(name, self.app._read_settings())
            self.refresh(select=name)

    def _copy(self) -> None:
        row = self.app.preset_row(self._selected()) if self._selected() else None
        if row is None:
            return
        name = self._ask_name("Copy preset", f"{row.name} (mine)")
        if name:
            self.app.add_preset(name, row.settings)
            self.refresh(select=name)

    def _rename(self) -> None:
        name = self._selected()
        if name:
            self.app._rename_preset(name)
            self.refresh()

    def _delete(self) -> None:
        name = self._selected()
        if name:
            self.app._delete_or_restore(name)
            self.refresh()
