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
from catvision.autolevels import (ALTERNATE_HOLD_S, BURST_QUICK_S, BURST_S, CEILING_RUSH,
                                  CEILING_STEP, DEAD_ZONE, FADER_HZ, FLASH_RATIO, INTERVAL_QUICK_S,
                                  INTERVAL_S, QUICK_CAPTURE_MS, RETURN_HOLD_S, RUSH_S,
                                  SETTLE_FAST_S, SETTLE_S, STEADY_RATIO, THUMB_H, THUMB_W, VIEW_LAYOUT,
                                  VIEW_SPREAD, VIEW_STEADY_S, Controller, Measurement,
                                  capture_interval, measure,
                                  VIEW_MOVED, target_offset, view_layout, view_moved,
                                  view_spread)

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


def _simulate(scene_at, seconds, start, hz=FADER_HZ, phase=0.07, latency=0.03, cost_ms=None):
    """[(t, offset, want)] per fader step from t=0 (the switch); before
    that it has been in `start` for a few seconds.  ``cost_ms``: what each
    capture costs (see capture_interval)."""
    c = Controller()
    c.offset = c.want = _settled(start)
    frame = 1.0 / hz
    t, next_cap, prev, older, pending, n, out = -3.0, -3.0 + phase, None, None, [], 0, []
    while t < seconds:
        while next_cap <= t:
            name = scene_at(next_cap) if next_cap >= 0 else start
            n += 1
            m = replace(PICS[name], signature=n)     # a live picture: never "frozen"
            pending.append((next_cap + latency, m))
            next_cap += capture_interval(prev, m, cost_ms, older)
            older, prev = prev, m
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

    def test_cheap_captures_measure_twice_as_often(self):
        # A quick capture (a GPU copy) keeps bursting one picture longer, so
        # a new view is seen again VIEW_STEADY_S after it first was.
        quick, slow = QUICK_CAPTURE_MS, QUICK_CAPTURE_MS + 0.1
        day, room = PICS["day"], PICS["room"]
        self.assertEqual(capture_interval(day, day, quick), INTERVAL_QUICK_S)
        self.assertEqual(capture_interval(day, room, quick), BURST_QUICK_S)
        self.assertEqual(capture_interval(room, room, quick, day), BURST_QUICK_S)
        self.assertEqual(capture_interval(room, room, quick, room), INTERVAL_QUICK_S)
        self.assertGreaterEqual(2 * BURST_QUICK_S, VIEW_STEADY_S)
        # A slow one (GDI) keeps the usual pace.
        self.assertEqual(capture_interval(day, day, slow), INTERVAL_S)
        self.assertEqual(capture_interval(day, room, slow), BURST_S)
        self.assertEqual(capture_interval(room, room, slow, day), INTERVAL_S)
        black = Measurement(0.0, 0.0, 0.0, 1.0, 0.0, True)
        self.assertEqual(capture_interval(day, black, quick), INTERVAL_QUICK_S)

    def test_the_pace_follows_what_capturing_costs(self):
        clock = [0.0]
        starts = []
        costs = iter([0.003] * 6 + [0.03] * 6)
        pics = iter(["day", "day", "room", "room", "room", "room"] * 2)

        def capture(_rect):
            starts.append(clock[0])
            clock[0] += next(costs)
            return b"x"

        class Stop:
            def is_set(self):
                return len(starts) >= 12

            def wait(self, seconds):
                clock[0] += seconds

        w = autolevels.Watcher(capture=capture)
        w._rects = {"d": (0, 0, 10, 10)}
        with mock.patch.object(autolevels.time, "perf_counter", lambda: clock[0]), \
                mock.patch.object(autolevels, "measure", lambda *_a: PICS[next(pics)]):
            w._run(Stop())
        gaps = [round(b - a, 6) for a, b in zip(starts, starts[1:])]
        # The first capture's cost isn't known yet: the usual pace.  Then
        # quick, bursting for two pictures after the change.  Once captures
        # are slow (the slower of the last two counts), the usual pace
        # (back to the day and into the room again: one burst each).
        self.assertEqual(gaps, [INTERVAL_S, INTERVAL_QUICK_S, BURST_QUICK_S, BURST_QUICK_S,
                                INTERVAL_QUICK_S, INTERVAL_QUICK_S, BURST_S, INTERVAL_S,
                                BURST_S, INTERVAL_S, INTERVAL_S])

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


# A flashbang in the dark room: the screen goes white, then the room shows
# through again as the white fades (more of the room each picture).
FADE_STEPS = (0.92, 0.8, 0.66, 0.52, 0.4, 0.3, 0.22, 0.15, 0.1, 0.06, 0.03)
PICS["white"] = measure(_frame(lambda fx, fy: 250), THUMB_W)
for _k in FADE_STEPS:
    PICS[f"fade {_k}"] = measure(_frame(lambda fx, fy, k=_k: _room(fx, fy) * (1 - k) + 250 * k),
                                 THUMB_W)


def _flashbang(white_s=0.3, fade_s=1.0):
    def scene_at(t):
        if t < white_s:
            return "white"
        left = (1 - min(1.0, (t - white_s) / fade_s)) ** 2      # how much white is left
        if left < FADE_STEPS[-1] / 2:
            return "room"
        return "fade %s" % min(FADE_STEPS, key=lambda k: abs(k - left))
    return scene_at


class WhiteOutTests(unittest.TestCase):
    def test_a_flashbang_fading_back_is_followed_as_it_goes(self):
        target = _settled("room")
        for hz in (60, 120):
            for phase in (0.01, 0.07, 0.13, 0.19):
                frames = _simulate(_flashbang(), 4.0, "room", hz=hz, phase=phase)
                # It used to wait out CALM_S after the white (as if a light
                # had flashed): not within 2 points until 2.3 s or later.
                near = next(t for t, off, _w in frames if t >= 0.5 and off > target - 2.0)
                self.assertLess(near, 1.8, (hz, phase))
                # Never brighter than the room had, and it only goes up
                # once the white has gone (no pumping on the way).
                self.assertLessEqual(max(off for t, off, _w in frames if t >= 0.5), target + 1e-9)
                lows = [off for t, off, _w in frames if t >= 0.5]
                low = lows.index(min(lows))
                self.assertTrue(all(b >= a - 1e-9 for a, b in zip(lows[low:], lows[low + 1:])),
                                (hz, phase))

    def test_a_white_out_that_snaps_back_still_waits(self):
        # Lightning (or a strobe) whiting the screen out and snapping off
        # may come again: it waits like any other flash.
        # (Back once the last white picture seen is SETTLE_S old, as before.)
        target = _settled("room")
        for scene_at in (lambda t: "white" if t < 0.3 else "room",
                         lambda t: "white" if t % 0.25 < 0.1 and t < 0.6 else "room"):
            for phase in (0.01, 0.07, 0.13, 0.19):
                frames = _simulate(scene_at, 4.0, "room", phase=phase)
                dimmed = next(t for t, _o, want in frames if want < target - 5.0)
                back = next(t for t, _o, want in frames if t > dimmed and want > target - 1.0)
                self.assertGreater(back, SETTLE_S + 0.2, phase)


# Game-like pictures: a HUD drawn over every view, the same in each (a
# minimap on a dark panel top right, a health bar, an ammo counter and a
# crosshair), a vignette darkening the corners, and lights that reach only
# part of the view.
def _hud(level_at):
    def at(fx, fy):
        if fx > 0.78 and fy < 0.26:
            edge = fx < 0.8 or fx > 0.96 or fy < 0.04 or fy > 0.24
            return 230 if edge else 70                   # minimap
        if fx < 0.33 and 0.88 < fy < 0.93:
            return 235                                   # health bar
        if fx > 0.85 and fy > 0.84 and int(fx * 96 + fy * 54) % 2:
            return 245                                   # ammo counter
        if (abs(fx - 0.5) < 0.035 and abs(fy - 0.5) < 0.01) or (
                abs(fx - 0.5) < 0.006 and abs(fy - 0.5) < 0.06):
            return 255                                   # crosshair
        return level_at(fx, fy)
    return at


def _vignette(level_at):
    def at(fx, fy):
        dx, dy = fx - 0.5, fy - 0.5
        return level_at(fx, fy) * (1 - 0.45 * min(1.0, (dx * dx + dy * dy) / 0.5))
    return at


def _lit_part(cx, cy, radius, gain):
    """The room with a light reaching only part of it (around cx, cy)."""
    def at(fx, fy):
        d = ((fx - cx) ** 2 * 2.5 + (fy - cy) ** 2) ** 0.5
        v = _room(fx, fy)
        return v * (1 + gain * (1 - d / radius)) + 30 * (1 - d / radius) if d < radius else v
    return at


for _name, _fn in (("day", _day), ("room", _room), ("cave", _cave), ("lit", _lit_room)):
    PICS["hud " + _name] = measure(_frame(_hud(_vignette(_fn))), THUMB_W)
PICS["centre lit"] = measure(_frame(_lit_part(0.5, 0.55, 0.4, 9)), THUMB_W)   # a muzzle flash
PICS["corner lit"] = measure(_frame(_lit_part(0.08, 0.12, 0.5, 8)), THUMB_W)  # a lamp in a corner
PICS["hud white"] = measure(_frame(_hud(_vignette(lambda fx, fy: 250))), THUMB_W)
for _k in FADE_STEPS:
    PICS[f"hud fade {_k}"] = measure(_frame(_hud(_vignette(
        lambda fx, fy, k=_k: _room(fx, fy) * (1 - k) + 250 * k))), THUMB_W)


class GameLikePictureTests(unittest.TestCase):
    def test_a_light_reaching_part_of_the_view_is_not_a_new_view(self):
        # A muzzle flash lighting the middle, or a lamp in a corner, changes
        # those parts unevenly and changes the layout, like a turn would;
        # but it leaves the rest of the room as it was.
        for lit in ("centre lit", "corner lit"):
            self.assertGreater(view_spread(PICS[lit], PICS["room"]), VIEW_SPREAD, lit)
            self.assertLess(view_layout(PICS[lit], PICS["room"]), VIEW_LAYOUT, lit)
            self.assertLess(view_moved(PICS[lit], PICS["room"]), VIEW_MOVED, lit)
        # Turning changes nearly every part, even with a HUD that is the
        # same in every view.
        for a, b in (("day", "room"), ("day", "cave"),
                     ("hud day", "hud room"), ("hud day", "hud cave")):
            self.assertGreaterEqual(view_moved(PICS[a], PICS[b]), VIEW_MOVED, (a, b))
            self.assertGreater(view_spread(PICS[a], PICS[b]), VIEW_SPREAD, (a, b))
            self.assertLess(view_layout(PICS[a], PICS[b]), VIEW_LAYOUT, (a, b))
        # A light going off with the HUD on still keeps the layout.
        self.assertGreaterEqual(view_layout(PICS["hud lit"], PICS["hud room"]), VIEW_LAYOUT)

    def test_a_light_reaching_part_of_the_view_going_off_still_waits(self):
        # Settled in the room lit by a light in the middle (or a corner):
        # it going off may be a flickering light (or the next shot), so it
        # waits as before.  It used to be followed at once, as a turn.
        for lit in ("centre lit", "corner lit"):
            start = _settled(lit)
            for phase in (0.01, 0.07, 0.13, 0.19):
                frames = _simulate(lambda t: "room", 3.0, lit, phase=phase)
                early = [want for t, _o, want in frames if t < SETTLE_FAST_S - INTERVAL_S]
                self.assertTrue(all(abs(w - start) < 1e-9 for w in early), (lit, phase))
                self.assertAlmostEqual(frames[-1][2], _settled("room"),
                                       delta=STRENGTH * DEAD_ZONE)

    def test_a_turn_with_a_hud_on_screen_is_followed_at_once(self):
        target = _settled("hud room")
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "hud room", 2.0, "hud day", phase=phase)
            near = next(t for t, off, _w in frames if off > target - 2.0)
            self.assertLess(near, SETTLE_FAST_S, phase)

    def test_a_light_going_off_with_a_hud_on_screen_still_waits(self):
        start = _settled("hud lit")
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "hud room", 3.0, "hud lit", phase=phase)
            early = [want for t, _o, want in frames if t < SETTLE_FAST_S - INTERVAL_S]
            self.assertTrue(all(abs(w - start) < 1e-9 for w in early), phase)

    def test_a_flashbang_under_a_hud_is_followed_as_it_goes(self):
        # The HUD's dark minimap panel and the vignette's corners stay
        # darker than the white, so the white-out has a layout of its own:
        # it used to wait out CALM_S (not within 2 points until 2.3 s).
        target = _settled("hud room")

        def scene_at(t):
            name = _flashbang()(t)
            return name if name == "room" else "hud " + name
        for hz in (60, 120):
            for phase in (0.01, 0.07, 0.13, 0.19):
                frames = _simulate(lambda t: "hud room" if scene_at(t) == "room" else scene_at(t),
                                   4.0, "hud room", hz=hz, phase=phase)
                near = next(t for t, off, _w in frames if t >= 0.5 and off > target - 2.0)
                self.assertLess(near, 1.8, (hz, phase))
                self.assertLessEqual(max(off for t, off, _w in frames if t >= 0.5),
                                     target + 1e-9)

    def test_lightning_under_a_hud_still_waits(self):
        target = _settled("hud room")
        for phase in (0.01, 0.07, 0.13, 0.19):
            frames = _simulate(lambda t: "hud white" if t < 0.3 else "hud room", 4.0, "hud room",
                               phase=phase)
            dimmed = next(t for t, _o, want in frames if want < target - 5.0)
            back = next(t for t, _o, want in frames if t > dimmed and want > target - 1.0)
            self.assertGreater(back, SETTLE_S + 0.2, phase)


def _blend(a, b, share):
    return lambda fx, fy: (1 - share) * a(fx, fy) + share * b(fx, fy)


for _share in (25, 50, 75):
    PICS[f"day>room {_share}"] = measure(_frame(_blend(_day, _room, _share / 100)), THUMB_W)
    PICS[f"lit>room {_share}"] = measure(_frame(_blend(_lit_room, _room, _share / 100)), THUMB_W)


def _cross_fade(a, b, seconds=0.2):
    """A quick cross-fade from a to b (sprinting into a building)."""
    def scene_at(t):
        share = int(4 * t / seconds)
        if t >= seconds:
            return b
        return f"{a}>{b} {25 * share}" if share > 0 else a
    return scene_at


class QuickCaptureTests(unittest.TestCase):
    """Cheap captures (see QUICK_CAPTURE_MS) measure twice as often: the
    first moment of a quick fade into the dark is then still part of the
    settled view, which mustn't hide the new view from the quick catch-up,
    and nothing that waited before may start to pump."""

    QUICK = 3.0

    def test_a_quick_fade_into_a_darker_view_is_followed_early(self):
        for cost in (self.QUICK, None):
            for phase in (0.0, 0.03, 0.07):
                with self.subTest(cost=cost, phase=phase):
                    frames = _simulate(_cross_fade("day", "room"), 3.0, "day", hz=120,
                                       phase=phase, cost_ms=cost)
                    good = next(t for t, o, _w in frames if o >= _settled("room") - 1.5)
                    self.assertLess(good, 0.8)
                    steps = [b[1] - a[1] for a, b in zip(frames, frames[1:])]
                    self.assertLessEqual(max(steps), CEILING_STEP + 1e-9)

    def test_a_light_fading_off_still_waits(self):
        # The same fade with the layout kept (a light going off) is no new
        # view: at the quick pace too it is not followed early, only
        # estimated halfway (it got darker and darker, see DARKENING_COUNT)
        # as at the usual pace, and nothing pumps afterwards.
        for phase in (0.0, 0.03, 0.07):
            with self.subTest(phase=phase):
                frames = _simulate(_cross_fade("lit", "room"), 3.0, "lit", hz=120,
                                   phase=phase, cost_ms=self.QUICK)
                early = [w for t, _o, w in frames if t < SETTLE_FAST_S - INTERVAL_QUICK_S]
                self.assertLess(max(early), _settled("room") - 5.0)
                self.assertEqual(_rises(frames, since=SETTLE_S + 0.5), 0)
                self.assertLessEqual(max(w for _t, _o, w in frames), _settled("room") + 1e-9)

    def test_no_new_pumping_at_the_quick_pace(self):
        for a, b, stay_a, stay_b in (("room", "lit", 0.3, 0.3), ("room", "window", 0.35, 0.35),
                                     ("room", "day", 0.15, 0.6)):
            with self.subTest(a=a, b=b):
                frames = _simulate(_alternate(a, b, stay_a, stay_b), 8.0, a, hz=120,
                                   cost_ms=self.QUICK)
                slow = _simulate(_alternate(a, b, stay_a, stay_b), 8.0, a, hz=120)
                self.assertLessEqual(_rises(frames, since=1.0), _rises(slow, since=1.0) + 1)


class FlashAtTheQuickPaceTests(unittest.TestCase):
    """At the quick capture pace a brighter picture counts only once it is
    seen again FLASH_CONFIRM_S later (see FLASH_CONFIRM_S): lightning and
    muzzle flashes, caught twice as often, mustn't dim stormy nights more
    than at the usual pace, while a flash that lasts still dims at once."""

    @staticmethod
    def _feed(c, pictures, gap, start=0.0, ats=None):
        """Observe pictures `gap` seconds apart (capture times ``ats``)."""
        for i, name in enumerate(pictures):
            m = replace(PICS[name], signature=id(c) + i + int(start * 1000) * 100)
            if ats is not None:
                m = replace(m, at=ats[i])
            c.observe(m, STRENGTH, AIM, now=start + i * gap)
        return start + len(pictures) * gap

    def test_a_flash_gone_by_the_next_picture_is_not_counted(self):
        for flash in ("lit", "day", "white", "brighter room"):
            with self.subTest(flash=flash):
                quick = Controller()
                quick.offset = quick.want = _settled("room")
                t = self._feed(quick, ["room"] * 20, INTERVAL_QUICK_S)
                t = self._feed(quick, [flash], INTERVAL_QUICK_S, t)
                self.assertEqual(quick.want, _settled("room"))
                self._feed(quick, ["room"] * 5, autolevels.FLASH_AGAIN_S, t)
                self.assertEqual(quick.want, _settled("room"))
                self.assertFalse(quick.rush)
                # At the usual pace one picture still counts, as before.
                slow = Controller()
                slow.offset = slow.want = _settled("room")
                t = self._feed(slow, ["room"] * 10, INTERVAL_S)
                self._feed(slow, [flash], INTERVAL_S, t)
                self.assertLess(slow.want, _settled("room") - 1.0)

    def test_a_flash_that_lasts_still_dims_at_once(self):
        c = Controller()
        c.offset = c.want = _settled("room")
        t = self._feed(c, ["room"] * 20, INTERVAL_QUICK_S)
        t = self._feed(c, ["day"], autolevels.FLASH_AGAIN_S, t)
        self.assertEqual(c.want, _settled("room"))
        # A burst picture sooner than FLASH_CONFIRM_S doesn't confirm it...
        t = self._feed(c, ["day"], autolevels.FLASH_AGAIN_S, t - autolevels.FLASH_AGAIN_S
                       + BURST_QUICK_S)
        self.assertEqual(c.want, _settled("room"))
        # ...one FLASH_CONFIRM_S or more after the first does, as a flash.
        self._feed(c, ["day"], INTERVAL_QUICK_S, t)
        self.assertAlmostEqual(c.want, _settled("day"), delta=1.0)
        self.assertTrue(c.rush)

    def test_a_strokes_afterglow_starts_again(self):
        # A stroke (the day, 9.6x) and then its afterglow (the lit room,
        # 6.4x but much dimmer than the stroke): the stroke is not counted
        # for the afterglow; the afterglow, gone by the next picture, isn't
        # either.  Had it stayed, it would count (not the stroke).
        for stays in (False, True):
            with self.subTest(stays=stays):
                c = Controller()
                c.offset = c.want = _settled("room")
                t = self._feed(c, ["room"] * 20, INTERVAL_QUICK_S)
                after = ["lit", "lit"] if stays else ["lit", "room"]
                self._feed(c, ["white"] + after, autolevels.FLASH_AGAIN_S, t)
                if stays:
                    self.assertAlmostEqual(c.want, _settled("lit"), delta=1.0)
                else:
                    self.assertEqual(c.want, _settled("room"))

    def test_capture_times_decide_how_far_apart(self):
        # The pictures' capture times count, not when the window saw them.
        for apart, counted in ((BURST_QUICK_S, False), (autolevels.FLASH_AGAIN_S, True)):
            with self.subTest(apart=apart):
                c = Controller()
                c.offset = c.want = _settled("room")
                ats = [100.0 + i * INTERVAL_QUICK_S for i in range(20)]
                t = self._feed(c, ["room"] * 20, INTERVAL_QUICK_S, ats=ats)
                ats = [ats[-1] + INTERVAL_QUICK_S, ats[-1] + INTERVAL_QUICK_S + apart]
                # (seen 0.1 s apart either way: the window checks now and then)
                self._feed(c, ["day", "day"], INTERVAL_QUICK_S, t, ats=ats)
                self.assertEqual(c.want < _settled("room") - 1.0, counted)

    def test_capture_looks_again_soon_after_a_brightening(self):
        quick = QUICK_CAPTURE_MS
        room, day = PICS["room"], PICS["day"]
        self.assertEqual(capture_interval(room, day, quick), autolevels.FLASH_AGAIN_S)
        self.assertEqual(capture_interval(day, room, quick), BURST_QUICK_S)
        self.assertEqual(capture_interval(room, day, quick + 5.0), BURST_S)
        self.assertGreater(autolevels.FLASH_AGAIN_S, autolevels.FLASH_CONFIRM_S)
        self.assertGreater(autolevels.FLASH_CONFIRM_S, BURST_QUICK_S)
        self.assertGreater(INTERVAL_S, autolevels.QUICK_GAP_S)
        self.assertGreater(autolevels.QUICK_GAP_S, INTERVAL_QUICK_S)

    def test_storms_dim_no_more_than_at_the_usual_pace(self):
        strokes = [0.6, 0.7, 2.3, 4.1, 4.25, 6.0, 7.2]

        def storm(stroke_s):
            return lambda t: "lit" if any(0 <= t - s < stroke_s for s in strokes) else "room"

        for stroke_s in (0.05, 0.08):
            dimmed = {}
            for cost in (3.0, None):
                total = 0.0
                for phase in (0.0, 0.03, 0.07, 0.11, 0.16):
                    frames = _simulate(storm(stroke_s), 8.0, "room", hz=120, phase=phase,
                                       cost_ms=cost)
                    total += sum(1 / 120 for _t, o, _w in frames if o < _settled("room") - 1.5)
                dimmed[cost] = total
            with self.subTest(stroke_s=stroke_s):
                self.assertLessEqual(dimmed[3.0], dimmed[None] * 0.75)

    def test_a_lasting_flash_dims_about_as_soon_as_at_the_usual_pace(self):
        firsts = {}
        for cost in (3.0, None):
            firsts[cost] = []
            for phase in (0.0, 0.03, 0.07, 0.11, 0.16):
                frames = _simulate(lambda t: "day", 2.0, "room", hz=120, phase=phase,
                                   latency=0.003 if cost else 0.03, cost_ms=cost)
                firsts[cost].append(next(t for t, o, _w in frames if o < _settled("room") - 1.0))
        self.assertLessEqual(max(firsts[3.0]), max(firsts[None]))
        self.assertLessEqual(sum(firsts[3.0]), sum(firsts[None]) + 0.01 * len(firsts[None]))

    def test_the_quick_pace_does_not_flap(self):
        self.assertTrue(autolevels.quick_pace(QUICK_CAPTURE_MS, False))
        self.assertFalse(autolevels.quick_pace(QUICK_CAPTURE_MS + 1.0, False))
        self.assertTrue(autolevels.quick_pace(QUICK_CAPTURE_MS + 1.0, True))
        self.assertFalse(autolevels.quick_pace(autolevels.QUICK_LEAVE_MS + 0.1, True))
        # Two displays costing about QUICK_CAPTURE_MS between them: once
        # quick, it stays quick instead of switching pace every picture.
        clock = [0.0]
        starts = []
        costs = iter([0.007] * 3 + [0.009, 0.007] * 4 + [0.012] * 3)

        def capture(_rect):
            starts.append(clock[0])
            clock[0] += next(costs)
            return b"x"

        class Stop:
            def is_set(self):
                return len(starts) >= 14

            def wait(self, seconds):
                clock[0] += seconds

        w = autolevels.Watcher(capture=capture)
        w._rects = {"d": (0, 0, 10, 10)}
        with mock.patch.object(autolevels.time, "perf_counter", lambda: clock[0]), \
                mock.patch.object(autolevels, "measure", lambda *_a: PICS["room"]):
            w._run(Stop())
        gaps = [round(b - a, 6) for a, b in zip(starts, starts[1:])]
        self.assertEqual(gaps, [INTERVAL_S] + [INTERVAL_QUICK_S] * 10 + [INTERVAL_S] * 2)


class TurnRoundTests(unittest.TestCase):
    def test_a_goal_dropping_mid_rise_stops_the_rise_promptly(self):
        # Brightening quickly for a dark room when a light comes up (less
        # than a flash): the goal drops just below the rising offset.  It
        # used to carry on up for about a tenth of a second (+2.2 points at
        # 120 Hz, +2.6 at 60) before turning back.
        for hz in (60, 120):
            c = Controller()
            c.offset = c.want = _settled("day")
            c.strength, c.want, c._view_rise = STRENGTH, _settled("room"), True
            while c.velocity < 60.0:
                c.advance(1.0 / hz)
            before, last_step = c.offset, c.velocity / hz
            c.want = c.offset - 1.0
            path = [c.offset]
            for _ in range(int(1.5 * hz)):
                path.append(c.advance(1.0 / hz))
            self.assertLess(max(path) - before, 0.8, hz)
            steps = [b - a for a, b in zip(path, path[1:])]
            # No step: each move is smaller than the one before it while
            # it slows, and it never goes past the new goal.
            self.assertLessEqual(steps[0], last_step + 1e-9, hz)
            self.assertTrue(all(abs(s) <= CEILING_STEP for s in steps), hz)
            self.assertGreaterEqual(min(path), c.want - 1e-9, hz)
            self.assertAlmostEqual(path[-1], c.want, delta=0.05)


if __name__ == "__main__":
    unittest.main()
