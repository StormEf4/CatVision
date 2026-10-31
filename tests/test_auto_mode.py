"""Auto mode (beta): auto levels as a mode of its own (no preset underneath).

The pure parts (neutral base, the mapping, tone-down, config, tray) always
run; the app parts (switching modes, hotkeys, arrows) only with
CATVISION_GUI_TESTS set, like tests/test_autolevels_app.py.
"""
import json
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

from catvision import autolevels as al, config as cm, curves, hotkeys, presets, tray
from tests.test_autolevels_config import _fake_tray_modules, _Item, _Menu


class NeutralBaseTest(unittest.TestCase):
    def test_base_is_plain(self):
        b = al.AUTO_BASE
        self.assertEqual((b.shadow_boost, b.exposure, b.gamma, b.contrast, b.brightness,
                          b.black_floor, b.white_cap, b.saturation, b.color_filter),
                         (0.0, 0.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, "none"))
        self.assertTrue(curves.is_identity_ramp(curves.build_ramp(b, curves.plan_tint(b))))
        self.assertIsNone(curves.plan_tint(b))

    def test_no_offset_is_plain(self):
        self.assertEqual(al.auto_amounts(0.0), {"shadow_boost": 0.0})
        self.assertEqual(al.auto_look(0.0), al.AUTO_BASE)


class MappingTest(unittest.TestCase):
    CAP = al.auto_cap(30.0)

    def test_cap_scales_how_much(self):
        self.assertEqual(al.auto_cap(30.0), 30.0 * al.AUTO_LIFT_PER_STRENGTH)
        self.assertEqual(al.auto_cap(0.0), 0.0)
        self.assertEqual(al.auto_cap(-5.0), 0.0)

    def test_dark_scene_is_shown_near_the_aim(self):
        aim = 0.12
        for key in (0.02, 0.04, 0.06, 0.1):
            with self.subTest(key=key):
                off = al.auto_offset(key, self.CAP, aim)
                self.assertGreater(off, 0)
                shown = al.shown_key(key, off)
                if off < self.CAP - 0.01:
                    self.assertAlmostEqual(shown, aim * al.AUTO_SHOWN, delta=0.01)
                else:
                    self.assertLessEqual(shown, aim * al.AUTO_SHOWN + 0.01)

    def test_lift_falls_steadily_with_brightness(self):
        last = None
        for i in range(1, 200):
            key = i / 200
            off = al.auto_offset(key, self.CAP, 0.12)
            if last is not None:
                self.assertLessEqual(off, last + 1e-6, key)
            last = off

    def test_lift_is_capped_by_how_much(self):
        for s in (0.0, 5.0, 30.0, 60.0):
            cap = al.auto_cap(s)
            self.assertLessEqual(al.auto_offset(1 / 255, cap, 0.3), cap + 1e-9)
            self.assertGreaterEqual(al.auto_offset(1.0, cap, 0.04), -cap - 1e-9)

    def test_only_pitch_dark_spills_into_exposure(self):
        night = al.auto_amounts(al.auto_offset(0.04, self.CAP, 0.12))
        self.assertNotIn("exposure", night)
        pitch = al.auto_amounts(al.auto_offset(0.005, self.CAP, 0.12))
        self.assertGreater(pitch.get("exposure", 0.0), 0.0)
        self.assertAlmostEqual(pitch["shadow_boost"], 100.0)

    def test_scene_at_aim_is_still_lifted_a_little_and_bright_ones_toned_down(self):
        aim = 0.12
        self.assertGreater(al.auto_offset(aim, self.CAP, aim), 0)
        bright = aim * 2 ** (al.AUTO_RAMP_STOPS + al.AUTO_DAY_STOPS + 1)
        self.assertLess(al.auto_offset(bright, self.CAP, aim, dim=True), 0)
        self.assertEqual(al.auto_offset(bright, self.CAP, aim, dim=False), 0.0)

    def test_daylight_keeps_a_moderate_lift(self):
        """A bright day view still gets the daylight lift (its shadows), an
        all-bright one less, fading out before the tone-down."""
        aim, day = 0.12, al.auto_day_lift(self.CAP)
        self.assertEqual(day, self.CAP * al.AUTO_DAY_SHARE)
        sunny = aim * 2 ** al.AUTO_RAMP_STOPS
        self.assertAlmostEqual(al.auto_offset(sunny, self.CAP, aim), day, delta=0.01)
        washed = aim * 2 ** (al.AUTO_RAMP_STOPS + al.AUTO_DAY_STOPS / 2)
        self.assertAlmostEqual(al.auto_offset(washed, self.CAP, aim), day / 2, delta=0.01)
        self.assertEqual(al.auto_day_lift(0.0), 0.0)
        # A lift mostly in the shadows: the darkest quarter of the levels
        # gains far more than the brightest, and nothing clips.
        look = al.auto_look(day)
        self.assertGreater(curves.tone_curve(0.1, look) - 0.1, 0.08)
        self.assertLess(curves.tone_curve(0.9, look) - 0.9, 0.06)
        self.assertLess(curves.tone_curve(0.97, look), 1.0)

    def test_aim_for_auto_look_round_trip(self):
        for key, aim in ((0.03, 0.12), (0.08, 0.2), (0.5, 0.06)):
            off = al.auto_offset(key, self.CAP, aim)
            got = al.aim_for_auto_look(key, off, self.CAP)
            self.assertAlmostEqual(al.auto_offset(key, self.CAP, got), off, delta=0.5)
        # The Aim for in use is kept when it already gives the look.
        off = al.auto_offset(0.03, self.CAP, 0.12)
        self.assertEqual(al.aim_for_auto_look(0.03, off, self.CAP, current=0.12), 0.12)


class ToneDownTest(unittest.TestCase):
    def test_tone_down_darkens_glare_but_keeps_shadows(self):
        plain = al.auto_look(0.0)
        down = al.auto_look(-60.0)
        self.assertLess(down.brightness, 1.0)
        self.assertGreaterEqual(down.brightness, al.AUTO_TONE_FLOOR)
        # Highlights come down...
        self.assertLess(curves.tone_curve(1.0, down), curves.tone_curve(1.0, plain) - 0.1)
        self.assertLess(curves.tone_curve(0.8, down), curves.tone_curve(0.8, plain) - 0.05)
        # ...the dark tones keep their level (never crushed).
        for x in (0.01, 0.03, 0.05):
            self.assertGreaterEqual(curves.tone_curve(x, down), curves.tone_curve(x, plain) * 0.97)

    def test_tone_down_has_a_floor_and_never_tints(self):
        look = al.auto_look(-10000.0)
        self.assertAlmostEqual(look.brightness, al.AUTO_TONE_FLOOR)
        self.assertEqual(look.color_filter, "none")
        self.assertEqual((look.red, look.green, look.blue), (1.0, 1.0, 1.0))

    def test_fader_amounts_standalone(self):
        base = al.look_values(al.AUTO_BASE)
        up = al.fader_amounts("shadow_boost", 50.0, base, standalone=True)
        self.assertEqual(up, {"shadow_boost": 50.0})
        down = al.fader_amounts("shadow_boost", -30.0, base, standalone=True)
        self.assertLess(down["brightness"], 0)
        # Not standalone: the old offsets on top of a preset, unchanged.
        self.assertEqual(al.fader_amounts("shadow_boost", 10.0, base), {"shadow_boost": 10.0})


class AutoHazeGiveWayTest(unittest.TestCase):
    def test_haze_kept_under_the_daylight_lift_and_off_at_night(self):
        """Auto mode: the haze cut and band give way only to the lift past
        the daylight one, over the same share of its wider range."""
        cap = al.auto_cap(30.0)
        c = al.Controller(standalone=True)
        c.strength, c.haze, c.band, c.band_lo, c.band_hi = cap, 40.0, 50.0, 0.4, 0.7
        base = al.look_values(al.AUTO_BASE)
        day = al.fader_amounts("shadow_boost", al.auto_day_lift(cap), base, c, standalone=True)
        self.assertAlmostEqual(day["haze_cut"], 40.0)
        self.assertAlmostEqual(day["haze_band"], 50.0)
        half = al.auto_day_lift(cap) + al.HAZE_LIFT_OFF * al.AUTO_LIFT_PER_STRENGTH / 2
        self.assertAlmostEqual(al.fader_amounts("shadow_boost", half, base, c,
                                                standalone=True)["haze_cut"], 20.0)
        night = al.fader_amounts("shadow_boost", 100.0, base, c, standalone=True)
        self.assertNotIn("haze_cut", night)
        self.assertNotIn("haze_band", night)
        # Presets as before.
        self.assertNotIn("haze_cut", al.fader_amounts("shadow_boost", al.HAZE_LIFT_OFF,
                                                      {"shadow_boost": 40.0, "exposure": 0.0},
                                                      c))


class AutoDaylightUnderHazeTest(unittest.TestCase):
    def test_daylight_lift_makes_way_for_the_haze_cut(self):
        """Stacked, the daylight lift and the haze cut push a bright hazy
        view's highlights to white: under haze it becomes a gentle
        tone-down instead (none with "tone down" unticked)."""
        cap = al.auto_cap(30.0)
        base = al.look_values(al.AUTO_BASE)
        day = al.auto_day_lift(cap)
        c = al.Controller(standalone=True)
        c.strength = cap
        clear = al.fader_amounts("shadow_boost", day, base, c, standalone=True)
        self.assertAlmostEqual(clear["shadow_boost"], day)
        c.haze = al.AUTO_DAY_HAZE
        hazy = al.fader_amounts("shadow_boost", day, base, c, standalone=True)
        self.assertEqual(hazy["haze_cut"], al.AUTO_DAY_HAZE)
        self.assertAlmostEqual(hazy.get("shadow_boost", 0.0), 0.0)
        # (Where the daylight lift fades out, a brighter view: toned down.)
        brighter = al.fader_amounts("shadow_boost", day / 2, base, c, standalone=True)
        self.assertLess(brighter["brightness"], 0.0)
        c._settings = (cap, 0.12, False)
        self.assertEqual(al.fader_amounts("shadow_boost", day / 2, base, c, standalone=True)
                         .get("brightness", 0.0), 0.0)
        # Half the haze: half the daylight lift given up (smoothly).
        c._settings, c.haze = (), al.AUTO_DAY_HAZE / 2
        self.assertAlmostEqual(al.fader_amounts("shadow_boost", day, base, c, standalone=True)
                               ["shadow_boost"], day / 2)
        # A dark view's lift stays (the haze cut gives way to it instead).
        c.haze = 40.0
        night = al.fader_amounts("shadow_boost", 100.0, base, c, standalone=True)
        self.assertEqual(night["shadow_boost"], 100.0)
        self.assertNotIn("haze_cut", night)


class AutoRoomAndPeekTest(unittest.TestCase):
    def test_room_makes_up_for_no_outside_detail_underneath(self):
        """A bright window from a pitch-dark room at night: auto mode's
        extra Outside detail (AUTO_ROOM) keeps the window's tones no
        brighter than Dark Areas plus auto levels showed them (its own
        Outside detail plus the extra), and leaves the room's tones alone."""
        cap = al.auto_cap(30.0)
        c = al.Controller(standalone=True)
        c._highs = 0.1
        c._aim_room(cap)
        self.assertAlmostEqual(c.room_want, al.room_for_highs(0.1, 30.0) * al.AUTO_ROOM)
        c._aim_room(al.auto_cap(al.ROOM_STRENGTH_FULL / 2))   # How much scales it as before
        self.assertAlmostEqual(c.room_want, al.room_for_highs(0.1, 30.0) * al.AUTO_ROOM / 2)
        c.room = al.room_for_highs(0.1, 30.0) * al.AUTO_ROOM
        base = al.look_values(al.AUTO_BASE)
        amounts = al.fader_amounts("shadow_boost", 94.4, base, c, standalone=True)
        auto = al.AUTO_BASE.clamped({k: base[k] + v for k, v in amounts.items()})
        dark = presets.builtin_by_name("Dark Areas").settings
        old = dark.clamped({"shadow_boost": dark.shadow_boost + 16.1,
                            "highlight_room": dark.highlight_room + 12.1})
        for x in (0.59, 0.8, 0.96):
            self.assertLessEqual(curves.tone_curve(x, auto), curves.tone_curve(x, old) + 0.002)
        plain = al.auto_look(94.4)
        for x in (0.02, 0.08, al.ROOM_KEEP_LEVEL / 255):
            self.assertAlmostEqual(curves.tone_curve(x, auto), curves.tone_curve(x, plain),
                                   places=3)

    def test_peek_on_auto_mode_lifts_shadow_boost_first(self):
        from catvision import engine
        for offset, extra in ((30.0, 35.0), (90.0, 35.0), (110.0, 60.0)):
            with self.subTest(offset=offset):
                amounts = al.auto_amounts(offset)
                look = al.AUTO_BASE.clamped(amounts)
                lift = engine.peek_lift(al.AUTO_BASE, amounts, extra)
                room = 100.0 - look.shadow_boost
                self.assertAlmostEqual(lift.get("shadow_boost", 0.0), min(extra, room), places=5)
                peeked = look.clamped({k: getattr(look, k) + v for k, v in lift.items()})
                self.assertLessEqual(peeked.shadow_boost, 100.0)
                self.assertLessEqual(peeked.exposure, 100.0)
                self.assertGreater(curves.tone_curve(0.05, peeked), curves.tone_curve(0.05, look))


class ViewChangeStepsTest(unittest.TestCase):
    def _steps(self, cut, rush, standalone=True):
        c = al.Controller(standalone=standalone)
        frm, to = (100.0, 20.0) if rush else (20.0, 100.0)
        c.offset, c.want = frm, to
        c.rush, c._rush_view, c._view_rise = rush, rush, not rush
        c._cut_at = 0.0 if cut else -1e9
        # (Each step's cap in a preset's points: see follow.)
        cap = al.CEILING_RUSH / 120 if rush else al.CEILING_STEP
        out = []
        for _ in range(int(al.AUTO_CUT_S * 120) + 30):
            c.clock += 1 / 120
            before, open_ = c.offset, c._cut_open()
            c.follow(to, 120.0, 1 / 120)
            pace = min(al.auto_pace(before), al.auto_pace(c.offset))
            limit = cap * pace * (al.AUTO_CUT_STEP if open_ else 1.0)
            out.append((c.clock, abs(c.offset - before), open_, limit))
        return out

    def test_bigger_steps_only_just_after_a_view_change(self):
        for rush in (False, True):
            with self.subTest(rush=rush):
                plain = self._steps(False, rush)
                cut = self._steps(True, rush)
                for _t, d, _o, limit in plain + cut:
                    self.assertLessEqual(d, limit + 1e-6)
                self.assertGreater(max(d for _t, d, _o, _l in cut),
                                   max(d for _t, d, _o, _l in plain) * 1.3)
                self.assertFalse(any(o for t, _d, o, _l in cut if t > al.AUTO_CUT_S + 1e-9))
        # Presets never open the window.
        c = al.Controller()
        c._cut_at, c._view_rise, c.clock = 0.0, True, 0.1
        self.assertFalse(c._cut_open())

    def test_new_darker_view_opens_it_a_light_going_off_does_not(self):
        c = al.Controller(standalone=True)
        c.clock, c._view_rise = 1.0, True
        self.assertFalse(c._cut_open())          # (never set: a light, a glance back)
        c._cut_at = 0.8
        self.assertTrue(c._cut_open())
        c.clock = 0.8 + al.AUTO_CUT_S + 0.01
        self.assertFalse(c._cut_open())


class ControllerModeTest(unittest.TestCase):
    def _dark(self, n):
        v = 8 + n % 2
        return al.measure(bytes([v, v, v, 255]) * (al.THUMB_W * al.THUMB_H), al.THUMB_W)

    def test_standalone_controller_heads_for_auto_offset(self):
        cap = al.auto_cap(30.0)
        c = al.Controller(standalone=True)
        for i in range(60):
            c.observe(self._dark(i), cap, 0.12, True, now=i * 0.1)
            for _ in range(12):
                c.advance(1 / 120)
        self.assertAlmostEqual(c.want, al.auto_offset(c.key, cap, 0.12), delta=cap * al.DEAD_ZONE)
        self.assertGreater(c.offset, al.auto_cap(30.0) * 0.5)

    def test_auto_mode_fades_faster_in_points(self):
        # Its range is wider, so it moves more points a second (see
        # AUTO_PACE_POINT), rising and falling.
        def move(standalone, start, goal, strength):
            c = al.Controller(standalone=standalone)
            c.offset, c.want = start, goal
            for _ in range(30):
                c.follow(goal, strength, 1 / 120)
            return abs(c.offset - start)
        self.assertGreater(move(True, 100.0, 0.0, 120.0), move(False, 100.0, 0.0, 30.0))
        self.assertGreater(move(True, 0.0, 100.0, 120.0), move(False, 0.0, 100.0, 30.0) * 1.3)

    def test_auto_pace_matches_a_preset_point_on_screen(self):
        """auto_pace points change the picture by about AUTO_PACE_POINT L*
        (the largest change of any level), lifting and toning down."""
        def worst(a, b):
            return max(abs(al._lightness(curves.tone_curve(x / 255, al.auto_look(a)))
                           - al._lightness(curves.tone_curve(x / 255, al.auto_look(b))))
                       for x in range(256))
        for off in (-60.0, -20.0, 10.0, 40.0, 80.0, 110.0):
            with self.subTest(offset=off):
                pace = al.auto_pace(off)
                self.assertGreaterEqual(pace, 1.0)
                self.assertLessEqual(pace, al.AUTO_PACE_MAX)
                self.assertLess(worst(off - pace / 2, off + pace / 2),
                                al.AUTO_PACE_POINT * 1.15)
        # Toning down moves the picture less per point than lifting.
        self.assertGreater(al.auto_pace(-40.0), al.auto_pace(40.0))

    def test_fade_steps_no_more_visible_than_a_presets(self):
        """At full speed (a flash's fade, the quickest rise into a new view)
        each step of auto mode's fade changes the picture no more than the
        same fade did on Dark Areas (L*, any level)."""
        def steps(standalone, rush, frm, to, strength):
            c = al.Controller(standalone=standalone)
            c.offset, c.want = frm, to
            c.rush, c._view_rise = rush, not rush
            out = [frm]
            for _ in range(240):
                out.append(c.follow(to, strength, 1 / 120))
            return out

        def worst_l(offsets, look):
            w = 0.0
            for a, b in zip(offsets, offsets[1:]):
                if a != b:
                    la, lb = look(a), look(b)
                    w = max(w, max(abs(al._lightness(curves.tone_curve(x / 255, la))
                                       - al._lightness(curves.tone_curve(x / 255, lb)))
                                   for x in range(256)))
            return w
        dark = presets.builtin_by_name("Dark Areas").settings
        base = al.look_values(dark)

        def preset_look(o):
            am = al.split_offset("shadow_boost", o, base)
            return dark.clamped({k: base[k] + v for k, v in am.items()})
        for rush, frm, to in ((True, 30.0, -30.0), (False, -30.0, 30.0)):
            with self.subTest(rush=rush):
                old = worst_l(steps(False, rush, frm, to, 30.0), preset_look)
                new = worst_l(steps(True, rush, frm * 4, to * 4, 120.0), al.auto_look)
                self.assertLessEqual(new, old * 1.05)

    def test_preset_controller_unchanged(self):
        c = al.Controller()
        for i in range(60):
            c.observe(self._dark(i), 30.0, 0.12, True, now=i * 0.1)
            for _ in range(12):
                c.advance(1 / 120)
        self.assertLessEqual(c.offset, 30.0)
        self.assertAlmostEqual(c.want, al.target_offset(c.key, 30.0, 0.12), delta=30 * al.DEAD_ZONE)


class ConfigMigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = os.environ.get("CATVISION_CONFIG_DIR")
        os.environ["CATVISION_CONFIG_DIR"] = self.tmp

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CATVISION_CONFIG_DIR", None)
        else:
            os.environ["CATVISION_CONFIG_DIR"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_old_auto_on_with_a_preset_starts_in_auto_mode_and_keeps_the_preset(self):
        data = cm.Config().to_json()
        data["options"]["auto_levels"] = True
        data["active_preset"] = "Gentle Boost"
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg = cm.load()
        self.assertTrue(cfg.options.auto_levels)          # Auto mode
        self.assertEqual(cfg.active_preset, "Gentle Boost")  # for when it is off

    def test_auto_off_stays_a_preset(self):
        data = cm.Config().to_json()
        data["options"]["auto_levels"] = False
        data["active_preset"] = "Dark Areas"
        with open(os.path.join(self.tmp, "config.json"), "w", encoding="utf-8") as f:
            json.dump(data, f)
        cfg = cm.load()
        self.assertFalse(cfg.options.auto_levels)
        self.assertEqual(cfg.active_preset, "Dark Areas")


class TrayAndHotkeyTest(unittest.TestCase):
    def test_tray_preset_list_starts_with_auto(self):
        calls = []
        with mock.patch.dict(sys.modules, _fake_tray_modules()):
            t = tray.Tray(calls.append)
            menu = t.build_menu()
        sub = next(i for i in menu.items if isinstance(i, _Item) and callable(i.text)
                   and isinstance(i.action, _Menu) and "Preset" in i.text(i))
        items = list(sub.action.items[0]())
        first = items[0]
        self.assertEqual(first.text, "Auto (beta)")
        first.action(None, first)
        self.assertEqual(calls, ["auto_mode"])
        t.state.auto_levels = False
        self.assertFalse(first.checked(first))
        t.state.auto_levels = True
        self.assertTrue(first.checked(first))

    def test_hotkey_label_says_mode_and_beta(self):
        label = hotkeys.ACTION_LABELS["auto_levels"]
        self.assertIn("Auto mode", label)
        self.assertIn("(beta)", label)

    def test_no_default_hotkeys(self):
        self.assertNotIn("auto_levels", cm.Config().hotkeys)
        self.assertNotIn("auto_levels", hotkeys.default_hotkeys())


GUI = bool(os.environ.get("CATVISION_GUI_TESTS"))
if GUI:
    try:
        # (The module, not the class: a class imported here would run again.)
        from tests import test_autolevels_app as app_tests
        GUI, D1 = app_tests.GUI, app_tests.D1
    except Exception:   # no tkinter or no display
        GUI = False


@unittest.skipUnless(GUI, "set CATVISION_GUI_TESTS=1 to run the GUI tests")
class AutoModeAppTest(unittest.TestCase):
    PRECONFIG = {
        "test_old_config_starts_in_auto_mode": {"auto_levels": True},
    }
    if GUI:
        _A = app_tests.AutoLevelsAppStress
        setUp, tearDown, pump, pump_until = _A.setUp, _A.tearDown, _A.pump, _A.pump_until
        start_fade, preset_ramp, assert_at_preset = _A.start_fade, _A.preset_ramp, _A.assert_at_preset

    def preset_settings(self, name):
        return self.app.preset_row(name).settings.clamped()

    def test_auto_mode_uses_the_neutral_look(self):
        self.app.activate_preset_by_name("Night Vision")
        self.app._set_enabled(True)
        self.app.set_auto_levels(True)
        self.assertEqual(self.engine.settings, al.AUTO_BASE)
        self.assertEqual(self.app.preset_combo.get(), al.AUTO_NAME)
        self.assertIn(al.AUTO_NAME, self.app.status.cget("text"))
        self.assertEqual(self.app.cfg.active_preset, "Night Vision")   # remembered

    def test_picking_a_preset_leaves_auto_mode(self):
        self.start_fade()
        self.app.activate_preset_by_name("Gentle Boost")
        self.assertFalse(self.app.cfg.options.auto_levels)
        self.assertEqual(self.engine.settings, self.preset_settings("Gentle Boost"))
        self.assert_at_preset()

    def test_auto_off_goes_back_to_the_last_preset(self):
        self.start_fade(preset="Dark Areas")
        self.app.set_auto_levels(False)
        self.assertEqual(self.engine.settings, self.preset_settings("Dark Areas"))
        self.assert_at_preset()
        self.assertEqual(self.app.preset_combo.get().split("  ")[0], "Dark Areas")

    def test_dark_game_lifts_from_the_neutral_look(self):
        self.start_fade(preset="Night Vision")
        self.assertEqual(self.engine.settings, al.AUTO_BASE)
        eff = self.engine.settings_for(D1, peek=False)
        self.assertGreater(eff.shadow_boost, 0)
        self.assertEqual(eff.color_filter, "none")
        self.assertIn("Shadow boost 0", self.app.auto_info.cget("text"))

    def test_arrows_cycle_through_auto(self):
        self.app._set_enabled(True)
        first, last = self.app.preset_names()[0], self.app.preset_names()[-1]
        self.app.activate_preset_by_name(last)
        self.app.handle_action("next")
        self.assertTrue(self.app.cfg.options.auto_levels)
        self.app.handle_action("next")
        self.assertFalse(self.app.cfg.options.auto_levels)
        self.assertEqual(self.app.cfg.active_preset, first)
        self.app.handle_action("prev")
        self.assertTrue(self.app.cfg.options.auto_levels)
        self.app.handle_action("prev")
        self.assertEqual(self.app.cfg.active_preset, last)
        self.assertFalse(self.app.cfg.options.auto_levels)

    def test_hotkeys_follow_the_mode(self):
        self.app.activate_preset_by_name("Dark Areas")
        self.app.handle_action("auto_levels")
        self.assertTrue(self.engine.enabled)
        self.assertEqual(self.engine.settings, al.AUTO_BASE)
        # A preset hotkey for the remembered preset switches to it (not off).
        self.app.handle_action("preset:Dark Areas")
        self.assertTrue(self.engine.enabled)
        self.assertFalse(self.app.cfg.options.auto_levels)
        self.assertEqual(self.engine.settings, self.preset_settings("Dark Areas"))
        self.app.handle_action("auto_mode")
        self.assertTrue(self.app.cfg.options.auto_levels)
        before = self.app._auto_aim()
        self.app.handle_action("boost_up")   # Auto mode: aim brighter
        self.assertGreater(self.app._auto_aim(), before)
        self.assertEqual(self.engine.settings, al.AUTO_BASE)

    def test_preset_list_starts_with_auto(self):
        values = self.app.preset_combo.cget("values")
        self.assertEqual(values[0], al.AUTO_NAME)
        self.assertEqual(len(values), len(self.app.preset_names()) + 1)
        self.app.preset_combo.current(0)
        self.app._on_preset_combo()
        self.assertTrue(self.app.cfg.options.auto_levels)
        self.app.preset_combo.current(2)
        self.app._on_preset_combo()
        self.assertFalse(self.app.cfg.options.auto_levels)
        self.assertEqual(self.app.cfg.active_preset, self.app.preset_names()[1])
        self.assertEqual(self.app.preset_combo.current(), 2)

    def test_old_config_starts_in_auto_mode(self):
        self.assertTrue(self.app.cfg.options.auto_levels)
        self.assertEqual(self.app.cfg.active_preset, "Pitch Black")
        self.app._set_enabled(True)
        self.assertEqual(self.engine.settings, al.AUTO_BASE)
        self.app.set_auto_levels(False)
        # Off: the preset's sliders as they were (the remembered preset).
        self.assertEqual(self.engine.settings, self.app._read_settings().clamped())
        self.assertEqual(self.app.cfg.active_preset, "Pitch Black")
        self.assertFalse(self.app.cfg.options.auto_levels)


if __name__ == "__main__":
    unittest.main()
