"""Outside detail (highlight_room): seeing out of a window from a dark room.

The gamma table only knows a pixel's brightness, never where it is, but a
window seen from a dark room is the bright pixels and the room is the dark
ones.  A ceiling line through white keeps the bright part's tones apart
instead of turning it into one white block, and auto levels (beta) gives the
bright part more range the more of the view it fills."""
import math
import random
import unittest

from catvision import curves, presets
from catvision.autolevels import (ROOM_EXTRA, ROOM_FALL_S, ROOM_FULL, ROOM_KEEP_LEVEL,
                                  ROOM_MIN_SHARE, ROOM_RISE_S, ROOM_STRENGTH_FULL, THUMB_H,
                                  THUMB_W, Controller, Fader, fader_amounts, look_values,
                                  measure, room_for_highs, split_offset)
from catvision.backends import NullColorBackend
from catvision.curves import Settings, build_ramp, limit_ramp_deviation, tone_curve
from catvision.engine import Engine
from tests.test_catvision import FakeGamma

D1 = "\\\\.\\DISPLAY1"
STRONG = ("Dark Areas", "Pitch Black", "Night Vision", "Night Vision (Blue)")
ROOM_LEVELS = range(0, 36)       # a dark room: levels 0..35
OUTSIDE = (45, 80, 100, 122, 152, 215)   # shadows, trees, ground, walls, lit walls, sky


def _look(name, boost=0.0, room=None):
    s = presets.builtin_by_name(name).settings
    key = "exposure" if s.exposure >= 40 else "shadow_boost"
    amounts = split_offset(key, boost, {k: getattr(s, k)
                                        for k in ("shadow_boost", "exposure", "anti_glare")})
    changes = {k: getattr(s, k) + v for k, v in amounts.items()}
    if room is not None:
        changes["highlight_room"] = room
    return s.clamped(changes)


def _tones(s):
    return [tone_curve(i / 255, s) for i in range(256)]


def _frame(level_at, w=THUMB_W, h=THUMB_H) -> bytes:
    out = bytearray()
    for y in range(h):
        for x in range(w):
            v = level_at(x, y)
            out += bytes((v, v, v, 255))
    return bytes(out)


class CurveTest(unittest.TestCase):
    def test_off_changes_nothing(self):
        s = _look("Dark Areas", 15)
        self.assertEqual(_tones(s.clamped({"highlight_room": 0})),
                         _tones(s.clamped({"highlight_room": 0.0})))
        self.assertEqual(curves.highlight_slope(0), 0.0)
        self.assertEqual(Settings().highlight_room, 0.0)

    def test_strong_presets_keep_the_outside_apart(self):
        """With and without auto levels' boost, the outside's tones get real
        output range and only the very top reaches white."""
        for name in STRONG:
            for boost in (0.0, 15.0, 30.0):
                t = _tones(_look(name, boost))
                top = t[255]
                near_white = next(i for i, v in enumerate(t) if v >= 0.98 * top)
                self.assertGreaterEqual(near_white, 225, (name, boost))
                steps = [t[b] - t[a] for a, b in zip(OUTSIDE, OUTSIDE[1:])]
                self.assertGreater(min(steps), 0.01, (name, boost, steps))
                self.assertGreater(t[240] - t[150], 0.07, (name, boost))

    def test_dark_room_keeps_its_lift(self):
        """The preset's Outside detail leaves the dark room's tones alone
        (at most the rounded corner brushes the top of the room)."""
        for name in STRONG:
            for boost in (0.0, 15.0, 30.0):
                with_room = _tones(_look(name, boost))
                without = _tones(_look(name, boost, room=0.0))
                for level in ROOM_LEVELS:
                    self.assertAlmostEqual(with_room[level], without[level], delta=0.01,
                                           msg=(name, boost, level))

    def test_input_255_still_reaches_white(self):
        for room in (0, 30, 60, 100):
            for boost in (0, 50, 100):
                s = Settings(shadow_boost=boost, contrast=1.3, highlight_room=room).clamped()
                self.assertEqual(tone_curve(1.0, s), 1.0)

    def test_monotone_and_valid_ramps(self):
        rng = random.Random(7)
        tints = [None, curves.TINT_CURVES["night_vision"], curves.TINT_CURVES["night_vision_blue"],
                 curves.plan_tint(Settings(color_filter="thermal"))]
        for _ in range(400):
            s = Settings(shadow_boost=rng.uniform(0, 100), exposure=rng.choice([0, 40, 80]),
                         anti_glare=rng.choice([0, 100]), gamma=rng.uniform(0.5, 3),
                         brightness=rng.uniform(0.5, 2), contrast=rng.uniform(0.5, 2),
                         black_floor=rng.uniform(0, 0.3), white_cap=rng.uniform(0.5, 1),
                         highlight_room=rng.uniform(0, 100)).clamped()
            t = _tones(s)
            self.assertTrue(all(b >= a for a, b in zip(t, t[1:])), s)
            for ramp in (build_ramp(s, rng.choice(tints)),):
                for ch in ramp:
                    self.assertTrue(all(0 <= v <= 65535 for v in ch))
                    self.assertTrue(all(b >= a for a, b in zip(ch, ch[1:])))

    def test_tints_still_change_across_the_outside(self):
        """Night vision's dark-to-pale tint and the thermal gradient keep
        coloring the outside by brightness instead of one flat color."""
        for name in ("Night Vision", "Night Vision (Blue)"):
            s = _look(name, 15)
            ramp = build_ramp(s, curves.plan_tint(s))
            for ch in ramp:
                if ch[255] > 0:
                    self.assertGreater(ch[240] - ch[150], 0.04 * 65535, name)
        s = _look("Dark Areas", 15).clamped({"color_filter": "thermal"})
        ramp = build_ramp(s, curves.plan_tint(s))
        self.assertGreater(sum(ch[240] - ch[150] for ch in ramp), 0.1 * 65535)

    def test_closer_to_a_straight_line_for_limited_pcs(self):
        """Windows may limit how far a table strays from a straight line;
        the ceiling only ever brings it closer."""
        def gap(ramp):
            return max(abs(v - i * 257) for ch in ramp for i, v in enumerate(ch)) // 257
        for name in STRONG:
            for boost in (0.0, 30.0):
                s = _look(name, boost)
                with_room = gap(build_ramp(s))
                without = gap(build_ramp(s.clamped({"highlight_room": 0})))
                self.assertLessEqual(with_room, without, name)
                limited = limit_ramp_deviation(build_ramp(s), 127)
                for ch in limited:
                    self.assertTrue(all(b >= a for a, b in zip(ch, ch[1:])))


class SharingTest(unittest.TestCase):
    def test_bright_share_is_measured(self):
        dark = measure(_frame(lambda x, y: 20), THUMB_W)
        self.assertEqual(dark.highs, 0.0)
        # A window filling the middle quarter of the view (the middle counts extra).
        window = measure(_frame(lambda x, y: 200 if 24 <= x < 72 and 13 <= y < 40 else 20),
                         THUMB_W)
        self.assertGreater(window.highs, 0.25)
        self.assertLess(window.highs, 0.6)
        self.assertEqual(measure(_frame(lambda x, y: 0), THUMB_W).highs, 0.0)

    def test_hud_text_and_specks_dont_count(self):
        """Thin bright lines (HUD text, bars, a crosshair) and scattered
        specks only dot the grid parts they are in: no Outside detail for
        them.  A small window or a lamp fills its parts and counts in full."""
        def hud(x, y):
            if y in (2, 51) and 4 <= x < 92:          # a bar along the top and the bottom
                return 230
            if x in (2, 93) and 10 <= y < 44:         # and down both sides
                return 230
            if (x == 48 and 24 <= y < 31) or (y == 27 and 45 <= x < 52):   # crosshair
                return 255
            if (x * 7 + y * 13) % 41 == 0:            # specks all over
                return 200
            return 20
        plain = sum(1 for y in range(THUMB_H) for x in range(THUMB_W) if hud(x, y) >= 96)
        self.assertGreater(plain / (THUMB_W * THUMB_H), 3 * ROOM_MIN_SHARE)
        m = measure(_frame(hud), THUMB_W)
        self.assertLess(m.highs, ROOM_MIN_SHARE / 10)   # (a corner where two bars meet)
        self.assertEqual(room_for_highs(m.highs), 0.0)
        # The same HUD over a small window (about 10% of the view): the
        # window still counts in full.
        small = lambda x, y: 200 if 60 <= x < 90 and 8 <= y < 25 else 20   # noqa: E731
        alone = measure(_frame(small), THUMB_W).highs
        with_hud = measure(_frame(lambda x, y: max(small(x, y), hud(x, y))), THUMB_W).highs
        self.assertGreater(alone, 0.05)
        self.assertAlmostEqual(with_hud, alone, delta=0.25 * alone)
        # Without the picture's width (no grid), every bright pixel counts.
        self.assertGreater(measure(_frame(hud)).highs, 0.0)

    def test_room_grows_with_the_share(self):
        self.assertEqual(room_for_highs(0.0), 0.0)
        self.assertEqual(room_for_highs(ROOM_MIN_SHARE), 0.0)
        shares = [i / 100 for i in range(51)]
        rooms = [room_for_highs(p) for p in shares]
        self.assertTrue(all(b >= a for a, b in zip(rooms, rooms[1:])))
        self.assertEqual(rooms[-1], ROOM_EXTRA)
        self.assertGreater(room_for_highs(0.1), ROOM_EXTRA / 2)   # a small window gets plenty
        # Standing close to a window (most of the view) still gets it all.
        self.assertEqual(room_for_highs(1.0 - ROOM_FULL - ROOM_MIN_SHARE), ROOM_EXTRA)

    def test_all_bright_view_has_no_room_to_share(self):
        """A flash whiting out the screen (or the open sky) is not a window
        seen from a dark room: nothing extra, whatever the flash logic does."""
        self.assertEqual(room_for_highs(1.0), 0.0)
        self.assertEqual(room_for_highs(0.999), 0.0)
        shares = [i / 100 for i in range(50, 101)]
        rooms = [room_for_highs(p) for p in shares]
        self.assertTrue(all(b <= a for a, b in zip(rooms, rooms[1:])))
        c = Controller()
        c.observe(measure(_frame(lambda x, y: 20), THUMB_W), 30.0, 0.12)
        c.observe(measure(_frame(lambda x, y: 255), THUMB_W), 30.0, 0.12)
        self.assertEqual(c.room_want, 0.0)

    def test_how_much_scales_it_and_0_turns_it_off(self):
        self.assertEqual(room_for_highs(0.3, 0.0), 0.0)
        self.assertEqual(room_for_highs(0.3, ROOM_STRENGTH_FULL / 2), ROOM_EXTRA / 2)
        self.assertEqual(room_for_highs(0.3, ROOM_STRENGTH_FULL), ROOM_EXTRA)
        self.assertEqual(room_for_highs(0.3, 60.0), ROOM_EXTRA)
        c = Controller()
        window = measure(_frame(lambda x, y: 200 if x < 48 else 20), THUMB_W)
        c.observe(window, 0.0, 0.12)
        for _ in range(600):
            c.advance(1 / 120)
        self.assertEqual((c.offset, c.room), (0.0, 0.0))
        # Changed in CatVision's own window (no new game picture): at once.
        c.hold(30.0, 0.12)
        self.assertEqual(c.room_want, room_for_highs(window.highs))
        c.hold(0.0, 0.12)
        self.assertEqual(c.room_want, 0.0)

    def test_controller_eases_room_in_quickly_and_out_slowly(self):
        c = Controller()
        dark = measure(_frame(lambda x, y: 20), THUMB_W)
        window = measure(_frame(lambda x, y: 200 if x < 48 else 20), THUMB_W)
        c.observe(window, 30.0, 0.12)
        self.assertEqual(c.room_want, room_for_highs(window.highs))
        dt = 1 / 120
        look = presets.builtin_by_name("Dark Areas").settings
        steps, prev = 0, curves._tone_table(look)
        while c.room < 0.95 * c.room_want:
            c.advance(dt)
            # No visible jump: each frame moves the picture well under a level.
            now = curves._tone_table(look.clamped(
                {"highlight_room": look.highlight_room + c.room}))
            self.assertLess(max(abs(a - b) for a, b in zip(prev, now)), 0.6 / 255)
            prev = now
            steps += 1
        self.assertLess(steps * dt, 4 * ROOM_RISE_S)
        for _ in range(1200):
            c.advance(dt)
        self.assertEqual(c.room, c.room_want)
        c.observe(dark, 30.0, 0.12)
        self.assertEqual(c.room_want, 0.0)
        top, steps = c.room, 0
        while c.room > 0.05 * top:
            c.advance(dt)
            steps += 1
        self.assertGreater(steps * dt, 2 * ROOM_FALL_S)
        for _ in range(2000):
            c.advance(dt)
        self.assertEqual(c.room, 0.0)

    def test_steady_noisy_view_keeps_its_goal(self):
        """A still view whose picture flickers a little (grain, foliage)
        must not wake the fader and redo the tables on every picture."""
        import random
        c = Controller()
        rnd = random.Random(7)

        def noisy(seed):
            rnd.seed(seed)
            return measure(_frame(lambda x, y: max(0, min(255, (
                [215, 150, 120, 100, 80, 45][(x // 5) % 6] if 20 <= x < 50 and 10 <= y < 40
                else 15) + rnd.randint(-6, 6)))), THUMB_W)

        c.observe(noisy(0), 30.0, 0.12)
        first = c.room_want
        self.assertGreater(first, 10)
        for seed in range(1, 20):
            c.observe(noisy(seed), 30.0, 0.12)
            self.assertEqual(c.room_want, first)
        # A real change still gets through at once.
        c.observe(measure(_frame(lambda x, y: 20), THUMB_W), 30.0, 0.12)
        self.assertEqual(c.room_want, 0.0)

    def test_lost_picture_keeps_it_and_release_drops_it(self):
        c = Controller()
        c.observe(measure(_frame(lambda x, y: 220 if x < 30 else 10), THUMB_W), 30.0, 0.12)
        want = c.room_want
        self.assertGreater(want, 0)
        c.observe(None, 30.0, 0.12)
        self.assertEqual(c.room_want, want)
        for _ in range(240):
            c.advance(1 / 120)
        self.assertGreater(c.room, want / 2)
        c.release()
        self.assertEqual(c.room_want, 0.0)
        # Auto levels stopping hands it back at the quick pace (and glides).
        for _ in range(int(4.0 * 120)):
            c.advance(1 / 120)
        self.assertEqual(c.room, 0.0)

    def test_fader_amounts(self):
        base = look_values(presets.builtin_by_name("Dark Areas").settings)
        c = Controller()
        self.assertEqual(fader_amounts("shadow_boost", 12.0, base, c),
                         split_offset("shadow_boost", 12.0, base))
        self.assertEqual(fader_amounts("shadow_boost", 12.0, base),
                         split_offset("shadow_boost", 12.0, base))
        c.room = 17.5
        out = fader_amounts("shadow_boost", -10.0, base, c)
        self.assertEqual(out["highlight_room"], 17.5)
        self.assertEqual(out["shadow_boost"], -10.0)
        # A strong lift leaves less room (none at +30: see the next test).
        c.room = ROOM_EXTRA
        self.assertLess(fader_amounts("shadow_boost", 15.0, base, c)["highlight_room"],
                        ROOM_EXTRA / 2)
        self.assertNotIn("highlight_room", fader_amounts("shadow_boost", 30.0, base, c))

    def test_the_room_comes_first(self):
        """However bright the view and however strong the lift, auto levels'
        extra Outside detail never changes the room's tones (ROOM_KEEP_LEVEL
        and darker), and it only ever adds to the preset's own."""
        c = Controller()
        c.room = ROOM_EXTRA
        for name in STRONG + ("Gentle Boost", "Fog & Haze Cutter"):
            s = presets.builtin_by_name(name).settings
            key = "exposure" if s.exposure >= 40 else "shadow_boost"
            base = look_values(s)
            for offset in (-30.0, -15.0, 0.0, 7.5, 15.0, 30.0):
                amounts = fader_amounts(key, offset, base, c)
                self.assertGreaterEqual(amounts.get("highlight_room", 0.0), 0.0)
                adds = {k: getattr(s, k) + v for k, v in amounts.items()}
                shown = s.clamped(adds)
                plain = s.clamped(dict(adds, highlight_room=s.highlight_room))
                for level in range(ROOM_KEEP_LEVEL + 1):
                    self.assertEqual(tone_curve(level / 255, shown),
                                     tone_curve(level / 255, plain), (name, offset, level))
        # Night Vision (Blue) with the window filling the view: its room's
        # 30 and 35 keep the lift they had without the window.
        s = presets.builtin_by_name("Night Vision (Blue)").settings
        amounts = fader_amounts("exposure", 0.0, look_values(s), c)
        for level in (30, 35):
            self.assertEqual(tone_curve(level / 255, s.clamped(
                {k: getattr(s, k) + v for k, v in amounts.items()})),
                tone_curve(level / 255, s), level)

    def test_highlight_room_cap(self):
        s = _look("Dark Areas", 0.0, room=0.0)
        x = ROOM_KEEP_LEVEL / 255
        cap = curves.highlight_room_cap(s, x)
        self.assertGreater(cap, 40)
        self.assertLess(cap, 100)
        at = s.clamped({"highlight_room": cap})
        self.assertEqual(tone_curve(x, at), tone_curve(x, s))
        self.assertLess(tone_curve(x + 4 / 255, s.clamped({"highlight_room": cap + 10})),
                        tone_curve(x + 4 / 255, s))
        # Stronger lift, less room; a lift past the line, none.
        self.assertLess(curves.highlight_room_cap(_look("Dark Areas", 30.0, room=0.0), x), cap)
        self.assertEqual(curves.highlight_room_cap(Settings(shadow_boost=100, brightness=2.0), x), 0.0)
        self.assertEqual(curves.highlight_room_cap(Settings(), 1.0), 100.0)
        self.assertEqual(curves.highlight_room_cap(Settings(), 0.0), 100.0)

    def test_fader_keeps_stepping_while_the_room_moves(self):
        """The offset is at rest but the room is still easing: the fader
        must keep applying small steps (not wait for its rest pause)."""
        c = Controller()
        calls = []
        f = Fader({D1: c}, lambda pos: calls.append(c.room))
        c.room_want = 30.0
        self.assertFalse(f.settled())
        dt = 1 / 120
        for _ in range(600):
            f.tick(dt)
        self.assertTrue(f.settled())
        self.assertEqual(calls[-1], 30.0)
        steps = [b - a for a, b in zip(calls, calls[1:])]
        self.assertGreater(len(calls), 20)
        self.assertLess(max(steps), 1.5)

    def test_engine_adds_room_to_the_preset(self):
        e = Engine(FakeGamma(), NullColorBackend())
        e.settings = presets.builtin_by_name("Dark Areas").settings.clamped()
        e.set_displays([])
        e.set_enabled(True)
        e.set_auto_offsets({D1: {"shadow_boost": 10.0, "highlight_room": 30.0}})
        got = e.settings_for(D1)
        self.assertEqual(got.highlight_room, e.settings.highlight_room + 30.0)
        e.set_auto_offsets({D1: {"highlight_room": 90.0}})
        self.assertEqual(e.settings_for(D1).highlight_room, 100.0)


class SavedLooksTest(unittest.TestCase):
    def test_saved_copies_of_strong_presets_get_outside_detail(self):
        from catvision import config as cm
        old = Settings(shadow_boost=70, exposure=3, anti_glare=100, contrast=1.34).to_dict()
        del old["highlight_room"]
        mine = dict(old, highlight_room=12.0)
        c = cm.Config.from_json({"current": old, "active_preset": "Dark Areas",
                                 "custom_presets": {"Dark Areas": old, "Pitch Black": mine,
                                                    "My look": old}})
        want = presets.builtin_by_name("Dark Areas").settings.highlight_room
        self.assertGreater(want, 0)
        self.assertEqual(c.current.highlight_room, want)
        self.assertEqual(c.custom_presets["Dark Areas"].highlight_room, want)
        self.assertEqual(c.custom_presets["Dark Areas"].shadow_boost, 70)
        self.assertEqual(c.custom_presets["Pitch Black"].highlight_room, 12.0)   # set: kept
        self.assertEqual(c.custom_presets["My look"].highlight_room, 0.0)       # own look: off
        # Saved again and loaded: unchanged.
        again = cm.Config.from_json(c.to_json())
        self.assertEqual(again.custom_presets, c.custom_presets)
        self.assertEqual(again.current, c.current)
        # A user preset active: the current look is left alone.
        c = cm.Config.from_json({"current": old, "active_preset": "My look"})
        self.assertEqual(c.current.highlight_room, 0.0)

    def test_copy_saved_under_an_old_name_and_builtins_untouched(self):
        from catvision import config as cm
        old = Settings(exposure=50, anti_glare=74, gamma=1.25).to_dict()
        del old["highlight_room"]
        name = presets.builtin_by_name("Night Vision (Blue)").name
        builtins = {p.name: p.settings.highlight_room for p in presets.BUILTIN_PRESETS}
        c = cm.Config.from_json({"current": old, "active_preset": "Night Vision (Blue/Red)",
                                 "custom_presets": {"Night Vision (Blue/Red)": old}})
        want = presets.builtin_by_name(name).settings.highlight_room
        self.assertEqual(c.custom_presets[name].highlight_room, want)
        self.assertEqual(c.current.highlight_room, want)
        # A removed preset was active: the current look falls back to the
        # default preset's own settings object, which must not be changed.
        cm.Config.from_json({"current": dict(old), "active_preset": "Thermal"})
        self.assertEqual({p.name: p.settings.highlight_room for p in presets.BUILTIN_PRESETS},
                         builtins)


class WindowSceneTest(unittest.TestCase):
    """A dark room with a window, through the real measurement, controller
    and curve: the outside stays visible and the room stays lifted."""

    def test_window_from_a_dark_room(self):
        def scene(x, y):
            if 30 <= x < 66 and 10 <= y < 34:       # the window
                return OUTSIDE[(x // 6 + y // 4) % len(OUTSIDE)]
            return 8 + (x * 7 + y * 3) % 25         # the dark room
        m = measure(_frame(scene), THUMB_W)
        for name in ("Dark Areas", "Pitch Black", "Night Vision", "Night Vision (Blue)"):
            c = Controller()
            c.observe(m, 30.0, 0.12)
            for _ in range(600):
                c.advance(1 / 120)
            look = presets.builtin_by_name(name).settings
            key = "exposure" if look.exposure >= 40 else "shadow_boost"
            amounts = fader_amounts(key, c.offset, look_values(look), c)
            s = look.clamped({k: getattr(look, k) + v for k, v in amounts.items()})
            shown = _tones(s)
            plain = _tones(s.clamped({"highlight_room": 0}))
            outside = sorted(shown[v] for v in OUTSIDE)
            self.assertGreater(min(b - a for a, b in zip(outside, outside[1:])), 0.015, name)
            self.assertLess(shown[215], 0.98 * shown[255], name)
            for level in ROOM_LEVELS:
                self.assertGreaterEqual(shown[level], plain[level] - 0.02, (name, level))
            self.assertTrue(math.isfinite(sum(shown)))


if __name__ == "__main__":
    unittest.main()
