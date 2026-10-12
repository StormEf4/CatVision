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

    def test_colorblind_filters_keep_grey_grey(self):
        for name in ("protanopia", "deuteranopia", "tritanopia"):
            r, g, b = curves.apply_matrix(curves.filter_matrix(name), (0.5, 0.5, 0.5))
            self.assertAlmostEqual(r, 0.5, delta=0.02, msg=name)
            self.assertAlmostEqual(g, 0.5, delta=0.02, msg=name)
            self.assertAlmostEqual(b, 0.5, delta=0.02, msg=name)

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
                    "grayscale", "deuteranopia", "protanopia", "tritanopia"):
                self.assertTrue(status.color_skipped, p.name)
            e.shutdown()

    def test_mixing_filter_with_chosen_displays(self):
        g = FakeGamma()
        color = FakeColor()
        e = Engine(g, color, self.dir)
        e.displays = ["\\\\.\\DISPLAY1"]
        e.settings = Settings(color_filter="deuteranopia")
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
        d = hk.default_hotkeys()
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
    def test_v1_file_gets_default_hotkeys(self):
        from catvision.hotkeys import default_hotkeys
        c = config.Config.from_json({"version": 1, "current": {"shadow_boost": 40}})
        self.assertEqual(c.hotkeys, default_hotkeys())
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

    def test_blue_red_goes_blue_to_red(self):
        s = [p for p in BUILTIN_PRESETS if p.name == "Night Vision (Blue/Red)"][0].settings
        tint, _m = curves.plan_color(s)
        r, g, b = curves.build_ramp(s, tint)
        self.assertGreater(b[8], 2 * r[8])                 # dark = blue
        self.assertGreater(r[250], b[250])                 # bright = red
        for ch in (r, g, b):
            self.assertTrue(all(x <= y for x, y in zip(ch, ch[1:])), "ramps must not decrease")

    def test_every_preset_ramp_is_valid_with_its_tint(self):
        for p in BUILTIN_PRESETS:
            for all_displays in (True, False):
                tint, _m = curves.plan_color(p.settings, all_displays=all_displays)
                for ch in curves.build_ramp(p.settings, tint):
                    self.assertTrue(all(0 <= v <= 65535 for v in ch), p.name)
                    self.assertTrue(all(x <= y for x, y in zip(ch, ch[1:])), p.name)
