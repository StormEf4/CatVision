"""Presets tab: see every preset and create, copy, rename, delete and use them
(works like the Filters tab)."""

from __future__ import annotations

from tkinter import ttk

from . import dialogs
from .theme import Tooltip, listbox


class PresetsTab:
    def __init__(self, notebook: ttk.Notebook, app):
        self.app = app
        self.fonts = app.fonts
        self.frame = f = ttk.Frame(notebook, padding=10)
        self.names: list[str] = []
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)  # the footnote sits at the bottom

        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.list = listbox(top, self.fonts, height=10)
        self.list.grid(row=0, column=0, sticky="nsew")
        self.list.bind("<<ListboxSelect>>", lambda e: self._on_select())
        self.list.bind("<Double-1>", lambda e: self._use())
        btns = ttk.Frame(top)  # same order as the Filters tab
        btns.grid(row=0, column=1, sticky="n", padx=(8, 0))
        new = ttk.Button(btns, text="New...", command=self._new)
        new.pack(fill="x")
        Tooltip(new, "Make a new preset from what's on screen now (your current settings).")
        ttk.Button(btns, text="Copy...", command=self._copy).pack(fill="x", pady=3)
        self.rename_btn = ttk.Button(btns, text="Rename...", command=self._rename)
        self.rename_btn.pack(fill="x")
        self.delete_btn = ttk.Button(btns, text="Delete", command=self._delete)
        self.delete_btn.pack(fill="x", pady=3)
        self.use_btn = ttk.Button(btns, text="Use it", style="Accent.TButton", command=self._use)
        self.use_btn.pack(fill="x")

        self.title = ttk.Label(f, text="", style="Head.TLabel")
        self.title.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.desc = ttk.Label(f, text="", style="Muted.TLabel", justify="left",
                              wraplength=app.px_(380))
        self.desc.grid(row=2, column=0, sticky="ew")
        self.hint = ttk.Label(f, text="", style="Footnote.TLabel", justify="left",
                              wraplength=app.px_(380))
        self.hint.grid(row=3, column=0, sticky="sew", pady=(8, 0))
        f.bind("<Configure>", self._on_resize)

    def _on_resize(self, e) -> None:
        for label in (self.desc, self.hint):
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
            for w in (self.title, self.desc, self.hint):
                w.configure(text="")
            return
        kind = "Built-in preset*" if row.builtin else "Your preset*"
        if row.builtin and row.changed:
            kind += " (you've saved changes to it)"
        self.title.configure(text=row.name)
        self.desc.configure(text=f"{kind}. {row.description}" if row.builtin else kind + ".")
        if row.builtin:
            self.hint.configure(text="* Built-in presets can't be renamed or deleted. Use Copy "
                                     "to make your own version, or adjust it in Tune and Save.")
        else:
            self.hint.configure(text="* Adjust it in the Tune tab and click Save to change it.")
        self.rename_btn.state(["disabled"] if row.builtin else ["!disabled"])
        if row.builtin:
            self.delete_btn.configure(text="Restore default")
            self.delete_btn.state(["!disabled"] if row.changed else ["disabled"])
        else:
            self.delete_btn.configure(text="Delete")
            self.delete_btn.state(["!disabled"])

    # -------------------------------------------------------------- actions

    def _ask_name(self, title: str, initial: str = "") -> str | None:
        name = dialogs.ask_string(self.app.root, title, "Preset name:", initial)
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
