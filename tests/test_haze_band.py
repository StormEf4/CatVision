"""Cut through haze, the band (curves.haze_band, autolevels.band_for_view):
more contrast in the band of tones a light haze squeezes the view into,
the room taken from above it, nothing ever darker.  Synthetic pictures only."""
import math
import random
import unittest
from dataclasses import replace

from catvision import autolevels, curves
from catvision.autolevels import Controller, Fader, band_for_view, fader_amounts, measure
from catvision.curves import Settings, build_ramp

W, H = 96, 54


def bgra(lum, tint=(1.0, 1.0, 1.0)):
    out = bytearray()
    for v in lum:
        r, g, b = (max(0, min(255, int(round(v * k * 255)))) for k in tint)
        out += bytes((b, g, r, 255))
    return bytes(out)


def landscape(rng, sky=0.75, lo=0.04, hi=0.6):
    return [sky - 0.1 * y / H if y < H * 0.35 else
            lo + (hi - lo) * (0.5 + 0.5 * math.sin(x * 0.35 + y * 0.2)) * rng.random()
            for y in range(H) for x in range(W)]


def veil(px, color, amount):
    return [v * (1 - amount) + color * amount for v in px]


def hud(px):
    out = list(px)
    for x in range(4, 30):
        for y in range(H - 6, H - 2):
            out[y * W + x] = 0.9 if (x + y) % 3 else 0.08
    return out


def light_snow(rng):
    """A light veil: the middle squeezed into bright greys, the near ground
    (bottom rows) and the HUD still dark."""
    px = veil(landscape(rng, sky=0.8, lo=0.05, hi=0.7), 0.72, 0.55)
    for y in range(int(H * 0.8), H):
        for x in range(W):
            px[y * W + x] = 0.05 + 0.3 * rng.random()
    return hud(px)


def snow(colour, seed=3):
    """Light snow with the veil at ``colour``: the brighter the veil, the
    higher its band (each the same scene and noise)."""
    rng = random.Random(seed)
    px = veil(landscape(rng, sky=0.8, lo=0.05, hi=0.7), colour, 0.55)
    for y in range(int(H * 0.8), H):
        for x in range(W):
            px[y * W + x] = 0.05 + 0.3 * rng.random()
    return hud(px)


class BandCurveTest(unittest.TestCase):
    def test_off_is_bit_identical(self):
        rng = random.Random(5)
        for _ in range(200):
            s = Settings(shadow_boost=rng.uniform(0, 100), contrast=rng.uniform(0.5, 2),
                         highlight_room=rng.choice([0, rng.uniform(0, 100)]),
                         haze_cut=rng.choice([0, rng.uniform(0, 100)])).clamped()
            plain = build_ramp(s)
            for lo, hi in ((0.0, 0.0), (0.4, 0.6), (rng.random(), rng.random())):
                self.assertEqual(build_ramp(replace(s, haze_band=0.0, haze_band_lo=lo,
                                                    haze_band_hi=hi)), plain)
            # (and on, but with no band: hi at or below lo)
            self.assertEqual(build_ramp(replace(s, haze_band=60.0, haze_band_lo=0.6,
                                                haze_band_hi=0.6)), plain)

    def test_never_darker_monotonic_white_kept_and_fast_path_exact(self):
        rng = random.Random(11)
        for _ in range(300):
            lo = rng.uniform(0.0, 0.9)
            s = Settings(shadow_boost=rng.uniform(0, 100), gamma=rng.uniform(0.5, 3),
                         contrast=rng.uniform(0.5, 2), black_floor=rng.uniform(0, 0.3),
                         highlight_room=rng.choice([0, rng.uniform(0, 100)]),
                         haze_cut=rng.choice([0, rng.uniform(0, 100)]),
                         haze_band=rng.uniform(0, 100), haze_band_lo=lo,
                         haze_band_hi=rng.uniform(lo, 1.0)).clamped()
            today = curves._tone_table(replace(s, haze_band=0.0))
            table = curves._tone_table(s)
            self.assertEqual([y.hex() for y in table],
                             [curves.tone_curve(x, s).hex() for x in curves._RAMP_XS])
            for x in range(256):
                self.assertGreaterEqual(table[x], today[x])      # never darker
                if today[x] <= curves.tone_curve(s.haze_band_lo,
                                                 replace(s, haze_band=0.0, haze_cut=0.0)) \
                        and not s.haze_cut and not s.black_floor:
                    self.assertEqual(table[x], today[x])         # below the band: as today
            for a, b in zip(table, table[1:]):
                self.assertLessEqual(a, b + 1e-12)
            if today[-1] == 1.0:
                self.assertAlmostEqual(table[-1], 1.0, places=12)   # white stays white

    def test_band_gets_steeper(self):
        s = Settings(haze_band=100.0, haze_band_lo=0.5, haze_band_hi=0.65)
        t, plain = curves._tone_table(s), curves._tone_table(Settings())
        lo, hi = int(0.5 * 255), round(0.65 * 255)
        self.assertGreater(t[hi] - t[lo], 1.4 * (plain[hi] - plain[lo]))
        self.assertEqual(t[:lo + 1], plain[:lo + 1])


class BandForViewTest(unittest.TestCase):
    def test_light_snow_is_caught(self):
        rng = random.Random(3)
        m = measure(bgra(light_snow(rng)), W)
        self.assertGreater(band_for_view(m), 30.0)
        self.assertGreater(m.band_lo, 0.4)

    def test_no_trigger_on_menus_sky_loading_walls_dark_rooms_night(self):
        rng = random.Random(9)
        scenes = {
            "white menu": [0.92 if rng.random() > 0.03 else 0.1 for _ in range(W * H)],
            "grey menu": [0.55 if rng.random() > 0.05 else 0.95 for _ in range(W * H)],
            "open sky": [0.62 + 0.18 * y / H for y in range(H) for _ in range(W)],
            "black loading screen": [0.0] * (W * H),
            "grey loading screen": hud([0.5] * (W * H)),
            "grey wall up close, sky above": hud(
                [0.85 if y < H // 4 else 0.52 + 0.01 * rng.random() if y < 3 * H // 4
                 else 0.1 + 0.4 * rng.random() for y in range(H) for x in range(W)]),
            "wall": [0.45 + 0.05 * rng.random() for _ in range(W * H)],
            "dark room": hud([0.04 + 0.35 * rng.random() * (0.5 + 0.5 * math.sin(x * 0.2))
                              for y in range(H) for x in range(W)]),
            "night": hud([0.01 + 0.10 * rng.random() for _ in range(W * H)]),
            "foggy night": hud(veil([0.01 + 0.1 * rng.random() for _ in range(W * H)], 0.1, 0.5)),
            "clear day": hud(landscape(rng)),
        }
        for name, px in scenes.items():
            self.assertEqual(band_for_view(measure(bgra(px), W)), 0.0, name)

    def test_colourful_views_count_less(self):
        rng = random.Random(4)
        px = light_snow(rng)
        grey = band_for_view(measure(bgra(px), W))
        red = band_for_view(measure(bgra(px, (1.0, 0.45, 0.3)), W))
        self.assertGreater(grey, 0.0)
        self.assertLessEqual(red, grey)


class BandControllerTest(unittest.TestCase):
    def _run(self, frames, c, fader, seconds=0.2):
        writes = 0
        for px in frames:
            c.observe(measure(bgra(px), W), 30.0, 0.12)
            for _ in range(int(seconds * 120)):
                if fader.tick(1 / 120):
                    writes += 1
        return c, writes

    def test_eases_in_and_still_view_never_rewrites(self):
        rng = random.Random(1)
        frame = light_snow(rng)
        c = Controller()
        fader = Fader({"d": c}, lambda shown: None)
        c, _writes = self._run([frame] * 25, c, fader)
        self.assertGreater(c.band, 30.0)
        self.assertTrue(c.band_resting)
        # Still view (the same picture, a little noise): no more table writes.
        jitter = [[min(1.0, v + 0.004 * rng.random()) for v in frame] for _ in range(10)]
        _c, writes = self._run(jitter, c, fader)
        self.assertEqual(writes, 0)

    def test_edges_follow_the_view_while_the_amount_stays(self):
        # A heavy veil scores 100 wherever you look, but turning moves its
        # band: the contrast must follow it, not stay on the old tones.
        a, b = snow(0.78), snow(0.9)
        ma, mb = measure(bgra(a), W), measure(bgra(b), W)
        self.assertEqual((band_for_view(ma), band_for_view(mb)), (100.0, 100.0))
        self.assertGreater(mb.band_lo - ma.band_lo, 2 * autolevels.BAND_EDGE_DEADBAND)
        c = Controller()
        fader = Fader({"d": c}, lambda shown: None)
        self._run([a] * 16, c, fader)
        self.assertEqual(c.band_edges, (ma.band_lo, ma.band_hi))
        self.assertTrue(c.band_resting)
        self._run([b] * 16, c, fader)
        self.assertEqual(c.band_edges, (mb.band_lo, mb.band_hi))
        self.assertTrue(c.band_resting)
        self.assertEqual((c.band_lo, c.band_hi), (mb.band_lo, mb.band_hi))
        self.assertEqual(fader._bands["d"], (100.0, mb.band_lo, mb.band_hi))

    def test_rewrites_follow_what_the_picture_shows(self):
        moved, step = autolevels.band_moved, autolevels.BAND_STEP
        shape = (60.0, 0.5, 0.65)
        self.assertEqual(moved(shape, shape), 0.0)
        # Half a point of amount is a tiny fraction of a level: not on its own...
        self.assertLess(moved(shape, (60.5, 0.5, 0.65)), step)
        # ...but the edges moving a level or two at this gain are.
        self.assertGreater(moved(shape, (60.0, 0.5 + 1 / 255, 0.65 + 1 / 255)), step)
        self.assertGreater(moved((0.0, 0.0, 0.0), (5.0, 0.5, 0.65)), step)   # from off

    def test_twitchy_score_on_a_still_view_does_not_pump(self):
        # Capture noise swings a still view's score by several points (more
        # than the haze cut's dead band): the band's goal stays put.
        a, b = snow(0.66), snow(0.67)
        ma, mb = measure(bgra(a), W), measure(bgra(b), W)
        self.assertGreater(abs(band_for_view(ma) - band_for_view(mb)), autolevels.HAZE_DEADBAND)
        c = Controller()
        fader = Fader({"d": c}, lambda shown: None)
        self._run([a, b] * 8, c, fader)
        goal, shape = (c.band_want, c.band_edges), fader._bands["d"]
        self.assertGreater(goal[0], 0.0)
        self.assertTrue(c.band_resting)
        for _ in range(3):
            self._run([a, b] * 4 + [b, a] * 4, c, fader)
            self.assertEqual((c.band_want, c.band_edges), goal)
            self.assertEqual(fader._bands["d"], shape)

    def test_gives_way_to_the_lift_and_eases_out(self):
        c = Controller()
        c.band = c.band_want = 60.0
        c.band_lo = c.band_hi = 0.0
        c.band_lo, c.band_hi, c.band_edges = 0.5, 0.65, (0.5, 0.65)
        base = autolevels.look_values(Settings())
        out = fader_amounts("shadow_boost", -5.0, base, c)
        self.assertEqual(out["haze_band"], 60.0)
        self.assertEqual((out["haze_band_lo"], out["haze_band_hi"]), (0.5, 0.65))
        self.assertNotIn("haze_band", fader_amounts("shadow_boost", autolevels.HAZE_LIFT_OFF,
                                                    base, c))
        c.set_haze(False)
        for _ in range(600):
            c.advance(1 / 120)
        self.assertEqual(c.band, 0.0)

    def test_peek_gives_way(self):
        from catvision.engine import Engine
        e = Engine.__new__(Engine)
        e.settings = Settings(shadow_boost=20.0)
        e.auto_offsets = {"d": {"haze_band": 50.0, "haze_band_lo": 0.5, "haze_band_hi": 0.6}}
        e.peek_amount = 0.0
        self.assertEqual(e.settings_for("d").haze_band, 50.0)
        e.peek_amount = 100.0
        self.assertEqual(e.settings_for("d").haze_band, 0.0)


class BandConfigTest(unittest.TestCase):
    def test_old_and_junk_configs_load_with_the_band_off(self):
        from catvision.config import Config
        old = Settings().to_dict()
        for name in ("haze_band", "haze_band_lo", "haze_band_hi"):
            del old[name]
        junk = dict(Settings().to_dict(), haze_band="lots", haze_band_lo=float("nan"),
                    haze_band_hi=7.0)
        for data in (old, junk):
            c = Config.from_json({"current": data, "custom_presets": {"Mine": data}})
            for s in (c.current, c.custom_presets["Mine"]):
                self.assertEqual(s.haze_band, 0.0)
                self.assertEqual(s.haze_band_lo, 0.0)
                self.assertLessEqual(s.haze_band_hi, 1.0)
                self.assertEqual(build_ramp(s), build_ramp(Settings()))
        # A saved look round-trips (auto levels' band is never part of it).
        c = Config.from_json(Config.from_json({"current": old}).to_json())
        self.assertEqual(c.current, Settings())


if __name__ == "__main__":
    unittest.main()
