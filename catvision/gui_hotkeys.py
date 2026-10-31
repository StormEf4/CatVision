"""Hotkeys tab: see and change every global hotkey."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

from . import dialogs
from .dialogs import Modal
from .hotkeys import (ACTION_LABELS, MODIFIER_NAMES, MOUSE_NAME, NEEDS_MODIFIER_BUTTONS,
                      SHARED_NOTE, TK_MODIFIER_KEYSYMS, VK_LBUTTON, VK_MBUTTON, VK_RBUTTON,
                      VK_CONTROL, VK_LWIN, VK_MENU, VK_RWIN, VK_SHIFT, VK_TO_NAME, VK_XBUTTON1,
                      VK_XBUTTON2, action_label, async_key_down,
                      combo_is_shared, combo_problem, key_name_from_tk, suggested_hotkeys)
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
                                 "or mouse button you want. Preset hotkeys turn that preset "
                                 "on and off.",
                         style="Muted.TLabel", justify="left",
                         wraplength=app.px_(380))
        hint.grid(row=0, column=0, sticky="w", pady=(0, 6))
        # Shown while some hotkey is shared with the game (plain key, Shift
        # combo or mouse button: watched, not taken).
        self.shared_note = ttk.Label(f, text=SHARED_NOTE, style="Muted.TLabel",
                                     justify="left", wraplength=app.px_(380))
        self.shared_note.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.shared_note.grid_remove()

        def wrap(e):
            hint.configure(wraplength=max(100, e.width - 24))
            self.shared_note.configure(wraplength=max(100, e.width - 24))
        f.bind("<Configure>", wrap)

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
            elif combo and combo_problem(combo):
                keys += "  (not used: pick another)"   # e.g. a hand-edited plain click
                tags = ("warn",)
            elif combo and combo_is_shared(combo):
                keys += "  (shared with game)"
            self.tree.insert("", "end", iid=action, values=(label, keys), tags=tags)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        if any(c and combo_is_shared(c) for c in self.cfg.hotkeys.values()):
            self.shared_note.grid()
        else:
            self.shared_note.grid_remove()

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
                if not dialogs.ask_yes_no(self.app.root, 
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
        if dialogs.ask_yes_no(self.app.root, 
                "CatVision", "Use the suggested keys (Ctrl+Alt+V on/off, Ctrl+Alt+1-9 for "
                             "presets, and so on)? This replaces the hotkeys you have now."):
            self.cfg.hotkeys = suggested_hotkeys()
            self._commit()

    def _clear_all(self) -> None:
        if dialogs.ask_yes_no(self.app.root, "CatVision", "Remove every hotkey?"):
            self.cfg.hotkeys = {}
            self._commit()

    def _commit(self, select: str | None = None) -> None:
        self.app.reload_hotkeys()
        self.app._schedule_save()
        if select and self.tree.exists(select):
            self.tree.selection_set(select)


# Tk mouse button numbers -> our button (Tk on Windows: 2 middle, 3 right;
# X buttons arrive as 4/5 on some Windows builds, 8/9 on newer Tk and on
# Linux, where 4/5 are the scroll wheel; or not at all, hence the poll).
_TK_BUTTONS = {1: VK_LBUTTON, 2: VK_MBUTTON, 3: VK_RBUTTON, 8: VK_XBUTTON1, 9: VK_XBUTTON2}
if sys.platform == "win32":
    _TK_BUTTONS.update({4: VK_XBUTTON1, 5: VK_XBUTTON2})
# Buttons read with GetAsyncKeyState while recording (Windows; read-only).
_POLL_BUTTONS = (VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2, VK_LBUTTON, VK_RBUTTON)
_MOD_VKS = (("Ctrl", (VK_CONTROL,)), ("Alt", (VK_MENU,)), ("Shift", (VK_SHIFT,)),
            ("Win", (VK_LWIN, VK_RWIN)))
RECORD_POLL_MS = 20
ARM_MS = 200     # the click that opened the recorder never counts


class KeyRecorder(Modal):
    """Waits for the user to press a key combination or mouse button."""

    def __init__(self, root: tk.Misc, what: str, fonts,
                 key_down=async_key_down, run: bool = True):
        super().__init__(root, "Press new keys")
        self.held: set[str] = set()
        self._close_job = None
        self.key_down = key_down
        self.armed = False
        # Buttons already down when recording starts (the opening click) only
        # count once they have been let go and pressed again.
        self._buttons_down = {vk: bool(key_down(vk)) for vk in _POLL_BUTTONS}
        # Likewise keys already down (Space on the Change button, Enter in the
        # preset picker): held on into auto-repeat they would be recorded as
        # the new hotkey, now that plain keys are allowed.  Windows only (Tk's
        # keycode is the virtual-key code there); empty elsewhere.
        self._keys_down = {vk for vk in VK_TO_NAME if key_down(vk)}
        tk.Label(self, text="Press the new keys for:", bg=BG, fg=MUTED, font=fonts.normal).pack()
        tk.Label(self, text=what, bg=BG, fg=FG, font=fonts.bold).pack(pady=(0, 10))
        self.combo = tk.Label(self, text="...", bg=BG, fg=ACCENT, font=fonts.big, width=22)
        self.combo.pack()
        self.msg = tk.Label(self, text="Press a key or a mouse button (Middle, Mouse 4, "
                                       "Mouse 5), with Ctrl, Alt, Shift or Win if you "
                                       "like.\nEsc cancels.",
                            bg=BG, fg=MUTED, font=fonts.small, justify="center", wraplength=360)
        self.msg.pack(pady=(10, 0))
        self.bind("<KeyPress>", self._press)
        self.bind("<KeyRelease>", self._release)
        self.bind("<ButtonPress>", self._button)
        self._arm_job = self.after(ARM_MS, self._arm)
        self._poll_job = self.after(RECORD_POLL_MS, self._poll)
        if run:
            self.run()

    def _arm(self) -> None:
        self._arm_job = None
        self.armed = True

    def destroy(self) -> None:
        # (No timer may outlive the dialog: Esc, the window's X or a capture.)
        for name in ("_poll_job", "_arm_job", "_close_job"):
            job = getattr(self, name, None)
            setattr(self, name, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        super().destroy()

    def _mods(self) -> list[str]:
        held = set(self.held)
        # Windows: also a modifier already held when the dialog opened (Tk
        # only sees its KeyPress if it is pressed afterwards).  Without this,
        # Ctrl+Alt held early and then V would be taken as a plain V, which
        # is now allowed (and shared with the game).
        for name, vks in _MOD_VKS:
            if any(self.key_down(vk) for vk in vks):
                held.add(name)
        return [name for name, _v in MODIFIER_NAMES if name in held]

    def _poll(self) -> None:
        """Mouse buttons by GetAsyncKeyState, whatever the Tk build passes on
        (a read-only question; does nothing off Windows)."""
        self._poll_job = None
        if self.result is not None or not self.winfo_exists():
            return
        if self._keys_down:   # (a key released while the dialog lacked focus)
            self._keys_down = {vk for vk in self._keys_down if self.key_down(vk)}
        for vk in _POLL_BUTTONS:
            down = bool(self.key_down(vk))
            was = self._buttons_down.get(vk, False)
            self._buttons_down[vk] = down
            if down and not was and self.armed:
                self._take_button(vk)
                if self.result is not None:
                    return
        self._poll_job = self.after(RECORD_POLL_MS, self._poll)

    def _button(self, e: tk.Event) -> str:
        vk = _TK_BUTTONS.get(getattr(e, "num", 0))
        if vk in NEEDS_MODIFIER_BUTTONS and self.key_down(vk) is not None:
            # Windows: left / right are taken from the key-state poll only.
            # Like the hotkey poller it sees the physical button, while Tk
            # reports the logical one (they differ when the buttons are
            # swapped for left-handed use).
            return "break"
        if vk is not None and self.armed:
            self._take_button(vk)
        return "break"

    def _take_button(self, vk: int) -> None:
        if self.result is not None:
            return
        if vk in NEEDS_MODIFIER_BUTTONS and not self._mods():
            # Clicking around in the dialog is normal: just a gentle hint.
            self.msg.configure(text=combo_problem(MOUSE_NAME[vk]), fg=WARN)
            return
        self._take("+".join(self._mods() + [MOUSE_NAME[vk]]))

    def _take(self, combo: str) -> None:
        self.combo.configure(text=combo)
        problem = combo_problem(combo)
        if problem:
            self.msg.configure(text=problem, fg=WARN)
            return
        self.result = combo
        if combo_is_shared(combo):
            self.msg.configure(text="Shared with the game: it gets this key / button too.",
                               fg=MUTED)
            self._close_job = self.after(900, self.destroy)   # time to read the note
        else:
            self._close_job = self.after(250, self.destroy)   # let the user see what was recorded

    def _press(self, e: tk.Event) -> str:
        mod = TK_MODIFIER_KEYSYMS.get(e.keysym)
        if mod:
            self.held.add(mod)
            if self.result is None:   # (keep showing what was recorded)
                self.combo.configure(text="+".join(self._mods()) + "+ ...")
            return "break"
        if e.keysym == "Escape" and not self.held:
            # Esc cancels, even while the "shared with the game" note of a
            # key just pressed is still showing.
            self.result = None
            self.destroy()
            return "break"
        if self.result is not None:
            return "break"
        if getattr(e, "keycode", None) in self._keys_down:
            return "break"   # held since before the dialog opened (auto-repeat)
        key = key_name_from_tk(e.keysym, e.keycode)
        if key is None:
            self.msg.configure(text=f"'{e.keysym}' can't be used as a hotkey. Try another key.",
                               fg=WARN)
            return "break"
        self._take("+".join(self._mods() + [key]))
        return "break"

    def _release(self, e: tk.Event) -> str:
        mod = TK_MODIFIER_KEYSYMS.get(e.keysym)
        if mod:
            self.held.discard(mod)
        self._keys_down.discard(getattr(e, "keycode", None))
        return "break"


class PresetPicker(Modal):
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
