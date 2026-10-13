"""Global hotkeys using the plain Windows ``RegisterHotKey`` API.

This is the same mechanism apps like Discord and OBS use for push-to-talk /
record shortcuts.  It does not install a keyboard hook and never touches the
game: Windows simply tells CatVision when the key combination is pressed.

Hotkeys are stored as readable strings such as ``"Ctrl+Alt+V"`` so they can
be edited in the app (or in the config file).
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

MODIFIER_NAMES = [("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT), ("Shift", MOD_SHIFT), ("Win", MOD_WIN)]


def _build_key_table() -> dict[str, int]:
    keys: dict[str, int] = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        keys[c] = ord(c)
    for n in range(1, 25):
        keys[f"F{n}"] = 0x6F + n
    for n in range(10):
        keys[f"Num {n}"] = 0x60 + n
    keys.update({
        "Num Multiply": 0x6A, "Num Add": 0x6B, "Num Subtract": 0x6D,
        "Num Decimal": 0x6E, "Num Divide": 0x6F,
        "Space": 0x20, "Page Up": 0x21, "Page Down": 0x22, "End": 0x23, "Home": 0x24,
        "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
        "Insert": 0x2D, "Delete": 0x2E, "Backspace": 0x08, "Tab": 0x09, "Enter": 0x0D,
        "Pause": 0x13, "Scroll Lock": 0x91,
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
        "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    })
    return keys


KEYS: dict[str, int] = _build_key_table()
VK_TO_NAME: dict[int, str] = {vk: name for name, vk in KEYS.items()}

# Keys that may be used without Ctrl/Alt/Win because games rarely need them.
FREE_KEYS = {f"F{n}" for n in range(1, 25)} | {"Pause", "Scroll Lock"}

# tkinter keysym -> our key name (used when the user presses a new hotkey).
_TK_KEYSYMS = {
    "Prior": "Page Up", "Next": "Page Down", "space": "Space", "BackSpace": "Backspace",
    "Return": "Enter", "Scroll_Lock": "Scroll Lock",
    "KP_Add": "Num Add", "KP_Subtract": "Num Subtract", "KP_Multiply": "Num Multiply",
    "KP_Divide": "Num Divide", "KP_Decimal": "Num Decimal",
    "equal": "=", "plus": "=", "minus": "-", "underscore": "-",
    "bracketleft": "[", "braceleft": "[", "bracketright": "]", "braceright": "]",
    "semicolon": ";", "colon": ";", "apostrophe": "'", "quotedbl": "'",
    "comma": ",", "less": ",", "period": ".", "greater": ".", "slash": "/", "question": "/",
    "backslash": "\\", "bar": "\\", "grave": "`", "asciitilde": "`",
    "exclam": "1", "at": "2", "numbersign": "3", "dollar": "4", "percent": "5",
    "asciicircum": "6", "ampersand": "7", "asterisk": "8", "parenleft": "9", "parenright": "0",
}
for _n in range(10):
    _TK_KEYSYMS[f"KP_{_n}"] = f"Num {_n}"

TK_MODIFIER_KEYSYMS = {
    "Control_L": "Ctrl", "Control_R": "Ctrl", "Alt_L": "Alt", "Alt_R": "Alt",
    "Shift_L": "Shift", "Shift_R": "Shift", "Super_L": "Win", "Super_R": "Win",
    "Win_L": "Win", "Win_R": "Win", "Meta_L": "Alt", "Meta_R": "Alt",
}


def key_name_from_tk(keysym: str, keycode: int) -> str | None:
    """Translate a tkinter key event into one of our key names."""
    if sys.platform == "win32" and keycode in VK_TO_NAME:
        # On Windows tk's keycode is the virtual-key code: it names the
        # physical key, whatever keyboard layout or AltGr symbol is in play.
        return VK_TO_NAME[keycode]
    if keysym in _TK_KEYSYMS:
        return _TK_KEYSYMS[keysym]
    if len(keysym) == 1 and keysym.upper() in KEYS:
        return keysym.upper()
    if keysym in KEYS:  # F1.., Home, End, Insert, Delete, Pause, Tab, Up, ...
        return keysym
    return None


def parse_combo(text: str) -> tuple[int, int] | None:
    """``"Ctrl+Alt+V"`` -> ``(modifiers, virtual_key)``; None if invalid."""
    if not isinstance(text, str) or not text.strip():
        return None
    *mods, key = [p.strip() for p in text.split("+")]
    mod_value = 0
    lookup = {name.lower(): value for name, value in MODIFIER_NAMES}
    lookup["control"] = MOD_CONTROL
    for m in mods:
        value = lookup.get(m.lower())
        if value is None:
            return None
        mod_value |= value
    key_lookup = {k.lower(): v for k, v in KEYS.items()}
    vk = key_lookup.get(key.lower())
    if vk is None:
        return None
    return mod_value, vk


def format_combo(modifiers: int, vk: int) -> str:
    parts = [name for name, value in MODIFIER_NAMES if modifiers & value]
    parts.append(VK_TO_NAME.get(vk, f"Key {vk}"))
    return "+".join(parts)


def normalize_combo(text: str) -> str | None:
    parsed = parse_combo(text)
    return format_combo(*parsed) if parsed else None


def combo_problem(text: str) -> str | None:
    """Explain why a combination is a bad idea, or None if it's fine."""
    parsed = parse_combo(text)
    if parsed is None:
        return "That key combination isn't recognised."
    mods, vk = parsed
    key = VK_TO_NAME.get(vk, "")
    if mods & MOD_CONTROL and mods & MOD_ALT and key == "Delete":
        return "Ctrl+Alt+Delete is reserved by Windows."
    if not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN) and key not in FREE_KEYS:
        return ("Add Ctrl or Alt to that key. A plain key would stop working in "
                "your games while CatVision is running.")
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

ACTION_LABELS: dict[str, str] = {
    "toggle": "Turn CatVision on / off",
    "off": "Panic: everything off",
    "next": "Next preset",
    "prev": "Previous preset",
    "boost_up": "More boost (+10)",
    "boost_down": "Less boost (-10)",
    "filter_next": "Next color filter",
    "auto_levels": "Auto levels on / off (beta)",
    "auto_keep": "Auto levels: keep this look (beta)",
    "show_window": "Show the CatVision window",
}

REPEATING_ACTIONS = {"boost_up", "boost_down"}

_SUGGESTED_KEYS = {
    "toggle": "Ctrl+Alt+V",
    "off": "Ctrl+Alt+0",
    "next": "Ctrl+Alt+Page Down",
    "prev": "Ctrl+Alt+Page Up",
    "boost_up": "Ctrl+Alt+=",
    "boost_down": "Ctrl+Alt+-",
    "filter_next": "Ctrl+Alt+F",
    "auto_levels": "Ctrl+Alt+A",
    "auto_keep": "Ctrl+Alt+K",
    "show_window": "Ctrl+Alt+C",
}

# Versions 1.1-1.3 came with exactly this set already applied; used only to
# recognise (and clear) those untouched old defaults when upgrading.
LEGACY_DEFAULT_HOTKEYS = dict(_SUGGESTED_KEYS, **{
    f"preset:{name}": f"Ctrl+Alt+{n}" for n, name in enumerate([
        "Gentle Boost", "Dark Areas", "Pitch Black", "Night Vision", "Horror / Flashlight",
        "Fog & Haze Cutter", "Mixed Light", "High-Contrast Mono", "Warm Low-Light",
    ], start=1)
})


def default_hotkeys() -> dict[str, str]:
    """CatVision starts with no hotkeys: the user picks every one."""
    return {}


def suggested_hotkeys() -> dict[str, str]:
    """A ready-made set the user can apply with one click (Hotkeys tab):
    every action, plus an on/off toggle for each built-in preset
    (Ctrl+Alt+1, 2, 3, ... in list order)."""
    from .presets import BUILTIN_PRESETS
    keys = dict(_SUGGESTED_KEYS)
    for n, preset in enumerate(BUILTIN_PRESETS[:9], start=1):
        keys[f"preset:{preset.name}"] = f"Ctrl+Alt+{n}"
    return keys


def action_label(action: str) -> str:
    if action.startswith("preset:"):
        return "On/off: " + action[len("preset:"):]
    return ACTION_LABELS.get(action, action)


@dataclass(frozen=True)
class Binding:
    action: str
    modifiers: int
    vk: int
    label: str


def bindings_from_config(hotkeys: dict[str, str]) -> list[Binding]:
    out = []
    seen = set()
    for action, combo in hotkeys.items():
        parsed = parse_combo(combo)
        if not parsed or parsed in seen:
            continue
        seen.add(parsed)
        out.append(Binding(action, parsed[0], parsed[1], format_combo(*parsed)))
    return out


class HotkeyListener:
    """Registers hotkeys on a background thread and reports presses.

    ``on_action`` is called from the background thread, so the GUI must hand
    the work back to its own thread.
    """

    def __init__(self, on_action: Callable[[str], None]):
        self.on_action = on_action
        self.bindings: list[Binding] = []
        self.failed: list[str] = []      # actions whose keys another app owns
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def start(self, bindings: list[Binding]) -> None:
        self.stop()
        self.bindings = list(bindings)
        if not self.supported or not self.bindings:
            self.failed = []
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="catvision-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)

    def stop(self) -> None:
        if not self._thread:
            return
        import ctypes
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]

        msg = wintypes.MSG()
        # Make sure this thread has a message queue before anyone posts to it.
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._thread_id = kernel32.GetCurrentThreadId()

        registered: dict[int, Binding] = {}
        failed = []
        for idx, b in enumerate(self.bindings, start=1):
            repeat = b.action in REPEATING_ACTIONS
            mods = b.modifiers | (0 if repeat else MOD_NOREPEAT)
            if user32.RegisterHotKey(None, idx, mods, b.vk):
                registered[idx] = b
            else:
                failed.append(b.action)  # usually: another app owns it
        self.failed = failed
        self._ready.set()

        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in registered:
                    try:
                        self.on_action(registered[msg.wParam].action)
                    except Exception:  # never let a GUI error kill hotkeys
                        pass
        finally:
            for idx in registered:
                user32.UnregisterHotKey(None, idx)
