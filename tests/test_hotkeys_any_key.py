"""Any key, Shift combos and mouse buttons as hotkeys (the read-only key poller).

The poller is driven with a fake key-state function and a fake clock (no
thread, no Windows).  The GUI capture tests only run when
CATVISION_GUI_TESTS is set (tkinter plus a display).
"""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from catvision import hotkeys as hk
from catvision import config as cm


class ParseFormatTest(unittest.TestCase):
    def test_mouse_buttons_round_trip(self):
        for combo, parsed in [
            ("Mouse 4", (0, hk.VK_XBUTTON1)),
            ("Mouse 5", (0, hk.VK_XBUTTON2)),
            ("Middle mouse", (0, hk.VK_MBUTTON)),
            ("Ctrl+Mouse 5", (hk.MOD_CONTROL, hk.VK_XBUTTON2)),
            ("Alt+Shift+Mouse 4", (hk.MOD_ALT | hk.MOD_SHIFT, hk.VK_XBUTTON1)),
            ("Win+Middle mouse", (hk.MOD_WIN, hk.VK_MBUTTON)),
            ("Ctrl+Left mouse", (hk.MOD_CONTROL, hk.VK_LBUTTON)),
            ("Shift+Right mouse", (hk.MOD_SHIFT, hk.VK_RBUTTON)),
        ]:
            with self.subTest(combo):
                self.assertEqual(hk.parse_combo(combo), parsed)
                self.assertEqual(hk.format_combo(*parsed), combo)
                self.assertEqual(hk.normalize_combo(combo), combo)

    def test_mouse_aliases_normalize(self):
        for text, want in [("mouse 3", "Middle mouse"), ("middle", "Middle mouse"),
                           ("ctrl+middle mouse", "Ctrl+Middle mouse"),
                           ("XButton1", "Mouse 4"), ("mouse back", "Mouse 4"),
                           ("Mouse Forward", "Mouse 5"), ("control+mouse5", "Ctrl+Mouse 5"),
                           ("shift+mouse   4", "Shift+Mouse 4"), ("alt+lmb", "Alt+Left mouse")]:
            with self.subTest(text):
                self.assertEqual(hk.normalize_combo(text), want)

    def test_plain_and_shift_keys_round_trip(self):
        for combo in ["Q", "Shift+Q", "Space", "Shift+F8", "Num 5", "`"]:
            with self.subTest(combo):
                self.assertEqual(hk.normalize_combo(combo), combo)

    def test_bad_ones_still_rejected(self):
        for bad in ["Mouse 6", "Ctrl+Mouse", "Hyper+Mouse 4", "Ctrl+"]:
            self.assertIsNone(hk.parse_combo(bad), bad)

    def test_combo_problems(self):
        for ok in ["Q", "Shift+Q", "Space", "Mouse 4", "Mouse 5", "Middle mouse",
                   "Ctrl+Mouse 5", "Ctrl+Left mouse", "Shift+Right mouse", "Win+Left mouse",
                   "Ctrl+Alt+V", "F8"]:
            self.assertIsNone(hk.combo_problem(ok), ok)
        for bad in ["Left mouse", "Right mouse"]:
            problem = hk.combo_problem(bad)
            self.assertIsNotNone(problem, bad)
            self.assertIn("all the time", problem)
        self.assertIsNotNone(hk.combo_problem("Ctrl+Alt+Delete"))
        self.assertIsNotNone(hk.combo_problem("nonsense"))

    def test_shared_or_registered(self):
        for shared in ["Q", "Shift+Q", "Space", "Shift+Num 5", "Mouse 4", "Ctrl+Mouse 5",
                       "Ctrl+Alt+Middle mouse", "Shift+Right mouse"]:
            self.assertTrue(hk.combo_is_shared(shared), shared)
        for taken in ["Ctrl+Alt+V", "Alt+Q", "Win+F8", "Ctrl+Shift+F12"]:
            self.assertFalse(hk.combo_is_shared(taken), taken)
        self.assertFalse(hk.combo_is_shared(""))
        self.assertFalse(hk.combo_is_shared("nonsense"))

    def test_free_keys_stay_registered_as_in_1_18(self):
        # F-keys, Pause and Scroll Lock (alone or with Shift) were allowed
        # without Ctrl/Alt before the poller existed and were taken from the
        # game with RegisterHotKey.  They must stay that way, so nobody's F8
        # suddenly starts reaching the game too.
        for combo in ["F1", "F8", "F12", "F24", "Shift+F8", "Pause", "Shift+Pause",
                      "Scroll Lock", "Shift+Scroll Lock", "f8", "shift+f8"]:
            self.assertFalse(hk.combo_is_shared(combo), combo)
            mods, vk = hk.parse_combo(combo)
            self.assertFalse(hk.is_polled(mods, vk), combo)
            self.assertIsNone(hk.combo_problem(combo), combo)
        # Every key in FREE_KEYS, checked directly.
        for name in hk.FREE_KEYS:
            self.assertFalse(hk.is_polled(0, hk.KEYS[name]), name)
            self.assertFalse(hk.is_polled(hk.MOD_SHIFT, hk.KEYS[name]), name)

    def test_hand_edited_plain_click_is_never_bound(self):
        b = hk.bindings_from_config({"toggle": "Left mouse", "off": "right click",
                                     "next": "Shift+Left mouse"})
        self.assertEqual([x.action for x in b], ["next"])

    def test_suggested_keys_all_registered(self):
        # The suggestions stay Ctrl+Alt ones (taken from the game, as before).
        for combo in hk.suggested_hotkeys().values():
            self.assertFalse(hk.combo_is_shared(combo), combo)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"CATVISION_CONFIG_DIR": self.tmp})
        env.start()
        self.addCleanup(env.stop)

    def test_round_trip(self):
        c = cm.Config()
        c.hotkeys = {"peek": "Mouse 4", "toggle": "Ctrl+Mouse 5", "off": "Ctrl+Alt+0",
                     "next": "Q", "prev": "Shift+Middle mouse"}
        data = json.loads(json.dumps(c.to_json()))
        back = cm.Config.from_json(data)
        self.assertEqual(back.hotkeys, c.hotkeys)

    def test_old_and_hand_written_configs(self):
        back = cm.Config.from_json({"hotkeys": {"toggle": "ctrl+alt+v", "peek": "mouse 4",
                                                "off": "XButton2", "next": "junk", "prev": ""}})
        self.assertEqual(back.hotkeys, {"toggle": "Ctrl+Alt+V", "peek": "Mouse 4",
                                        "off": "Mouse 5", "next": "", "prev": ""})
        # An untouched 1.1-1.3 default set is still cleared.
        old = cm.Config.from_json({"hotkeys": dict(hk.LEGACY_DEFAULT_HOTKEYS)})
        self.assertEqual(old.hotkeys, {})


class Keys:
    """Fake key state: a set of held virtual-key codes, counting reads."""

    def __init__(self):
        self.down: set[int] = set()
        self.reads: list[int] = []

    def __call__(self, vk: int) -> bool:
        self.reads.append(vk)
        return vk in self.down


def poller(config: dict, keys: Keys, shadow_config: dict | None = None):
    fired: list[str] = []
    b = hk.bindings_from_config(config)
    shadow = hk.bindings_from_config(shadow_config or {})
    p = hk.KeyPoller(b, fired.append, shadow=shadow, key_down=keys, clock=lambda: 0.0)
    return p, fired


class PollerTest(unittest.TestCase):
    def test_press_edge_fires_once(self):
        keys = Keys()
        p, fired = poller({"toggle": "Mouse 4"}, keys)
        p.tick(0.0)
        keys.down.add(hk.VK_XBUTTON1)
        for i in range(1, 50):
            p.tick(i * 0.016)
        self.assertEqual(fired, ["toggle"])
        keys.down.clear()
        p.tick(1.0)
        keys.down.add(hk.VK_XBUTTON1)
        p.tick(1.02)
        self.assertEqual(fired, ["toggle", "toggle"])

    def test_held_at_start_does_not_fire(self):
        keys = Keys()
        keys.down.add(hk.VK_MBUTTON)
        p, fired = poller({"toggle": "Middle mouse"}, keys)
        for i in range(20):
            p.tick(i * 0.016)
        self.assertEqual(fired, [])
        keys.down.clear()
        p.tick(1.0)
        keys.down.add(hk.VK_MBUTTON)
        p.tick(1.016)
        self.assertEqual(fired, ["toggle"])

    def test_repeating_action_repeats_while_held(self):
        keys = Keys()
        p, fired = poller({"boost_up": "Mouse 5", "next": "Q"}, keys)
        p.tick(0.0)
        keys.down |= {hk.VK_XBUTTON2, ord("Q")}
        t = 0.0
        while t < 1.0:
            t += 0.016
            p.tick(t)
        ups = fired.count("boost_up")
        # 1 at the press, then every REPEAT_EVERY_S after REPEAT_DELAY_S.
        want = 1 + int((1.0 - hk.REPEAT_DELAY_S) / hk.REPEAT_EVERY_S)
        self.assertTrue(want - 1 <= ups <= want + 1, (ups, want))
        self.assertEqual(fired.count("next"), 1)       # not a repeating action
        keys.down.clear()
        p.tick(t + 0.016)
        n = len(fired)
        for i in range(30):
            p.tick(t + 0.1 + i * 0.016)
        self.assertEqual(len(fired), n)                 # let go: no more

    def test_no_burst_after_a_stall(self):
        keys = Keys()
        p, fired = poller({"boost_down": "Q"}, keys)
        p.tick(0.0)
        keys.down.add(ord("Q"))
        p.tick(0.01)
        p.tick(5.0)       # the thread stalled for seconds
        p.tick(5.016)
        self.assertEqual(fired.count("boost_down"), 2)

    def test_peek_fires_once_and_reads_its_button(self):
        keys = Keys()
        p, fired = poller({"peek": "Mouse 4"}, keys)
        p.tick(0.0)
        keys.down.add(hk.VK_XBUTTON1)
        for i in range(1, 200):
            p.tick(i * 0.016)
        self.assertEqual(fired, ["peek"])               # hold: one press, no repeat
        listener = hk.HotkeyListener(lambda a: None)
        listener.bindings = hk.bindings_from_config({"peek": "Mouse 4"})
        self.assertEqual(listener.key_of("peek"), hk.VK_XBUTTON1)

    def test_peek_hold_and_release_with_mouse_button(self):
        from catvision import peek
        keys = Keys()
        p, fired = poller({"peek": "Mouse 4"}, keys)
        p.tick(0.0)
        keys.down.add(hk.VK_XBUTTON1)
        p.tick(0.016)
        self.assertEqual(fired, ["peek"])
        run = peek.PeekRun(30.0, 0.0, lambda: keys(hk.VK_XBUTTON1))
        t = 0.0
        while t < 1.0:
            t += 1 / 120
            amount, done = run.step(t)
        self.assertGreater(amount, 29)
        self.assertFalse(done)
        keys.down.clear()          # let go of the button: the peek glides back
        while not done and t < 3.0:
            t += 1 / 120
            amount, done = run.step(t)
        self.assertTrue(done)
        self.assertEqual(amount, 0)

    def test_modifier_combos(self):
        keys = Keys()
        p, fired = poller({"toggle": "Ctrl+Mouse 5", "next": "Mouse 5",
                           "off": "Shift+Left mouse"}, keys)
        p.tick(0.0)
        keys.down.add(hk.VK_XBUTTON2)
        p.tick(0.016)
        self.assertEqual(fired, ["next"])
        keys.down.clear()
        p.tick(0.032)
        keys.down |= {hk.VK_CONTROL, hk.VK_XBUTTON2}
        p.tick(0.048)
        self.assertEqual(fired, ["next", "toggle"])     # the more specific one wins
        keys.down = {hk.VK_LBUTTON}
        p.tick(0.064)
        self.assertEqual(fired, ["next", "toggle"])     # plain left click: nothing
        keys.down = {hk.VK_SHIFT}
        p.tick(0.08)
        keys.down = {hk.VK_SHIFT, hk.VK_LBUTTON}
        p.tick(0.096)
        self.assertEqual(fired, ["next", "toggle", "off"])

    def test_extra_modifier_does_not_block_plain_binding(self):
        # Holding Shift to sprint must not stop a plain Mouse 4 peek.
        keys = Keys()
        p, fired = poller({"peek": "Mouse 4"}, keys)
        p.tick(0.0)
        keys.down |= {hk.VK_SHIFT, hk.VK_XBUTTON1}
        p.tick(0.016)
        self.assertEqual(fired, ["peek"])

    def test_missing_modifier_does_not_fire(self):
        keys = Keys()
        p, fired = poller({"toggle": "Ctrl+Mouse 4"}, keys)
        p.tick(0.0)
        keys.down.add(hk.VK_XBUTTON1)
        p.tick(0.016)
        self.assertEqual(fired, [])
        keys.down.clear()
        p.tick(0.03)
        keys.down |= {hk.VK_RWIN}
        keys.down.add(hk.VK_XBUTTON1)
        p.tick(0.05)
        self.assertEqual(fired, [])

    def test_own_window_in_front_ignores_keys_not_buttons(self):
        keys = Keys()
        fired = []
        p = hk.KeyPoller(hk.bindings_from_config({"next": "Q", "peek": "Mouse 4",
                                                  "toggle": "Ctrl+Alt+Middle mouse"}),
                         fired.append, key_down=keys, in_front=lambda: True)
        p.tick(0.0)
        keys.down |= {ord("Q"), hk.VK_XBUTTON1}
        p.tick(0.016)
        self.assertEqual(fired, ["peek"])
        self.assertFalse(hk.catvision_in_front() and os.name != "nt")

    def test_own_window_in_front_ignores_left_right_clicks(self):
        # Shift- / Ctrl-clicking in CatVision's own lists is using the app,
        # not a hotkey.  In the game (CatVision not in front) they fire.
        keys = Keys()
        fired = []
        front = [True]
        p = hk.KeyPoller(hk.bindings_from_config({"next": "Shift+Left mouse",
                                                  "prev": "Ctrl+Right mouse",
                                                  "peek": "Middle mouse"}),
                         fired.append, key_down=keys, in_front=lambda: front[0])
        p.tick(0.0)
        keys.down |= {hk.VK_SHIFT, hk.VK_LBUTTON}
        p.tick(0.016)
        keys.down = {hk.VK_CONTROL, hk.VK_RBUTTON}
        p.tick(0.032)
        keys.down = {hk.VK_MBUTTON}
        p.tick(0.048)
        self.assertEqual(fired, ["peek"])
        front[0] = False
        keys.down = {hk.VK_SHIFT}
        p.tick(0.064)
        keys.down = {hk.VK_SHIFT, hk.VK_LBUTTON}
        p.tick(0.080)
        self.assertEqual(fired, ["peek", "next"])

    def test_win_either_side(self):
        keys = Keys()
        p, fired = poller({"toggle": "Win+Mouse 4"}, keys)
        p.tick(0.0)
        keys.down |= {hk.VK_RWIN, hk.VK_XBUTTON1}
        p.tick(0.016)
        self.assertEqual(fired, ["toggle"])

    def test_registered_combo_on_same_key_is_left_to_windows(self):
        # "X" polled, "Ctrl+Alt+X" registered: Ctrl+Alt+X must not also fire "X".
        keys = Keys()
        p, fired = poller({"next": "X"}, keys, shadow_config={"toggle": "Ctrl+Alt+X"})
        p.tick(0.0)
        keys.down |= {hk.VK_CONTROL, hk.VK_MENU, ord("X")}
        p.tick(0.016)
        self.assertEqual(fired, [])
        keys.down = {ord("X")}
        p.tick(0.03)
        keys.down.clear()
        p.tick(0.05)
        keys.down.add(ord("X"))
        p.tick(0.07)
        self.assertEqual(fired, ["next"])

    def test_listener_splits_bindings_without_double_fire(self):
        listener = hk.HotkeyListener(lambda a: None)
        listener.bindings = hk.bindings_from_config({
            "toggle": "Ctrl+Alt+V", "peek": "Mouse 4", "next": "Q", "prev": "Shift+Q",
            "off": "Alt+Mouse 5", "boost_up": "Win+F8"})
        self.assertEqual({b.action for b in listener.registered_bindings},
                         {"toggle", "boost_up"})
        self.assertEqual({b.action for b in listener.polled_bindings},
                         {"peek", "next", "prev", "off"})
        # The poller never sees the registered ones, so pressing Ctrl+Alt+V
        # can only be reported by Windows (one press).
        keys = Keys()
        fired = []
        p = hk.KeyPoller(listener.polled_bindings, fired.append,
                         shadow=listener.registered_bindings, key_down=keys)
        p.tick(0.0)
        keys.down |= {hk.VK_CONTROL, hk.VK_MENU, ord("V")}
        p.tick(0.016)
        self.assertEqual(fired, [])
        self.assertNotIn(ord("V"), p.vks)

    def test_listener_keeps_free_keys_on_register_hotkey(self):
        listener = hk.HotkeyListener(lambda a: None)
        listener.bindings = hk.bindings_from_config({
            "toggle": "F8", "off": "Shift+F8", "peek": "Pause", "next": "Scroll Lock",
            "prev": "Q", "boost_up": "Shift+Mouse 4"})
        self.assertEqual({b.action for b in listener.registered_bindings},
                         {"toggle", "off", "peek", "next"})
        self.assertEqual({b.action for b in listener.polled_bindings}, {"prev", "boost_up"})
        # The poller never reads (or fires) the F-keys / Pause.
        keys = Keys()
        fired = []
        p = hk.KeyPoller(listener.polled_bindings, fired.append,
                         shadow=listener.registered_bindings, key_down=keys)
        self.assertEqual(set(p.vks), {ord("Q"), hk.VK_XBUTTON1})
        p.tick(0.0)
        keys.down |= {hk.KEYS["F8"], hk.KEYS["Pause"], hk.VK_SHIFT}
        p.tick(0.016)
        self.assertEqual(fired, [])
        # Peek on a registered key: its key is still known for the release.
        self.assertEqual(listener.key_of("peek"), hk.KEYS["Pause"])

    def test_only_bound_keys_are_read(self):
        keys = Keys()
        p, fired = poller({"next": "Q", "peek": "Mouse 4", "toggle": "Ctrl+Mouse 5"}, keys)
        for i in range(10):
            p.tick(i * 0.016)
        self.assertEqual(set(keys.reads), {ord("Q"), hk.VK_XBUTTON1, hk.VK_XBUTTON2})

    def test_callback_errors_do_not_stop_it(self):
        keys = Keys()

        def boom(action):
            raise RuntimeError(action)
        p = hk.KeyPoller(hk.bindings_from_config({"next": "Q"}), boom, key_down=keys)
        p.tick(0.0)
        keys.down.add(ord("Q"))
        p.tick(0.02)    # no exception escapes

    def test_thread_starts_and_stops_cleanly(self):
        keys = Keys()
        fired = []
        got = threading.Event()

        def on_action(a):
            fired.append(a)
            got.set()
        p = hk.KeyPoller(hk.bindings_from_config({"toggle": "Mouse 4"}), on_action,
                         key_down=keys)
        p.start()
        self.assertTrue(p.running)
        time.sleep(0.06)
        keys.down.add(hk.VK_XBUTTON1)
        self.assertTrue(got.wait(1.0))
        t0 = time.perf_counter()
        p.stop()
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertFalse(p.running)
        self.assertEqual(fired, ["toggle"])
        names = [t.name for t in threading.enumerate()]
        self.assertNotIn("catvision-keypoller", names)
        p.stop()          # twice is fine

    def test_poller_reads_about_every_16ms(self):
        keys = Keys()
        p = hk.KeyPoller(hk.bindings_from_config({"toggle": "Mouse 4"}), lambda a: None,
                         key_down=keys)
        p.start()
        time.sleep(0.5)
        p.stop()
        # ~31 passes in 0.5 s; generous bounds for a busy CI machine.
        self.assertTrue(10 <= len(keys.reads) <= 45, len(keys.reads))

    def test_listener_off_windows_starts_nothing(self):
        listener = hk.HotkeyListener(lambda a: None)
        with mock.patch.object(hk.sys, "platform", "linux"):
            listener.start(hk.bindings_from_config({"peek": "Mouse 4"}))
        self.assertIsNone(listener.poller)
        self.assertEqual(listener.failed, [])
        self.assertEqual(listener.key_of("peek"), hk.VK_XBUTTON1)
        listener.stop()

    def test_listener_starts_and_stops_poller(self):
        listener = hk.HotkeyListener(lambda a: None)
        with mock.patch.object(hk.HotkeyListener, "supported", True), \
                mock.patch.object(hk, "async_key_down", lambda vk: False):
            # Only polled bindings: no RegisterHotKey thread is needed.
            listener.start(hk.bindings_from_config({"peek": "Mouse 4", "next": "Q"}))
            poller = listener.poller
            self.assertIsNotNone(poller)
            self.assertTrue(poller.running)
            self.assertIsNone(listener._thread)
            self.assertEqual(listener.failed, [])     # poller bindings never fail
            listener.stop()
        self.assertFalse(poller.running)
        self.assertIsNone(listener.poller)

    def test_async_key_down_off_windows(self):
        if os.name != "nt":
            self.assertIsNone(hk.async_key_down(hk.VK_XBUTTON1))


# ---------------------------------------------------------------------------
# GUI: capturing mouse buttons in the Hotkeys tab
# ---------------------------------------------------------------------------

def _gui_ok() -> bool:
    if not os.environ.get("CATVISION_GUI_TESTS"):
        return False
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


class _Fonts:
    normal = bold = big = small = "TkDefaultFont"


@unittest.skipUnless(_gui_ok(), "set CATVISION_GUI_TESTS=1 with tkinter and a display")
class KeyRecorderGuiTest(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def _pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.01)

    def _recorder(self, keys=None):
        from catvision.gui_hotkeys import KeyRecorder
        keys = keys or Keys()
        rec = KeyRecorder(self.root, "Peek", _Fonts(), key_down=keys, run=False)
        rec.deiconify()
        rec.update()
        return rec, keys

    def test_tk_middle_button_is_captured(self):
        from catvision import gui_hotkeys as gh
        rec, _ = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec.event_generate("<ButtonPress-2>", x=5, y=5)
        self.assertEqual(rec.result, "Middle mouse")

    def test_opening_click_is_ignored(self):
        rec, _ = self._recorder()
        rec.event_generate("<ButtonPress-2>", x=5, y=5)   # before it is armed
        self.assertIsNone(rec.result)

    def test_plain_left_click_is_not_taken(self):
        from catvision import gui_hotkeys as gh
        from catvision.gui_hotkeys import KeyRecorder
        # Off Windows: the Tk click.
        rec = KeyRecorder(self.root, "Peek", _Fonts(), key_down=lambda vk: None, run=False)
        rec.deiconify()
        rec.update()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec.event_generate("<ButtonPress-1>", x=5, y=5)
        self.assertIsNone(rec.result)
        self.assertIn("all the time", rec.msg.cget("text"))
        rec.destroy()
        # Windows: the key-state poll.
        rec, keys = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        keys.down.add(hk.VK_LBUTTON)
        self._pump(0.1)
        self.assertIsNone(rec.result)
        self.assertIn("all the time", rec.msg.cget("text"))

    def test_ctrl_left_click_is_taken(self):
        # Off Windows (key state unreadable) the Tk click is used.
        from catvision import gui_hotkeys as gh
        from catvision.gui_hotkeys import KeyRecorder
        rec = KeyRecorder(self.root, "Peek", _Fonts(), key_down=lambda vk: None, run=False)
        rec.deiconify()
        rec.update()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec.held.add("Ctrl")          # (what a Control_L KeyPress does)
        rec.event_generate("<ButtonPress-1>", x=5, y=5)
        self.assertEqual(rec.result, "Ctrl+Left mouse")

    def test_left_right_on_windows_come_from_physical_key_state(self):
        # Where the key state can be read (Windows), a Tk left / right click
        # (the logical button) is ignored: the poll reads the physical button,
        # like the hotkey poller, so swapped buttons record what will fire.
        from catvision import gui_hotkeys as gh
        rec, keys = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec.held.add("Shift")
        rec.event_generate("<ButtonPress-1>", x=5, y=5)   # logical left...
        self.assertIsNone(rec.result)
        keys.down.add(hk.VK_RBUTTON)                       # ...is the physical right
        self._pump(0.1)
        self.assertEqual(rec.result, "Shift+Right mouse")

    def test_esc_cancels_even_after_a_shared_key_was_pressed(self):
        from catvision import gui_hotkeys as gh
        rec, _ = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec._press(mock.Mock(keysym="q", keycode=0))
        self.assertEqual(rec.result, "Q")
        self.assertIn("Shared", rec.msg.cget("text"))
        rec._press(mock.Mock(keysym="Shift_L", keycode=0))
        self.assertEqual(rec.combo.cget("text"), "Q")     # still shows the capture
        rec._release(mock.Mock(keysym="Shift_L", keycode=0))
        rec._press(mock.Mock(keysym="Escape", keycode=0))
        self.assertIsNone(rec.result)
        self.assertFalse(rec.winfo_exists())

    def test_key_held_when_opened_is_not_recorded_by_auto_repeat(self):
        # Space on the Change button (or Enter in the preset picker) held
        # into auto-repeat must not become the new hotkey.
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down.add(hk.KEYS["Space"])
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec._press(mock.Mock(keysym="space", keycode=hk.KEYS["Space"]))   # a repeat
        self.assertIsNone(rec.result)
        rec._release(mock.Mock(keysym="space", keycode=hk.KEYS["Space"]))
        keys.down.clear()
        rec._press(mock.Mock(keysym="space", keycode=hk.KEYS["Space"]))   # a real press
        self.assertEqual(rec.result, "Space")

    def test_key_held_when_opened_counts_once_seen_up(self):
        # (Its release went elsewhere: the poll notices it is up.)
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down.add(hk.KEYS["Enter"])
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.05)
        keys.down.clear()
        self._pump(0.1)
        rec._press(mock.Mock(keysym="Return", keycode=hk.KEYS["Enter"]))
        self.assertEqual(rec.result, "Enter")

    def test_modifiers_held_before_opening_still_count(self):
        # Ctrl+Alt held before the dialog had focus (Tk never saw them go
        # down): V must be Ctrl+Alt+V, not a plain (shared) V.
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down |= {hk.VK_CONTROL, hk.VK_MENU}
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec._press(mock.Mock(keysym="v", keycode=0))
        self.assertEqual(rec.result, "Ctrl+Alt+V")

    def test_shift_held_before_opening_counts_for_a_click(self):
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down.add(hk.VK_SHIFT)
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.05)
        keys.down.add(hk.VK_RBUTTON)
        self._pump(0.1)
        self.assertEqual(rec.result, "Shift+Right mouse")

    def test_f_key_is_not_marked_shared(self):
        from catvision import gui_hotkeys as gh
        rec, _ = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec._press(mock.Mock(keysym="F8", keycode=0))
        self.assertEqual(rec.result, "F8")
        self.assertNotIn("Shared", rec.msg.cget("text"))

    def test_x_buttons_by_tk_number(self):
        from catvision import gui_hotkeys as gh
        rec, _ = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        ev = mock.Mock(num=9)
        rec._button(ev)
        self.assertEqual(rec.result, "Mouse 5")

    def test_x_button_by_polling_key_state(self):
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down.add(hk.VK_LBUTTON)       # the click that opened it, still held
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.1)
        self.assertIsNone(rec.result)
        keys.down.add(hk.VK_XBUTTON1)
        self._pump(0.1)
        self.assertEqual(rec.result, "Mouse 4")
        self.assertIn("Shared", rec.msg.cget("text"))

    def test_opening_click_held_is_not_polled_as_a_press(self):
        from catvision import gui_hotkeys as gh
        keys = Keys()
        keys.down |= {hk.VK_MBUTTON}
        rec, keys = self._recorder(keys)
        self._pump(gh.ARM_MS / 1000 + 0.1)
        self.assertIsNone(rec.result)
        keys.down.clear()
        self._pump(0.05)
        keys.down.add(hk.VK_MBUTTON)
        self._pump(0.1)
        self.assertEqual(rec.result, "Middle mouse")

    def test_shift_key_combo_is_taken(self):
        from catvision import gui_hotkeys as gh
        rec, _ = self._recorder()
        self._pump(gh.ARM_MS / 1000 + 0.05)
        rec._press(mock.Mock(keysym="Shift_L", keycode=0))
        rec._press(mock.Mock(keysym="q", keycode=0))
        self.assertEqual(rec.result, "Shift+Q")

    def test_closes_after_capture_and_stops_polling(self):
        rec, keys = self._recorder()
        self._pump(0.3)
        keys.down.add(hk.VK_XBUTTON2)
        self._pump(1.3)
        self.assertFalse(bool(self.root.winfo_children()) and rec.winfo_exists())
        n = len(keys.reads)
        self._pump(0.1)
        self.assertEqual(len(keys.reads), n)

    def test_hotkeys_tab_shows_shared_note(self):
        from catvision.backends import NullColorBackend
        from catvision.engine import Engine
        from catvision.gui import CatVisionApp
        from tests.test_catvision import FakeGamma
        tmp = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"CATVISION_CONFIG_DIR": tmp})
        env.start()
        self.addCleanup(env.stop)
        Path(tmp, "config.json").write_text(
            json.dumps({"hotkeys": {"peek": "Mouse 4", "toggle": "Ctrl+Alt+V",
                                    "off": "F8", "next": "Shift+F8"}}),
            encoding="utf-8")
        self.root.destroy()
        import tkinter as tk
        self.root = tk.Tk()
        engine = Engine(FakeGamma(), NullColorBackend(), cm.app_dir())
        app = CatVisionApp(self.root, engine, cm.load())
        try:
            tab = app.hotkeys_tab
            self.root.update()
            self.assertEqual(app.cfg.hotkeys["peek"], "Mouse 4")
            self.assertIn("shared", tab.tree.item("peek", "values")[1])
            self.assertNotIn("shared", tab.tree.item("toggle", "values")[1])
            # F-keys stay taken from the game, as in 1.18.
            self.assertNotIn("shared", tab.tree.item("off", "values")[1])
            self.assertNotIn("shared", tab.tree.item("next", "values")[1])
            self.assertTrue(tab.shared_note.winfo_manager())
            app.cfg.hotkeys["peek"] = "Ctrl+Alt+P"
            tab.refresh()
            self.assertFalse(tab.shared_note.winfo_manager())
        finally:
            try:
                app.quit()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
