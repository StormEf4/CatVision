"""Auto levels (beta): turning or running into the dark (or back out toward
the light), told apart from a light going off (or on) by how unevenly the
parts of the picture change and by whether its layout (where the bright and
dark parts are) changes; and coming back to a settled dark view after a
quick look at a brighter one.

Small game-like pictures (96x54, like the real thumbnail) are measured once;
the simulation captures them on the Watcher's beat (capture_interval), each
reaching the controller a little later, and moves the fader 60 (or 120)
times a second.
"""

import threading
import unittest
from dataclasses import replace
from unittest import mock

from catvision import autolevels
from catvision.autolevels import (ALTERNATE_HOLD_S, BURST_S, CEILING_RUSH, CEILING_STEP, DEAD_ZONE,
                                  FADER_HZ, FLASH_RATIO, INTERVAL_S, RETURN_HOLD_S, RUSH_S,
                                  SETTLE_FAST_S, STEADY_RATIO, THUMB_H, THUMB_W, VIEW_LAYOUT,
                                  VIEW_SPREAD, Controller, Measurement, capture_interval, measure,
                                  target_offset, view_layout, view_spread)

STRENGTH, AIM = 30.0, 0.12


def _frame(level_at, w=THUMB_W, h=THUMB_H) -> bytes:
    out = bytearray()
    for y in range(h):
        for x in range(w):
            v = max(0, min(255, int(level_at(x / w, y / h))))
            out += bytes((v, v, v, 255))
    return bytes(out)


def _day(fx, fy):
    if fy < 0.35:
        return 215                                   # sky
    if 0.55 < fx < 0.7:
        return 45                                    # a building's shadow
    return 110 + 40 * (1 - fy)


def _day_turned(fx, fy):                             # daylight, looking another way
    if fy < 0.5:
        return 215
    if fx < 0.3:
        return 40
    return 110 + 40 * (1 - fy)


def _room(fx, fy):
    if 0.15 < fx < 0.22 and 0.2 < fy < 0.45:
        return 170                                   # a lamp
    if 0.6 < fx < 0.75 and 0.3 < fy < 0.9:
        return 30                                    # a dim doorway
    return 9 + 8 * fy


def _lit_room(fx, fy):                               # the same room, a light on
    if 0.15 < fx < 0.22 and 0.2 < fy < 0.45:
        return 240
    return 70 + 30 * fy


def _cave(fx, fy):
    if 0.45 < fx < 0.5 and 0.4 < fy < 0.5:
        return 60                                    # a torch far away
    return 4 + 4 * fy


def _window_room(fx, fy):                            # the room, a daylit window in the middle
    if 0.25 <= fx < 0.75 and 0.08 <= fy < 0.85:
        return _day(fx, fy)
    return _room(fx, fy)


def _side_window(room):                              # a daylit window down the left side
    def level_at(fx, fy):
        return _day(fx, fy) if fx < 0.2 else room(fx, fy)
    return level_at


def _turning_out(fx, fy):                            # mid-turn from the room toward daylight
    return _day(fx, fy) if fx < 0.4 else _room(fx, fy)


def _brighter_room(fx, fy):                          # the room, a lamp coming on (1.6x)
    return 1.6 * _room(fx, fy)


def _cave_mouth(opened):                             # walking out: daylight fills more of the middle
    def level_at(fx, fy):
        inside = abs(fx - 0.5) < opened / 2 and abs(fy - 0.5) < opened / 2
        return _day(fx, fy) if inside else _cave(fx, fy)
    return level_at


PICS = {name: measure(_frame(fn), THUMB_W)
        for name, fn in (("day", _day), ("turned", _day_turned), ("room", _room), ("lit", _lit_room),
                         ("cave", _cave), ("window", _window_room),
                         ("lit side", _side_window(_lit_room)), ("side", _side_window(_room)),
                         ("turning out", _turning_out), ("brighter room", _brighter_room),
                         ("mouth 20", _cave_mouth(0.2)), ("mouth 40", _cave_mouth(0.4)),
                         ("mouth 60", _cave_mouth(0.6)))}


def _settled(name):
    return target_offset(PICS[name].key, STRENGTH, AIM)


def _simulate(scene_at, seconds, start, hz=FADER_HZ, phase=0.07, latency=0.03):
    """[(t, offset, want)] per fader step from t=0 (the switch); before
    that it has been in `start` for a few seconds."""
    c = Controller()
    c.offset = c.want = _settled(start)
    frame = 1.0 / hz
    t, next_cap, prev, pending, n, out = -3.0, -3.0 + phase, None, [], 0, []
    while t < seconds:
        while next_cap <= t:
            name = scene_at(next_cap) if next_cap >= 0 else start
            n += 1
            m = replace(PICS[name], signature=n)     # a live picture: never "frozen"
            pending.append((next_cap + latency, m))
            next_cap += capture_interval(prev, m)
            prev = m
        while pending and pending[0][0] <= t:
            c.observe(pending.pop(0)[1], STRENGTH, AIM)
        c.advance(frame)
        if t >= 0:
            out.append((t, c.offset, c.want))
        t += frame
    return out


def _rises(frames, since=0.0, until=None):
    """How many times the goal went up (by more than half a point)."""
    count, last = 0, None
    for t, _off, want in frames:
        if t < since or (until is not None and t > until):
            continue
        if last is not None and want > last + 0.5:
            count += 1
        last = want
    return count


def _alternate(a, b, stay_a, stay_b):
    period = stay_a + stay_b

    def scene_at(t):
        return a if t % period < stay_a else b
    return scene_at


class RegionTests(unittest.TestCase):
    def test_a_real_capture_has_its_parts_measured(self):
        m = PICS["room"]
        self.assertEqual(len(m.regions), autolevels.REGION_COLS * autolevels.REGION_ROWS)
        # Without the picture's width there is no layout: nothing is known.
        self.assertEqual(measure(bytes([40, 40, 40, 255]) * 400).regions, ())
        self.assertIsNone(view_spread(measure(bytes([40, 40, 40, 255]) * 400), m))

    def test_a_light_changing_is_the_same_view(self):
        dim = measure(_frame(lambda fx, fy: 0.5 * _day(fx, fy)), THUMB_W)
        self.assertLess(view_spread(PICS["day"], dim), 0.05)        # a cloud
        self.assertLess(view_spread(PICS["lit"], PICS["room"]), VIEW_SPREAD)

    def test_turning_somewhere_else_is_a_new_view(self):
        for a, b in (("day", "room"), ("day", "cave"), ("window", "room")):
            self.assertGreater(view_spread(PICS[a], PICS[b]), VIEW_SPREAD, (a, b))
            self.assertLess(view_layout(PICS[a], PICS[b]), VIEW_LAYOUT, (a, b))

    def test_a_light_going_off_keeps_the_layout(self):
        # With a daylit window it doesn't light, the parts change unevenly,
        # but where the bright and dark parts are stays the same.
        self.assertGreater(view_spread(PICS["lit side"], PICS["side"]), VIEW_SPREAD)
        for a, b in (("lit side", "side"), ("lit", "room")):
            self.assertGreaterEqual(view_layout(PICS[a], PICS[b]), VIEW_LAYOUT, (a, b))
        flat = measure(_frame(lambda fx, fy: 30), THUMB_W)
        self.assertEqual(view_layout(flat, PICS["room"]), 1.0)     # no layout: alike
        self.assertIsNone(view_layout(measure(bytes([40, 40, 40, 255]) * 400), flat))


class CaptureBeatTests(unittest.TestCase):
    def test_measures_twice_as_often_while_the_brightness_changes(self):
        self.assertEqual(capture_interval(None, PICS["day"]), INTERVAL_S)
        self.assertEqual(capture_interval(PICS["day"], PICS["day"]), INTERVAL_S)
        self.assertEqual(capture_interval(PICS["day"], PICS["room"]), BURST_S)
        self.assertEqual(capture_interval(PICS["room"], PICS["day"]), BURST_S)
        black = Measurement(0.0, 0.0, 0.0, 1.0, 0.0, True)
        self.assertEqual(capture_interval(PICS["day"], black), INTERVAL_S)

    def test_the_capture_time_does_not_add_to_the_gap(self):
        # A fake clock: each capture takes 40 ms, and the waits make up the
        # rest of the beat (old: 0.2 s + capture time = about 4 a second).
        clock = [0.0]
        starts, waits = [], []
        pics = iter(["day", "day", "room", "room", "room", "room"])

        def capture(_rect):
            starts.append(clock[0])
            clock[0] += 0.04
            return b"x"

        class Stop:
            def is_set(self):
                return len(starts) >= 6

            def wait(self, seconds):
                waits.append(seconds)
                clock[0] += seconds

        w = autolevels.Watcher(capture=capture)
        w._rects = {"d": (0, 0, 10, 10)}
        with mock.patch.object(autolevels.time, "perf_counter", lambda: clock[0]), \
                mock.patch.object(autolevels, "measure", lambda *_a: PICS[next(pics)]):
            w._run(Stop())
        gaps = [round(b - a, 6) for a, b in zip(starts, starts[1:])]
        # Steady, then the room appears (a big change: burst), then steady.
        self.assertEqual(gaps, [INTERVAL_S, INTERVAL_S, BURST_S, INTERVAL_S, INTERVAL_S])

    def test_each_picture_is_stamped_with_when_it_was_taken(self):
        clock = [5.0]

        def capture(_rect):
            clock[0] += 0.03
            return _frame(_room)

        w = autolevels.Watcher(capture=capture)
        w._rects = {"a": (0, 0, 10, 10), "b": (10, 0, 20, 10)}
        with mock.patch.object(autolevels.time, "perf_counter", lambda: clock[0]):
            out = w.measure_once()
        self.assertAlmostEqual(out["a"].at, 5.0)
        self.assertAlmostEqual(out["b"].at, 5.03)
        self.assertEqual(replace(out["a"], at=0.0), PICS["room"])

    def test_a_slow_capture_never_runs_back_to_back(self):
        clock = [0.0]
        starts = []

        def capture(_rect):
            starts.append(clock[0])
            clock[0] += 0.3                 # slower than the beat
            return b"x"

        class Stop:
            def is_set(self):
                return len(starts) >= 4

            def wait(self, seconds):
                clock[0] += seconds

        w = autolevels.Watcher(capture=capture)
        w._rects = {"d": (0, 0, 10, 10)}
        with mock.patch.object(autolevels.time, "perf_counter", lambda: clock[0]), \
                mock.patch.object(autolevels, "measure", lambda *_a: PICS["day"]):
            w._run(Stop())
        for a, b in zip(starts, starts[1:]):
            self.assertGreaterEqual(b - a, 0.3 + INTERVAL_S - 1e-9)

    def test_the_real_thread_still_stops(self):
        done = threading.Event()

        def capture(_rect):
            done.set()
            return None

        w = autolevels.Watcher(capture=capture)
        w.watch({"d": (0, 0, 10, 10)})
        self.assertTrue(done.wait(2.0))
        w.stop()
        self.assertFalse(w.running)


class TurningIntoTheDarkTests(unittest.TestCase):
    def test_turning_into_a_dark_room_is_followed_at_once(self):
        target = _settled("room")
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "room", 3.0, "day", phase=phase)
            moved = next(t for t, _o, want in frames if want > target - 1.0)
            near = next(t for t, off, _w in frames if off > target - 2.0)
            # It used to wait for SETTLE_FAST_S (goal at about 1.0 s, there
            # at about 1.6 s).
            self.assertLess(moved, 0.45, phase)
            self.assertLess(near, 0.95, phase)
            self.assertAlmostEqual(frames[-1][1], target, delta=STRENGTH * DEAD_ZONE)

    def test_a_fast_screen_catches_up_with_the_dark_quicker_still(self):
        # At 120 updates a second (a 240 Hz screen) the quick fade may run
        # up to MAX_RISE_VIEW, still in steps of CEILING_STEP at most: from
        # the goal moving to within 2 points took 0.56 s into the room and
        # 0.64 s into the cave at the old RISE_VIEW_S 0.25 / 110 a second.
        for name, limit in (("room", 0.4), ("cave", 0.5)):
            target = _settled(name)
            for phase in (0.01, 0.07, 0.13, 0.19):
                frames = _simulate(lambda t, n=name: n, 3.0, "day", hz=120, phase=phase)
                moved = next(t for t, _o, want in frames if want > target - 1.0)
                near = next(t for t, off, _w in frames if off > target - 2.0)
                self.assertLess(near - moved, limit, (name, phase))
                steps = [b[1] - a[1] for a, b in zip(frames, frames[1:])]
                self.assertLessEqual(max(steps), CEILING_STEP + 1e-9, (name, phase))
                # Never past the goal, and it comes to rest on it.
                self.assertTrue(all(off <= want + 1e-9 for _t, off, want in frames))
                self.assertLessEqual(max(off for _t, off, _w in frames), target + 1e-9)
                self.assertAlmostEqual(frames[-1][1], target, delta=STRENGTH * DEAD_ZONE)

    def test_looking_around_in_daylight_first_still_counts_as_settled(self):
        # The camera keeps moving outside (every picture a different view,
        # the same brightness): that is still a settled bright scene.
        self.assertGreater(view_spread(PICS["day"], PICS["turned"]), VIEW_SPREAD)
        target = _settled("room")

        def scene_at(t):
            if t < 3.0:
                return "day" if int(t / 0.2) % 2 else "turned"
            return "room"
        for phase in (0.03, 0.11, 0.17):
            frames = _simulate(scene_at, 5.0, "day", phase=phase)
            moved = next(t for t, _o, want in frames if t >= 3.0 and want > target - 1.0)
            self.assertLess(moved - 3.0, 0.45, phase)

    def test_running_into_a_cave_gets_all_the_way(self):
        target = _settled("cave")
        for phase in (0.03, 0.11, 0.17):
            frames = _simulate(lambda t: "cave", 3.0, "day", phase=phase)
            self.assertLess(next(t for t, off, _w in frames if off > target - 3.0), 1.1, phase)
            self.assertAlmostEqual(frames[-1][1], target, delta=STRENGTH * DEAD_ZONE)
            # The goal is never passed.
            self.assertTrue(all(off <= want + 1e-9 for _t, off, want in frames))

    def test_a_resting_clock_does_not_hold_back_the_quick_path(self):
        # While the fader rests its clock moves only every IDLE_S, so two
        # pictures a tenth of a second apart can arrive on the same clock
        # time: when they were taken decides.
        for stamped in (True, False):
            c = Controller()
            c.offset = c.want = _settled("day")
            for n in range(15):
                c.observe(replace(PICS["day"], signature=n + 1, at=n * 0.2 if stamped else 0.0),
                          STRENGTH, AIM)
                c.advance(0.2)
            for n in range(2):
                at = 3.0 + n * BURST_S if stamped else 0.0
                c.observe(replace(PICS["room"], signature=100 + n, at=at), STRENGTH, AIM)
            moved = c.want > _settled("room") - 1.0
            self.assertEqual(moved, stamped)

    def test_walking_into_a_cave_gets_the_quick_pace_on_the_way(self):
        # Still getting darker (a halfway goal): once that has kept up for a
        # second step and the view is plainly another one, the quick pace.
        def walk(f):
            return lambda fx, fy: _cave(fx, fy) if fx < f else _day(fx, fy)

        def dimming(k):
            return lambda fx, fy: _lit_room(fx, fy) * k

        for start, pics, quick in (
                (_day, [walk(f / 10) for f in range(1, 11)], True),
                (_lit_room, [dimming(0.8 ** n) for n in range(1, 11)], False)):
            c = Controller()
            first = measure(_frame(start), THUMB_W)
            c.offset = c.want = target_offset(first.key, STRENGTH, AIM)
            for n in range(15):
                c.observe(replace(first, signature=n + 1), STRENGTH, AIM)
                c.advance(0.2)
            seen = []
            for n, fn in enumerate(pics):
                c.observe(replace(measure(_frame(fn), THUMB_W), signature=100 + n), STRENGTH, AIM)
                seen.append((c._estimate, c._view_rise))
                c.advance(0.1)
            self.assertTrue(any(est for est, _vr in seen), quick)
            self.assertEqual(any(est and vr for est, vr in seen), quick)

    def test_a_light_going_off_in_the_same_view_still_waits(self):
        # Not a new view: it may be a flickering light, so it waits as before.
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "room", 3.0, "lit", phase=phase)
            start = _settled("lit")
            # (SETTLE_FAST_S counts from the last lit picture, up to a
            # capture before the switch.)
            early = [want for t, _o, want in frames if t < SETTLE_FAST_S - INTERVAL_S]
            self.assertTrue(all(abs(w - start) < 1e-9 for w in early), phase)
            self.assertAlmostEqual(frames[-1][2], _settled("room"), delta=STRENGTH * DEAD_ZONE)

    def test_an_unevenly_lit_light_going_off_still_waits(self):
        # A lamp-lit room with a daylit window down the side: the parts
        # change unevenly when the lamp fails, but it is the same view (it
        # may be a failing light that comes straight back on), so it waits
        # as before instead of pumping.
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "side", 3.0, "lit side", phase=phase)
            start = _settled("lit side")
            early = [want for t, _o, want in frames if t < SETTLE_FAST_S - INTERVAL_S]
            self.assertTrue(all(abs(w - start) < 1e-9 for w in early), phase)
            self.assertAlmostEqual(frames[-1][2], _settled("side"), delta=STRENGTH * DEAD_ZONE)

    def test_the_quick_fade_stays_below_a_visible_step(self):
        for hz in (60, 120):
            frames = _simulate(lambda t: "cave", 2.0, "day", hz=hz)
            steps = [b[1] - a[1] for a, b in zip(frames, frames[1:])]
            self.assertLessEqual(max(steps), CEILING_STEP + 1e-9, hz)
            if hz == 60:     # 60 Hz: 80 points a second at most, as before
                self.assertLessEqual(max(steps) * hz, 80.0 + 1e-6)
        # A stalled update can't make a bigger step either.
        c = Controller()
        c.offset = c.want = _settled("day")
        c._view_rise, c.want = True, _settled("cave")
        before = c.offset
        c.follow(c.want, STRENGTH, 0.25)
        self.assertLessEqual(c.offset - before, CEILING_STEP + 1e-9)

    def test_a_flash_right_after_stops_the_rise_at_once(self):
        # Turned into the room, then back out into daylight while still
        # brightening: it turns round without first carrying on up.
        frames = _simulate(lambda t: "room" if t < 0.5 else "day", 2.0, "day")
        top = max(off for _t, off, _w in frames)
        # (the last capture of the room, plus latency)
        at_turn = max(off for t, off, _w in frames if t <= 0.5 + 0.13)
        self.assertLessEqual(top, at_turn + 0.6)


class TurningTowardTheLightTests(unittest.TestCase):
    @staticmethod
    def _settled_in(name):
        c = Controller()
        c.offset = c.want = _settled(name)
        for i in range(8):
            c.observe(replace(PICS[name], signature=i + 1), STRENGTH, AIM)
            c.advance(INTERVAL_S)
        return c

    def test_turning_toward_daylight_fades_at_the_flash_pace_at_once(self):
        # Caught halfway through the turn: brighter, but less than a flash,
        # and nothing fading yet.  It is a different view (uneven change,
        # new layout), so the boost comes off at the flash pace from this
        # first picture instead of the normal pace.
        room, turning = PICS["room"], PICS["turning out"]
        self.assertTrue(STEADY_RATIO <= turning.key / room.key < FLASH_RATIO)
        self.assertGreater(view_spread(room, turning), VIEW_SPREAD)
        self.assertLess(view_layout(room, turning), VIEW_LAYOUT)
        c = self._settled_in("room")
        c.observe(replace(turning, signature=99), STRENGTH, AIM)
        self.assertLess(c.want, c.offset)
        self.assertTrue(c.rush)
        self.assertTrue(c._rush_view)

    def test_a_light_coming_on_in_the_same_view_dims_as_before(self):
        # A lamp coming on (1.6x, same layout): the normal pace, as before,
        # so flicker never dims more than it used to...
        c = self._settled_in("room")
        c.observe(replace(PICS["brighter room"], signature=99), STRENGTH, AIM)
        self.assertLess(c.want, c.offset)
        self.assertFalse(c.rush)
        # ...and a light flashing on (a flash, same layout) fades at the
        # flash pace with its gentle start, not the quicker one for a view.
        c = self._settled_in("room")
        c.observe(replace(PICS["lit"], signature=99), STRENGTH, AIM)
        self.assertTrue(c.rush)
        self.assertFalse(c._rush_view)

    def test_the_way_out_fades_quicker_but_never_in_a_visible_step(self):
        def scene_at(t):
            return "turning out" if t < 0.12 else "day"
        target, start = _settled("day"), _settled("room")

        def good_at(frames):     # 90% of the way down
            return next(t for t, off, _w in frames if off < start + 0.9 * (target - start))
        for hz in (60, 120):
            frames = _simulate(scene_at, 1.5, "room", hz=hz, phase=0.05)
            steps = [abs(b[1] - a[1]) for a, b in zip(frames, frames[1:])]
            # The usual speed limit for a fast fade-down: never a bigger step
            # than a flash makes today (2 points an update at 120 Hz).
            self.assertLessEqual(max(steps), CEILING_RUSH / hz + 1e-9, hz)
            self.assertGreaterEqual(min(off for _t, off, _w in frames), target - 1e-6)  # never past
            self.assertAlmostEqual(frames[-1][1], target, delta=0.1)
            with mock.patch.object(autolevels, "RUSH_VIEW_S", RUSH_S):
                gentle = _simulate(scene_at, 1.5, "room", hz=hz, phase=0.05)
            self.assertLess(good_at(frames), good_at(gentle) - 0.03, hz)
            self.assertLess(good_at(frames), 0.3, hz)

    def test_walking_out_of_a_cave_mouth_fades_at_the_flash_pace(self):
        # Walking out, each picture is only a little brighter than the one
        # before and laid out much like it (the first even joins the
        # settled cave stretch), so only the dark view it was settled in
        # shows it is a different view: the rush that starts on the way out
        # runs at the view pace, like a turn toward the light.
        steps = ["mouth 20", "mouth 40", "mouth 60"]
        for a, b in zip(steps, steps[1:]):
            self.assertLess(PICS[b].key / PICS[a].key, FLASH_RATIO)
            self.assertGreaterEqual(view_layout(PICS[a], PICS[b]), VIEW_LAYOUT)  # alike, one to the next
        self.assertLess(view_layout(PICS["cave"], PICS["mouth 60"]), VIEW_LAYOUT)

        def walk_out():
            c = self._settled_in("cave")
            for i, name in enumerate(steps):
                c.observe(replace(PICS[name], signature=50 + i), STRENGTH, AIM)
                if name != steps[-1]:
                    c.advance(0.1)
            return c
        c = walk_out()
        self.assertTrue(c.rush)
        self.assertTrue(c._rush_view)

        def down_by(c, points):     # seconds until the boost is this much lower
            start = c.offset
            for n in range(1, 200):
                c.advance(1 / 120)
                if c.offset <= start - points:
                    return n / 120
            return None
        with mock.patch.object(Controller, "_left_for_brighter_view", return_value=False):
            gentle = walk_out()
        self.assertFalse(gentle._rush_view)       # (what it did before: the gentler start)
        self.assertLess(down_by(c, 8.0), down_by(gentle, 8.0))
        # A light coming on in a settled view is still not a new view.
        c = self._settled_in("room")
        self.assertFalse(c._left_for_brighter_view(c.clock, PICS["brighter room"]))
        self.assertFalse(c._left_for_brighter_view(c.clock, PICS["lit"]))


class NoPumpingTests(unittest.TestCase):
    def test_turning_back_and_forth_brightens_at_most_once(self):
        # Daylight 1.2 s, a dark room 0.6 s, again and again: following the
        # first turn is fine, but not every one after it.
        for phase in (0.03, 0.11, 0.17):
            frames = _simulate(_alternate("day", "room", 1.2, 0.6), 12.0, "day", phase=phase)
            self.assertLessEqual(_rises(frames), 1, phase)
            frames = _simulate(_alternate("room", "day", 0.6, 1.2), 12.0, "room", phase=phase)
            self.assertLessEqual(_rises(frames), 1, phase)

    def test_quick_glances_brighten_at_most_once(self):
        for stay in (0.4, 0.7):
            for phase in (0.03, 0.11, 0.17):
                frames = _simulate(_alternate("day", "room", stay, stay), 10.0, "day", phase=phase)
                self.assertLessEqual(_rises(frames), 1, (stay, phase))

    def test_running_past_windows_never_brightens(self):
        # The dark room with a big bright window going by every half second
        # (a brief bright view, never settled): it dims and holds.
        for phase in (0.03, 0.11, 0.17):
            frames = _simulate(_alternate("room", "window", 0.5, 0.5), 8.0, "room", phase=phase)
            self.assertEqual(_rises(frames), 0, phase)
            self.assertLessEqual(max(off for _t, off, _w in frames), _settled("room") + 1e-9)

    def test_after_the_turning_stops_it_is_quick_again(self):
        def scene_at(t):
            if t < 6.0:
                return "day" if t % 1.8 < 1.2 else "room"
            return "day" if t < 6.0 + ALTERNATE_HOLD_S + 1.5 else "room"
        frames = _simulate(scene_at, 14.0, "day")
        switch = 6.0 + ALTERNATE_HOLD_S + 1.5
        target = _settled("room")
        moved = next(t for t, _o, want in frames if t >= switch and want > target - 1.0)
        self.assertLess(moved - switch, 0.45)

    def test_steady_scenes_never_breathe(self):
        for name in PICS:
            frames = _simulate(lambda t, n=name: n, 6.0, name)
            wants = {round(w, 9) for _t, _o, w in frames}
            self.assertEqual(len(wants), 1, name)


class BackFromAQuickLookTests(unittest.TestCase):
    """Settled in the dark, a quick look at something much brighter and
    different (out of a doorway, a door opened and shut), then back."""

    def _back(self, away, look, start="room", phases=(0.01, 0.07, 0.13, 0.19)):
        """(seconds from the return until the goal is back, frames) per phase."""
        target = _settled(start)
        out = []
        for phase in phases:
            frames = _simulate(lambda t: away if t < look else start, look + 4.0, start,
                               phase=phase)
            moved = next(t for t, _o, want in frames if t >= look and want > target - 1.0)
            out.append((moved - look, frames))
        return out

    def test_glancing_out_and_back_is_followed_quickly(self):
        target = _settled("room")
        for away in ("day", "window"):
            for look in (0.4, 0.6, 0.9):
                for wait, frames in self._back(away, look):
                    # It used to wait about a second after the return (as if
                    # a light had flashed, up to 1.13 s), then fade at the
                    # slower pace (within 2 points after 2.0-3.0 s).
                    self.assertLess(wait, RETURN_HOLD_S + 0.4, (away, look))
                    near = next(t for t, off, _w in frames if t >= look and off > target - 2.0)
                    self.assertLess(near - look, RETURN_HOLD_S + 1.0, (away, look))
                    # Back where it was, never brighter, never past the goal.
                    self.assertAlmostEqual(frames[-1][1], target, delta=STRENGTH * DEAD_ZONE)
                    self.assertLessEqual(max(off for _t, off, _w in frames), target + 1e-9)
                    self.assertTrue(all(off <= want + 1e-9 for t, off, want in frames
                                        if t >= look + wait))

    def test_it_waits_out_the_hold(self):
        # A dark spell shorter than RETURN_HOLD_S (running past windows)
        # is not followed.
        for wait, _frames in self._back("day", 0.6):
            self.assertGreater(wait, RETURN_HOLD_S - 0.05)

    def test_a_light_flashing_on_and_off_still_waits(self):
        # The same room with a light on for a moment keeps its layout: it
        # may be flickering, so it waits as before.
        for wait, _frames in self._back("lit", 0.6):
            self.assertGreater(wait, SETTLE_FAST_S)

    def test_looking_out_again_and_again_brightens_at_most_once(self):
        for stay in (0.7, 0.9):
            for phase in (0.03, 0.11, 0.17):
                frames = _simulate(_alternate("room", "day", stay, stay), 10.0, "room",
                                   phase=phase)
                self.assertLessEqual(_rises(frames), 1, (stay, phase))
                frames = _simulate(_alternate("room", "window", stay, stay), 10.0, "room",
                                   phase=phase)
                self.assertLessEqual(_rises(frames), 1, (stay, phase))


class CaveEntryTests(unittest.TestCase):
    """Flat grey pictures (no parts known): only the old rules apply."""

    def _m(self, level):
        return measure(bytes([level, level, level, 255]) * 400)

    def test_a_halfway_goal_does_not_get_stuck_short(self):
        # Walking slowly into a near-black cave: the halfway goal on the way
        # in was a guess, and near black the noise filter used to hold it,
        # leaving the cave 6-7 points too dark for good.
        target = target_offset(self._m(4).key, STRENGTH, AIM)
        for start, ratio in ((120, 0.8), (60, 0.8), (120, 0.6), (200, 0.6)):
            levels, level = [start] * 10, float(start)
            while level * ratio > 4:
                level *= ratio
                levels.append(int(round(level)))
            levels += [4] * 40
            c = Controller()
            c.offset = c.want = target_offset(self._m(start).key, STRENGTH, AIM)
            for i, level in enumerate(levels):
                c.observe(replace(self._m(level), signature=i + 1), STRENGTH, AIM)
                for _ in range(12):
                    c.advance(1 / 60)
            self.assertAlmostEqual(c.want, target, delta=STRENGTH * DEAD_ZONE,
                                   msg=(start, ratio))


if __name__ == "__main__":
    unittest.main()
