"""Peek: hold a hotkey for a strong, short brightness lift (to check a dark
corner), let go and the picture glides back.

Only the brightness tables are changed (through the engine, like every other
look).  Windows only reports that a hotkey was pressed, not when it is let
go, so while peeking the key is checked with ``GetAsyncKeyState``: a plain,
read-only question ("is this key down?"), not a keyboard hook.

Why that works although RegisterHotKey keeps the press from the game:
Windows updates its asynchronous key state table as the key arrives, before
it looks for hotkeys, so GetAsyncKeyState reports the physical key even for
a press it turned into WM_HOTKEY (the release isn't a hotkey and goes through
as usual).  Only the main key is checked, never the modifiers: letting go of
Ctrl or Alt alone doesn't matter, letting go of the main key ends the peek.
Numpad keys: a hotkey on a numpad digit is the NumLock-on code (VK_NUMPAD5
...), and that is the code GetAsyncKeyState knows it by while it is held.
Extended keys (the arrows, Insert, the numpad Enter) share their code with
their twins, which only means holding either twin counts as held.
GetAsyncKeyState answers 0 when it isn't allowed to look (another desktop
such as the lock screen or a UAC prompt; possibly a game running as
administrator), and a quick tap can be over before the press reaches the
peek: a key never seen down still gets a short look (GLANCE_S) instead of
nothing.  See also key_state_check (used by the Windows self-test).
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Callable

STRENGTH_LIMITS = (10.0, 60.0)    # Peek strength slider (points, shared out like auto levels)
DEFAULT_STRENGTH = 35.0
GLIDE_IN_S = 0.13      # quick, so the dark corner shows almost at once
GLIDE_OUT_S = 0.28     # a little slower, so letting go doesn't snap the eyes
STEP_S = 1 / 120       # how often the lift moves (small steps, no visible jump)
KEY_CHECK_S = 0.03     # how often the key is checked while peeking
SAFETY_S = 10.0        # a key "held" this long (or unreadable) ends the peek anyway
# A key never seen down (tapped faster than the press reached CatVision, or
# Windows won't say) still holds the lift this long: a quick look, not nothing.
GLANCE_S = 0.4


def windows_key_down(vk: int) -> bool | None:
    """True while the key is held down, None if that can't be read here."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(int(vk)) & 0x8000)
    except Exception:
        return None


def key_state_check(vk: int = 0x87) -> dict:
    """For the Windows self-test (information only, never raises): can the
    key state be read here, and does a key nobody is holding (F24 by
    default) read as up?  Nothing is pressed or sent."""
    out: dict = {"platform": sys.platform}
    try:
        if sys.platform != "win32":
            out["skipped"] = "not Windows"
            return out
        import ctypes
        # (A SHORT: only the low 16 bits of what ctypes hands back count.)
        raw = int(ctypes.windll.user32.GetAsyncKeyState(int(vk))) & 0xFFFF
        out["raw"] = raw
        out["unpressed_reads_up"] = not raw & 0x8000
        out["windows_key_down"] = windows_key_down(vk)
    except Exception as exc:
        out["error"] = repr(exc)
    return out


def ease(t: float) -> float:
    """0..1 -> 0..1, gentle at both ends (smoothstep)."""
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


class PeekRun:
    """One peek, worked out step by step from the time (no thread, no
    Windows), so it can be tested with a fake clock and key."""

    def __init__(self, strength: float, now: float,
                 key_down: Callable[[], bool | None]):
        self.strength = float(strength)
        self.key_down = key_down
        self.started = now
        self.level = 0.0          # 0 = no lift, 1 = full Peek strength
        self.held = True
        self.last = now
        self.checked = -1e9       # when the key was last checked
        self.seen_down = False    # the key has been read as held during this peek

    def hold_again(self, now: float) -> bool:
        """Pressed again: while gliding back, lift again from where it is (and
        count the safety time afresh).  False if it is still held anyway."""
        if self.held:
            return False
        self.held = True
        self.started = now
        self.checked = -1e9
        return True

    def step(self, now: float) -> tuple[float, bool]:
        """(how much to lift now, finished)."""
        if self.held:
            if now - self.started >= SAFETY_S:
                self.held = False
            elif now - self.checked >= KEY_CHECK_S:
                self.checked = now
                down = self.key_down()
                if down:
                    self.seen_down = True
                # Can't tell (not Windows): hold until the safety time.
                # Never seen down: a quick look (see GLANCE_S).
                self.held = (down is None or bool(down)
                             or not self.seen_down and now - self.started < GLANCE_S)
        dt = max(0.0, now - self.last)
        self.last = now
        if self.held:
            self.level = min(1.0, self.level + dt / GLIDE_IN_S)
        else:
            self.level = max(0.0, self.level - dt / GLIDE_OUT_S)
        amount = round(self.strength * ease(self.level), 2)
        return amount, (not self.held and self.level <= 0.0)


class Peeker:
    """Runs a peek on its own small thread, calling ``apply(amount)`` about
    120 times a second while the lift moves and once with 0 at the end.

    ``apply`` must be thread-safe (the engine is)."""

    # perf_counter, not monotonic: on Windows monotonic only moves every
    # 15.6 ms, which would make the steps uneven (some double, some none).
    def __init__(self, key_down: Callable[[int], bool | None] = windows_key_down,
                 clock: Callable[[], float] = time.perf_counter):
        self.key_down = key_down
        self.clock = clock
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._run_now: PeekRun | None = None

    @property
    def peeking(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def press(self, vk: int | None, strength: float,
              apply: Callable[[float], None]) -> bool:
        """Start a peek.  Pressed again while gliding back: lift again.
        False (and nothing changes) while the peek is still held."""
        with self._lock:
            if self.peeking and self._run_now is not None:
                return self._run_now.hold_again(self.clock())
            self._stop = threading.Event()
            run = self._run_now = PeekRun(strength, self.clock(),
                                          lambda: None if vk is None else self.key_down(vk))
            self._thread = threading.Thread(target=self._run, args=(run, apply, self._stop),
                                            name="catvision-peek", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        """End at once without gliding (CatVision off, quit...).  The thread
        sends 0 as it ends; turning CatVision off retires the engine's lift
        itself too, so even a late step can't bring it back."""
        with self._lock:
            self._stop.set()
            self._run_now = None   # a press from now on starts a new peek
            t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)

    def _run(self, run: PeekRun, apply: Callable[[float], None], stop: threading.Event) -> None:
        last = None
        due = time.perf_counter()
        try:
            while not stop.is_set():
                with self._lock:   # (a press may lift it again meanwhile)
                    amount, done = run.step(self.clock())
                    if done and self._run_now is run:
                        self._run_now = None
                if amount != last:
                    last = amount
                    apply(amount)
                if done:
                    break
                # A steady beat with time.sleep, which is precise on Windows
                # (Python 3.11+); waiting on an Event snaps to Windows' 15.6 ms
                # clock tick and would halve the number of steps.
                due += STEP_S
                now = time.perf_counter()
                if due < now - STEP_S:   # fell well behind (a stall): restart the beat
                    due = now
                if due > now:
                    time.sleep(due - now)
        except Exception:   # never leave a lift behind because of an error
            pass
        finally:
            if last:
                try:
                    apply(0.0)
                except Exception:
                    pass
