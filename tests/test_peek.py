"""Peek: hold a hotkey for a short, strong brightness lift.

Plain tests run everywhere; the GUI test only runs when CATVISION_GUI_TESTS
is set and tkinter plus a display are available.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from catvision import curves, hotkeys, peek
from catvision.backends import NullColorBackend
from catvision.config import Config, Options
from catvision.curves import Settings
from catvision.engine import Engine
from tests.test_catvision import FakeGamma

D1, D2 = "\\\\.\\DISPLAY1", "\\\\.\\DISPLAY2"


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def run_to_end(run, clock, key=None, until=60.0, dt=peek.STEP_S):
    """Step a PeekRun on a fake clock; ``key``: time -> down.  Returns
    (amounts, time it finished)."""
    amounts = []
    start = clock.t
    while clock.t - start < until:
        amount, done = run.step(clock.t)
        amounts.append(amount)
        if done:
            return amounts, clock.t - start
        clock.t += dt
    return amounts, None


class PeekRunTest(unittest.TestCase):
    def test_release_is_seen_and_glides_back(self):
        clock = FakeClock()
        start = clock.t
        run = peek.PeekRun(35, clock.t, lambda: clock.t - start < 0.5)
        amounts, ended = run_to_end(run, clock)
        self.assertEqual(max(amounts), 35)
        self.assertEqual(amounts[-1], 0)
        # Let go at 0.5 s: seen within one key check, then the glide out.
        self.assertGreater(ended, 0.5 + peek.GLIDE_OUT_S - 0.02)
        self.assertLess(ended, 0.5 + peek.KEY_CHECK_S + peek.GLIDE_OUT_S + 0.05)

    def test_glides_in_quickly_with_small_steps(self):
        clock = FakeClock()
        run = peek.PeekRun(60, clock.t, lambda: True)
        amounts = []
        for _ in range(int(0.2 / peek.STEP_S)):
            amounts.append(run.step(clock.t)[0])
            clock.t += peek.STEP_S
        # Full strength within the glide in (plus a step).
        reached = next(i for i, a in enumerate(amounts) if a == 60) * peek.STEP_S
        self.assertLessEqual(reached, peek.GLIDE_IN_S + peek.STEP_S)
        steps = [b - a for a, b in zip(amounts, amounts[1:])]
        self.assertTrue(all(s >= 0 for s in steps))
        # At 120 steps a second even the strongest peek moves only a few points.
        self.assertLess(max(steps), 6.0)

    def test_steps_at_60_a_second_stay_small(self):
        # Windows' timer may give only ~64 steps a second.
        clock = FakeClock()
        start = clock.t
        run = peek.PeekRun(35, clock.t, lambda: clock.t - start < 0.3)
        amounts, _ended = run_to_end(run, clock, dt=1 / 60)
        steps = [abs(b - a) for a, b in zip(amounts, amounts[1:])]
        self.assertLess(max(steps), 7.0)

    def test_safety_time_ends_a_stuck_key(self):
        clock = FakeClock()
        run = peek.PeekRun(35, clock.t, lambda: True)
        amounts, ended = run_to_end(run, clock, dt=0.02)
        self.assertIsNotNone(ended)
        self.assertGreaterEqual(ended, peek.SAFETY_S)
        self.assertLess(ended, peek.SAFETY_S + peek.GLIDE_OUT_S + 0.1)
        self.assertEqual(amounts[-1], 0)

    def test_unreadable_key_ends_after_the_safety_time(self):
        clock = FakeClock()
        run = peek.PeekRun(35, clock.t, lambda: None)
        _amounts, ended = run_to_end(run, clock, dt=0.02)
        self.assertGreaterEqual(ended, peek.SAFETY_S)
        self.assertLess(ended, peek.SAFETY_S + peek.GLIDE_OUT_S + 0.1)

    def test_key_is_checked_about_every_30_ms(self):
        clock = FakeClock()
        checks = []

        def key():
            checks.append(clock.t)
            return True
        run = peek.PeekRun(35, clock.t, key)
        for _ in range(120):    # one second at 120 steps a second
            run.step(clock.t)
            clock.t += peek.STEP_S
        self.assertTrue(25 <= len(checks) <= 40, len(checks))

    def test_pressed_again_while_gliding_back_lifts_again(self):
        clock = FakeClock()
        down = {"v": True}
        run = peek.PeekRun(35, clock.t, lambda: down["v"])
        for _ in range(30):
            run.step(clock.t)
            clock.t += peek.STEP_S
        self.assertFalse(run.hold_again(clock.t))   # still held: nothing changes
        down["v"] = False
        for _ in range(10):
            low, done = run.step(clock.t)
            clock.t += peek.STEP_S
        self.assertFalse(done)
        self.assertLess(low, 35)
        down["v"] = True
        self.assertTrue(run.hold_again(clock.t))
        amounts, _ = run_to_end(run, clock, until=0.5)
        self.assertEqual(max(amounts), 35)
        self.assertGreaterEqual(min(amounts), low - 6)   # from where it was, no dip to 0

    def test_quick_tap_still_returns_to_zero(self):
        clock = FakeClock()
        run = peek.PeekRun(35, clock.t, lambda: False)
        amounts, ended = run_to_end(run, clock)
        self.assertIsNotNone(ended)
        self.assertEqual(amounts[-1], 0)


class PeekerThreadTest(unittest.TestCase):
    def test_thread_lifts_then_applies_zero_and_ignores_second_press(self):
        down = {"v": True}
        got = []
        p = peek.Peeker(key_down=lambda vk: down["v"])
        self.assertTrue(p.press(0x50, 35, got.append))
        self.assertFalse(p.press(0x50, 35, got.append))   # harmless
        time.sleep(0.3)
        self.assertEqual(max(got), 35)
        down["v"] = False
        deadline = time.monotonic() + 3
        while p.peeking and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(p.peeking)
        self.assertEqual(got[-1], 0.0)

    def test_press_while_gliding_back_lifts_again(self):
        down = {"v": True}
        got = []
        p = peek.Peeker(key_down=lambda vk: down["v"])
        p.press(0x50, 35, got.append)
        time.sleep(0.25)
        down["v"] = False
        time.sleep(0.1)
        down["v"] = True
        self.assertTrue(p.press(0x50, 35, got.append))
        time.sleep(0.3)
        self.assertTrue(p.peeking)
        self.assertEqual(got[-1], 35)
        p.stop()
        self.assertEqual(got[-1], 0.0)

    def test_stop_ends_at_once_and_drops_the_lift(self):
        got = []
        p = peek.Peeker(key_down=lambda vk: True)
        p.press(0x50, 35, got.append)
        time.sleep(0.2)
        p.stop()
        self.assertFalse(p.peeking)
        self.assertEqual(got[-1], 0.0)

    def test_no_key_known_holds_until_stopped(self):
        got = []
        p = peek.Peeker(key_down=lambda vk: self.fail("must not be asked"))
        p.press(None, 35, got.append)
        time.sleep(0.25)
        self.assertTrue(p.peeking)
        p.stop()
        self.assertEqual(got[-1], 0.0)


def two_engine(limit=None):
    eng = Engine(FakeGamma(limit=limit), NullColorBackend())
    eng.settings = Settings(shadow_boost=30.0, exposure=0.0).clamped()
    eng.set_enabled(True)
    return eng


def ramps(eng):
    return {m.id: eng.gamma.get_ramp(m.id) for m in eng.monitors()}


def glide(eng, top, gen=None, steps=12):
    """Peek up to ``top`` and back down to 0 in small steps."""
    ups = [top * (i + 1) / steps for i in range(steps)]
    for a in ups + ups[::-1][1:] + [0.0]:
        eng.set_peek(round(a, 2), generation=gen)


class EnginePeekTest(unittest.TestCase):
    def test_settings_for_adds_peek_on_top_and_clamps(self):
        eng = two_engine()
        eng.set_peek(35)
        self.assertEqual(eng.settings_for(D1).shadow_boost, 65.0)
        eng.set_auto_offsets({D1: {"shadow_boost": 20.0, "exposure": 3.0}})
        s = eng.settings_for(D1)
        self.assertEqual(s.shadow_boost, 85.0)
        self.assertEqual(s.exposure, 3.0)
        self.assertEqual(eng.settings_for(D2).shadow_boost, 65.0)
        eng.set_peek(60)
        self.assertEqual(eng.settings_for(D1).shadow_boost, 100.0)   # clamped
        eng.set_peek(0)
        self.assertEqual(eng.settings_for(D2).shadow_boost, 30.0)

    def test_bad_amounts_mean_no_lift(self):
        eng = two_engine()
        for bad in (float("nan"), float("inf"), -5, "x", None):
            eng.set_peek(bad)
            self.assertEqual(eng.peek_amount, 0.0)

    def test_peek_raises_the_ramp_and_returns_exactly(self):
        eng = two_engine()
        before = ramps(eng)
        eng.set_peek(35)
        lifted = ramps(eng)
        for did in before:
            self.assertGreater(sum(lifted[did][0][:64]), sum(before[did][0][:64]))
        eng.set_peek(0)
        self.assertEqual(ramps(eng), before)
        glide(eng, 35)
        self.assertEqual(ramps(eng), before)
        self.assertEqual(eng._applied, {d: r for d, r in before.items()})

    def test_returns_exactly_with_auto_levels_offsets(self):
        eng = two_engine()
        eng.set_auto_offsets({D1: {"shadow_boost": 12.5}})
        before = ramps(eng)
        glide(eng, 40)
        self.assertEqual(ramps(eng), before)

    def test_haze_cut_gives_way_to_the_peek(self):
        # Cut through haze lowers the black point, which would swallow most
        # of a Peek's lift in the dark corners it is for: like auto levels'
        # own lift (autolevels.HAZE_LIFT_OFF) the Peek comes first, and the
        # cut comes back as the Peek glides out.
        from catvision import autolevels
        eng = two_engine()
        eng.set_auto_offsets({D1: {"haze_cut": 60.0}})
        before = ramps(eng)
        self.assertEqual(eng.settings_for(D1).haze_cut, 60.0)
        eng.set_peek(autolevels.HAZE_LIFT_OFF / 2)
        self.assertAlmostEqual(eng.settings_for(D1).haze_cut, 30.0)
        self.assertEqual(eng.settings_for(D1, peek=False).haze_cut, 60.0)
        eng.set_peek(35)
        self.assertEqual(eng.settings_for(D1).haze_cut, 0.0)
        lifted = ramps(eng)
        self.assertEqual(lifted[D1], lifted[D2])    # the same as a clear view's Peek
        glide(eng, 35)
        self.assertEqual(ramps(eng), before)

    def test_peek_and_auto_levels_updates_interleaved(self):
        # The fader keeps sending offsets while the peek moves: each update
        # shows both, and neither undoes the other.
        eng = two_engine()
        ups = [i * 2.5 for i in range(1, 13)]
        for i, p in enumerate(ups + ups[::-1] + [0.0]):
            eng.set_auto_offsets({D1: {"shadow_boost": 5.0 + (i % 5)}})
            eng.set_peek(p)
            want = curves.build_ramp(eng.settings_for(D1),
                                     curves.plan_tint(eng.settings, eng.custom_tints))
            self.assertEqual(eng.gamma.get_ramp(D1), want)
        eng.set_auto_offsets({D1: {"shadow_boost": 7.0}})
        alone = Engine(FakeGamma(), NullColorBackend())
        alone.settings = eng.settings
        alone.set_enabled(True)
        alone.set_auto_offsets({D1: {"shadow_boost": 7.0}})
        self.assertEqual(ramps(eng), ramps(alone))

    def test_late_steps_from_a_cut_short_peek_are_dropped(self):
        eng = two_engine()
        before = ramps(eng)
        gen = eng.peek_generation
        eng.set_peek(20, generation=gen)
        eng.end_peek()
        self.assertEqual(eng.peek_amount, 0.0)
        self.assertEqual(ramps(eng), before)
        eng.set_peek(30, generation=gen)   # late step
        self.assertEqual(eng.peek_amount, 0.0)
        self.assertEqual(ramps(eng), before)

    def test_turning_off_mid_peek_leaves_no_lift(self):
        eng = two_engine()
        before = ramps(eng)
        gen = eng.peek_generation
        eng.set_peek(35, generation=gen)
        eng.set_enabled(False)
        self.assertEqual(eng.peek_amount, 0.0)
        identity = curves.identity_ramp()
        self.assertTrue(all(r == identity for r in ramps(eng).values()))
        eng.set_peek(35, generation=gen)   # late step from the old peek
        eng.set_enabled(True)
        self.assertEqual(ramps(eng), before)

    def test_restore_now_and_shutdown_end_the_peek(self):
        for how in ("restore_now", "shutdown"):
            eng = two_engine()
            gen = eng.peek_generation
            eng.set_peek(35, generation=gen)
            getattr(eng, how)()
            self.assertEqual(eng.peek_amount, 0.0)
            self.assertNotEqual(eng.peek_generation, gen)

    def test_preset_change_mid_peek_ends_on_the_new_preset(self):
        eng = two_engine()
        eng.set_peek(35)
        eng.set_settings(Settings(shadow_boost=50.0, gamma=1.2))
        self.assertEqual(eng.settings_for(D1).shadow_boost, 85.0)
        glide(eng, 20)
        fresh = Engine(FakeGamma(), NullColorBackend())
        fresh.settings = eng.settings
        fresh.set_enabled(True)
        self.assertEqual(ramps(eng), ramps(fresh))

    def test_displays_changed_mid_peek(self):
        eng = two_engine()
        eng.set_peek(35)
        eng.set_displays([D2])
        identity = curves.identity_ramp()
        self.assertEqual(eng.gamma.get_ramp(D1), identity)   # unticked: put back
        glide(eng, 20)
        fresh = Engine(FakeGamma(), NullColorBackend())
        fresh.settings = eng.settings
        fresh.displays = [D2]
        fresh.set_enabled(True)
        self.assertEqual(ramps(eng), ramps(fresh))

    def test_refused_peek_still_returns_exactly(self):
        # Windows refuses ramps that stray too far: the peek is held back
        # (or refused), and the end is still exactly the preset's own look.
        eng = two_engine(limit=60)
        before = ramps(eng)
        eng.set_peek(60)
        glide(eng, 60)
        self.assertEqual(ramps(eng), before)
        self.assertTrue(eng.status.gamma_ok)

    def test_peek_does_nothing_while_off(self):
        eng = Engine(FakeGamma(), NullColorBackend())
        eng.settings = Settings(shadow_boost=30.0)
        calls = eng.gamma.calls
        eng.set_peek(35)
        self.assertEqual(eng.gamma.calls, calls)


class PeekConfigTest(unittest.TestCase):
    def test_default_and_saved(self):
        self.assertEqual(Options().peek_strength, 35.0)
        c = Config()
        c.options.peek_strength = 50.0
        again = Config.from_json(json.loads(json.dumps(c.to_json())))
        self.assertEqual(again.options.peek_strength, 50.0)

    def test_cleaned_on_load(self):
        for raw, want in ((1000, 60.0), (5, 10.0), (-3.0, 10.0), (40, 40.0),
                          ("strong", 35.0), (True, 35.0), (None, 35.0),
                          (float("nan"), 35.0), (float("inf"), 35.0), (10 ** 400, 35.0)):
            with self.subTest(raw=raw):
                o = Config.from_json({"options": {"peek_strength": raw}}).options
                self.assertEqual(o.peek_strength, want)

    def test_old_config_gets_the_default(self):
        self.assertEqual(Config.from_json({"options": {}}).options.peek_strength, 35.0)


class PeekHotkeyTest(unittest.TestCase):
    def test_action_is_listed_unbound_and_never_repeats(self):
        self.assertIn("peek", hotkeys.ACTION_LABELS)
        self.assertIn("hold", hotkeys.action_label("peek").lower())
        self.assertNotIn("peek", hotkeys.REPEATING_ACTIONS)
        self.assertIn("peek", hotkeys.HOLD_ACTIONS)
        self.assertNotIn("peek", hotkeys.default_hotkeys())
        self.assertNotIn("peek", Config().hotkeys)

    def test_suggested_key_is_valid(self):
        combo = hotkeys.suggested_hotkeys()["peek"]
        self.assertIsNone(hotkeys.combo_problem(combo))
        self.assertEqual(hotkeys.normalize_combo(combo), combo)

    def test_key_of_finds_the_peek_key(self):
        listener = hotkeys.HotkeyListener(lambda a: None)
        listener.bindings = hotkeys.bindings_from_config({"peek": "Ctrl+Alt+P",
                                                          "toggle": "Ctrl+Alt+V"})
        self.assertEqual(listener.key_of("peek"), ord("P"))
        self.assertIsNone(listener.key_of("off"))

    def test_windows_key_check_is_safe_elsewhere(self):
        if os.name != "nt":
            self.assertIsNone(peek.windows_key_down(0x50))


def preset_engine(settings, limit=None):
    eng = Engine(FakeGamma(limit=limit), NullColorBackend())
    eng.settings = settings.clamped()
    eng.set_enabled(True)
    return eng


class PeekLiftTest(unittest.TestCase):
    """The lift is shared out like auto levels' changes: the preset's main
    slider first, the rest spills to the other one."""

    def test_every_builtin_preset_clearly_brightens_and_returns_exactly(self):
        from catvision.presets import BUILTIN_PRESETS
        for preset in BUILTIN_PRESETS:
            for strength in (peek.STRENGTH_LIMITS[0], peek.DEFAULT_STRENGTH,
                             peek.STRENGTH_LIMITS[1]):
                with self.subTest(preset=preset.name, strength=strength):
                    eng = preset_engine(preset.settings)
                    before = ramps(eng)
                    glide(eng, strength, steps=6)
                    eng.set_peek(strength)
                    lifted = ramps(eng)
                    for did in before:
                        # (the darkest quarter, every channel: the colour
                        # filters leave some channels low)
                        gain = sum(sum(c[:64]) for c in lifted[did]) - sum(
                            sum(c[:64]) for c in before[did])
                        self.assertGreater(gain / (3 * 64 * 65535), 0.015)
                    eng.set_peek(0)
                    self.assertEqual(ramps(eng), before)

    def test_night_vision_lifts_shadow_boost_first(self):
        # Shadow boost lifts the dark corners more than Exposure, even here.
        nv = Settings(shadow_boost=0.0, exposure=40.0, anti_glare=100.0)
        eng = preset_engine(nv)
        eng.set_peek(35)
        s = eng.settings_for(D1)
        self.assertEqual((s.exposure, s.shadow_boost), (40.0, 35.0))
        eng.set_peek(70)   # (more than the slider allows: tested directly)
        s = eng.settings_for(D1)
        self.assertEqual((s.exposure, s.shadow_boost), (40.0, 70.0))

    def test_near_the_top_the_rest_spills_to_exposure(self):
        pitch = Settings(shadow_boost=85.0, exposure=0.0, anti_glare=0.0)
        eng = preset_engine(pitch)
        eng.set_peek(35)
        s = eng.settings_for(D1)
        self.assertEqual((s.shadow_boost, s.exposure), (100.0, 10.0))   # 20 spills at half

    def test_spill_respects_anti_glare_like_auto_levels(self):
        # Exposure off with Anti-glare on stays off (switching it on jumps).
        s0 = Settings(shadow_boost=90.0, exposure=0.0, anti_glare=60.0)
        eng = preset_engine(s0)
        eng.set_peek(35)
        s = eng.settings_for(D1)
        self.assertEqual((s.shadow_boost, s.exposure), (100.0, 0.0))

    def test_lift_goes_on_top_of_auto_levels(self):
        from catvision import autolevels
        nv = Settings(shadow_boost=0.0, exposure=40.0, anti_glare=100.0)
        eng = preset_engine(nv)
        eng.set_auto_offsets({D1: {"exposure": 50.0, "highlight_room": 4.0}})
        eng.set_peek(35)
        s = eng.settings_for(D1)
        self.assertEqual((s.exposure, s.shadow_boost), (90.0, 35.0))
        self.assertEqual(s.highlight_room, nv.clamped().highlight_room + 4.0)
        self.assertEqual((eng.settings_for(D2).exposure, eng.settings_for(D2).shadow_boost),
                         (40.0, 35.0))
        # Auto levels still moves the preset's own main slider.
        self.assertEqual(autolevels.main_setting(nv), "exposure")
        self.assertEqual(autolevels.main_setting(Settings(shadow_boost=30, exposure=6)),
                         "shadow_boost")

    def test_lift_never_darkens(self):
        from catvision.engine import peek_lift
        # (an out-of-range base, from a hand-edited or old offset)
        s = Settings(shadow_boost=60.0, exposure=10.0)
        self.assertEqual(peek_lift(s, {"shadow_boost": 50.0, "exposure": 95.0}, 35), {})
        for k, v in peek_lift(s, {"shadow_boost": 39.0}, 35).items():
            self.assertGreater(v, 0, k)

    def test_settings_for_without_peek_is_auto_levels_alone(self):
        eng = two_engine()
        eng.set_auto_offsets({D1: {"shadow_boost": 12.5}})
        alone = eng.settings_for(D1)
        eng.set_peek(35)
        self.assertEqual(eng.settings_for(D1, peek=False), alone)
        self.assertEqual(eng.settings_for(D2, peek=False), eng.settings)
        self.assertNotEqual(eng.settings_for(D1), alone)

    def test_limited_display_returns_exactly_after_a_spilled_peek(self):
        pitch = Settings(shadow_boost=85.0, exposure=0.0, anti_glare=0.0)
        eng = preset_engine(pitch, limit=0.15)
        before = ramps(eng)
        glide(eng, 60)
        self.assertEqual(ramps(eng), before)


class KeyCheckTest(unittest.TestCase):
    def test_a_key_never_seen_down_still_gets_a_short_look(self):
        # A tap over before the press reached the peek, or Windows not
        # letting GetAsyncKeyState look: a quick look, not nothing.
        clock = FakeClock()
        run = peek.PeekRun(35, clock.t, lambda: False)
        amounts, ended = run_to_end(run, clock)
        self.assertEqual(max(amounts), 35)
        self.assertGreaterEqual(ended, peek.GLANCE_S)
        self.assertLess(ended, peek.GLANCE_S + peek.GLIDE_OUT_S + 0.1)
        self.assertEqual(amounts[-1], 0)

    def test_release_after_being_seen_down_is_not_held_back(self):
        clock = FakeClock()
        down = {"v": True}
        run = peek.PeekRun(35, clock.t, lambda: down["v"])
        for _ in range(3):
            run.step(clock.t)
            clock.t += peek.STEP_S
        down["v"] = False
        amounts, ended = run_to_end(run, clock)
        self.assertIsNotNone(ended)
        self.assertEqual(amounts[-1], 0)
        self.assertLess(clock.t - 100.0, peek.GLANCE_S)   # ended well before a glance would

    def test_only_the_main_key_is_checked(self):
        # Ctrl+Alt+P: the peek asks about P only (letting go of Ctrl or Alt
        # alone doesn't end it).
        asked = []
        clock = FakeClock()
        p = peek.Peeker(key_down=lambda vk: asked.append(vk) or True, clock=clock)
        self.assertTrue(p.press(ord("P"), 35, lambda a: None))
        try:
            end = time.monotonic() + 2
            while not asked and time.monotonic() < end:
                time.sleep(0.01)
        finally:
            p.stop()
        self.assertTrue(asked)
        self.assertEqual(set(asked), {ord("P")})

    def test_selftest_key_check_never_raises(self):
        out = peek.key_state_check()
        self.assertIsInstance(out, dict)
        if os.name != "nt":
            self.assertIn("skipped", out)
        from unittest import mock
        with mock.patch.object(peek.sys, "platform", "win32"):
            out = peek.key_state_check()   # (no ctypes.windll here: an error, not a raise)
        self.assertIsInstance(out, dict)
        if os.name != "nt":
            self.assertIn("error", out)


GUI = bool(os.environ.get("CATVISION_GUI_TESTS"))
_why = "set CATVISION_GUI_TESTS=1 to run the GUI tests"
if GUI:
    try:
        import tkinter as tk
        _r = tk.Tk()
        _r.destroy()
    except Exception as exc:  # no tkinter or no display
        GUI, _why = False, f"needs tkinter and a display ({exc})"


@unittest.skipUnless(GUI, _why)
class PeekAppTest(unittest.TestCase):
    def setUp(self):
        from catvision import config as cm
        from catvision.gui import CatVisionApp
        self.tmp = tempfile.mkdtemp()
        self._old_env = os.environ.get("CATVISION_CONFIG_DIR")
        os.environ["CATVISION_CONFIG_DIR"] = self.tmp
        self.gamma = FakeGamma()
        self.engine = Engine(self.gamma, NullColorBackend(), cm.app_dir())
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = CatVisionApp(self.root, self.engine, cm.load())
        self.down = {"v": True}
        self.app.peeker.key_down = lambda vk: self.down["v"]
        self.app.hotkeys.key_of = lambda action: 0x50
        self.toasts = []
        self.app.toast.show = lambda text, ms=1300: self.toasts.append(text)

    def tearDown(self):
        try:
            if not self.app._closed:
                self.app.quit()
        except Exception:
            pass
        if self._old_env is None:
            os.environ.pop("CATVISION_CONFIG_DIR", None)
        else:
            os.environ["CATVISION_CONFIG_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump_until(self, cond, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.app._closed:
                self.root.update()
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def test_peek_raises_the_ramp_and_returns_exactly(self):
        self.app._set_enabled(True)
        before = self.gamma.get_ramp(D1)
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(
            lambda: self.engine.peek_amount == self.app.cfg.options.peek_strength))
        self.assertGreater(sum(self.gamma.get_ramp(D1)[0][:64]), sum(before[0][:64]))
        self.app.handle_action("peek")   # pressed again while held: harmless
        self.assertEqual(self.toasts, ["Peek"])   # one popup, no flicker
        self.down["v"] = False
        self.assertTrue(self.pump_until(lambda: not self.app.peeker.peeking))
        self.assertEqual(self.engine.peek_amount, 0.0)
        self.assertEqual(self.gamma.get_ramp(D1), before)

    def test_turning_off_mid_peek_leaves_nothing(self):
        self.app._set_enabled(True)
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(lambda: self.engine.peek_amount > 0))
        self.app.handle_action("off")
        self.assertFalse(self.app.peeker.peeking)
        self.assertEqual(self.engine.peek_amount, 0.0)
        self.assertEqual(self.gamma.get_ramp(D1), curves.identity_ramp())
        time.sleep(0.1)
        self.assertEqual(self.gamma.get_ramp(D1), curves.identity_ramp())

    def test_peek_while_off_only_shows_a_hint(self):
        calls = self.gamma.calls
        self.app.handle_action("peek")
        self.assertFalse(self.app.peeker.peeking)
        self.assertEqual(self.gamma.calls, calls)
        self.assertEqual(len(self.toasts), 1)
        self.assertIn("on", self.toasts[0].lower())

    def test_press_after_a_peek_was_cut_short_elsewhere_starts_afresh(self):
        # CatVision turned off and on again without going through the window
        # (the peek thread still runs, its steps all dropped): the next press
        # must start a peek that counts, not join the dead one.
        self.app._set_enabled(True)
        strength = self.app.cfg.options.peek_strength
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(lambda: self.engine.peek_amount == strength))
        self.engine.set_enabled(False)
        self.engine.set_enabled(True)
        self.assertEqual(self.engine.peek_amount, 0.0)
        self.assertTrue(self.app.peeker.peeking)
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(lambda: self.engine.peek_amount == strength))
        self.down["v"] = False
        self.assertTrue(self.pump_until(lambda: not self.app.peeker.peeking))
        self.assertEqual(self.engine.peek_amount, 0.0)

    def test_auto_levels_and_peek_pick_the_same_main_slider(self):
        from catvision import autolevels
        # Auto mode always lifts Shadow boost first, as a Peek on its
        # neutral look does (peek_lift); main_setting is for presets.
        for s in (Settings(shadow_boost=0, exposure=40), Settings(shadow_boost=40, exposure=6)):
            self.engine.settings = s
            self.assertEqual(self.app._auto_setting(), "shadow_boost")
        self.assertEqual(autolevels.main_setting(autolevels.AUTO_BASE), "shadow_boost")

    def test_peek_strength_slider_is_saved(self):
        self.app.peek_var.set(50)
        self.app._on_peek_strength()
        self.assertEqual(self.app.cfg.options.peek_strength, 50.0)

    def test_quit_mid_peek(self):
        self.app._set_enabled(True)
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(lambda: self.engine.peek_amount > 0))
        threads = [t for t in threading.enumerate() if t.name == "catvision-peek"]
        self.app.quit()
        self.assertEqual(self.gamma.get_ramp(D1), curves.identity_ramp())
        self.assertFalse(any(t.is_alive() for t in threads))


if GUI:
    # (The module, not the class: a test class imported here would run twice.)
    from tests import test_autolevels_app as _app_tests


@unittest.skipUnless(GUI, _why)
class PeekWithAutoLevelsAppTest(unittest.TestCase):
    """Peek while auto levels (beta) is fading the real app: they add up,
    neither undoes the other, and nothing is left once both stop."""
    PRECONFIG: dict = {}
    if GUI:
        _Stress = _app_tests.AutoLevelsAppStress
        setUp, tearDown = _Stress.setUp, _Stress.tearDown
        pump, pump_until, start_fade = _Stress.pump, _Stress.pump_until, _Stress.start_fade
        preset_ramp, assert_at_preset = _Stress.preset_ramp, _Stress.assert_at_preset

    def test_peek_on_top_of_auto_levels_then_back_to_the_preset(self):
        self.start_fade()
        down = {"v": True}
        self.app.peeker.key_down = lambda vk: down["v"]
        self.app.hotkeys.key_of = lambda action: 0x50
        strength = self.app.cfg.options.peek_strength
        self.app.handle_action("peek")
        self.assertTrue(self.pump_until(lambda: self.engine.peek_amount == strength))
        self.pump(0.3)   # the fader keeps moving meanwhile
        tint = curves.plan_tint(self.engine.settings, self.engine.custom_tints)
        with self.engine._lock:
            want = curves.build_ramp(self.engine.settings_for(D1), tint)
            self.assertEqual(self.gamma.ramps[D1], want)
        self.assertGreater(sum(want[0][:64]), sum(self.preset_ramp()[0][:64]))
        # The auto levels readout shows auto levels' own change, not the peek.
        with self.engine._lock:   # (holds the fader and the peek still meanwhile)
            self.assertNotEqual(self.engine.settings_for(D1),
                                self.engine.settings_for(D1, peek=False))
            self.app._render_auto_info(self.app._auto_shown)
            peeking = self.app.auto_info.cget("text")
            lift, self.engine.peek_amount = self.engine.peek_amount, 0.0
            try:
                self.app._render_auto_info(self.app._auto_shown)
                alone = self.app.auto_info.cget("text")
            finally:
                self.engine.peek_amount = lift
        self.assertIn("Shadow boost", peeking)
        self.assertEqual(peeking, alone)
        down["v"] = False
        self.assertTrue(self.pump_until(lambda: not self.app.peeker.peeking))
        self.assertEqual(self.engine.peek_amount, 0.0)
        with self.engine._lock:
            want = curves.build_ramp(self.engine.settings_for(D1), tint)
            self.assertEqual(self.gamma.ramps[D1], want)
        self.app.handle_action("auto_levels")   # off: glides back to the preset
        self.assert_at_preset()


if __name__ == "__main__":
    unittest.main()
