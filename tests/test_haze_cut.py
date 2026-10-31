"""Auto levels (beta): Cut through haze.  Synthetic pictures only."""
import math
import random
import unittest
from dataclasses import replace

from catvision import autolevels, curves
from catvision.autolevels import (HAZE_DEADBAND, Controller, Fader, fader_amounts,
                                  haze_for_view, look_values, measure)
from catvision.curves import Settings, build_ramp, tone_curve

W, H = 96, 54


def _picture(levels) -> bytes:
    """A 96x54 BGRA picture from a list of 0-1 grey levels."""
    out = bytearray()
    for v in levels:
        c = max(0, min(255, int(round(v * 255))))
        out += bytes((c, c, c, 255))
    return bytes(out)


def _scene(rng):
    """A clear daylight view: sky on top, textured ground with deep shadows."""
    return [0.75 - 0.1 * y / H if y < H * 0.35
            else 0.03 + 0.6 * (0.5 + 0.5 * math.sin(x * 0.35 + y * 0.2)) * rng.random()
            for y in range(H) for x in range(W)]


def _veil(levels, color, amount):
    return [v * (1 - amount) + color * amount for v in levels]


def _night(rng):
    out = [0.01 + 0.1 * rng.random() for _ in range(W * H)]
    for _ in range(6):
        out[rng.randrange(W * H)] = 0.95
    return out


def _view(levels):
    return measure(_picture(levels), W)


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.rng = random.Random(3)

    def test_fog_smoke_and_dust_are_hazy(self):
        rng = self.rng
        for color, amount in ((0.7, 0.6), (0.45, 0.55), (0.55, 0.5)):
            m = _view(_veil(_scene(rng), color, amount))
            self.assertGreater(m.lows, 0.12)
            self.assertLess(m.spread, 0.4)
            self.assertGreater(haze_for_view(m), 30, (color, amount))

    def test_clear_and_flat_views_are_left_alone(self):
        rng = self.rng
        views = {
            "clear day": _scene(rng),
            "dark night": _night(rng),
            "foggy night": _veil(_night(rng), 0.10, 0.5),
            "white menu": [0.92 if rng.random() > 0.03 else 0.1 for _ in range(W * H)],
            "open sky": [0.62 + 0.18 * y / H for y in range(H) for _ in range(W)],
            "black": [0.0] * (W * H),
            "grey loading screen": [0.3] * (W * H),
            "dark room": [0.02 + 0.3 * rng.random() for _ in range(W * H // 2)]
                         + [0.02] * (W * H // 2),
        }
        for name, levels in views.items():
            self.assertEqual(haze_for_view(_view(levels)), 0.0, name)

    def test_more_veil_more_cut_up_to_the_limit(self):
        base = _scene(self.rng)
        cuts = [haze_for_view(_view(_veil(base, 0.6, a))) for a in (0.0, 0.3, 0.45, 0.6)]
        self.assertEqual(cuts[0], 0.0)
        self.assertEqual(cuts, sorted(cuts))
        self.assertTrue(all(0.0 <= c <= 100.0 for c in cuts))

    def test_measure_statistics(self):
        m = _view([0.4] * (W * H // 2) + [0.6] * (W * H // 2))
        self.assertAlmostEqual(m.lows, round(0.4 * 255) / 255)
        self.assertAlmostEqual(m.spread, (round(0.6 * 255) - round(0.4 * 255)) / 255)
        self.assertEqual(measure(b"").spread, 0.0)


def _hud(levels, dark=0.06):
    """A game's HUD drawn on top: dark panels with bright marks in the bottom
    corners and a compass strip at the top."""
    out = list(levels)
    for y in range(H - 8, H):
        for x in list(range(2, 34)) + list(range(66, 94)):
            out[y * W + x] = 0.9 if (x + y) % 4 == 0 else dark
    for y in range(1, 5):
        for x in range(30, 66):
            out[y * W + x] = 0.85 if (x * 3 + y) % 5 == 0 else dark
    return out


def _hazy_game_view(rng, sun=True, figure=True):
    """Haze over textured ground that thickens with distance (higher up),
    seen past the player's own dark figure, with the sun in a corner and a
    HUD: the whole picture's levels look clear."""
    out = []
    for y in range(H):
        for x in range(W):
            v = 0.03 + 0.6 * (0.5 + 0.5 * math.sin(x * 0.35 + y * 0.2)) * rng.random()
            a = 0.85 - 0.45 * y / H
            v = v * (1 - a) + 0.72 * a
            if figure and 36 <= x < 48 and 16 <= y:
                v = 0.03 + 0.04 * rng.random()
            if sun and (x - 80) ** 2 + (y - 8) ** 2 < 30:
                v = 1.0
            out.append(v)
    return _hud(out)


class PartsTest(unittest.TestCase):
    """The veil measured part by part (Measurement.veiled)."""

    def setUp(self):
        self.rng = random.Random(11)

    def test_haze_past_a_dark_figure_hud_and_sun(self):
        m = _view(_hazy_game_view(self.rng))
        # The whole picture's levels alone say "clear"...
        self.assertTrue(m.lows < autolevels.HAZE_LOW[0] or m.spread > autolevels.HAZE_SPREAD[0])
        # ...the parts don't.
        self.assertEqual(m.veiled, 1.0)
        cut = haze_for_view(m)
        self.assertGreater(cut, 5.0)
        # Never more than HAZE_SHARE of the darkest few percent: the figure
        # and the HUD aren't cut to black.
        self.assertLessEqual(curves.haze_floor(cut),
                             autolevels.HAZE_SHARE * m.lows + 1e-9)

    def test_lookalikes_are_left_alone(self):
        rng = self.rng
        views = {
            "clear day with HUD": _hud(_scene(rng)),
            "clear day, figure and sun": _hud([v if not 36 <= i % W < 48 else 0.04
                                              for i, v in enumerate(_scene(rng))]),
            "overcast sky with HUD": _hud([0.70 + 0.1 * y / H + rng.uniform(-0.02, 0.02)
                                           for y in range(H) for _ in range(W)]),
            "blue sky with HUD": _hud([0.66 + 0.12 * y / H for y in range(H) for _ in range(W)]),
            "snow field with HUD": _hud([0.85 + rng.uniform(-0.04, 0.04) for _ in range(W * H)]),
            "plain wall with HUD": _hud([0.42 + rng.uniform(-0.05, 0.05) for _ in range(W * H)]),
            "rough wall with HUD": _hud([0.40 + rng.uniform(-0.1, 0.1) for _ in range(W * H)]),
            "grey loading screen, logo, bar": [
                0.0 if y > H - 6 else 0.95 if 20 < y < 30 and 30 < x < 66 and (x + y) % 3
                else 0.4 for y in range(H) for x in range(W)],
            "menu panels": [0.15 if x < 30 else 0.5 if y % 9 else 0.9
                            for y in range(H) for x in range(W)],
            "dark room with HUD": _hud([0.02 + 0.25 * rng.random() for _ in range(W * H)]),
            "foggy night with HUD": _hud(_veil(_night(rng), 0.12, 0.6), dark=0.02),
            "hazy view at night": _hud([v * 0.12 for v in _hazy_game_view(rng)], dark=0.01),
        }
        for name, levels in views.items():
            self.assertEqual(haze_for_view(_view(levels)), 0.0, name)

    def test_needs_most_of_the_middle(self):
        rng = self.rng
        hazy = _hazy_game_view(rng, sun=False, figure=False)
        clear = _hud(_scene(rng))
        # The haze over only the right three of the eight columns: not enough.
        mixed = [h if i % W >= 60 else c for i, (h, c) in enumerate(zip(hazy, clear))]
        self.assertEqual(_view(mixed).veiled, 0.0)
        self.assertEqual(haze_for_view(_view(mixed)), 0.0)
        # Without the picture's width there is no grid, so no parts.
        self.assertEqual(measure(_picture(hazy)).veiled, 0.0)


class CurveTest(unittest.TestCase):
    def test_off_is_bit_identical(self):
        rng = random.Random(5)
        for _ in range(200):
            s = Settings(shadow_boost=rng.uniform(0, 100), gamma=rng.uniform(0.5, 3),
                         contrast=rng.uniform(0.5, 2), black_floor=rng.choice([0, 0.1]),
                         highlight_room=rng.choice([0, rng.uniform(0, 100)])).clamped()
            self.assertEqual(build_ramp(replace(s, haze_cut=0.0)), build_ramp(s))
            self.assertEqual(build_ramp(replace(s, haze_cut=-0.0)), build_ramp(s))

    def test_table_matches_tone_curve(self):
        rng = random.Random(6)
        for _ in range(300):
            s = Settings(shadow_boost=rng.uniform(0, 100), exposure=rng.choice([0, 40]),
                         contrast=rng.uniform(0.5, 2), black_floor=rng.choice([0, 0.2]),
                         highlight_room=rng.choice([0, rng.uniform(0, 100)]),
                         haze_cut=rng.uniform(0, 100)).clamped()
            want = [tone_curve(i / 255, s).hex() for i in range(256)]
            self.assertEqual([y.hex() for y in curves._tone_table(s)], want)

    def test_monotonic_never_brighter_white_kept(self):
        for boost in (0, 40, 90):
            for room in (0, 60):
                plain = Settings(shadow_boost=boost, highlight_room=room)
                for cut in (1, 25, 50, 100):
                    s = replace(plain, haze_cut=cut)
                    for ch in build_ramp(s):
                        self.assertEqual(ch, sorted(ch))
                        self.assertEqual(ch[-1], 65535)       # white stays white
                    ys = [tone_curve(i / 255, s) for i in range(256)]
                    base = [tone_curve(i / 255, plain) for i in range(256)]
                    self.assertTrue(all(y <= b + 1e-12 for y, b in zip(ys, base)))
                    # Nothing but black reaches black (no crushed shadows)...
                    self.assertTrue(all(y > 0 for y, b in zip(ys, base) if b > 0))
                    # ...and nothing reaches white before the plain curve does
                    # (no clipped highlights).
                    self.assertTrue(all(y < 1 for y, b in zip(ys, base) if b < 1))

    def test_spreads_the_hazy_range(self):
        s = Settings(haze_cut=80)
        lo, hi = 0.35, 0.75
        self.assertGreater(tone_curve(hi, s) - tone_curve(lo, s), (hi - lo) * 1.2)


class ControllerTest(unittest.TestCase):
    def setUp(self):
        rng = random.Random(9)
        self.clear = _view(_scene(rng))
        self.foggy = _view(_veil(_scene(rng), 0.7, 0.6))

    def _run(self, c, m, seconds, every=0.2):
        for _ in range(round(seconds / every)):
            c.observe(m, 30.0, 0.12)
            for _ in range(round(every * 120)):
                c.advance(1 / 120)

    def test_eases_in_and_out(self):
        c = Controller()
        for _ in range(2):
            c.observe(self.foggy, 30.0, 0.12)
            c.advance(1 / 120)
        self.assertEqual(c.haze_want, 0.0)      # one or two pictures are not enough
        self._run(c, self.foggy, 0.6)
        want = haze_for_view(self.foggy)
        self.assertGreater(want, 30)
        self.assertEqual(c.haze_want, want)
        self.assertLess(c.haze, want)          # still on its way
        steps = []
        for _ in range(240):
            before = c.haze
            c.advance(1 / 120)
            steps.append(c.haze - before)
        self.assertLess(max(steps), 2.0)       # small steps
        self.assertGreater(c.haze, want * 0.9)
        self._run(c, self.clear, 0.6)
        self.assertEqual(c.haze_want, 0.0)
        self.assertGreater(c.haze, 0.0)        # eases out, no jump
        self._run(c, self.clear, 5.0)
        self.assertEqual(c.haze, 0.0)

    def test_one_flash_or_puff_moves_nothing(self):
        c = Controller()
        self._run(c, self.foggy, 2.0)
        want = c.haze_want
        c.observe(self.clear, 30.0, 0.12)      # one picture without the veil (a flash)
        self._run(c, self.foggy, 0.4)
        self.assertEqual(c.haze_want, want)
        c = Controller()
        self._run(c, self.clear, 1.0)
        c.observe(self.foggy, 30.0, 0.12)      # one smoky picture
        c.observe(self.clear, 30.0, 0.12)
        self.assertEqual(c.haze_want, 0.0)

    def test_quick_capture_pace_waits_as_long(self):
        # Ten pictures a second (twenty in bursts) when capturing is cheap:
        # a puff of smoke a few tenths of a second long still moves nothing,
        # as at five a second; haze that stays is still cut within a second
        # or so.
        for every, puff in ((0.1, 0.4), (0.05, 0.3)):
            c, t = Controller(), [1.0]

            def show(m, seconds):
                for _ in range(round(seconds / every)):
                    c.observe(replace(m, at=t[0]), 30.0, 0.12)
                    for _ in range(round(every * 120)):
                        c.advance(1 / 120)
                    t[0] += every
            show(self.clear, 1.0)
            show(self.foggy, puff)
            self.assertEqual(c.haze_want, 0.0, every)
            show(self.clear, 1.0)
            self.assertEqual(c.haze_want, 0.0, every)
            show(self.foggy, 1.2)
            self.assertGreater(c.haze_want, 30, every)

    def test_dead_band(self):
        c = Controller()
        self._run(c, self.foggy, 2.0)
        want = c.haze_want
        near = replace(self.foggy, lows=self.foggy.lows * 0.99)
        self.assertNotEqual(haze_for_view(near), want)
        self.assertLess(abs(haze_for_view(near) - want), HAZE_DEADBAND)
        self._run(c, near, 2.0)
        self.assertEqual(c.haze_want, want)

    def test_unticked_and_release(self):
        c = Controller()
        self._run(c, self.foggy, 2.0)
        self.assertGreater(c.haze, 10)
        c.set_haze(False)
        self.assertEqual(c.haze_want, 0.0)        # at once, not at the next picture
        self._run(c, self.foggy, 0.2)
        self.assertEqual(c.haze_want, 0.0)
        c.set_haze(True)
        c.observe(self.foggy, 30.0, 0.12)
        self.assertEqual(c.haze_want, 0.0)        # starts again from a clear view
        c = Controller()
        self._run(c, self.foggy, 2.0)
        c.release()
        self.assertEqual(c.haze_want, 0.0)
        self._run(c, self.clear, 5.0)
        self.assertEqual(c.haze, 0.0)

    def test_frozen_or_lost_picture_lets_go(self):
        c = Controller()
        self._run(c, self.foggy, 2.0)
        self.assertGreater(c.haze_want, 30)
        # The same picture, pixel for pixel, for seconds: a stale capture.
        self._run(c, self.foggy, autolevels.FROZEN_S + autolevels.LOST_S)
        self.assertEqual(c.haze_want, 0.0)
        c = Controller()
        self._run(c, self.foggy, 2.0)
        for _ in range(round(autolevels.LOST_S / 0.2) + 2):
            c.observe(None, 30.0, 0.12)
            for _ in range(24):
                c.advance(1 / 120)
        self.assertEqual(c.haze_want, 0.0)

    def test_amounts_give_way_to_the_lift(self):
        c = Controller()
        c.haze = c.haze_want = 60.0
        base = look_values(Settings())
        self.assertEqual(fader_amounts("shadow_boost", -5.0, base, c)["haze_cut"], 60.0)
        lifted = fader_amounts("shadow_boost", autolevels.HAZE_LIFT_OFF, base, c)
        self.assertNotIn("haze_cut", lifted)
        half = fader_amounts("shadow_boost", autolevels.HAZE_LIFT_OFF / 2, base, c)
        self.assertAlmostEqual(half["haze_cut"], 30.0)
        self.assertNotIn("haze_cut", fader_amounts("shadow_boost", 0.0, base, Controller()))

    def test_fader_passes_it_on_and_waits_for_it(self):
        c = Controller()
        applied = []
        fader = Fader({"d": c}, applied.append)
        fader.tick(1 / 120, force=True)
        for i in range(4):      # (five a second: see HAZE_GAP_S)
            c.observe(replace(self.foggy, at=1.0 + 0.2 * i), 30.0, 0.12)
        self.assertGreater(c.haze_want, 30)
        self.assertFalse(fader.settled())
        n = len(applied)
        for _ in range(600):
            fader.tick(1 / 120)
        self.assertEqual(c.haze, c.haze_want)
        self.assertGreater(len(applied), n)
        self.assertLess(len(applied) - n, 300)  # only every HAZE_STEP, not every step
        self.assertTrue(fader.settled())
        n = len(applied)
        for _ in range(120):
            fader.tick(1 / 120)
        self.assertEqual(len(applied), n)       # at rest: no more rewrites


if __name__ == "__main__":
    unittest.main()
