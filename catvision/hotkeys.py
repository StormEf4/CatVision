"""Global hotkeys using the plain Windows ``RegisterHotKey`` API.

This is the same mechanism apps like Discord and OBS use for push-to-talk /
record shortcuts.  It does not install a keyboard hook and never touches the
game: Windows simply tells CatVision when the key combination is pressed.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import Callable

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

VK_PRIOR = 0x21   # Page Up
VK_NEXT = 0x22    # Page Down
VK_OEM_PLUS = 0xBB
VK_OEM_MINUS = 0xBD


@dataclass(frozen=True)
class Hotkey:
    action: str
    modifiers: int
    vk: int
    label: str
    repeat: bool = False


def default_hotkeys() -> list[Hotkey]:
    ca = MOD_CONTROL | MOD_ALT
    keys = [
        Hotkey("toggle", ca, ord("V"), "Ctrl+Alt+V"),
        Hotkey("off", ca, ord("0"), "Ctrl+Alt+0"),
        Hotkey("next", ca, VK_NEXT, "Ctrl+Alt+Page Down"),
        Hotkey("prev", ca, VK_PRIOR, "Ctrl+Alt+Page Up"),
        Hotkey("boost_up", ca, VK_OEM_PLUS, "Ctrl+Alt+=", repeat=True),
        Hotkey("boost_down", ca, VK_OEM_MINUS, "Ctrl+Alt+-", repeat=True),
    ]
    for n in range(1, 10):
        keys.append(Hotkey(f"preset:{n}", ca, ord(str(n)), f"Ctrl+Alt+{n}"))
    return keys


HOTKEY_HELP = [
    ("Ctrl+Alt+V", "Turn CatVision on / off"),
    ("Ctrl+Alt+1 ... 9", "Switch to preset 1-9"),
    ("Ctrl+Alt+Page Down / Up", "Next / previous preset"),
    ("Ctrl+Alt+=  /  Ctrl+Alt+-", "More / less shadow boost"),
    ("Ctrl+Alt+0", "Panic button: everything off"),
]


class HotkeyListener:
    """Registers hotkeys on a background thread and reports presses.

    ``on_action`` is called from the background thread, so the GUI must hand
    the work back to its own thread.
    """

    def __init__(self, hotkeys: list[Hotkey], on_action: Callable[[str], None]):
        self.hotkeys = hotkeys
        self.on_action = on_action
        self.failed: list[str] = []
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def start(self) -> None:
        if not self.supported or self._thread:
            return
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
        self._ready.clear()

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

        registered: dict[int, Hotkey] = {}
        self.failed = []
        for idx, hk in enumerate(self.hotkeys, start=1):
            mods = hk.modifiers | (0 if hk.repeat else MOD_NOREPEAT)
            if user32.RegisterHotKey(None, idx, mods, hk.vk):
                registered[idx] = hk
            else:
                self.failed.append(hk.label)  # usually: another app owns it
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
