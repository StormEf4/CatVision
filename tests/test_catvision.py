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

    def test_single_monitor_choice(self):
        g = FakeGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.monitor_choice = "\\\\.\\DISPLAY2"
        e.settings = Settings(shadow_boost=50)
        e.set_enabled(True)
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))
        e.set_monitor_choice("\\\\.\\DISPLAY1")
        self.assertFalse(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY1"]))
        self.assertTrue(curves.is_identity_ramp(g.ramps["\\\\.\\DISPLAY2"]))

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
            self.assertEqual(config.load(path).current, Settings())
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
