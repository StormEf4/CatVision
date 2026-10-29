import json
import tempfile
import unittest
from pathlib import Path

from catvision import config, curves
from catvision.backends import Monitor, NullColorBackend, apply_ramp_with_fallback
from catvision.curves import Settings
from catvision.engine import Engine, emergency_reset
from catvision.presets import BUILTIN_PRESETS


class FakeGamma:
    """Mimics Windows: rejects ramps that stray more than ``limit`` levels."""

    available = True
    reason = ""

    def __init__(self, limit=None, start=None):
        self.limit = limit
        self.ramps = {"\\\\.\\DISPLAY1": start or curves.identity_ramp(),
                      "\\\\.\\DISPLAY2": curves.identity_ramp()}
        self.calls = 0

    def monitors(self):
        return [Monitor(k, k, i == 0) for i, k in enumerate(self.ramps)]

    def set_ramp(self, mid, ramp):
        self.calls += 1
        if self.limit is not None:
            for ch in ramp:
                for i, v in enumerate(ch):
                    if abs((v >> 8) - i) > self.limit:
                        return False
        self.ramps[mid] = [list(c) for c in ramp]
        return True

    def get_ramp(self, mid):
        return [list(c) for c in self.ramps[mid]]

    def close(self):
        pass


class FakeColor(NullColorBackend):
    available = True


class CurveTests(unittest.TestCase):
    def test_default_settings_are_identity(self):
        self.assertTrue(curves.is_identity_ramp(curves.build_ramp(Settings()), tolerance=1))
        self.assertTrue(curves.is_identity_matrix(curves.color_matrix(Settings())))

    def test_shadow_boost_lifts_darks_more_than_brights(self):
        ramp = curves.build_ramp(Settings(shadow_boost=60))[0]
        ident = curves.identity_ramp()[0]
        dark_gain = ramp[10] / ident[10]
        bright_gain = ramp[240] / ident[240]
        self.assertGreater(dark_gain, 3)
        self.assertLess(bright_gain, 1.1)
        self.assertEqual(ramp[255], 65535)

    def test_more_boost_is_brighter(self):
        lows = [curves.build_ramp(Settings(shadow_boost=b))[0][8] for b in (0, 25, 50, 75, 100)]
        self.assertEqual(lows, sorted(lows))
        self.assertEqual(len(set(lows)), len(lows))

    def test_ramps_valid_for_all_presets_and_extremes(self):
        cases = [p.settings for p in BUILTIN_PRESETS]
        lo = Settings(**{k: v[0] for k, v in curves.LIMITS.items()})
        hi = Settings(**{k: v[1] for k, v in curves.LIMITS.items()})
        for s in cases + [lo, hi]:
            for ch in curves.build_ramp(s):
                self.assertEqual(len(ch), 256)
                self.assertTrue(all(0 <= v <= 65535 for v in ch))
                self.assertTrue(all(a <= b for a, b in zip(ch, ch[1:])), "must be non-decreasing")

    def test_black_floor_and_white_cap(self):
        ramp = curves.build_ramp(Settings(black_floor=0.1, white_cap=0.8))[1]
        self.assertAlmostEqual(ramp[0] / 65535, 0.08, places=2)
        self.assertAlmostEqual(ramp[255] / 65535, 0.8, places=2)

    def test_channel_gain_tints(self):
        r, g, b = curves.build_ramp(Settings(blue=0.6))
        self.assertEqual(r, g)
        self.assertLess(b[200], r[200])

    def test_limit_ramp_deviation(self):
        ramp = curves.build_ramp(Settings(shadow_boost=100, gamma=3))
        limited = curves.limit_ramp_deviation(ramp, 127)
        for ch in limited:
            for i, v in enumerate(ch):
                self.assertLessEqual(abs((v >> 8) - i), 127)
            self.assertTrue(all(a <= b for a, b in zip(ch, ch[1:])))

    def test_settings_from_untrusted_dict(self):
        s = Settings.from_dict({"shadow_boost": "999", "gamma": "nan", "color_filter": "evil",
                                "contrast": None, "unknown": 1})
        self.assertEqual(s.shadow_boost, 100)
        self.assertEqual(s.gamma, 1.0)
        self.assertEqual(s.color_filter, "none")
        self.assertEqual(Settings.from_dict("garbage"), Settings())


class MatrixTests(unittest.TestCase):
    def test_grayscale_rows_sum_to_one(self):
        m = curves.filter_matrix("grayscale")
        for row in m:
            self.assertAlmostEqual(sum(row), 1.0, places=4)
        r, g, b = curves.apply_matrix(m, (1.0, 0.0, 0.0))
        self.assertAlmostEqual(r, g)
        self.assertAlmostEqual(g, b)

    def test_night_vision_is_green(self):
        r, g, b = curves.apply_matrix(curves.filter_matrix("night_vision"), (0.5, 0.5, 0.5))
        self.assertGreater(g, r)
        self.assertGreater(g, b)

    def test_filter_strength_zero_is_identity(self):
        m = curves.color_matrix(Settings(color_filter="night_vision", filter_strength=0))
        self.assertTrue(curves.is_identity_matrix(m))

    def test_mag_effect_layout_is_transposed(self):
        m = [[1, 2, 3], [4, 5, 6], [7, 8, 9]]
        e = curves.to_mag_color_effect(m)
        self.assertEqual(e[0:3], [1, 4, 7])   # input red -> outputs r, g, b
        self.assertEqual(e[5:8], [2, 5, 8])
        self.assertEqual(e[18], 1.0)
        self.assertEqual(e[24], 1.0)


class FallbackTests(unittest.TestCase):
    def test_full_ramp_when_allowed(self):
        g = FakeGamma()
        ramp = curves.build_ramp(Settings(shadow_boost=100))
        r = apply_ramp_with_fallback(g, "\\\\.\\DISPLAY1", ramp)
        self.assertTrue(r.ok)
        self.assertFalse(r.limited)
        self.assertEqual(g.ramps["\\\\.\\DISPLAY1"], ramp)

    def test_clamped_when_windows_limits(self):
        g = FakeGamma(limit=128)
        ramp = curves.build_ramp(Settings(shadow_boost=100, gamma=3))
        r = apply_ramp_with_fallback(g, "\\\\.\\DISPLAY1", ramp)
        self.assertTrue(r.ok)
        self.assertTrue(r.limited)
        applied = g.ramps["\\\\.\\DISPLAY1"][0]
        self.assertGreater(applied[10], curves.identity_ramp()[0][10] * 3)

    def test_blend_search_when_very_strict(self):
        g = FakeGamma(limit=20)
        ramp = curves.build_ramp(Settings(shadow_boost=100))
        r = apply_ramp_with_fallback(g, "\\\\.\\DISPLAY1", ramp)
        self.assertTrue(r.ok)
        self.assertTrue(r.limited)
        self.assertLess(r.strength, 1.0)
        self.assertLess(g.calls, 20)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_enable_disable_restores_original(self):
        calibrated = curves.blend_ramp(curves.build_ramp(Settings(gamma=0.9)), 1.0)
        g = FakeGamma(start=calibrated)
        color = FakeColor()
        e = Engine(g, color, self.dir)
        e.settings = Settings(shadow_boost=70, color_filter="night_vision")
        e.set_enabled(True)
        self.assertNotEqual(g.ramps["\\\\.\\DISPLAY1"], calibrated)
        self.assertIsNotNone(color.last)
        self.assertTrue((self.dir / "display_modified.flag").exists())
        e.set_enabled(False)
        self.assertEqual(g.ramps["\\\\.\\DISPLAY1"], calibrated)
        self.assertIsNone(color.last)
        self.assertFalse((self.dir / "display_modified.flag").exists())

    def test_crash_recovery_uses_identity(self):
        (self.dir / "display_modified.flag").touch()
        leftover = curves.build_ramp(Settings(shadow_boost=90))
        g = FakeGamma(start=leftover)
        e = Engine(g, NullColorBackend(), self.dir)
        # Cleaned up right away, before the user turns anything on.
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertFalse((self.dir / "display_modified.flag").exists())
        e.settings = Settings(shadow_boost=10)
        e.set_enabled(True)
        e.shutdown()
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))

    def test_chosen_displays_only(self):
        g = FakeGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.displays = ["\\\\.\\DISPLAY2"]
        e.settings = Settings(shadow_boost=50)
        e.set_enabled(True)
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))
        self.assertFalse(e.all_displays_selected())
        e.set_displays(["\\\\.\\DISPLAY1"])
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))
        e.set_displays([])  # empty = all
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))
        self.assertTrue(e.all_displays_selected())

    def test_unplugged_display_falls_back_to_all(self):
        g = FakeGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.displays = ["\\\\.\\DISPLAY9"]
        e.settings = Settings(shadow_boost=50)
        e.set_enabled(True)
        self.assertTrue(e.all_displays_selected())
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))

    def test_tint_filter_stays_on_chosen_display(self):
        g = FakeGamma()
        color = FakeColor()
        e = Engine(g, color, self.dir)
        e.displays = ["\\\\.\\DISPLAY1"]
        e.settings = Settings(color_filter="night_vision")
        status = e.set_enabled(True)
        r, gr, b = g.ramps["\\\\.\\DISPLAY1"]
        self.assertLess(r[200], gr[200])               # green tint via brightness table
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))
        self.assertIsNone(color.last)                   # no screen-wide filter needed
        self.assertFalse(status.color_all_displays)

    def test_unticked_display_untouched_by_every_preset_and_filter(self):
        """Regression: saturation / color-mixing filters must not leak onto
        displays the user left unticked (the screen-wide color engine would
        change every display)."""
        from catvision.presets import BUILTIN_PRESETS as presets
        for p in presets:
            g = FakeGamma()
            color = FakeColor()
            e = Engine(g, color, self.dir)
            e.displays = ["\\\\.\\DISPLAY2"]
            e.settings = p.settings
            status = e.set_enabled(True)
            self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]), p.name)
            self.assertIsNone(color.last, f"{p.name} changed every display's colors")
            if p.settings.saturation != 1.0 or p.settings.color_filter in (
                    "grayscale",):
                self.assertTrue(status.color_skipped, p.name)
            e.shutdown()

    def test_mixing_filter_with_chosen_displays(self):
        g = FakeGamma()
        color = FakeColor()
        e = Engine(g, color, self.dir)
        e.displays = ["\\\\.\\DISPLAY1"]
        e.settings = Settings(color_filter="grayscale")
        status = e.set_enabled(True)
        self.assertTrue(status.color_skipped)           # off by default: others untouched
        self.assertIsNone(color.last)
        self.assertIn("other displays", status.summary())
        e.color_all_displays = True                     # the user opted in
        status = e.apply()
        self.assertTrue(status.color_all_displays)
        self.assertIsNotNone(color.last)

    def test_restore_now(self):
        g = FakeGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=80)
        e.set_enabled(True)
        e.restore_now()
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertFalse((self.dir / "display_modified.flag").exists())

    def test_unavailable_color_backend_reports_reason(self):
        e = Engine(FakeGamma(), NullColorBackend(), self.dir)
        e.settings = Settings(color_filter="grayscale")
        status = e.set_enabled(True)
        self.assertFalse(status.color_ok)
        self.assertIn("Windows", status.summary())

    def test_reapply_after_game_resets_gamma(self):
        g = FakeGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=50)
        e.set_enabled(True)
        self.assertFalse(e.check_and_reapply())
        g.ramps["\\\\.\\DISPLAY1"] = curves.identity_ramp()  # a game resets it
        self.assertTrue(e.check_and_reapply())
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))

    def test_identity_color_does_not_touch_color_backend(self):
        color = FakeColor()
        e = Engine(FakeGamma(), color, self.dir)
        e.settings = Settings(shadow_boost=50)
        status = e.set_enabled(True)
        self.assertIsNone(color.last)
        self.assertFalse(status.color_used)

    def test_emergency_reset(self):
        g = FakeGamma(start=curves.build_ramp(Settings(shadow_boost=90)))
        (self.dir / "display_modified.flag").touch()
        self.assertEqual(emergency_reset(g, NullColorBackend(), self.dir), 2)
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertFalse((self.dir / "display_modified.flag").exists())


class ConfigTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            c = config.Config()
            c.current = Settings(shadow_boost=42, color_filter="amber")
            c.custom_presets["Mine"] = Settings(gamma=1.5)
            c.options.hotkey_popup = False
            config.save(c, path)
            loaded = config.load(path)
            self.assertEqual(loaded.current, c.current)
            self.assertEqual(loaded.custom_presets, c.custom_presets)
            self.assertFalse(loaded.options.hotkey_popup)

    def test_corrupt_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{not json")
            self.assertEqual(config.load(path).current, config.Config().current)
            path.write_text(json.dumps({"options": {"hotkeys": "yes"}, "custom_presets": []}))
            loaded = config.load(path)
            self.assertTrue(loaded.options.hotkeys)
            self.assertEqual(loaded.custom_presets, {})


class PresetTests(unittest.TestCase):
    def test_unique_names_and_valid(self):
        names = [p.name for p in BUILTIN_PRESETS]
        self.assertEqual(len(names), len(set(names)))
        for p in BUILTIN_PRESETS:
            self.assertEqual(p.settings, p.settings.clamped(), p.name)


if __name__ == "__main__":
    unittest.main()


class HotkeyParsingTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self):
        from catvision import hotkeys as hk
        for combo in ["Ctrl+Alt+V", "Ctrl+Alt+Page Down", "Ctrl+Alt+=", "Ctrl+Alt+-",
                      "Ctrl+Shift+F12", "Alt+Num 5", "F8", "Ctrl+Win+Num Add"]:
            parsed = hk.parse_combo(combo)
            self.assertIsNotNone(parsed, combo)
            self.assertEqual(hk.format_combo(*parsed), combo)
        self.assertEqual(hk.normalize_combo("alt+control+v"), "Ctrl+Alt+V")
        self.assertEqual(hk.parse_combo("Ctrl+Alt+V"), (hk.MOD_CONTROL | hk.MOD_ALT, 0x56))

    def test_invalid_combos(self):
        from catvision import hotkeys as hk
        for bad in ["", "Ctrl+", "Hyper+V", "Ctrl+Alt+Nope", None, 5]:
            self.assertIsNone(hk.parse_combo(bad), bad)

    def test_combo_problems(self):
        from catvision import hotkeys as hk
        self.assertIsNone(hk.combo_problem("Ctrl+Alt+V"))
        self.assertIsNone(hk.combo_problem("F8"))
        self.assertIsNone(hk.combo_problem("Shift+F8"))
        self.assertIsNotNone(hk.combo_problem("Q"))          # would block typing in games
        self.assertIsNotNone(hk.combo_problem("Shift+Q"))
        self.assertIsNotNone(hk.combo_problem("Ctrl+Alt+Delete"))

    def test_tk_key_names(self):
        from catvision import hotkeys as hk
        self.assertEqual(hk.key_name_from_tk("k", 0), "K")
        self.assertEqual(hk.key_name_from_tk("Next", 0), "Page Down")
        self.assertEqual(hk.key_name_from_tk("KP_7", 0), "Num 7")
        self.assertEqual(hk.key_name_from_tk("plus", 0), "=")
        self.assertEqual(hk.key_name_from_tk("F11", 0), "F11")
        self.assertIsNone(hk.key_name_from_tk("Caps_Lock", 0))

    def test_bindings_skip_blank_bad_and_duplicate(self):
        from catvision import hotkeys as hk
        b = hk.bindings_from_config({"toggle": "Ctrl+Alt+V", "off": "", "next": "junk",
                                     "prev": "ctrl+alt+v", "preset:X": "Ctrl+Alt+1"})
        self.assertEqual([x.action for x in b], ["toggle", "preset:X"])

    def test_defaults_are_valid_and_unique(self):
        from catvision import hotkeys as hk
        self.assertEqual(hk.default_hotkeys(), {})          # the user sets their own
        d = hk.suggested_hotkeys()
        self.assertEqual(len(set(d.values())), len(d))
        for combo in d.values():
            self.assertIsNone(hk.combo_problem(combo), combo)
        names = {p.name for p in BUILTIN_PRESETS}
        for action in d:
            if action.startswith("preset:"):
                self.assertIn(action[7:], names)


class CustomFilterTests(unittest.TestCase):
    def test_tint_and_mixer_matrices(self):
        from catvision.filters import CustomFilter
        tint = CustomFilter("tint", [0.0, 1.0, 0.0])
        r, g, b = curves.apply_matrix(tint.to_matrix(), (0.5, 0.5, 0.5))
        self.assertEqual((r, b), (0.0, 0.0))
        self.assertAlmostEqual(g, 0.5, places=3)
        swap = CustomFilter("mixer", matrix=[[0, 0, 1], [0, 1, 0], [1, 0, 0]])
        self.assertEqual(curves.apply_matrix(swap.to_matrix(), (1.0, 0.0, 0.0)), (0.0, 0.0, 1.0))

    def test_from_untrusted_dict_is_clamped(self):
        from catvision.filters import CustomFilter
        f = CustomFilter.from_dict({"kind": "evil", "tint": [9, "x", -1],
                                    "matrix": [[99, 0, 0], [0, "nan", 0], [0, 0, 1]]})
        self.assertEqual(f.kind, "tint")
        self.assertEqual(f.tint, [1.5, 1.0, 0.0])
        self.assertEqual(f.matrix[0][0], 2.0)
        self.assertEqual(f.matrix[1][1], 1.0)
        self.assertEqual(CustomFilter.from_dict({"matrix": [[1, 2]]}).matrix, curves.IDENTITY3)

    def test_copy_of_builtin_matches_builtin(self):
        from catvision import filters
        for key in curves.COLOR_FILTERS:
            if key == "none":
                continue
            copy = filters.copy_of_builtin(key).to_matrix()
            orig = curves.filter_matrix(key)
            for i in range(3):
                for j in range(3):
                    self.assertAlmostEqual(copy[i][j], orig[i][j], places=2, msg=key)

    def test_settings_accept_custom_filter_keys(self):
        s = Settings.from_dict({"color_filter": "custom:My Green"})
        self.assertEqual(s.color_filter, "custom:My Green")
        self.assertEqual(Settings.from_dict({"color_filter": "custom:"}).color_filter, "none")
        self.assertEqual(Settings.from_dict({"color_filter": "custom:" + "x" * 99}).color_filter, "none")

    def test_engine_uses_custom_filter(self):
        from catvision import filters
        from catvision.filters import CustomFilter
        color = FakeColor()
        with tempfile.TemporaryDirectory() as d:
            e = Engine(FakeGamma(), color, Path(d))
            custom = {"Red Only": CustomFilter("mixer", matrix=[[1, 0, 0], [0, 0, 0], [0, 0, 0]])}
            e.custom_filters = filters.matrices(custom)
            e.settings = Settings(color_filter="custom:Red Only")
            e.set_enabled(True)
            self.assertEqual(color.last[0:3], [1.0, 0.0, 0.0])
            self.assertEqual(color.last[5:8], [0.0, 0.0, 0.0])
            # a deleted filter falls back to no filter instead of breaking
            e.custom_filters = {}
            e.apply()
            self.assertIsNone(color.last)


class PresetOverrideTests(unittest.TestCase):
    def test_saved_builtin_overrides_and_customs_follow(self):
        from catvision.presets import all_presets
        mine = Settings(shadow_boost=99)
        rows = all_presets({"Dark Areas": mine, "zeta": Settings(), "Alpha": Settings()})
        names = [r.name for r in rows]
        self.assertEqual(names[:len(BUILTIN_PRESETS)], [p.name for p in BUILTIN_PRESETS])
        self.assertEqual(names[len(BUILTIN_PRESETS):], ["Alpha", "zeta"])
        dark = rows[names.index("Dark Areas")]
        self.assertTrue(dark.builtin and dark.changed)
        self.assertEqual(dark.settings, mine)
        self.assertFalse(rows[0].changed)


class ConfigV2Tests(unittest.TestCase):
    def test_v1_file_gets_no_hotkeys(self):
        c = config.Config.from_json({"version": 1, "current": {"shadow_boost": 40}})
        self.assertEqual(c.hotkeys, {})
        self.assertEqual(c.custom_filters, {})

    def test_round_trip_filters_and_hotkeys(self):
        from catvision.filters import CustomFilter
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            c = config.Config()
            c.custom_filters["Mine"] = CustomFilter("mixer", matrix=[[0, 1, 0], [1, 0, 0], [0, 0, 1]])
            c.hotkeys = {"toggle": "alt+ctrl+k", "off": "", "preset:Mine": "garbage"}
            config.save(c, path)
            loaded = config.load(path)
            self.assertEqual(loaded.custom_filters["Mine"].matrix, [[0, 1, 0], [1, 0, 0], [0, 0, 1]])
            self.assertEqual(loaded.hotkeys, {"toggle": "Ctrl+Alt+K", "off": "", "preset:Mine": ""})

    def test_config_dir_override(self):
        import os
        old = os.environ.get("CATVISION_CONFIG_DIR")
        os.environ["CATVISION_CONFIG_DIR"] = "/tmp/somewhere"
        try:
            self.assertEqual(config.app_dir(), Path("/tmp/somewhere"))
        finally:
            if old is None:
                del os.environ["CATVISION_CONFIG_DIR"]
            else:
                os.environ["CATVISION_CONFIG_DIR"] = old


class DisplayOptionTests(unittest.TestCase):
    def test_old_single_monitor_setting_migrates(self):
        o = config.Options.from_dict({"monitor": "\\\\.\\DISPLAY2"})
        self.assertEqual(o.displays, ["\\\\.\\DISPLAY2"])
        self.assertEqual(config.Options.from_dict({"monitor": "all"}).displays, [])
        o = config.Options.from_dict({"displays": ["a", 3, None, "b"]})
        self.assertEqual(o.displays, ["a", "b"])

    def test_split_for_displays(self):
        tint, m = curves.split_for_displays(Settings(color_filter="amber", filter_strength=0.5))
        self.assertTrue(curves.is_identity_matrix(m))
        lo, hi, _p = tint
        self.assertAlmostEqual(lo[2], 1 + (0.25 - 1) * 0.5)
        tint, m = curves.split_for_displays(Settings(color_filter="grayscale"))
        self.assertIsNone(tint)
        self.assertFalse(curves.is_identity_matrix(m))
        tint, m = curves.split_for_displays(
            Settings(color_filter="custom:Mine"), custom_tints={"custom:Mine": (1.0, 0.5, 0.5)})
        self.assertEqual(tint[0], (1.0, 0.5, 0.5))


class FirstRunTests(unittest.TestCase):
    def test_fresh_config_matches_active_preset(self):
        from catvision.presets import builtin_by_name
        c = config.Config()
        self.assertEqual(c.current, builtin_by_name(c.active_preset).settings)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(config.load(Path(d) / "missing.json").current, c.current)


class ColorEverywhereOptionTests(unittest.TestCase):
    def test_old_default_on_setting_is_not_carried_over(self):
        o = config.Options.from_dict({"color_all_displays": True})
        self.assertFalse(o.color_everywhere)
        self.assertTrue(config.Options.from_dict({"color_everywhere": True}).color_everywhere)


class NightVisionTests(unittest.TestCase):
    def test_exposure_off_is_identity_and_on_lifts_evenly(self):
        self.assertTrue(curves.is_identity_ramp(curves.build_ramp(Settings(exposure=0)), tolerance=1))
        ramp = curves.build_ramp(Settings(exposure=70))[1]
        self.assertTrue(all(a <= b for a, b in zip(ramp, ramp[1:])))
        # The dim range a dark game lives in (levels 6..32) is spread widely,
        # instead of being squeezed together like the log-shaped Shadow boost.
        exp_spread = ramp[32] - ramp[6]
        log = curves.build_ramp(Settings(shadow_boost=80, gamma=1.2))[1]
        self.assertGreater(exp_spread, 1.3 * (log[32] - log[6]))

    def test_night_vision_is_grayscale_then_green_on_all_displays(self):
        s = [p for p in BUILTIN_PRESETS if p.name == "Night Vision"][0].settings
        tint, m = curves.plan_color(s, all_displays=True)
        self.assertFalse(curves.is_identity_matrix(m))     # grayscale step
        r, g, b = curves.build_ramp(s, tint)
        self.assertLess(r[8], g[8] / 2)                    # deep green shadows
        self.assertGreater(r[250] / g[250], 0.5)           # paler phosphor highlights

    def test_blue_and_red_night_vision(self):
        def ramp_for(name):
            st = [p for p in BUILTIN_PRESETS if p.name == name][0].settings
            tint, _m = curves.plan_color(st)
            return curves.build_ramp(st, tint)
        r, g, b = ramp_for("Night Vision (Blue)")
        self.assertGreater(b[8], r[8] * 2)                 # blue shadows
        self.assertGreater(b[180], r[180])                 # still blue in brights
        red = Settings(exposure=80, anti_glare=70, contrast=1.1, black_floor=0.015,
                       white_cap=0.88, color_filter="night_vision_red")
        tint, _m = curves.plan_color(red)
        r, g, b = curves.build_ramp(red, tint)
        self.assertGreater(r[8], g[8] * 2)                 # red shadows
        self.assertGreater(r[8], b[8] * 2)
        self.assertGreater(r[180], b[180])

    def test_every_preset_ramp_is_valid_with_its_tint(self):
        for p in BUILTIN_PRESETS:
            for all_displays in (True, False):
                tint, _m = curves.plan_color(p.settings, all_displays=all_displays)
                for ch in curves.build_ramp(p.settings, tint):
                    self.assertTrue(all(0 <= v <= 65535 for v in ch), p.name)
                    self.assertTrue(all(x <= y for x, y in zip(ch, ch[1:])), p.name)


class AntiGlareTests(unittest.TestCase):
    def test_off_matches_plain_exposure(self):
        for x in (0.0, 0.03, 0.1, 0.4, 1.0):
            self.assertAlmostEqual(curves.exposure_curve(x, 70, 0), curves.exposure_curve(x, 70))

    def test_curve_is_continuous_and_rising(self):
        prev = curves.exposure_curve(0.0, 70, 70)
        for i in range(1, 1001):
            y = curves.exposure_curve(i / 1000, 70, 70)
            self.assertGreaterEqual(y, prev)
            self.assertLess(y - prev, 0.03, "no jumps at the knee")
            prev = y
        self.assertAlmostEqual(curves.exposure_curve(1.0, 70, 70), 1.0)

    def test_bright_things_stay_apart_in_night_vision(self):
        """A mid-tone player (level 70) in front of a lit window (level 180)
        must not merge into it, and the brightest output must stay capped."""
        looks = {p.name: p.settings for p in BUILTIN_PRESETS if "Night Vision" in p.name}
        # filters kept without a preset, with the settings they were tuned for
        looks["night_vision_red"] = Settings(exposure=80, anti_glare=70, contrast=1.1,
                                             black_floor=0.015, white_cap=0.88,
                                             color_filter="night_vision_red")
        looks["night_vision_grey"] = Settings(exposure=72, anti_glare=70, contrast=1.25,
                                              brightness=0.9, white_cap=0.80,
                                              color_filter="night_vision_grey")
        looks["thermal"] = Settings(exposure=70, anti_glare=70, contrast=1.1, white_cap=0.80,
                                    color_filter="thermal")
        for name, s in looks.items():
            tint, _m = curves.plan_color(s)
            ramp = curves.build_ramp(s, tint)
            luma = [0.2126 * r + 0.7152 * g + 0.0722 * b for r, g, b in zip(*ramp)]
            self.assertGreater(luma[180] - luma[70], 0.12 * 65535, name)
            self.assertGreater(luma[250] - luma[180], 0.015 * 65535, name)
            self.assertLessEqual(max(max(ch) for ch in ramp), 0.89 * 65535, name)
            # dark game detail clearly lifted (3x the game on its own)
            self.assertGreater(luma[16], 3 * (16 / 255) * 65535, name)


class LegacyBlueRedTests(unittest.TestCase):
    def test_old_blue_red_settings_and_names_migrate(self):
        c = config.Config.from_json({
            "current": {"color_filter": "night_vision_br", "exposure": 75},
            "active_preset": "Night Vision (Blue/Red)",
            "custom_presets": {"Night Vision (Blue/Red)": {"color_filter": "night_vision_br"}},
            "hotkeys": {"preset:Night Vision (Blue/Red)": "Ctrl+Alt+B"},
        })
        self.assertEqual(c.current.color_filter, "night_vision_blue")
        self.assertEqual(c.active_preset, "Night Vision (Blue)")
        self.assertEqual(c.custom_presets["Night Vision (Blue)"].color_filter, "night_vision_blue")
        self.assertNotIn("Night Vision (Blue/Red)", c.custom_presets)
        self.assertEqual(c.hotkeys.get("preset:Night Vision (Blue)"), "Ctrl+Alt+B")
        self.assertNotIn("preset:Night Vision (Blue/Red)", c.hotkeys)


class NoDefaultHotkeysTests(unittest.TestCase):
    def test_fresh_config_has_no_hotkeys(self):
        self.assertEqual(config.Config().hotkeys, {})
        from catvision.hotkeys import bindings_from_config
        self.assertEqual(bindings_from_config(config.Config().hotkeys), [])

    def test_untouched_old_defaults_are_cleared_but_custom_keys_kept(self):
        from catvision.hotkeys import LEGACY_DEFAULT_HOTKEYS
        self.assertEqual(config.Config.from_json({"hotkeys": LEGACY_DEFAULT_HOTKEYS}).hotkeys, {})
        custom = dict(LEGACY_DEFAULT_HOTKEYS, toggle="Ctrl+Alt+K")
        self.assertEqual(config.Config.from_json({"hotkeys": custom}).hotkeys["toggle"], "Ctrl+Alt+K")


class GreyNightVisionTests(unittest.TestCase):
    def test_grey_is_neutral_and_greyscale_on_all_displays(self):
        st = Settings(exposure=72, anti_glare=70, contrast=1.25, brightness=0.9,
                      white_cap=0.80, color_filter="night_vision_grey")
        tint, m = curves.plan_color(st, all_displays=True)
        self.assertFalse(curves.is_identity_matrix(m))        # picture made grey first
        r, g, b = curves.build_ramp(st, tint)
        self.assertEqual(r, g)
        self.assertEqual(g, b)
        green = [p for p in BUILTIN_PRESETS if p.name == "Night Vision"][0].settings
        gt, _ = curves.plan_color(green)
        gr = curves.build_ramp(green, gt)
        luma = lambda ch, i: 0.2126 * ch[0][i] + 0.7152 * ch[1][i] + 0.0722 * ch[2][i]
        # IR look: deep blacks, but dark tones spread further apart than green's,
        # i.e. more visible detail in the dark range games live in (levels 6-32).
        spread = lambda ch: luma(ch, 32) - luma(ch, 6)
        self.assertGreater(spread((r, g, b)), spread(gr))


class ThermalTests(unittest.TestCase):
    def test_gradient_stops_only_rise(self):
        for key, grad in curves.GRADIENTS.items():
            for (p0, c0), (p1, c1) in zip(grad.stops, grad.stops[1:]):
                self.assertLess(p0, p1, key)
                for j in range(3):
                    self.assertLessEqual(c0[j], c1[j], f"{key}: channel {j} must not fall")

    def test_thermal_cold_purple_hot_yellow(self):
        st = Settings(exposure=70, anti_glare=70, contrast=1.1, white_cap=0.80,
                      color_filter="thermal")
        tint, m = curves.plan_color(st)
        self.assertIsInstance(tint, curves.Gradient)
        self.assertFalse(curves.is_identity_matrix(m))        # grey first, then palette
        r, g, b = curves.build_ramp(st, tint)
        self.assertGreater(b[3], g[3])                        # cold: purple (blue > green)
        self.assertGreater(r[3], g[3])
        self.assertGreater(g[250], b[250])                    # hot: yellow-white
        self.assertGreater(r[250], b[250])

    def test_gradient_strength_blends_to_grey(self):
        grad = curves.GRADIENTS["thermal"]
        self.assertEqual(curves.Gradient(grad.stops, 0.0).color(0.4), (0.4, 0.4, 0.4))


class PresetCleanupTests(unittest.TestCase):
    def test_only_the_kept_presets_remain(self):
        self.assertEqual([p.name for p in BUILTIN_PRESETS], [
            "Gentle Boost", "Dark Areas", "Pitch Black", "Night Vision", "Night Vision (Blue)",
            "Fog & Haze Cutter", "Vivid Pop", "Glare Guard"])
        for key in ("deuteranopia", "protanopia", "tritanopia"):
            self.assertNotIn(key, curves.COLOR_FILTERS)
        for key in ("night_vision_red", "night_vision_grey", "thermal", "grayscale", "amber", "ice"):
            self.assertIn(key, curves.COLOR_FILTERS)          # filters kept

    def test_removed_presets_and_filters_migrate(self):
        c = config.Config.from_json({
            "active_preset": "Thermal",
            "current": {"color_filter": "deuteranopia", "shadow_boost": 30},
            "custom_presets": {"Horror / Flashlight": {"shadow_boost": 70}, "Mine": {}},
            "hotkeys": {"preset:Mixed Light": "Ctrl+Alt+7", "toggle": "Ctrl+Alt+K"},
        })
        self.assertEqual(c.active_preset, "Dark Areas")
        self.assertEqual(c.current, config.Config().current)
        self.assertNotIn("Horror / Flashlight", c.custom_presets)
        self.assertIn("Mine", c.custom_presets)
        self.assertEqual(c.hotkeys, {"toggle": "Ctrl+Alt+K"})
        self.assertEqual(Settings.from_dict({"color_filter": "protanopia"}).color_filter, "none")

    def test_suggested_keys_cover_every_builtin_preset(self):
        from catvision.hotkeys import suggested_hotkeys
        keys = suggested_hotkeys()
        for p in BUILTIN_PRESETS:
            self.assertIn("preset:" + p.name, keys)


def _picture(level: int, pixels: int = 400) -> bytes:
    """A flat grey 32-bit BGRA picture."""
    return bytes([level, level, level, 255]) * pixels


class AutoLevelsTests(unittest.TestCase):
    def test_measure_dark_and_bright_pictures(self):
        from catvision.autolevels import measure
        dark, bright = measure(_picture(10)), measure(_picture(220))
        self.assertLess(dark.key, 0.06)
        self.assertGreater(bright.key, 0.8)
        self.assertEqual(dark.dark, 1.0)
        self.assertFalse(dark.black)
        self.assertTrue(measure(_picture(0)).black)

    def test_target_offset_raises_dark_and_lowers_bright(self):
        from catvision.autolevels import target_offset
        self.assertAlmostEqual(target_offset(0.12, 30, 0.12), 0.0)
        self.assertAlmostEqual(target_offset(0.01, 30, 0.12), 30.0)    # very dark: full
        self.assertAlmostEqual(target_offset(0.99, 30, 0.12), -30.0)   # very bright: full down
        self.assertGreater(target_offset(0.05, 30, 0.12), 0)
        self.assertLess(target_offset(0.3, 30, 0.12), 0)

    def test_controller_brightens_slowly_and_dims_fast(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        up = c.step(measure(_picture(3)), 30, 0.12, 0.2)
        self.assertGreater(up, 0)
        self.assertLess(up, 10)                 # no sudden jump when it gets dark
        for _ in range(40):
            c.step(measure(_picture(3)), 30, 0.12, 0.2)
        self.assertGreater(c.offset, 25)
        before = c.offset
        c.step(measure(_picture(250)), 30, 0.12, 0.2)
        self.assertLess(c.offset, before - 25)  # a flash pulls it back quickly
        goal = c.want
        c.step(measure(_picture(0)), 30, 0.12, 0.2)
        self.assertEqual(c.want, goal)          # black screen: keeps its goal

    def test_engine_adjusts_each_display_on_its_own(self):
        with tempfile.TemporaryDirectory() as d:
            g = FakeGamma()
            e = Engine(g, NullColorBackend(), Path(d))
            e.set_settings(Settings(shadow_boost=40))
            e.set_enabled(True)
            plain = g.get_ramp("\\\\.\\DISPLAY2")
            e.set_auto_offsets({"\\\\.\\DISPLAY1": {"shadow_boost": 30.0}})
            self.assertEqual(g.get_ramp("\\\\.\\DISPLAY2"), plain)      # untouched
            boosted = g.get_ramp("\\\\.\\DISPLAY1")
            self.assertGreater(boosted[0][20], plain[0][20])           # brighter darks
            self.assertEqual(e.settings.shadow_boost, 40)              # preset unchanged
            self.assertFalse(e.check_and_reapply())                    # not seen as drift
            e.set_auto_offsets({})
            self.assertEqual(g.get_ramp("\\\\.\\DISPLAY1"), plain)
            e.set_enabled(False)
            self.assertEqual(g.get_ramp("\\\\.\\DISPLAY1"), curves.identity_ramp())

    def test_watcher_measures_with_any_capture(self):
        import time
        from catvision.autolevels import Watcher
        w = Watcher(capture=lambda rect: _picture(30))
        w.watch({"a": (0, 0, 100, 100), "b": (100, 0, 200, 100)})
        deadline = time.time() + 3
        while w.latest()[0] == 0 and time.time() < deadline:
            time.sleep(0.02)
        seq, latest = w.latest()
        w.stop()
        self.assertGreater(seq, 0)
        self.assertEqual(set(latest), {"a", "b"})
        self.assertFalse(w.running)

    def test_auto_levels_options_default_off(self):
        self.assertFalse(config.Options().auto_levels)
        o = config.Options.from_dict({"auto_levels": True, "auto_strength": 45.0})
        self.assertTrue(o.auto_levels)
        self.assertEqual(o.auto_strength, 45.0)


def _split_picture(width: int, height: int, inside: int, outside: int) -> bytes:
    """Grey picture whose middle third is one level and the rest another."""
    out = bytearray()
    for y in range(height):
        for x in range(width):
            middle = width / 3 <= x < 2 * width / 3 and height / 3 <= y < 2 * height / 3
            v = inside if middle else outside
            out += bytes([v, v, v, 255])
    return bytes(out)


class AutoLevelsEnhancedTests(unittest.TestCase):
    def test_middle_of_the_screen_counts_more(self):
        from catvision.autolevels import measure
        dark_middle = _split_picture(96, 54, 10, 200)
        bright_middle = _split_picture(96, 54, 200, 10)
        # Compared with treating every pixel the same, what's in the middle
        # of the screen pulls the reading its way.
        self.assertLess(measure(dark_middle, 96).average, measure(dark_middle).average)
        self.assertGreater(measure(bright_middle, 96).average, measure(bright_middle).average)

    def test_dark_patches_count_even_in_bright_scenes(self):
        from catvision.autolevels import measure
        mixed = bytes([8, 8, 8, 255]) * 200 + bytes([230, 230, 230, 255]) * 200
        m = measure(mixed)
        self.assertLess(m.shadows, 0.05)
        self.assertLess(m.key, m.average)   # the dark half pulls the steering value down

    def test_dead_zone_stops_breathing(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        for _ in range(30):
            c.step(measure(_picture(20)), 30, 0.12, 0.2)
        goal = c.want
        for _ in range(10):                                  # a slightly different scene
            c.step(measure(_picture(21)), 30, 0.12, 0.2)
        self.assertEqual(c.want, goal)                       # tiny change: same goal
        for _ in range(10):
            c.step(measure(_picture(5)), 30, 0.12, 0.2)
        self.assertGreater(c.want, goal + 5)                 # real change: new goal

    def test_big_changes_catch_up_faster(self):
        from catvision.autolevels import Controller
        small, big = Controller(), Controller()
        small.follow(10.0, 30, 0.2)
        big.follow(30.0, 30, 0.2)
        self.assertGreater(big.offset / 30.0, small.offset / 10.0)

    def test_speed_setting(self):
        from catvision.autolevels import Controller
        slow, fast = Controller(), Controller()
        slow.follow(20.0, 30, 0.2, speed=0.5)
        fast.follow(20.0, 30, 0.2, speed=2.0)
        self.assertGreater(fast.offset, slow.offset)

    def test_brighten_only(self):
        from catvision.autolevels import target_offset
        self.assertEqual(target_offset(0.9, 30, 0.12, dim=False), 0.0)
        self.assertGreater(target_offset(0.02, 30, 0.12, dim=False), 0.0)

    def test_relax_goes_back_to_the_preset(self):
        from catvision.autolevels import Controller
        c = Controller(offset=25.0)
        for _ in range(30):
            c.relax(0.2)
        self.assertLess(abs(c.offset), 1.0)

    def test_full_screen_check_is_skipped_off_windows(self):
        import sys
        from catvision.autolevels import full_screen_displays
        if sys.platform != "win32":
            self.assertIsNone(full_screen_displays({"a": (0, 0, 10, 10)}))


class AutoLevelsRefinedTests(unittest.TestCase):
    def test_flickering_light_does_not_make_it_pump(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        dark, bright = measure(_picture(4)), measure(_picture(120))
        for i in range(60):                       # 12 s of a light flickering on and off
            c.step(dark if i % 2 else bright, 30, 0.12, 0.2)
        self.assertLess(c.offset, 3.0)            # never pumped up between flashes

    def test_brightens_once_it_stays_dark(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        for _ in range(25):
            c.step(measure(_picture(4)), 30, 0.12, 0.2)
        self.assertGreater(c.offset, 20)

    def test_flash_dims_within_a_fraction_of_a_second(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        for _ in range(40):
            c.step(measure(_picture(4)), 30, 0.12, 0.2)
        start = c.offset
        c.observe(measure(_picture(250)), 30, 0.12)
        for _ in range(15):                           # a quarter of a second at 60 a second
            c.advance(1 / 60)
        self.assertLess(c.offset, start - 40)
        self.assertLess(c.offset, 0)

    def test_lost_picture_eases_back_to_the_preset(self):
        from catvision.autolevels import LOST_S, Controller, measure
        c = Controller()
        for _ in range(40):
            c.step(measure(_picture(4)), 30, 0.12, 0.2)
        held = c.step(None, 30, 0.12, 0.2)
        self.assertGreater(held, 20)              # a moment's glitch: hold
        for _ in range(int(LOST_S / 0.2) + 30):
            c.step(None, 30, 0.12, 0.2)
        self.assertLess(abs(c.offset), 3.0)       # still gone: back to the preset

    def test_keep_this_look_keeps_the_current_adjustment(self):
        from catvision.autolevels import aim_for_current_look, target_offset
        for key, offset in ((0.02, 18.0), (0.3, -10.0), (0.12, 0.0)):
            aim = aim_for_current_look(key, offset, 30)
            self.assertAlmostEqual(target_offset(key, 30, aim), offset, places=6)


class AutoLevelsRoomAndGamesTests(unittest.TestCase):
    def test_room_left_goes_to_the_main_slider(self):
        from catvision.autolevels import split_offset
        self.assertEqual(split_offset("shadow_boost", 20, {"shadow_boost": 40, "exposure": 0}),
                         {"shadow_boost": 20})

    def test_extra_spills_into_exposure_near_the_top(self):
        from catvision.autolevels import split_offset
        out = split_offset("shadow_boost", 30, {"shadow_boost": 85, "exposure": 0})
        self.assertEqual(out["shadow_boost"], 15)
        self.assertAlmostEqual(out["exposure"], 7.5)     # Exposure works harder per point

    def test_dimming_below_zero_takes_from_the_other_slider(self):
        from catvision.autolevels import split_offset
        out = split_offset("shadow_boost", -30, {"shadow_boost": 10, "exposure": 6})
        self.assertEqual(out["shadow_boost"], -10)
        self.assertAlmostEqual(out["exposure"], -6)       # can't go below 0 either

    def test_engine_applies_several_settings_per_display(self):
        with tempfile.TemporaryDirectory() as d:
            g = FakeGamma()
            e = Engine(g, NullColorBackend(), Path(d))
            e.set_settings(Settings(shadow_boost=85))
            e.set_enabled(True)
            only_boost = g.get_ramp("\\\\.\\DISPLAY1")
            e.set_auto_offsets({"\\\\.\\DISPLAY1": {"shadow_boost": 15, "exposure": 8}})
            s = e.settings_for("\\\\.\\DISPLAY1")
            self.assertEqual((s.shadow_boost, s.exposure), (100, 8))
            self.assertNotEqual(g.get_ramp("\\\\.\\DISPLAY1"), only_boost)

    def test_game_looks_are_saved_and_cleaned(self):
        o = config.Options.from_dict({"auto_game_looks": {"ARC Raiders": 0.09, "bad": "x"}})
        self.assertEqual(o.auto_game_looks, {"ARC Raiders": 0.09})
        from catvision.autolevels import clean_title
        self.assertEqual(clean_title("  ARC   Raiders \n"), "ARC Raiders")

    def test_window_check_is_skipped_off_windows(self):
        import sys
        from catvision.autolevels import window_in_front
        if sys.platform != "win32":
            self.assertEqual(window_in_front({"a": (0, 0, 10, 10)}), (None, ""))


class AutoLevelsSmoothnessTests(unittest.TestCase):
    """Moves made 60 times a second must be too small to see as steps."""

    def _run(self, scenes, seconds_each=4.0):
        from catvision.autolevels import Controller, measure
        c = Controller()
        frames = []
        frame = 1 / 60
        for level in scenes:
            m = measure(_picture(level))
            for i in range(int(seconds_each * 60)):
                if i % 12 == 0:                       # a measurement five times a second
                    c.observe(m, 30, 0.12)
                before = c.offset
                c.advance(frame)
                frames.append((c.offset - before, c.rush))
        return frames

    def test_normal_fades_move_less_than_a_point_per_update(self):
        frames = self._run([6, 10, 5, 9, 4, 7])       # scenes changing, but no flashes
        self.assertFalse(any(rush for _, rush in frames))
        self.assertLess(max(abs(d) for d, _ in frames), 1.4)

    def test_even_a_flash_fades_instead_of_jumping(self):
        frames = self._run([4, 250, 4])
        self.assertLess(max(abs(d) for d, _ in frames), 4.1)

    def test_changes_ease_in(self):
        frames = self._run([4], seconds_each=0.5)
        peak = max(abs(d) for d, _ in frames)
        self.assertLess(abs(frames[0][0]), peak * 0.2)  # starts gently, no lurch
        self.assertGreater(abs(frames[20][0]), abs(frames[1][0]))

    def test_fader_moves_displays_on_its_own_thread(self):
        import time
        from catvision.autolevels import Controller, Fader
        ctrls = {"a": Controller(want=20.0, strength=30.0)}
        seen = []
        f = Fader(ctrls, lambda pos: seen.append(pos["a"]))
        f.start()
        deadline = time.time() + 3
        while (not seen or seen[-1] < 5) and time.time() < deadline:
            time.sleep(0.02)
        f.stop()
        self.assertFalse(f.running)
        self.assertGreater(seen[-1], 5)
        steps = [b - a for a, b in zip(seen, seen[1:])]
        self.assertLess(max(steps), 1.4)


class AutoLevelsStaleAndAimTests(unittest.TestCase):
    def test_frozen_picture_eases_back_to_the_preset(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        frames = [measure(_picture(4 + i % 2), 96) for i in range(2)]   # a live, changing picture
        for i in range(40):
            c.step(frames[i % 2], 30, 0.12, 0.2)
        self.assertGreater(c.offset, 20)
        stuck = measure(_picture(4), 96)                 # then the same picture over and over
        for _ in range(60):
            c.step(stuck, 30, 0.12, 0.2)
        self.assertTrue(c.frozen)
        self.assertLess(abs(c.offset), 3)
        for i in range(30):                              # it starts moving again: back to work
            c.step(frames[i % 2], 30, 0.12, 0.2)
        self.assertFalse(c.frozen)
        self.assertGreater(c.offset, 15)

    def test_still_picture_without_tracking_is_not_frozen(self):
        from catvision.autolevels import Controller, measure
        c = Controller()
        for _ in range(60):
            c.step(measure(_picture(4)), 30, 0.12, 0.2)   # tests and fakes: no signature
        self.assertFalse(c.frozen)

    def test_aim_brighter_and_darker(self):
        from catvision.autolevels import TARGET_LIMITS, nudge_aim
        self.assertAlmostEqual(nudge_aim(0.12, True), 0.15)
        self.assertAlmostEqual(nudge_aim(0.15, False), 0.12)
        self.assertEqual(nudge_aim(TARGET_LIMITS[1], True), TARGET_LIMITS[1])
        self.assertEqual(nudge_aim(TARGET_LIMITS[0], False), TARGET_LIMITS[0])
