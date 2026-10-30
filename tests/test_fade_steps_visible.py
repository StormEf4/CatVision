"""Auto levels (beta): no fade step is a visible jump.

Each fade step moves the slider at most CEILING / FADER_MAX_HZ points.  How
much the picture changes for that depends on the look; these tests measure
it as CIE L* lightness on screen (display gamma 2.2), the largest change of
any of the 256 levels on any channel.
"""
import math
import unittest

from catvision import autolevels, curves, presets
from catvision.autolevels import EXPOSURE_FLOOR, split_offset
from catvision.curves import Settings


def lightness(v: int) -> float:
    y = (v / curves.RAMP_MAX) ** 2.2
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


def applied(look: Settings, key: str, offset: float, guard: bool = True) -> Settings:
    """The look a display gets for this fader position, the way
    gui._fader_apply and engine.settings_for work it out."""
    names = ("shadow_boost", "exposure", "anti_glare") if guard else ("shadow_boost", "exposure")
    base = {k: getattr(look, k) for k in names}
    amounts = {k: round(v, 2) for k, v in split_offset(key, offset, base).items()}
    return look.clamped({k: getattr(look, k) + v for k, v in amounts.items() if v})


def biggest_step(look: Settings, key: str, start: float, end: float, guard: bool = True) -> float:
    """Largest L* change of one fade step at full speed from start to end."""
    step = autolevels.CEILING / autolevels.FADER_MAX_HZ
    tint = curves.plan_tint(look)
    n = max(1, math.ceil(abs(end - start) / step))
    worst, before = 0.0, None
    for i in range(n + 1):
        ramp = curves.build_ramp(applied(look, key, start + (end - start) * i / n, guard), tint)
        now = [[lightness(v) for v in ch] for ch in ramp]
        if before is not None:
            worst = max(worst, max(abs(a - b) for x, y in zip(now, before) for a, b in zip(x, y)))
        before = now
    return worst


class ExposureNeverCrossesZeroTest(unittest.TestCase):
    def test_night_vision_dims_without_a_jump(self):
        """Night vision with How much 60 in a bright scene: Exposure 40 -> 0
        would jump about 10 L* in its last step (Anti-glare switching off)."""
        look = presets.builtin_by_name("Night Vision").settings
        self.assertGreater(biggest_step(look, "exposure", -30, -60, guard=False), 8)
        self.assertLess(biggest_step(look, "exposure", -30, -60), 1.5)
        lowest = applied(look, "exposure", -60)
        self.assertGreater(lowest.exposure, 0)
        self.assertLessEqual(lowest.exposure, EXPOSURE_FLOOR + 0.005)

    def test_gentle_boost_spill_stops_short_of_zero(self):
        look = presets.builtin_by_name("Gentle Boost").settings   # SB 40, Exposure 6
        self.assertGreater(biggest_step(look, "shadow_boost", -40, -60, guard=False), 3)
        self.assertLess(biggest_step(look, "shadow_boost", -40, -60), 1.5)
        self.assertGreater(applied(look, "shadow_boost", -60).exposure, 0)

    def test_no_spill_into_exposure_from_zero_with_anti_glare(self):
        look = Settings(shadow_boost=85, anti_glare=60)
        self.assertGreater(biggest_step(look, "shadow_boost", 0, 30, guard=False), 5)
        self.assertLess(biggest_step(look, "shadow_boost", 0, 30), 1.5)
        self.assertEqual(split_offset("shadow_boost", 30, {"shadow_boost": 85, "exposure": 0,
                                                           "anti_glare": 60}),
                         {"shadow_boost": 15})

    def test_without_anti_glare_nothing_changes(self):
        """Exposure 0 is no jump without Anti-glare: the full range stays."""
        for base in ({"shadow_boost": 85, "exposure": 0}, {"shadow_boost": 85, "exposure": 0,
                                                           "anti_glare": 0.0}):
            self.assertEqual(split_offset("shadow_boost", 30, base),
                             {"shadow_boost": 15, "exposure": 7.5})
        self.assertEqual(split_offset("exposure", -60, {"shadow_boost": 0, "exposure": 40}),
                         {"exposure": -40})
        look = presets.builtin_by_name("Pitch Black").settings   # no Anti-glare
        self.assertLess(biggest_step(look, "shadow_boost", 0, 30), 1.5)

    def test_floor_details(self):
        base = {"shadow_boost": 10, "exposure": 40, "anti_glare": 100}
        out = split_offset("exposure", -60, base)
        self.assertAlmostEqual(out["exposure"], EXPOSURE_FLOOR - 40)
        self.assertEqual(out["shadow_boost"], -10)   # the rest still goes to Shadow boost
        # A preset Exposure already below the floor stays where it is.
        self.assertEqual(split_offset("exposure", -5, {"shadow_boost": 0, "exposure": 0.004,
                                                       "anti_glare": 100}), {"exposure": 0.0})
        # Brightening is untouched.
        self.assertEqual(split_offset("exposure", 20, base), {"exposure": 20})
        # Rounded to 2 decimals (as the fader passes it on) Exposure stays above 0.
        for start in (0.006, 0.0151, 6.123, 6.127, 40.004, 40.006, 99.995):
            amount = split_offset("exposure", -200, {"shadow_boost": 0, "exposure": start,
                                                     "anti_glare": 1})["exposure"]
            self.assertGreater(start + round(amount, 2), 0)


class FadeStepsTest(unittest.TestCase):
    def test_every_builtin_preset_fades_in_small_steps(self):
        """Over each preset's whole auto levels range (How much 60), no step
        at full speed changes any level by more than this many L*.  The
        largest are where Exposure passes 16 with Anti-glare (its knee
        starts moving): about 2.9 L* at 120 steps a second."""
        for p in presets.BUILTIN_PRESETS:
            s = p.settings
            key = "exposure" if s.exposure > s.shadow_boost else "shadow_boost"
            with self.subTest(preset=p.name):
                self.assertLess(biggest_step(s, key, -60, 60), 3.5)


class EvenTintPowersTest(unittest.TestCase):
    """build_ramp's even-tint shortcut under a tint power (night vision's
    green) fails exactly where the plain arithmetic does."""

    def test_bad_powers_fail_the_same_way(self):
        from tests.test_ramp_speed import reference_build_ramp
        s = Settings()   # tone value 0.0 at the bottom: 0.0 ** -1 fails
        for power in (-1.0, math.nan, math.inf, 1e300, -0.0, 0, 64.0, 64.5, True):
            tint = ((0.5, 1.0, 0.5), (0.5, 1.0, 0.5), power)
            try:
                want = reference_build_ramp(s, tint)
            except Exception as exc:
                with self.assertRaises(type(exc)):
                    curves.build_ramp(s, tint)
            else:
                self.assertEqual(curves.build_ramp(s, tint), want)

    def test_int_white_cap_rounds_the_same(self):
        self.assertEqual(curves._ramp_channel([-1, 0, 0.5, 1, 2], 1),
                         [0, 0, 32768, 65535, 65535])
        self.assertEqual(curves._ramp_channel([0.25, 0.5], 1.0), [16384, 32768])


if __name__ == "__main__":
    unittest.main()
