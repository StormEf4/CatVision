"""Auto levels (beta): scenario stress of the real CatVisionApp, headless.

Skipped unless CATVISION_GUI_TESTS is set and tkinter plus a display are
available, so a plain "python -m unittest" (the Windows CI run) is unaffected.
The screen is never captured: a fake capture hands the watcher made-up
pictures and the window in front is simulated.
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

GUI = bool(os.environ.get("CATVISION_GUI_TESTS"))
_why = "set CATVISION_GUI_TESTS=1 to run the GUI tests"
if GUI:
    try:
        import tkinter as tk
        _r = tk.Tk()
        _r.destroy()
    except Exception as exc:  # no tkinter or no display
        GUI, _why = False, f"needs tkinter and a display ({exc})"

if GUI:
    from catvision import autolevels, curves
    from catvision import config as cm
    from catvision.backends import Monitor, NullColorBackend
    from catvision.engine import Engine
    from catvision.gui import CatVisionApp
    from tests.test_catvision import FakeGamma

D1, D2 = "\\\\.\\DISPLAY1", "\\\\.\\DISPLAY2"
THREAD_NAMES = ("catvision-auto-fader", "catvision-auto-levels")


class FakeScreen:
    """What the fake capture returns.  mode: dark / bright / black / frozen /
    none (can't capture) / block (capture hangs until released)."""

    def __init__(self):
        self.mode = "dark"
        self.n = 0
        self.release = threading.Event()
        self.calls = 0

    def capture(self, rect):
        self.calls += 1
        mode = self.mode
        if mode == "block":
            self.release.wait(10)
            return None
        if mode == "none":
            return None
        self.n += 1
        flick = self.n % 2
        if mode == "dark":
            base = bytes([6, 6, 6, 255]) * 400
        elif mode == "bright":
            base = bytes([200, 200, 200, 255]) * 400
        elif mode == "black":
            base = bytes([0, 0, 0, 255]) * 400
            return base   # black stays black
        else:  # frozen: the very same picture every time
            return bytes([10, 10, 10, 255]) * 400
        return base[:-4] + bytes([flick + 6, flick + 6, flick + 6, 255])


if GUI:
    class Two(FakeGamma):
        def monitors(self):
            return [Monitor(D1, "Display 1 - Game", True, short="Display 1 (main)",
                            rect=(0, 0, 800, 600)),
                    Monitor(D2, "Display 2 - Side", False, short="Display 2",
                            rect=(800, 0, 1600, 600))]


@unittest.skipUnless(GUI, _why)
class AutoLevelsAppStress(unittest.TestCase):
    # test name -> "options" written to config.json before the app starts
    PRECONFIG = {
        "test_corrupt_config_on_disk": {
            "auto_levels": True, "auto_strength": 1000.0, "auto_target": 5.0,
            "auto_speed": "warp", "auto_game_looks": {"Game A": 99.0, "Game B": -3.0},
        },
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_env = os.environ.get("CATVISION_CONFIG_DIR")
        os.environ["CATVISION_CONFIG_DIR"] = self.tmp
        pre = self.PRECONFIG.get(self._testMethodName)
        if pre is not None:
            data = cm.Config().to_json()
            data["options"].update(pre)
            data["active_preset"] = "Pitch Black"
            with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
        self.screen = FakeScreen()
        self.front = {"title": "Game A", "displays": {D1}}
        self._real_watcher = autolevels.Watcher
        self._real_front = autolevels.window_in_front
        real = self._real_watcher
        autolevels.Watcher = lambda: real(capture=self.screen.capture)

        def front(rects):
            t = self.front["title"]
            return (set(self.front["displays"]) & set(rects), t) if t else (set(), "")
        autolevels.window_in_front = front
        self.errors = []
        self.gamma = Two()
        self.engine = Engine(self.gamma, NullColorBackend(), cm.app_dir())
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = CatVisionApp(self.root, self.engine, cm.load())
        app_err = self.app._on_error

        def on_error(exc_type, exc, tb):
            self.errors.append(f"{exc_type.__name__}: {exc}")
            app_err(exc_type, exc, tb)
        self.root.report_callback_exception = on_error
        o = self.app.cfg.options
        # What the app made of config.json, before setUp changes anything.
        self.loaded = {"auto_speed": o.auto_speed, "auto_strength": o.auto_strength,
                       "auto_target": o.auto_target, "auto_game_looks": dict(o.auto_game_looks)}
        self.app.cfg.options.hotkey_popup = False
        self.app.auto_speed_var.set("fast")
        self.app._on_auto_option()

    def tearDown(self):
        self.screen.release.set()
        try:
            if not self.app._closed:
                self.app.quit()
        except Exception:
            pass
        autolevels.Watcher = self._real_watcher
        autolevels.window_in_front = self._real_front
        if self._old_env is None:
            os.environ.pop("CATVISION_CONFIG_DIR", None)
        else:
            os.environ["CATVISION_CONFIG_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers

    def pump(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end:
            if self.app._closed:
                time.sleep(0.02)
                continue
            self.root.update()
            time.sleep(0.01)

    def pump_until(self, cond, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.app._closed:
                self.root.update()
            if cond():
                return True
            time.sleep(0.01)
        return cond()

    def start_fade(self, preset="Pitch Black", wait=2.5):
        """Auto levels on in a dark game, until the display has moved."""
        self.app.activate_preset_by_name(preset)
        if not self.app.cfg.options.auto_levels:
            self.app.handle_action("auto_levels")
        self.assertTrue(self.engine.enabled)
        def moved():
            c = self.app._auto_ctrl.get(D1)
            return bool(self.engine.auto_offsets.get(D1)) and c is not None and abs(c.offset) > 2
        self.assertTrue(self.pump_until(moved, wait + 6), "auto levels never moved the display")

    def preset_ramp(self):
        tint, _m = curves.plan_color(self.engine.settings, self.engine.custom_filters,
                                     self.engine.custom_tints,
                                     all_displays=self.engine.all_displays_selected())
        return curves.build_ramp(self.engine.settings, tint)

    def assert_at_preset(self, timeout=8.0):
        """Every display ends at the exact preset (or untouched if CatVision
        is off / the display is not ticked) with no auto offsets left."""
        ok = self.pump_until(lambda: not self.engine.auto_offsets
                             and not self.app._auto_winding_down, timeout)
        self.assertTrue(ok, f"auto offsets never cleared: {self.engine.auto_offsets}")
        self.pump(0.3)   # nothing may move them again afterwards
        self.assertEqual(self.engine.auto_offsets, {})
        targets = {m.id for m in self.engine.target_monitors()}
        want = self.preset_ramp()
        for did in (D1, D2):
            if self.engine.enabled and did in targets:
                self.assertEqual(self.gamma.ramps[did], want, f"{did} not at the preset ramp")
            else:
                self.assertEqual(self.gamma.ramps[did], curves.identity_ramp(),
                                 f"{did} should be untouched")

    def assert_clean(self):
        self.assertEqual(self.errors, [], "Tk callback errors")
        self.assertEqual(self.app._auto_fader.last_error, "", "fader error")
        if not self.app._closed:
            self.assertNotIn("Error:", self.app.status.cget("text"))

    def assert_offsets_sane(self):
        for did, amounts in self.engine.auto_offsets.items():
            self.assertIn(did, (D1, D2))
            self.assertTrue(set(amounts) <= {"shadow_boost", "exposure"}, amounts)
            s = self.engine.settings_for(did)
            for k in ("shadow_boost", "exposure"):
                lo, hi = curves.LIMITS[k]
                self.assertTrue(lo <= getattr(s, k) <= hi)

    def assert_no_threads(self):
        def alive():
            return [t.name for t in threading.enumerate()
                    if t.name in THREAD_NAMES and t.is_alive()]
        end = time.monotonic() + 3
        while alive() and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(alive(), [], "auto levels threads left after quit")

    def assert_config_valid(self):
        loaded = cm.load()
        o = loaded.options
        lo, hi = autolevels.TARGET_LIMITS
        slo, shi = autolevels.STRENGTH_LIMITS
        self.assertIn(o.auto_speed, autolevels.SPEEDS)
        self.assertTrue(lo <= o.auto_target <= hi, o.auto_target)
        self.assertTrue(slo <= o.auto_strength <= shi, o.auto_strength)
        for title, aim in o.auto_game_looks.items():
            self.assertIsInstance(title, str)
            self.assertTrue(lo <= aim <= hi, (title, aim))
        return loaded

    def info(self):
        return self.app.auto_info.cget("text")

    # -------------------------------------------------------------- tests

    def test_rapid_auto_and_power_toggles(self):
        self.start_fade()
        actions = ["auto_levels", "toggle", "auto_levels", "auto_levels", "toggle",
                   "toggle", "auto_levels", "off", "auto_levels", "toggle"] * 3
        for i, a in enumerate(actions):
            self.app.handle_action(a)
            self.pump(0.02 + (i % 4) * 0.04)
            self.assert_offsets_sane()
        # end: CatVision on, auto levels off -> back at the exact preset
        if self.app.cfg.options.auto_levels:
            self.app.handle_action("auto_levels")
        if not self.engine.enabled:
            self.app.handle_action("toggle")
        self.assert_at_preset()
        self.assertEqual(self.info(), "Off.")
        self.assert_clean()

    def test_off_and_back_on_during_wind_down(self):
        self.start_fade()
        self.app.handle_action("auto_levels")        # off: glides back
        self.pump(0.15)
        self.assertTrue(self.app._auto_winding_down)
        self.app.handle_action("auto_levels")        # on again mid wind-down
        self.pump(0.3)
        self.assertFalse(self.app._auto_winding_down)
        self.assertTrue(self.app._auto_fader.running)
        self.app.handle_action("auto_levels")        # off again
        self.pump(0.1)
        self.app.handle_action("toggle")             # CatVision off mid wind-down
        self.pump(0.3)
        self.assertFalse(self.engine.enabled)
        self.assert_at_preset()
        self.assertFalse(self.app._auto_fader.running)
        self.app.handle_action("toggle")             # on again: plain preset
        self.assert_at_preset()
        self.assert_clean()

    def test_power_off_mid_fade_then_on_is_plain_preset(self):
        self.start_fade()
        self.app.handle_action("off")
        self.pump(0.2)
        self.assertEqual(self.engine.auto_offsets, {})
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        self.assertEqual(self.info(), "Starts when CatVision is on.")
        # auto stays wanted: turning CatVision on starts again from the preset
        self.app.set_auto_levels(False)
        self.app.handle_action("toggle")
        self.assert_at_preset()
        self.assert_clean()

    def test_preset_switch_mid_fade(self):
        self.start_fade("Dark Areas")
        for i in range(10):
            name = "Night Vision" if i % 2 == 0 else "Dark Areas"
            self.app.activate_preset_by_name(name)
            self.pump(0.15)
            self.assert_offsets_sane()
            key = self.app._auto_setting()
            self.assertEqual(key, "exposure" if name == "Night Vision" else "shadow_boost")
        self.app.activate_preset_by_name("Night Vision")
        self.pump(1.0)
        self.assert_offsets_sane()
        self.assertIn("Exposure", self.info())
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.assert_clean()

    def test_display_chips_mid_fade(self):
        self.front["displays"] = {D1, D2}
        self.start_fade()
        self.assertTrue(self.pump_until(lambda: D2 in self.engine.auto_offsets, 6))
        for i in range(8):
            self.app.display_vars[D2].set(i % 2 == 1)
            self.app._on_display_chip(D2)
            self.pump(0.12)
            self.assert_offsets_sane()
            targets = {m.id for m in self.engine.target_monitors()}
            if D2 not in targets:
                # an unticked display is back to normal right away
                self.pump(0.2)
                self.assertEqual(self.gamma.ramps[D2], curves.identity_ramp())
        # unticking everything is refused
        self.app.display_vars[D1].set(False)
        self.app.display_vars[D2].set(False)
        self.app._on_display_chip(D1)
        self.assertTrue(self.app.display_vars[D1].get())
        self.app.display_vars[D2].set(False)
        self.app._on_display_chip(D2)
        self.pump(0.5)
        self.assertNotIn(D2, self.engine.auto_offsets)
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.assert_clean()

    def test_game_looks_nudge_limits_and_forget(self):
        lo, hi = autolevels.TARGET_LIMITS
        self.start_fade()
        self.assertTrue(self.pump_until(lambda: "Game: Game A" in self.info(), 3))
        default = self.app.cfg.options.auto_target
        msg = self.app._keep_auto_look(quiet=True)
        self.assertIn("for Game A", msg)
        kept = self.app.cfg.options.auto_game_looks["Game A"]
        self.assertTrue(lo <= kept <= hi)
        self.assertEqual(self.app.cfg.options.auto_target, default)
        self.pump(0.2)
        self.assertTrue(self.app.auto_forget_btn.winfo_manager())
        self.assertIn("its own kept look", self.info())

        for _ in range(30):
            self.app.handle_action("auto_brighter")
        self.assertEqual(self.app.cfg.options.auto_game_looks["Game A"], hi)
        self.assertEqual(self.app.cfg.options.auto_target, default)

        self.front["title"] = "Game B"
        self.assertTrue(self.pump_until(lambda: self.app._auto_game == "Game B", 2))
        self.assertAlmostEqual(self.app.auto_vars["auto_target"].get(), default)
        for _ in range(30):
            self.app.handle_action("auto_darker")
        self.assertEqual(self.app.cfg.options.auto_target, lo)
        self.assertNotIn("Game B", self.app.cfg.options.auto_game_looks)

        for t in ["Game A", "Game B", "", "Game A", "Game C", "Game A"]:
            self.front["title"] = t
            self.pump(0.3)
        self.assertTrue(self.pump_until(lambda: self.app._auto_game == "Game A", 2))
        self.assertAlmostEqual(self.app.auto_vars["auto_target"].get(), hi)
        self.app._forget_game_look()
        self.assertNotIn("Game A", self.app.cfg.options.auto_game_looks)
        self.assertAlmostEqual(self.app.auto_vars["auto_target"].get(), lo)
        self.app._forget_game_look()   # twice is harmless
        self.pump(0.2)
        self.assertFalse(self.app.auto_forget_btn.winfo_manager())
        self.app._save()
        loaded = self.assert_config_valid()
        self.assertEqual(loaded.options.auto_game_looks, {})
        self.assertEqual(loaded.options.auto_target, lo)
        self.assert_clean()

    def test_game_only_and_speed_changes(self):
        self.start_fade()
        self.front["title"] = ""   # back on the desktop
        self.assertTrue(self.pump_until(lambda: "waiting for a full-screen game" in self.info(), 3))
        self.assertTrue(self.pump_until(lambda: not self.engine.auto_offsets.get(D1), 8),
                        "display never eased back on the desktop")
        # game only off: watches anyway
        self.app.auto_flag_vars["auto_game_only"].set(False)
        self.app._on_auto_option()
        self.assertTrue(self.pump_until(lambda: "% bright" in self.info(), 3), self.info())
        for sp in ["slow", "fast", "normal", "slow", "fast"] * 2:
            self.app.auto_speed_var.set(sp)
            self.app._on_auto_option()
            self.app.auto_flag_vars["auto_game_only"].set(
                not self.app.auto_flag_vars["auto_game_only"].get())
            self.app._on_auto_option()
            self.pump(0.12)
            self.assertEqual(self.app._auto_fader.speed, autolevels.SPEEDS[sp])
            self.assert_offsets_sane()
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.app._save()
        self.assert_config_valid()
        self.assert_clean()

    def test_keep_look_without_game(self):
        self.front["title"] = ""
        self.app.activate_preset_by_name("Pitch Black")
        self.app.handle_action("auto_levels")
        self.pump(1.0)
        before = self.app.cfg.options.auto_target
        msg = self.app._keep_auto_look(quiet=True)
        # Auto levels is on, there is just no game picture to take a look from.
        self.assertIn("no game picture", msg)
        self.assertEqual(self.app.cfg.options.auto_target, before)
        self.assertEqual(self.app.cfg.options.auto_game_looks, {})
        self.assert_clean()

    def test_keep_look_when_auto_off(self):
        before = self.app.cfg.options.auto_target
        msg = self.app._keep_auto_look(quiet=True)
        self.assertIn("turn it on", msg)
        msg = self.app._nudge_auto_aim(True)
        self.assertIn("auto levels is off", msg)
        self.assertNotEqual(self.app.cfg.options.auto_target, before)
        self.assert_clean()

    def test_keep_look_on_black_picture(self):
        self.screen.mode = "black"
        self.app.activate_preset_by_name("Pitch Black")
        self.app.handle_action("auto_levels")
        self.assertTrue(self.pump_until(lambda: "black screen" in self.info(), 4), self.info())
        before = self.app.cfg.options.auto_target
        msg = self.app._keep_auto_look(quiet=True)
        self.assertIn("screen is black", msg)
        self.assertEqual(self.app.cfg.options.auto_game_looks, {})
        self.assertEqual(self.app.cfg.options.auto_target, before)
        self.assert_clean()

    def test_keep_look_on_frozen_picture_is_refused(self):
        self.screen.mode = "frozen"
        self.app.activate_preset_by_name("Pitch Black")
        self.app.handle_action("auto_levels")
        self.assertTrue(self.pump_until(lambda: "isn't changing" in self.info(), 8), self.info())
        msg = self.app._keep_auto_look(quiet=True)
        # A frozen (paused / exclusive fullscreen) picture is not "how the
        # game looks right now": it should not be remembered as the look.
        self.assertEqual(self.app.cfg.options.auto_game_looks, {}, msg)
        self.assertIn("isn't changing", msg)
        self.assert_clean()

    def test_capture_returns_nothing(self):
        self.screen.mode = "none"
        self.app.activate_preset_by_name("Pitch Black")
        self.app.handle_action("auto_levels")
        self.assertTrue(self.pump_until(lambda: "can't see the picture" in self.info(), 4),
                        self.info())
        self.assertEqual(self.engine.auto_offsets.get(D1, {}), {})
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.assert_clean()

    def test_stalled_capture_eases_back(self):
        self.start_fade()
        self.screen.mode = "block"   # the capture hangs: no new pictures
        ok = self.pump_until(lambda: not self.engine.auto_offsets.get(D1), 10)
        self.assertTrue(ok, f"stalled capture kept the boost: {self.engine.auto_offsets}")
        self.assert_offsets_sane()
        # turning off while the capture still hangs must not hang the window
        t0 = time.monotonic()
        self.app.handle_action("auto_levels")
        self.pump(0.05)
        self.assertLess(time.monotonic() - t0, 2.5)
        self.assert_at_preset()
        self.screen.release.set()
        self.assert_clean()

    def test_stalled_capture_after_game_left(self):
        self.start_fade()
        self.screen.mode = "block"
        self.front["title"] = ""   # game gone while capture is stuck
        self.assertTrue(self.pump_until(lambda: "waiting for a full-screen game" in self.info(),
                                        3), self.info())
        self.assertTrue(self.pump_until(lambda: not self.engine.auto_offsets.get(D1), 8))
        self.screen.release.set()
        self.assert_clean()

    def test_corrupt_saved_looks_stay_safe(self):
        # A hand-edited / damaged config.json: out-of-range and NaN values.
        o = self.app.cfg.options
        o.auto_strength = 1000.0
        o.auto_target = 5.0
        o.auto_game_looks = {"Game A": float("nan"), "Game B": 99.0}
        self.app.activate_preset_by_name("Dark Areas")
        self.app.handle_action("auto_levels")
        for t in ["Game A", "Game B", "Game C"]:
            self.front["title"] = t
            self.pump(1.5)
            self.assert_offsets_sane()
            for did, amounts in self.engine.auto_offsets.items():
                for k, v in amounts.items():
                    self.assertEqual(v, v, f"NaN offset for {t}")   # NaN != NaN
                    self.assertLessEqual(abs(v), autolevels.STRENGTH_LIMITS[1] + 0.01,
                                         f"{t}: offset {v} beyond How much")
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.assert_clean()

    def test_corrupt_config_on_disk(self):
        # config.json damaged by hand: auto levels must stay inside its own
        # limits (the sliders show the clamped value, so must the controller)
        o = self.app.cfg.options
        _slo, shi = autolevels.STRENGTH_LIMITS
        lo, hi = autolevels.TARGET_LIMITS
        self.assertTrue(o.auto_levels)
        # The damaged config turned auto levels on but not CatVision itself:
        # turn it on, or auto levels never runs and nothing here is tested.
        self.app.handle_action("toggle")
        self.assertTrue(self.engine.enabled)
        # Every damaged value was brought back into range when it was loaded.
        self.assertEqual(self.loaded, {"auto_speed": "normal", "auto_strength": shi,
                                       "auto_target": hi,
                                       "auto_game_looks": {"Game A": hi, "Game B": lo}})
        for t, look in [("Game A", hi), ("Game B", lo), ("Game C", hi)]:
            self.front["title"] = t
            self.pump(1.5)
            # Aim for shows the clamped look of the game in front (Game C has
            # none, so it shows the normal aim, itself clamped to the top).
            self.assertAlmostEqual(self.app.auto_vars["auto_target"].get(), look, places=6)
            self.assert_offsets_sane()
            for did, amounts in self.engine.auto_offsets.items():
                for k, v in amounts.items():
                    self.assertLessEqual(abs(v), shi + 0.01,
                                         f"{t}: offset {v} beyond the How much limit")
        self.app.handle_action("auto_levels")
        self.assert_at_preset()
        self.app._save()
        self.assert_config_valid()
        self.assert_clean()

    def test_keep_look_while_capture_stalled(self):
        self.start_fade()
        self.screen.mode = "block"   # no new pictures: the shown one is stale
        self.assertTrue(self.pump_until(lambda: not self.engine.auto_offsets.get(D1), 10))
        msg = self.app._keep_auto_look(quiet=True)
        # The last picture is more than a second old and the display has
        # eased back: that is not "how the game looks right now".
        self.assertEqual(self.app.cfg.options.auto_game_looks, {}, msg)
        self.assertIn("can't see the picture", msg)
        self.screen.release.set()
        self.assert_clean()

    def test_settings_changed_at_runtime_stay_in_range(self):
        # Options changed behind the sliders' back (NaN, out of range, a
        # bool) never reach the controller: it always gets the slider limits.
        o = self.app.cfg.options
        lo, hi = autolevels.TARGET_LIMITS
        slo, shi = autolevels.STRENGTH_LIMITS
        for strength, target, look in [(float("nan"), float("inf"), True),
                                       (-5.0, -1.0, float("nan")), (1e9, 9.0, 99.0)]:
            o.auto_strength, o.auto_target = strength, target
            o.auto_game_looks = {"Game A": look}
            s, a = self.app._auto_strength(), self.app._auto_aim()
            self.assertTrue(slo <= s <= shi, (strength, s))
            self.assertTrue(lo <= a <= hi, (target, look, a))
        self.app._clean_auto_options()
        self.assertTrue(lo <= o.auto_target <= hi)
        self.assertEqual(o.auto_game_looks, {"Game A": hi})

    def test_hotkey_actions_through_handle_action(self):
        self.start_fade()
        for a in ["auto_keep", "auto_brighter", "auto_darker", "auto_keep",
                  "auto_levels", "auto_brighter", "auto_keep", "auto_levels"] * 2:
            self.app.handle_action(a)
            self.pump(0.05)
            self.assertNotIn("Error", self.app.status.cget("text"))
        self.app.set_auto_levels(False)
        self.pump(0.1)
        self.assert_at_preset()
        self.app._save()
        self.assert_config_valid()
        self.assert_clean()

    def test_quit_leaves_no_tk_timers(self):
        # A late auto tick / save / poll after destroy would touch dead widgets.
        self.start_fade()
        self.app.handle_action("auto_brighter")   # also schedules a save
        self.app.quit()
        self.assertEqual(self.root.tk.splitlist(self.root.tk.call("after", "info")), ())
        self.assertIsNone(self.app._auto_job)

    def test_quit_mid_fade(self):
        self.start_fade()
        self.app.handle_action("auto_brighter")
        self.app.handle_action("auto_keep")
        self.pump(0.1)
        t0 = time.monotonic()
        self.app.quit()
        self.assertLess(time.monotonic() - t0, 3.0)
        snap = {d: [list(c) for c in r] for d, r in self.gamma.ramps.items()}
        time.sleep(0.4)   # a late fader update must not touch the screen
        for did in (D1, D2):
            self.assertEqual(self.gamma.ramps[did], curves.identity_ramp())
            self.assertEqual(self.gamma.ramps[did], snap[did])
        self.assertFalse(self.app._auto_fader.running)
        self.assert_no_threads()
        loaded = self.assert_config_valid()
        self.assertTrue(loaded.options.auto_levels)
        self.assertIn("Game A", loaded.options.auto_game_looks)
        self.assert_clean()

    def test_quit_during_wind_down_and_stalled_capture(self):
        self.start_fade()
        self.app.handle_action("auto_levels")
        self.pump(0.1)
        self.screen.mode = "block"
        self.app.quit()
        self.screen.release.set()
        for did in (D1, D2):
            self.assertEqual(self.gamma.ramps[did], curves.identity_ramp())
        self.assert_no_threads()
        self.assert_config_valid()
        self.assert_clean()

    def test_quit_while_capture_blocked(self):
        self.start_fade()
        self.screen.mode = "block"
        self.pump(0.3)
        t0 = time.monotonic()
        self.app.quit()
        self.assertLess(time.monotonic() - t0, 3.0)
        # the watcher thread is stuck in capture: after quit it is still
        # alive until the capture returns; it must not outlive that
        self.screen.release.set()
        self.assert_no_threads()
        self.assert_clean()


@unittest.skipUnless(GUI, _why)
class AutoOptionsRoundTrip(unittest.TestCase):
    def test_bad_values_are_made_valid_on_load(self):
        o = cm.Options.from_dict({
            "auto_target": 5.0, "auto_strength": 1000.0, "auto_speed": "warp",
            "auto_game_looks": {"x": 99.0, "y": True, "z": "a", "w": float("nan")},
        })
        lo, hi = autolevels.TARGET_LIMITS
        slo, shi = autolevels.STRENGTH_LIMITS
        self.assertIn(o.auto_speed, autolevels.SPEEDS)
        self.assertTrue(lo <= o.auto_target <= hi, o.auto_target)
        self.assertTrue(slo <= o.auto_strength <= shi, o.auto_strength)
        for k, v in o.auto_game_looks.items():
            self.assertTrue(lo <= v <= hi, (k, v))

    def test_integer_values_are_kept(self):
        # A hand-edited config.json (or another tool) may store whole numbers.
        o = cm.Options.from_dict({"auto_strength": 40})
        self.assertEqual(o.auto_strength, 40)


if __name__ == "__main__":
    unittest.main()
