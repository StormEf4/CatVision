"""The fast brightness-table code gives exactly what the plain version did.

build_ramp, limit_ramp_deviation, ramp_deviation and the Windows buffer copy
were sped up for auto levels (up to 120 updates a second per display).  The
reference versions below are frozen copies of the 1.15.0 code; every random
look must come out bit-for-bit the same.
"""
import ctypes
import math
import operator
import os
import random
import unittest
from dataclasses import fields, replace

from catvision import backends, curves, presets
from catvision.curves import Gradient, Settings

SEED = 20261030
# Random looks per run (CATVISION_RAMP_FUZZ=50000 for a long soak).
FUZZ_LOOKS = int(os.environ.get("CATVISION_RAMP_FUZZ", "2000"))


# -- frozen 1.15.0 reference versions --------------------------------------------

def reference_build_ramp(s, tint=None):
    s = s.clamped()
    gradient = tint if isinstance(tint, Gradient) else None
    lo, hi, power = curves.NO_TINT if gradient else (tint or curves.NO_TINT)
    tone = [curves.tone_curve(i / (curves.RAMP_SIZE - 1), s) for i in range(curves.RAMP_SIZE)]
    colors = [gradient.color(y) for y in tone] if gradient else None
    ramp = []
    for c, gain in enumerate((s.red, s.green, s.blue)):
        channel = []
        prev = 0
        for i, y in enumerate(tone):
            if colors:
                out = colors[i][c] * gain
            else:
                out = y * gain * (lo[c] + (hi[c] - lo[c]) * y ** power)
            v = int(round(curves._clamp01(out) * s.white_cap * curves.RAMP_MAX))
            v = max(prev, v)
            channel.append(v)
            prev = v
        ramp.append(channel)
    return ramp


def reference_tone_curve_1_16(x, s):
    """tone_curve as it was before Outside detail (highlight_room)."""
    y = curves.exposure_curve(curves._clamp01(x), s.exposure, s.anti_glare)
    if s.gamma != 1.0:
        y = y ** (1.0 / s.gamma)
    a = curves.shadow_boost_factor(s.shadow_boost)
    if a > 0:
        y = math.log1p(a * y) / math.log1p(a)
    y *= s.brightness
    y = (y - 0.5) * s.contrast + 0.5
    y = curves._clamp01(y)
    y = s.black_floor + (1.0 - s.black_floor) * y
    return y


def reference_limit_ramp_deviation(ramp, max_steps=127):
    out = []
    for channel in ramp:
        new = []
        prev = 0
        for i, v in enumerate(channel):
            lo = max(0, i - max_steps) * 257
            hi = min(255, i + max_steps) * 257
            v = min(hi, max(lo, v))
            v = max(prev, v)
            new.append(v)
            prev = v
        out.append(new)
    return out


def reference_ramp_deviation(ramp):
    dev = 0
    for ch in ramp:
        if ch:
            levels = [v >> 8 for v in ch]
            index = range(len(levels))
            dev = max(dev, max(map(operator.sub, levels, index)),
                      max(map(operator.sub, index, levels)))
    return dev


# -- random looks ------------------------------------------------------------------

def random_settings(rng: random.Random) -> Settings:
    """Any field anywhere in (and a bit past) its range, often on an edge."""
    data = {}
    for name, (lo, hi) in curves.LIMITS.items():
        pick = rng.random()
        if pick < 0.15:
            data[name] = rng.choice([lo, hi])
        elif pick < 0.25:
            data[name] = rng.choice([lo - 1.0, hi + 1.0, 1.0, 0.0])
        elif pick < 0.3:
            data[name] = getattr(Settings(), name)   # the 'no change' value
        else:
            data[name] = rng.uniform(lo, hi)
    if rng.random() < 0.5:   # the exact values the presets and fades use
        data["shadow_boost"] = round(rng.uniform(0, 100), rng.choice([0, 1, 2]))
    data["color_filter"] = rng.choice(list(curves.COLOR_FILTERS))
    if rng.random() < 0.3:   # an even grey: gains equal, but not 1
        data["green"] = data["blue"] = data["red"]
    if rng.random() < 0.3:   # Outside detail off (most saved looks), exactly
        data["highlight_room"] = rng.choice([0.0, -0.0, 0])
    return Settings(**data)


def random_gradient(rng: random.Random) -> Gradient:
    n = rng.randint(1, 7)
    positions = [rng.uniform(-0.1, 1.1) for _ in range(n)]
    if rng.random() < 0.8:
        positions.sort()
    if rng.random() < 0.2 and n > 1:
        positions[1] = positions[0]          # a zero-width step
    stops = tuple((p, tuple(rng.choice([rng.random(), 0.0, 1.0]) for _ in range(3)))
                  for p in positions)
    if rng.random() < 0.2:   # a grey gradient: every channel the same
        stops = tuple((p, (c[0], c[0], c[0])) for p, c in stops)
    return Gradient(stops, rng.choice([1.0, 0.0, rng.random(), rng.uniform(0, 1.5)]))


def random_tint_curve(rng: random.Random):
    lo = tuple(rng.choice([1.0, 0.0, -0.0, rng.uniform(0, 1.6)]) for _ in range(3))
    if rng.random() < 0.4:
        hi = lo                               # an even tint
    else:
        hi = tuple(rng.choice([1.0, 0.0, rng.uniform(0, 1.6)]) for _ in range(3))
    if rng.random() < 0.3:
        lo = hi = (lo[0],) * 3                # the same on every channel
    power = rng.choice([1.0, 1, 2.0, 2.5, 0.5, rng.uniform(0.05, 6.0)])
    return (lo, hi, power)


def all_planned_tints(rng: random.Random) -> list:
    """What plan_color really hands build_ramp, for every filter (each once)."""
    out = {"None": None}
    custom_tints = {"custom:a": (0.3, 1.0, 0.4), "custom:b": (1.0, 1.0, 1.0),
                    "custom:c": (0.8, 0.8, 0.8)}
    looks = [p.settings for p in presets.BUILTIN_PRESETS]
    for key in list(curves.COLOR_FILTERS) + list(custom_tints):
        for base in looks[:4]:
            for strength in (1.0, 0.0, 0.5, rng.random()):
                s = Settings(**{**base.to_dict(), "color_filter": key,
                                "filter_strength": strength})
                for every in (True, False):
                    tint, _m = curves.plan_color(s, {}, custom_tints, all_displays=every)
                    out[repr(tint)] = tint
                    if tint != curves.plan_tint(s, custom_tints):
                        raise AssertionError(f"plan_tint differs for {key}")
    return list(out.values())


class BuildRampExactTest(unittest.TestCase):
    def assert_same(self, s, tint):
        want = reference_build_ramp(s, tint)
        got = curves.build_ramp(s, tint)
        if got != want:
            self.fail(f"build_ramp differs for {s} with tint {tint}")
        # separate lists: a caller may change one channel safely
        self.assertEqual(len({id(ch) for ch in got}), 3)
        self.assertTrue(all(type(v) is int for ch in got for v in ch))

    def test_presets_and_planned_tints(self):
        rng = random.Random(SEED)
        tints = all_planned_tints(rng)
        looks = [p.settings for p in presets.BUILTIN_PRESETS] + [Settings()]
        for s in looks:
            for tint in tints:
                self.assert_same(s, tint)

    def test_thousands_of_random_looks(self):
        rng = random.Random(SEED + 1)
        planned = all_planned_tints(rng)
        for _ in range(FUZZ_LOOKS):
            s = random_settings(rng)
            pick = rng.random()
            if pick < 0.3:
                tint = None
            elif pick < 0.55:
                tint = rng.choice(planned)
            elif pick < 0.75:
                tint = random_gradient(rng)
            else:
                tint = random_tint_curve(rng)
            self.assert_same(s, tint)

    def test_fade_steps(self):
        """The looks auto levels really asks for: every fade step on a preset."""
        for p in presets.BUILTIN_PRESETS:
            tint, _m = curves.plan_color(p.settings)
            for step in range(0, 400, 7):
                offset = step / 4.0 - 30.0
                s = Settings(**{**p.settings.to_dict(),
                                "shadow_boost": p.settings.shadow_boost + offset,
                                "exposure": p.settings.exposure + offset / 3})
                self.assert_same(s, tint)

    def test_shadow_boost_fades_reuse_the_exposure_stages(self):
        """A Shadow boost fade on two displays at once (exposure stages
        reused from the cache, interleaved) still matches the plain build."""
        curves._EXPOSURE_TONES.clear()
        tint = curves.TINT_CURVES["night_vision"]
        for step in range(80):
            for exposure, look_tint in ((6.0, None), (40.0, tint)):
                s = Settings(shadow_boost=20 + step * 0.37, exposure=exposure,
                             anti_glare=100, gamma=1.1)
                self.assert_same(s, look_tint)
        self.assertEqual(len(curves._EXPOSURE_TONES), 2)

    def test_exposure_stage_cache(self):
        curves._EXPOSURE_TONES.clear()
        fresh = curves._exposure_tones(5.0, 0.0, 1.1)
        self.assertIs(type(fresh), tuple)
        self.assertIs(curves._exposure_tones(5.0, 0.0, 1.1), fresh)
        # Values that compare equal share an entry: their tables are the same.
        for exposure, anti_glare in ((5, 0), (5.0, -0.0)):
            curves._EXPOSURE_TONES.clear()
            alone = curves._exposure_tones(exposure, anti_glare, 1.1)
            self.assertEqual([x.hex() for x in alone], [x.hex() for x in fresh])
        for exposure in (0.0, -0.0, 0):
            curves._EXPOSURE_TONES.clear()
            self.assertEqual(curves._exposure_tones(exposure, 50.0, 1.0), curves._RAMP_XS)
        # Night vision fades move Exposure: the cache stays small.
        for i in range(200):
            curves._exposure_tones(40 + i * 0.1, 100.0, 1.3)
            self.assertLessEqual(len(curves._EXPOSURE_TONES), curves._EXPOSURE_TONES_MAX)

    def test_bad_tints_fail_the_same_way(self):
        s = Settings(shadow_boost=50, black_floor=0.1)
        for tint in [((math.inf,) * 3, (math.inf,) * 3, 1.0),
                     ((math.nan,) * 3, (math.nan,) * 3, 1.0),
                     ((1.0, 1.0, 1.0), (0.5, 0.5, 0.5), math.nan),
                     ((1.0,), (1.0,), 1.0)]:
            try:
                want = reference_build_ramp(s, tint)
            except Exception as exc:
                with self.assertRaises(type(exc)):
                    curves.build_ramp(s, tint)
            else:
                self.assertEqual(curves.build_ramp(s, tint), want)

    def test_tone_table_matches_tone_curve(self):
        rng = random.Random(SEED + 2)
        for _ in range(300):
            s = random_settings(rng).clamped()
            want = [curves.tone_curve(i / 255, s) for i in range(256)]
            got = curves._tone_table(s)
            self.assertEqual([x.hex() for x in got], [x.hex() for x in want])

    def test_outside_detail_both_ways_match_tone_curve(self):
        """A new Outside detail slope is worked out inline, a repeated one
        from its table: both give tone_curve's values exactly, up to the
        strongest slope (where the shortcut's bound is reached)."""
        rng = random.Random(SEED + 6)
        for _ in range(150):
            room = rng.choice([100.0, rng.uniform(0.01, 100.0)])
            s = replace(random_settings(rng), highlight_room=room).clamped()
            want = [x.hex() for x in (curves.tone_curve(i / 255, s) for i in range(256))]
            curves._CEILINGS.pop(curves.highlight_slope(room), None)
            curves._CEILINGS_SEEN.discard(curves.highlight_slope(room))
            for _ in range(3):      # inline, then the table is built, then reused
                self.assertEqual([x.hex() for x in curves._tone_table(s)], want)
            self.assertIn(curves.highlight_slope(room), curves._CEILINGS)

    def test_fading_outside_detail_builds_no_tables(self):
        """While the Outside detail itself fades, every step is a new slope:
        no per-slope table is built for it (it would be used once)."""
        curves._CEILINGS.clear()
        curves._CEILINGS_SEEN.clear()
        s = curves.Settings(shadow_boost=80, contrast=1.3).clamped()
        for i in range(40):
            curves._tone_table(replace(s, highlight_room=40.0 + i * 0.25))
        self.assertEqual(curves._CEILINGS, {})

    def test_outside_detail_off_is_the_old_curve(self):
        """highlight_room 0 (every look saved before it existed) gives
        exactly the old curve and tables."""
        rng = random.Random(SEED + 5)
        for _ in range(400):
            s = replace(random_settings(rng), highlight_room=0.0).clamped()
            want = [reference_tone_curve_1_16(i / 255, s) for i in range(256)]
            self.assertEqual([x.hex() for x in curves._tone_table(s)],
                             [x.hex() for x in want])
            old = curves.tone_curve
            try:
                curves.tone_curve = reference_tone_curve_1_16
                reference = reference_build_ramp(s)
            finally:
                curves.tone_curve = old
            self.assertEqual(curves.build_ramp(s), reference)

    def test_clamped_unchanged(self):
        rng = random.Random(SEED + 3)
        for _ in range(500):
            s = random_settings(rng)
            if rng.random() < 0.2:
                s.color_filter = rng.choice(["custom:x", "bogus", 5, None])
            c = s.clamped()
            self.assertEqual(c, Settings(**{f.name: getattr(s, f.name) for f in fields(s)})
                             .clamped())
            for name in curves.LIMITS:
                lo, hi = curves.LIMITS[name]
                self.assertEqual(getattr(c, name), min(hi, max(lo, float(getattr(s, name)))))
            self.assertIs(type(c.shadow_boost), float)

    def test_clamped_with_changes_is_replace_then_clamped(self):
        """settings_for's one-pass version of dataclasses.replace(...).clamped()."""
        rng = random.Random(SEED + 7)
        for _ in range(500):
            s = random_settings(rng)
            changes = {k: getattr(s, k) + rng.uniform(-150, 150)
                       for k in rng.sample(["shadow_boost", "exposure"], rng.randint(0, 2))}
            self.assertEqual(s.clamped(changes), replace(s, **changes).clamped())
            self.assertEqual(s.clamped(None), s.clamped())
        with self.assertRaises(TypeError):
            Settings().clamped({"bogus": 1.0})

    def test_settings_for_unchanged(self):
        from catvision.backends import NullColorBackend, NullGammaBackend
        from catvision.engine import Engine
        engine = Engine(NullGammaBackend(), NullColorBackend())
        engine.set_settings(Settings(shadow_boost=90, exposure=40, gamma=1.3))
        did = engine.monitors()[0].id
        for offsets in ({}, {"shadow_boost": 0.0}, {"shadow_boost": 25.5},
                        {"shadow_boost": -12.25, "exposure": 7.5}, {"exposure": -60.0}):
            engine.auto_offsets = {did: offsets}
            amounts = {k: v for k, v in offsets.items() if v}
            want = replace(engine.settings, **{k: getattr(engine.settings, k) + v
                                               for k, v in amounts.items()}).clamped()
            self.assertEqual(engine.settings_for(did), want)


class RampHelpersExactTest(unittest.TestCase):
    def random_ramps(self, rng, n=600):
        looks = [curves.build_ramp(random_settings(rng), rng.choice(
            [None, curves.TINT_CURVES["night_vision"], curves.GRADIENTS["thermal"]]))
            for _ in range(n // 2)]
        noisy = [[[rng.randint(0, 65535) for _ in range(256)] for _ in range(3)]
                 for _ in range(n // 4)]
        odd = [[[rng.randint(0, 65535) for _ in range(rng.randint(0, 300))]
                for _ in range(3)] for _ in range(n // 4)]
        return looks + noisy + odd + [curves.identity_ramp()]

    def test_limit_ramp_deviation(self):
        rng = random.Random(SEED + 4)
        for ramp in self.random_ramps(rng, 200):
            for steps in (127, 96, rng.randint(1, 140), rng.choice([0, 300])):
                got = curves.limit_ramp_deviation(ramp, steps)
                self.assertEqual(got, reference_limit_ramp_deviation(ramp, steps))
                self.assertEqual(len({id(ch) for ch in got}), len(got))
        self.assertEqual(curves.limit_ramp_deviation(curves.identity_ramp(), 96.0),
                         reference_limit_ramp_deviation(curves.identity_ramp(), 96.0))
        self.assertIs(type(curves.limit_ramp_deviation([[0, 70000]], 96.0)[0][1]), float)

    def test_ramp_deviation(self):
        rng = random.Random(SEED + 5)
        wild = [[[rng.randint(-140000, 140000) for _ in range(rng.choice([1, 255, 256, 257, 520]))]
                 for _ in range(3)] for _ in range(100)]
        for ramp in self.random_ramps(rng) + wild + [[[], [7], []], [[0] * 256] * 3]:
            self.assertEqual(backends.ramp_deviation(ramp), reference_ramp_deviation(ramp))

    def test_windows_buffer_copy(self):
        """The slice copy stores what the old entry-by-entry copy stored."""
        row = ctypes.c_ushort * 256
        buffer_type = row * 3
        rng = random.Random(SEED + 6)
        for ramp in self.random_ramps(rng, 80)[:60] + [
                [[70000, -1, 65536] + [5] * 253] * 3, [list(range(300))] * 3]:
            if any(len(ch) < 256 for ch in ramp):
                continue
            old = buffer_type()
            for c in range(3):
                for i in range(256):
                    old[c][i] = ramp[c][i]
            new = buffer_type()
            backends.fill_ramp_buffer(new, ramp)
            self.assertEqual(bytes(new), bytes(old))


if __name__ == "__main__":
    unittest.main()
