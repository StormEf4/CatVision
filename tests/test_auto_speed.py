"""Auto mode (beta): quicker reaction to lights that stay on, 240 Hz fades,
a daylight lift that follows the shadows, and quicker dark-to-light switches.

Game-like 96x54 pictures (tests.test_autolevels_views) are captured on the
Watcher's beat at the quick (DXGI) pace and reach a standalone controller a
little later; the fader moves 120 (or 240) times a second.
"""

import unittest
from dataclasses import replace
from unittest import mock

from catvision import autolevels as al
from catvision.autolevels import Controller, Fader
from tests.test_autolevels_views import PICS, _frame, _room

CAP = al.auto_cap(30.0)
AIM = 0.12
QUICK_MS = 3.0


def _flat(level):
    return al.measure(bytes([level, level, level, 255]) * (al.THUMB_W * al.THUMB_H),
                      al.THUMB_W)


def _run(scene_at, seconds, start, hz=120, phase=0.03, latency=0.03, cost_ms=QUICK_MS):
    """[(t, offset, want, ctrl)] per fader step from t=0 (the change),
    after three seconds settled in ``start``."""
    c = Controller(standalone=True)
    first = PICS[start] if isinstance(start, str) else start
    c.observe(replace(first, signature=1, at=0.0), CAP, AIM, True, now=0.0)
    c.offset = c.want
    pre = 3.0
    frame = 1.0 / hz
    t, next_cap, prev, older, pending, n, out = -pre, -pre + phase, None, None, [], 1, []
    while t < seconds:
        while next_cap <= t:
            name = scene_at(next_cap) if next_cap >= 0 else start
            pic = PICS[name] if isinstance(name, str) else name
            n += 1
            m = replace(pic, signature=n, at=next_cap + pre)
            pending.append((next_cap + latency, m))
            next_cap += al.capture_interval(prev, m, cost_ms, older)
            older, prev = prev, m
        while pending and pending[0][0] <= t:
            ready, m = pending.pop(0)
            c.observe(m, CAP, AIM, True, now=ready + pre)
        c.advance(frame)
        if t >= 0:
            out.append((t, c.offset, c.want))
        t += frame
    return out, c


def _there(frames, within=2.0):
    """Seconds until the offset is within ``within`` points of where it ends."""
    end = frames[-1][1]
    last = 0.0
    for t, off, _w in frames:
        if abs(off - end) > within:
            last = t
    return last


PICS_EXTRA = {
    "lamp room": al.measure(_frame(lambda fx, fy: 1.7 * _room(fx, fy)
                                   + (90 if fx > 0.8 and fy > 0.4 else 0)), al.THUMB_W),
}
PICS.update(PICS_EXTRA)


class LastingLightTests(unittest.TestCase):
    """A light that comes on and stays (a lamp come into view, a flare, a
    light switched on) fades down at the new-view pace once it has lasted."""

    def test_a_light_that_stays_on_is_followed_quicker(self):
        for name in ("brighter room", "lamp room"):
            for phase in (0.0, 0.03, 0.07):
                with self.subTest(name=name, phase=phase):
                    quick, c = _run(lambda t, n=name: n, 2.0, "room", phase=phase)
                    with mock.patch.object(Controller, "_lasting_light", return_value=False):
                        before, _c = _run(lambda t, n=name: n, 2.0, "room", phase=phase)
                    # The same goal, reached sooner (it took about 0.7 s).
                    self.assertAlmostEqual(quick[-1][2], before[-1][2], places=6)
                    self.assertLess(quick[-1][2], quick[0][2] - 5.0)
                    self.assertLess(_there(quick), _there(before) - 0.15)
                    self.assertLess(_there(quick), 0.45)
                    # Never further than the goal, never back up.
                    offs = [o for _t, o, _w in quick]
                    self.assertTrue(all(b <= a + 1e-9 for a, b in zip(offs, offs[1:])))
                    self.assertGreaterEqual(min(offs), quick[-1][2] - 1e-9)

    def test_its_steps_stay_as_small_as_a_normal_fade(self):
        # Not a flash: the normal fades' ceiling (CEILING) in a preset's
        # points, per update, as each step is worth on screen.
        quick, _c = _run(lambda t: "brighter room", 1.5, "room", hz=120)
        for (_t, a, _w), (_t2, b, _w2) in zip(quick, quick[1:]):
            pace = min(al.auto_pace(a), al.auto_pace(b))
            self.assertLessEqual(abs(b - a), al.CEILING / 120 * pace + 1e-6)

    def test_marks_a_lasting_light_but_not_a_flicker(self):
        c = Controller(standalone=True)
        dark, lit = _flat(8), _flat(13)                # 1.6x: a light, not a flash
        for i in range(30):
            c.observe(replace(dark, signature=i + 1), CAP, AIM, True, now=i * 0.2)
            c.advance(0.2)
        now = 6.0
        c.observe(replace(lit, signature=100), CAP, AIM, True, now=now)
        self.assertLess(c.want, c.offset)
        self.assertFalse(c.rush)                       # one brighter picture: the normal pace
        c.advance(0.05)
        # Gone again by the next picture: still the normal pace.
        c.observe(replace(dark, signature=101), CAP, AIM, True, now=now + al.BURST_S)
        self.assertFalse(c.rush)
        # Still on a picture later: the new-view pace, with a normal fade's steps.
        c2 = Controller(standalone=True)
        for i in range(30):
            c2.observe(replace(dark, signature=i + 1), CAP, AIM, True, now=i * 0.2)
            c2.advance(0.2)
        c2.observe(replace(lit, signature=100), CAP, AIM, True, now=now)
        c2.advance(0.05)
        c2.observe(replace(lit, signature=101), CAP, AIM, True, now=now + al.BURST_S)
        self.assertTrue(c2.rush and c2._rush_view and c2._rush_light)

    def test_a_flash_that_lasts_keeps_its_own_ceiling(self):
        c = Controller(standalone=True)
        dark, flash = _flat(8), _flat(60)              # many times brighter: a flash
        for i in range(30):
            c.observe(replace(dark, signature=i + 1), CAP, AIM, True, now=i * 0.2)
            c.advance(0.2)
        c.observe(replace(flash, signature=100), CAP, AIM, True, now=6.0)
        self.assertTrue(c.rush)
        self.assertFalse(c._rush_view)                 # (a flash's gentler start first)
        c.advance(0.05)
        c.observe(replace(flash, signature=101), CAP, AIM, True, now=6.0 + al.BURST_S)
        self.assertTrue(c._rush_view)                  # lasted: the new-view pace...
        self.assertFalse(c._rush_light)                # ...with the flash ceiling

    def test_storms_and_strobes_dim_no_more_than_before(self):
        def storm(t):
            return "lit" if any(s <= t < s + d for s, d in ((0.5, 0.15), (2.5, 0.3),
                                                            (4.0, 0.08))) else "room"

        def strobe(t):
            return "lit" if t % 0.8 < 0.2 else "room"
        for scene_at in (storm, strobe):
            for phase in (0.0, 0.05):
                with self.subTest(scene=scene_at.__name__, phase=phase):
                    now, _c = _run(scene_at, 6.0, "room", phase=phase)
                    with mock.patch.object(Controller, "_lasting_light", return_value=False):
                        before, _c = _run(scene_at, 6.0, "room", phase=phase)
                    # The same goals (only quicker on the way down)...
                    self.assertEqual([w for _t, _o, w in now], [w for _t, _o, w in before])
                    # ...and never brighter between the flashes than before.
                    rises = lambda fr: sum(1 for a, b in zip(fr, fr[1:]) if b[1] > a[1] + 1e-9)  # noqa: E731
                    self.assertLessEqual(rises(now), rises(before))


class DarkToLightTests(unittest.TestCase):
    def test_a_lighter_view_of_the_same_kind_is_followed_in_about_a_third_of_a_second(self):
        # A dark room to a less dark one (1.6x: no flash, the layout kept,
        # so no new view either) took about 0.7 s at the normal pace; 1.19
        # took about 0.30 s.
        for phase in (0.0, 0.03, 0.07):
            with self.subTest(phase=phase):
                frames, _c = _run(lambda t: "brighter room", 2.0, "room", phase=phase)
                self.assertLess(_there(frames), 0.4)

    def test_turning_toward_daylight_is_as_quick_as_before(self):
        for phase in (0.0, 0.03, 0.07):
            with self.subTest(phase=phase):
                frames, _c = _run(lambda t: "day", 2.0, "room", phase=phase)
                with mock.patch.object(Controller, "_lasting_light", return_value=False):
                    before, _c = _run(lambda t: "day", 2.0, "room", phase=phase)
                self.assertLessEqual(_there(frames), _there(before) + 1e-9)


class FaderRateTests(unittest.TestCase):
    def test_up_to_240_on_fast_screens(self):
        self.assertEqual(al.FADER_MAX_HZ, 240)
        self.assertEqual(al.fader_rate([240]), 240)
        self.assertEqual(al.fader_rate([144]), 144)
        self.assertEqual(al.fader_rate([60, 120]), 120)
        self.assertEqual(al.fader_rate([360]), 240)
        self.assertEqual(al.fader_rate([]), al.FADER_HZ)

    def test_240_hz_fades_as_fast_in_half_size_steps(self):
        """The quickest fade (into a new darker view) is capped per update:
        at 240 Hz at the speed a 120 Hz screen gets (CEILING_STEP_HZ)."""
        def rise(hz):
            c = Controller(offset=0.0, want=30.0, strength=30.0)
            c._view_rise = True
            out = [0.0]
            for _ in range(hz):
                c.advance(1 / hz)
                out.append(c.offset)
            return out
        at120, at240 = rise(120), rise(240)
        step120 = max(b - a for a, b in zip(at120, at120[1:]))
        step240 = max(b - a for a, b in zip(at240, at240[1:]))
        self.assertAlmostEqual(step240, step120 / 2, delta=0.02)
        # (Equally far after a tenth of a second.)
        self.assertAlmostEqual(at240[24], at120[12], delta=0.5)

    def test_a_slow_pc_goes_back_to_120(self):
        fader = Fader({}, lambda pos: None)
        fader.set_rates({"a": 240})
        self.assertAlmostEqual(fader.interval, 1 / 240)
        # Cheap steps: stays at 240.
        for _ in range(al.FADER_BUSY_STEPS * 2):
            fader.note_step_cost(0.0002)
        self.assertFalse(fader.busy_limited)
        self.assertAlmostEqual(fader.interval, 1 / 240)
        # Steps costing more than FADER_BUSY_SHARE of their time (counted in
        # Windows' 15.6 ms ticks: one now and then): back to 120...
        for i in range(al.FADER_BUSY_STEPS):
            fader.note_step_cost(0.0156 if i % 10 == 0 else 0.0)
        self.assertTrue(fader.busy_limited)
        self.assertAlmostEqual(fader.interval, 1 / al.FADER_SAFE_HZ)
        # ...and stays there when the displays are read again...
        fader.set_rates({"a": 240})
        self.assertAlmostEqual(fader.interval, 1 / al.FADER_SAFE_HZ)
        fader.set_rates({"a": 60})
        self.assertAlmostEqual(fader.interval, 1 / 60)
        # ...until the next Auto mode session measures again.
        fader.set_rates({"a": 240})
        fader.start()
        fader.stop()
        self.assertFalse(fader.busy_limited)
        self.assertAlmostEqual(fader.interval, 1 / 240)

    def test_120_and_below_is_never_measured(self):
        fader = Fader({}, lambda pos: None)
        fader.set_rates({"a": 120})
        for _ in range(al.FADER_BUSY_STEPS * 2):
            fader.note_step_cost(0.05)
        self.assertFalse(fader.busy_limited)
        self.assertAlmostEqual(fader.interval, 1 / 120)

    def test_the_running_fader_measures_its_steps(self):
        import time
        c = Controller(offset=0.0, want=1e6, strength=1e6)          # always moving
        seen = []
        fader = Fader({"a": c}, lambda pos: None)
        fader.set_rates({"a": 240})
        with mock.patch.object(Fader, "note_step_cost", lambda self, cpu: seen.append(cpu)):
            fader.start()
            time.sleep(0.15)
            fader.stop()
        self.assertGreater(len(seen), 5)
        self.assertTrue(all(cpu >= 0 for cpu in seen))


class DaylightLiftTests(unittest.TestCase):
    DAY = al.auto_day_lift(CAP)

    def test_follows_the_darkest_quarter(self):
        bright = AIM * 2 ** (al.AUTO_RAMP_STOPS + 0.3)   # a daylight view
        deep = al.auto_offset(bright, CAP, AIM, shadows=0.08)       # a doorway, under a roof
        some = al.auto_offset(bright, CAP, AIM, shadows=0.28)
        none = al.auto_offset(bright, CAP, AIM, shadows=AIM * al.AUTO_SHOWN)   # already readable
        self.assertAlmostEqual(deep, self.DAY, delta=1e-6)
        self.assertTrue(0 < some < deep)
        self.assertAlmostEqual(none, 0.0, delta=0.05)
        # Darker shadows never get less, and never more than the daylight lift.
        last = None
        for i in range(1, 60):
            off = al.auto_offset(bright, CAP, AIM, shadows=i / 100)
            self.assertLessEqual(off, self.DAY + 1e-9)
            if last is not None:
                self.assertLessEqual(off, last + 1e-9)
            last = off

    def test_shadows_shown_at_the_night_look_level(self):
        bright = AIM * 2 ** (al.AUTO_RAMP_STOPS + 0.2)
        for shadows in (0.26, 0.3, 0.34):
            off = al.auto_offset(bright, CAP, AIM, shadows=shadows)
            self.assertGreater(off, 0)
            self.assertAlmostEqual(al.shown_key(shadows, off), AIM * al.AUTO_SHOWN, delta=0.005)

    def test_a_bright_view_with_deep_shadows_keeps_the_full_lift(self):
        # By the key alone it faded out over AUTO_DAY_STOPS; by its shadows
        # it keeps the lift until the last AUTO_DAY_TOP_STOPS.
        key = AIM * 2 ** (al.AUTO_RAMP_STOPS + 0.4)
        self.assertLess(al.auto_offset(key, CAP, AIM), self.DAY * 0.7)
        self.assertAlmostEqual(al.auto_offset(key, CAP, AIM, shadows=0.1), self.DAY, delta=1e-6)

    def test_meets_the_tone_down(self):
        top = AIM * 2 ** (al.AUTO_RAMP_STOPS + al.AUTO_DAY_STOPS)
        for shadows in (0.05, 0.2):
            self.assertAlmostEqual(al.auto_offset(top * 0.999, CAP, AIM, shadows=shadows), 0.0,
                                   delta=0.2)
            self.assertLess(al.auto_offset(top * 1.2, CAP, AIM, shadows=shadows), 0.0)
            self.assertEqual(al.auto_offset(top * 1.2, CAP, AIM, False, shadows), 0.0)

    def test_night_is_unchanged(self):
        for key in (0.01, 0.03, 0.06, 0.1):
            for shadows in (0.005, key * 0.7, key):
                self.assertAlmostEqual(al.auto_offset(key, CAP, AIM, shadows=shadows),
                                       al.auto_offset(key, CAP, AIM), delta=1e-9)

    def test_controller_follows_the_brightest_recent_shadows(self):
        sunny = _flat(150)          # all bright: shadows as bright as the rest
        c = Controller(standalone=True)
        for i in range(20):
            c.observe(replace(sunny, signature=i + 1), CAP, AIM, True, now=i * 0.2)
        self.assertAlmostEqual(c.shadows, sunny.shadows)
        self.assertAlmostEqual(c.want, al.auto_offset(sunny.key, CAP, AIM, True, sunny.shadows))
        # A daylight view with deep shadows: same overall key, a darker quarter.
        shady = replace(sunny, shadows=0.08)
        c.observe(replace(shady, signature=50), CAP, AIM, True, now=4.0)
        self.assertAlmostEqual(c.shadows, sunny.shadows)   # waits like brightening does
        for i in range(1, 12):
            c.observe(replace(shady, signature=50 + i), CAP, AIM, True, now=4.0 + i * 0.2)
        self.assertAlmostEqual(c.shadows, 0.08)
        lifted = c.want
        self.assertGreater(lifted, al.auto_offset(sunny.key, CAP, AIM, True, sunny.shadows) + 5)
        # Back to the sunny one: the lift comes off at once.
        c.observe(replace(sunny, signature=99), CAP, AIM, True, now=7.0)
        self.assertLess(c.want, lifted)

    def test_keep_this_look_round_trip_with_shadows(self):
        for key, shadows in ((0.45, 0.1), (0.45, 0.3), (0.2, 0.12)):
            off = al.auto_offset(key, CAP, AIM, True, shadows)
            aim = al.aim_for_auto_look(key, off, CAP, None, True, shadows)
            self.assertAlmostEqual(al.auto_offset(key, CAP, aim, True, shadows), off, delta=0.5)
            self.assertEqual(al.aim_for_auto_look(key, off, CAP, AIM, True, shadows), AIM)

    def test_less_glare_in_views_without_shadows(self):
        # A sunlit view whose darkest quarter is already readable: its
        # highlights are lifted less than by the key-based lift.
        key, shadows = AIM * 2 ** (al.AUTO_RAMP_STOPS + 0.2), 0.36
        old = al.auto_offset(key, CAP, AIM)
        new = al.auto_offset(key, CAP, AIM, shadows=shadows)
        self.assertLess(new, old - 10)
        self.assertLess(al.shown_key(0.85, new), al.shown_key(0.85, old))


if __name__ == "__main__":
    unittest.main()
