"""Hotkeys tab: see and change every global hotkey."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .hotkeys import (ACTION_LABELS, MODIFIER_NAMES, TK_MODIFIER_KEYSYMS, action_label,
                      combo_problem, key_name_from_tk, suggested_hotkeys)
from .theme import ACCENT, BG, FG, MUTED, WARN


class HotkeysTab:
    def __init__(self, notebook: ttk.Notebook, app):
        self.app = app
        self.cfg = app.cfg
        self.frame = f = ttk.Frame(notebook, padding=10)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        hint = ttk.Label(f, text="No hotkeys are set until you choose them. Double-click an "
                                 "action (or select it and click Change) and press the keys "
                                 "you want. Preset hotkeys turn that preset on and off.",
                         style="Muted.TLabel", justify="left",
                         wraplength=app.px_(380))
        hint.grid(row=0, column=0, sticky="w", pady=(0, 6))
        f.bind("<Configure>", lambda e: hint.configure(wraplength=max(100, e.width - 24)))

        self.tree = ttk.Treeview(f, columns=("action", "keys"), show="headings",
                                 selectmode="browse", height=12)
        self.tree.heading("action", text="Action")
        self.tree.heading("keys", text="Keys")
        self.tree.column("action", width=app.px_(250), stretch=True)
        self.tree.column("keys", width=app.px_(140), stretch=True)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", lambda e: self._change())

        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        btns.columnconfigure((0, 1), weight=1, uniform="b")
        ttk.Button(btns, text="Change...", command=self._change).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(btns, text="Clear", command=self._clear).grid(
            row=0, column=1, sticky="ew", padx=(3, 0))
        ttk.Button(btns, text="Add preset hotkey...", command=self._add_preset).grid(
            row=1, column=0, sticky="ew", padx=(0, 3), pady=(6, 0))
        ttk.Button(btns, text="Use suggested keys", command=self._suggested).grid(
            row=1, column=1, sticky="ew", padx=(3, 0), pady=(6, 0))
        ttk.Button(btns, text="Clear all", command=self._clear_all).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _actions_in_order(self) -> list[str]:
        """Every action, an on/off toggle row for each built-in preset (unset
        until the user picks keys), then the user's own presets that have keys."""
        from .presets import BUILTIN_PRESETS
        actions = list(ACTION_LABELS)
        builtin = ["preset:" + p.name for p in BUILTIN_PRESETS]
        presets = self.app.preset_names()
        mine = [a for a in self.cfg.hotkeys if a.startswith("preset:") and a not in builtin]
        mine.sort(key=lambda a: (presets.index(a[7:]) if a[7:] in presets else 999, a))
        return actions + builtin + mine

    def refresh(self) -> None:
        if not hasattr(self.app, "hotkeys"):
            return
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        presets = set(self.app.preset_names())
        failed = set(self.app.hotkeys.failed)
        for action in self._actions_in_order():
            combo = self.cfg.hotkeys.get(action, "")
            label = action_label(action)
            tags = ()
            keys = combo or "not set"
            if action.startswith("preset:") and action[7:] not in presets:
                label += "  (preset missing)"
                tags = ("warn",)
            if action in failed:
                keys += "  (taken by another app)"
                tags = ("warn",)
            self.tree.insert("", "end", iid=action, values=(label, keys), tags=tags)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])

    def _selected(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _change(self, action: str | None = None) -> None:
        action = action or self._selected()
        if not action:
            return
        self.app.pause_hotkeys()  # so the old keys reach the recorder
        try:
            combo = KeyRecorder(self.app.root, action_label(action), self.app.fonts).result
        finally:
            self.app.reload_hotkeys()
        if not combo:
            return
        for other, other_combo in list(self.cfg.hotkeys.items()):
            if other != action and other_combo == combo:
                if not messagebox.askyesno(
                        "CatVision", f"{combo} is already used for '{action_label(other)}'.\n\n"
                                     "Use it for this action instead?"):
                    return
                self._unbind(other)
        self.cfg.hotkeys[action] = combo
        self._commit(select=action)

    def _unbind(self, action: str) -> None:
        if action.startswith("preset:"):
            self.cfg.hotkeys.pop(action, None)
        else:
            self.cfg.hotkeys[action] = ""

    def _clear(self) -> None:
        action = self._selected()
        if action:
            self._unbind(action)
            self._commit()

    def _add_preset(self) -> None:
        name = PresetPicker(self.app.root, self.app.preset_names(), self.app.fonts).result
        if name:
            action = "preset:" + name
            self.cfg.hotkeys.setdefault(action, "")
            self.refresh()
            self.tree.selection_set(action)
            self.tree.see(action)
            self._change(action)

    def _suggested(self) -> None:
        if messagebox.askyesno(
                "CatVision", "Use the suggested keys (Ctrl+Alt+V on/off, Ctrl+Alt+1-9 for "
                             "presets, and so on)? This replaces the hotkeys you have now."):
            self.cfg.hotkeys = suggested_hotkeys()
            self._commit()

    def _clear_all(self) -> None:
        if messagebox.askyesno("CatVision", "Remove every hotkey?"):
            self.cfg.hotkeys = {}
            self._commit()

    def _commit(self, select: str | None = None) -> None:
        self.app.reload_hotkeys()
        self.app._schedule_save()
        if select and self.tree.exists(select):
            self.tree.selection_set(select)


class _Modal(tk.Toplevel):
    def __init__(self, root: tk.Misc, title: str):
        super().__init__(root)
        self.result = None
        self.title(title)
        self.configure(bg=BG, padx=20, pady=16)
        self.resizable(False, False)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def run(self) -> None:
        self.update_idletasks()
        root = self.master
        x = root.winfo_rootx() + (root.winfo_width() - self.winfo_reqwidth()) // 2
        y = root.winfo_rooty() + (root.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()
        self.wait_window()


class KeyRecorder(_Modal):
    """Waits for the user to press a key combination."""

    def __init__(self, root: tk.Misc, what: str, fonts):
        super().__init__(root, "Press new keys")
        self.held: set[str] = set()
        tk.Label(self, text="Press the new keys for:", bg=BG, fg=MUTED, font=fonts.normal).pack()
        tk.Label(self, text=what, bg=BG, fg=FG, font=fonts.bold).pack(pady=(0, 10))
        self.combo = tk.Label(self, text="...", bg=BG, fg=ACCENT, font=fonts.big, width=22)
        self.combo.pack()
        self.msg = tk.Label(self, text="Hold Ctrl and/or Alt, then press a key.\nEsc cancels.",
                            bg=BG, fg=MUTED, font=fonts.small, justify="center", wraplength=360)
        self.msg.pack(pady=(10, 0))
        self.bind("<KeyPress>", self._press)
        self.bind("<KeyRelease>", self._release)
        self.run()

    def _mods(self) -> list[str]:
        return [name for name, _v in MODIFIER_NAMES if name in self.held]

    def _press(self, e: tk.Event) -> str:
        mod = TK_MODIFIER_KEYSYMS.get(e.keysym)
        if mod:
            self.held.add(mod)
            self.combo.configure(text="+".join(self._mods()) + "+ ...")
            return "break"
        if e.keysym == "Escape" and not self.held:
            self.destroy()
            return "break"
        key = key_name_from_tk(e.keysym, e.keycode)
        if key is None:
            self.msg.configure(text=f"'{e.keysym}' can't be used as a hotkey. Try another key.",
                               fg=WARN)
            return "break"
        combo = "+".join(self._mods() + [key])
        self.combo.configure(text=combo)
        problem = combo_problem(combo)
        if problem:
            self.msg.configure(text=problem, fg=WARN)
            return "break"
        self.result = combo
        self.after(250, self.destroy)  # let the user see what was recorded
        return "break"

    def _release(self, e: tk.Event) -> str:
        mod = TK_MODIFIER_KEYSYMS.get(e.keysym)
        if mod:
            self.held.discard(mod)
        return "break"


class PresetPicker(_Modal):
    def __init__(self, root: tk.Misc, names: list[str], fonts):
        super().__init__(root, "Add preset hotkey")
        tk.Label(self, text="Which preset should get a hotkey?", bg=BG, fg=FG,
                 font=fonts.normal).pack(anchor="w")
        self.combo = ttk.Combobox(self, values=names, state="readonly", width=30,
                                  font=fonts.normal, height=20)
        self.combo.pack(fill="x", pady=8)
        if names:
            self.combo.current(0)
        row = ttk.Frame(self)
        row.pack(fill="x")
        ttk.Button(row, text="Next", command=self._ok).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.run()

    def _ok(self) -> None:
        self.result = self.combo.get() or None
        self.destroy()
