"""Global hotkeys using the plain Windows ``RegisterHotKey`` API, plus a
small read-only key poller.

Combinations with Ctrl, Alt or Win, and the keys games rarely need (F-keys,
Pause, Scroll Lock; alone or with Shift, as in 1.18) use ``RegisterHotKey``:
the same mechanism apps like Discord and OBS use for push-to-talk / record
shortcuts.  Windows tells CatVision when the combination is pressed (and the
game doesn't get it).

Other plain keys, other Shift-only combinations and anything with a mouse
button can't use RegisterHotKey without taking the key away from the game, so a tiny
thread (KeyPoller) asks Windows "is this key / button down?" with
``GetAsyncKeyState`` about 60 times a second, only for those bound keys.  It
is a read-only question: no keyboard or mouse hook, nothing is sent, and the
game gets the key or button as usual (it is shared, not taken).

Nothing here installs a hook or touches the game.

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

# Mouse buttons (virtual-key codes GetAsyncKeyState knows them by).
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON, VK_XBUTTON1, VK_XBUTTON2 = 0x01, 0x02, 0x04, 0x05, 0x06
MOUSE_BUTTONS: dict[str, int] = {
    "Left mouse": VK_LBUTTON, "Right mouse": VK_RBUTTON, "Middle mouse": VK_MBUTTON,
    "Mouse 4": VK_XBUTTON1, "Mouse 5": VK_XBUTTON2,
}
MOUSE_NAME: dict[int, str] = {vk: name for name, vk in MOUSE_BUTTONS.items()}
# Other spellings people (or an edited config file) may use.
_MOUSE_ALIASES = {
    "left mouse": VK_LBUTTON, "mouse left": VK_LBUTTON, "left click": VK_LBUTTON,
    "mouse 1": VK_LBUTTON, "lmb": VK_LBUTTON,
    "right mouse": VK_RBUTTON, "mouse right": VK_RBUTTON, "right click": VK_RBUTTON,
    "mouse 2": VK_RBUTTON, "rmb": VK_RBUTTON,
    "middle mouse": VK_MBUTTON, "mouse middle": VK_MBUTTON, "middle click": VK_MBUTTON,
    "middle": VK_MBUTTON, "mouse 3": VK_MBUTTON, "mmb": VK_MBUTTON, "wheel click": VK_MBUTTON,
    "mouse 4": VK_XBUTTON1, "mouse4": VK_XBUTTON1, "mouse back": VK_XBUTTON1,
    "back mouse": VK_XBUTTON1, "xbutton1": VK_XBUTTON1, "mouse 4 / back": VK_XBUTTON1,
    "mouse 5": VK_XBUTTON2, "mouse5": VK_XBUTTON2, "mouse forward": VK_XBUTTON2,
    "forward mouse": VK_XBUTTON2, "xbutton2": VK_XBUTTON2, "mouse 5 / forward": VK_XBUTTON2,
}
# Left / right only with a modifier: a plain click would fire all the time.
NEEDS_MODIFIER_BUTTONS = {VK_LBUTTON, VK_RBUTTON}

# Modifier keys as GetAsyncKeyState knows them (either side counts).
VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN = 0x10, 0x11, 0x12, 0x5B, 0x5C

# Keys games rarely need: alone or with Shift they keep using RegisterHotKey
# (taken from the game) exactly as in 1.18, so an existing F8 / Shift+F8 /
# Pause binding never starts reaching the game.  Other keys without Ctrl, Alt
# or Win are shared with the game (watched by the key poller, see is_polled).
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
    vk = _KEY_LOOKUP.get(" ".join(key.lower().split()))
    if vk is None:
        return None
    return mod_value, vk


_KEY_LOOKUP: dict[str, int] = {k.lower(): v for k, v in KEYS.items()}
_KEY_LOOKUP.update({k.lower(): v for k, v in MOUSE_BUTTONS.items()})
_KEY_LOOKUP.update(_MOUSE_ALIASES)


def key_name(vk: int) -> str:
    return MOUSE_NAME.get(vk) or VK_TO_NAME.get(vk, f"Key {vk}")


def format_combo(modifiers: int, vk: int) -> str:
    parts = [name for name, value in MODIFIER_NAMES if modifiers & value]
    parts.append(key_name(vk))
    return "+".join(parts)


def is_mouse(vk: int) -> bool:
    return vk in MOUSE_NAME


def is_polled(modifiers: int, vk: int) -> bool:
    """True for bindings the key poller watches (shared with the game):
    mouse buttons, and plain keys and Shift-only combinations other than the
    FREE_KEYS.  The rest (with Ctrl, Alt or Win, or a FREE_KEYS key such as
    F8 or Shift+F8) use RegisterHotKey and are taken from the game."""
    if is_mouse(vk):
        return True
    if modifiers & (MOD_CONTROL | MOD_ALT | MOD_WIN):
        return False
    return VK_TO_NAME.get(vk) not in FREE_KEYS


def combo_is_shared(text: str) -> bool:
    """True if the game also gets this key / button (see is_polled)."""
    parsed = parse_combo(text)
    return bool(parsed) and is_polled(*parsed)


def normalize_combo(text: str) -> str | None:
    parsed = parse_combo(text)
    return format_combo(*parsed) if parsed else None


def combo_problem(text: str) -> str | None:
    """Explain why a combination is a bad idea, or None if it's fine."""
    parsed = parse_combo(text)
    if parsed is None:
        return "That key combination isn't recognised."
    mods, vk = parsed
    if vk in NEEDS_MODIFIER_BUTTONS:
        if not mods:
            return (f"{MOUSE_NAME[vk].split()[0]} click needs Ctrl, Alt, Shift or Win with it: "
                    "a plain click would fire all the time while you play.")
        return None
    key = VK_TO_NAME.get(vk, "")
    if mods & MOD_CONTROL and mods & MOD_ALT and key == "Delete":
        return "Ctrl+Alt+Delete is reserved by Windows."
    return None


SHARED_NOTE = ("Mouse buttons, and keys without Ctrl, Alt or Win (other than F-keys, "
               "Pause and Scroll Lock), are shared: the game gets them too (CatVision "
               "only watches them).")


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
    "auto_levels": "Auto mode on / off (beta)",
    "auto_keep": "Auto levels: keep this look (beta)",
    "auto_brighter": "Auto levels: aim brighter (beta)",
    "auto_darker": "Auto levels: aim darker (beta)",
    "peek": "Peek (hold to see in the dark)",
    "show_window": "Show the CatVision window",
}

REPEATING_ACTIONS = {"boost_up", "boost_down"}
# Actions that last while their key is held (Peek): the press starts them and
# CatVision checks the key itself to see when it is let go (Windows only
# reports presses).  Never repeating, so holding gives exactly one press.
HOLD_ACTIONS = {"peek"}

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
    "auto_brighter": "Ctrl+Alt+]",
    "auto_darker": "Ctrl+Alt+[",
    "peek": "Ctrl+Alt+P",
    "show_window": "Ctrl+Alt+C",
}

# Versions 1.1-1.3 came with exactly this set already applied; used only to
# recognise (and clear) those untouched old defaults when upgrading.
# Spelled out (not built from _SUGGESTED_KEYS) so actions added later, such as
# the auto levels ones, never stop an untouched 1.1-1.3 set from matching.
LEGACY_DEFAULT_HOTKEYS = dict({
    "toggle": "Ctrl+Alt+V",
    "off": "Ctrl+Alt+0",
    "next": "Ctrl+Alt+Page Down",
    "prev": "Ctrl+Alt+Page Up",
    "boost_up": "Ctrl+Alt+=",
    "boost_down": "Ctrl+Alt+-",
    "filter_next": "Ctrl+Alt+F",
    "show_window": "Ctrl+Alt+C",
}, **{
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
        if parsed[1] in NEEDS_MODIFIER_BUTTONS and not parsed[0]:
            continue   # a plain left / right click (hand-edited config): never a hotkey
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
        self.poller: KeyPoller | None = None

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def key_of(self, action: str) -> int | None:
        """The key or mouse button (virtual-key code, without modifiers) of
        an action's hotkey, so a hold action can tell when it is let go."""
        for b in self.bindings:
            if b.action == action:
                return b.vk
        return None

    @property
    def registered_bindings(self) -> list[Binding]:
        return [b for b in self.bindings if not is_polled(b.modifiers, b.vk)]

    @property
    def polled_bindings(self) -> list[Binding]:
        return [b for b in self.bindings if is_polled(b.modifiers, b.vk)]

    def start(self, bindings: list[Binding]) -> None:
        self.stop()
        self.bindings = list(bindings)
        self.failed = []
        if not self.supported or not self.bindings:
            return
        if self.registered_bindings:
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, name="catvision-hotkeys",
                                            daemon=True)
            self._thread.start()
            self._ready.wait(timeout=2)
        if self.polled_bindings:
            self.poller = KeyPoller(self.polled_bindings, self.on_action,
                                    shadow=self.registered_bindings,
                                    in_front=catvision_in_front)
            self.poller.start()

    def stop(self) -> None:
        poller, self.poller = self.poller, None
        if poller is not None:
            poller.stop()
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
        for idx, b in enumerate(self.registered_bindings, start=1):
            repeat = b.action in REPEATING_ACTIONS and b.action not in HOLD_ACTIONS
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


# ---------------------------------------------------------------------------
# Key poller: plain keys, Shift combos and mouse buttons (shared with the game)
# ---------------------------------------------------------------------------

POLL_S = 0.016          # how often the bound keys are read (~60 times a second)
REPEAT_DELAY_S = 0.5    # held this long, a repeating action starts repeating...
REPEAT_EVERY_S = 0.1    # ...this often (like keyboard auto-repeat, a bit calmer)

# Modifier -> the keys that count as it (either side).
_MODIFIER_VKS = [(MOD_CONTROL, (VK_CONTROL,)), (MOD_ALT, (VK_MENU,)),
                 (MOD_SHIFT, (VK_SHIFT,)), (MOD_WIN, (VK_LWIN, VK_RWIN))]


def async_key_down(vk: int) -> bool | None:
    """GetAsyncKeyState: True while the key / button is held, None if it
    can't be read here (not Windows).  A read-only question, no hook."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return None


def catvision_in_front() -> bool:
    """True while one of CatVision's own windows is in front (Windows; asks
    only which process owns the foreground window, opens nothing)."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        import os
        from ctypes import wintypes
        global _USER32_FG
        if _USER32_FG is None:
            # Own user32 handle with full-width HWND types (the shared
            # ctypes.windll one is left as other modules set it up).
            u = ctypes.WinDLL("user32")
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetForegroundWindow.argtypes = []
            u.GetWindowThreadProcessId.restype = wintypes.DWORD
            u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            _USER32_FG = u
        hwnd = _USER32_FG.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD(0)
        _USER32_FG.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return False


_USER32_FG = None


class KeyPoller:
    """Watches a few keys / mouse buttons with GetAsyncKeyState and reports
    presses (the press edge), on its own small thread.

    - Only the bound keys are read every POLL_S; the modifiers only when one
      of them goes down.
    - A key already held when watching starts doesn't count until let go.
    - Repeating actions repeat while held (REPEAT_DELAY_S, REPEAT_EVERY_S).
      Hold actions (Peek) fire once: Peek then reads the same key itself to
      see when it is let go.
    - Modifiers: a binding fires when all its modifiers are held (extra ones,
      such as Shift for sprinting, don't stop it); the binding with the most
      modifiers wins.  ``shadow`` are the RegisterHotKey bindings: when one of
      them is the best match the poller stays quiet (Windows reports that one),
      so e.g. "X" and "Ctrl+Alt+X" never both fire.
    - ``in_front`` (CatVision's own window in front): keyboard keys and the
      left / right buttons are left alone, so naming a preset or Shift-clicking
      in a list doesn't fire a hotkey.  Middle, Mouse 4 and Mouse 5 still work
      there (they do nothing else in the app; handy to try one out).

    ``key_down``, ``in_front`` and ``clock`` can be replaced for tests; ``tick`` does one
    pass without any thread.
    """

    def __init__(self, bindings: list[Binding], on_action: Callable[[str], None],
                 shadow: list[Binding] = (), key_down: Callable[[int], bool | None] = async_key_down,
                 clock: Callable[[], float] | None = None,
                 in_front: Callable[[], bool] = lambda: False):
        import time
        self.in_front = in_front
        self.bindings = list(bindings)
        self.shadow = list(shadow)
        self.on_action = on_action
        self.key_down = key_down
        self.clock = clock or time.perf_counter
        self.vks = sorted({b.vk for b in self.bindings})
        self._down: dict[int, bool] | None = None   # None: first pass only looks
        self._held: dict[int, tuple[Binding, float]] = {}  # vk -> (binding, next repeat)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- one pass (tested directly) ---------------------------------------
    def _mods_held(self) -> int:
        mods = 0
        for value, vks in _MODIFIER_VKS:
            if any(self.key_down(vk) for vk in vks):
                mods |= value
        return mods

    def _match(self, vk: int) -> Binding | None:
        """The binding a press of ``vk`` means now (None: none, or one that
        RegisterHotKey reports itself)."""
        candidates = [b for b in self.bindings + self.shadow if b.vk == vk]
        if not candidates:
            return None
        held = self._mods_held() if any(b.modifiers for b in candidates) else 0
        fits = [b for b in candidates if b.modifiers & held == b.modifiers]
        if not fits:
            return None
        best = max(fits, key=lambda b: bin(b.modifiers).count("1"))
        if best in self.shadow or best not in self.bindings:
            return None
        if best.vk in NEEDS_MODIFIER_BUTTONS and not best.modifiers:
            return None   # never a plain left / right click
        if (not is_mouse(best.vk) or best.vk in NEEDS_MODIFIER_BUTTONS) and self.in_front():
            # Typing in CatVision's own window, or a Ctrl / Shift click on
            # its lists and buttons: that is using the app, not a hotkey.
            return None
        return best

    def _fire(self, action: str) -> None:
        try:
            self.on_action(action)
        except Exception:   # never let a GUI error kill the poller
            pass

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        first = self._down is None
        if first:
            self._down = {}
        for vk in self.vks:
            down = bool(self.key_down(vk))
            was = self._down.get(vk, True if first else False)
            self._down[vk] = down
            if not down:
                self._held.pop(vk, None)
                continue
            if first or was:
                held = self._held.get(vk)
                if held is not None and now >= held[1]:
                    nxt = held[1] + REPEAT_EVERY_S
                    self._held[vk] = (held[0], nxt if nxt > now else now + REPEAT_EVERY_S)
                    self._fire(held[0].action)
                continue
            b = self._match(vk)          # a fresh press
            if b is None:
                continue
            if b.action in REPEATING_ACTIONS and b.action not in HOLD_ACTIONS:
                self._held[vk] = (b, now + REPEAT_DELAY_S)
            self._fire(b.action)

    # -- the thread --------------------------------------------------------
    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> None:
        if self.running or not self.vks:
            return
        self._stop = threading.Event()
        self._down = None
        self._held = {}
        self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                        name="catvision-keypoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)

    def _run(self, stop: threading.Event) -> None:
        import time
        due = time.perf_counter()
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            # time.sleep pacing like the Fader (precise on Windows, 3.11+).
            due += POLL_S
            now = time.perf_counter()
            if due < now - POLL_S:
                due = now
            if due > now:
                time.sleep(due - now)
