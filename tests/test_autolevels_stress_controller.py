"""Auto levels (beta): stress tests for the controller and fader math.

Thousands of deterministic random scene sequences are fed at five
measurements a second while the controller advances at 60 Hz.  Only whole
picture brightness is ever involved.
"""

import math
import random
import threading
import time
import unittest

from catvision.autolevels import (BLACK_HOLD_S, FADER_HZ, FROZEN_S, INTERVAL_S, LOST_S, MAX_FALL,
                                  MAX_RISE, RISE_S, MAX_RISE_FAST, MAX_RUSH, SLIDER_RANGE, SPEEDS,
                                  SPILL, TARGET_LIMITS, Controller, Fader,
                                  aim_for_current_look, measure, nudge_aim, split_offset,
                                  target_offset)

W, H = 16, 9
DT = 1.0 / FADER_HZ
PER_MEASURE = round(INTERVAL_S * FADER_HZ)       # 12 fader steps per measurement
STEP_LIMIT = 1.4                                 # points per frame outside a rush
RUSH_LIMIT = 4.1                                 # points per frame during a rush
VARIANTS = 6


def _picture(level: int, rng: random.Random, noise: int) -> bytes:
    out = bytearray()
    for _ in range(W * H):
        v = max(0, min(255, level + (rng.randint(-noise, noise) if noise else 0)))
        out += bytes((v, v, v, 255))
    return bytes(out)


class _Pictures:
    """Measurements of grey pictures at every level, a few noisy variants
    each (so the signature changes like a live picture), cached."""

    def __init__(self, seed=1234):
        self.rng = random.Random(seed)
        self.cache = {}

    def get(self, level: int, variant: int, noise: int = 1):
        level = max(0, min(255, int(level)))
        key = (level, variant, noise)
        if key not in self.cache:
            self.cache[key] = measure(_picture(level, self.rng, noise), W)
        return self.cache[key]


PICS = _Pictures()


def _scene(rng: random.Random, seconds: float):
    """A random list of per-measurement entries: a Measurement, or None."""
    out = []
    n_total = int(seconds / INTERVAL_S)
    while len(out) < n_total:
        kind = rng.choice(["steady", "noise", "flicker", "flash", "pan", "black",
                           "lost", "frozen"])
        n = rng.randint(3, 25)
        base = rng.randint(3, 230)
        if kind == "steady":
            out += [PICS.get(base, rng.randrange(VARIANTS)) for _ in range(n)]
        elif kind == "noise":
            amp = rng.randint(1, 3)
            out += [PICS.get(base + rng.randint(-amp, amp), rng.randrange(VARIANTS), amp)
                    for _ in range(n)]
        elif kind == "flicker":
            hi = min(255, base * rng.randint(2, 4))
            period = rng.randint(2, 4)
            out += [PICS.get(hi if i % period == 0 else base, rng.randrange(VARIANTS))
                    for i in range(n)]
        elif kind == "flash":
            out += [PICS.get(base, rng.randrange(VARIANTS)) for _ in range(n)]
            out += [PICS.get(255, rng.randrange(VARIANTS), 0)
                    for _ in range(rng.randint(1, 2))]
        elif kind == "pan":
            end = rng.randint(3, 230)
            out += [PICS.get(round(base + (end - base) * i / max(n - 1, 1)),
                             rng.randrange(VARIANTS)) for i in range(n)]
        elif kind == "black":
            out += [PICS.get(0, 0, 0)] * n
        elif kind == "lost":
            out += [None] * n
        else:  # frozen: exactly the same picture again and again
            m = PICS.get(base, rng.randrange(VARIANTS))
            out += [m] * rng.randint(16, 40)
    return out[:n_total]


def _run(ctrl, entries, strength, target, speed=1.0, dim=True, trace=None):
    """Feed entries at 5/s and advance at 60 Hz.  Returns a list of
    (offset, want, rush_before_step, rush_after_step, velocity) per frame."""
    frames = [] if trace is None else trace
    for m in entries:
        ctrl.observe(m, strength, target, dim)
        for _ in range(PER_MEASURE):
            rush = ctrl.rush
            before = ctrl.offset
            pos = ctrl.advance(DT, speed)
            frames.append((pos, ctrl.want, rush, ctrl.rush, ctrl.velocity, before))
    return frames


def _random_params(rng):
    strength = rng.choice([5.0, 15.0, 30.0, 45.0, 60.0])
    target = rng.uniform(*TARGET_LIMITS)
    speed = SPEEDS[rng.choice(list(SPEEDS))]
    dim = rng.random() < 0.7
    return strength, target, speed, dim


class RandomSceneInvariantTests(unittest.TestCase):
    SEQUENCES = 300

    @classmethod
    def setUpClass(cls):
        rng = random.Random(20261030)
        cls.runs = []
        for _ in range(cls.SEQUENCES):
            strength, target, speed, dim = _random_params(rng)
            entries = _scene(rng, rng.uniform(3.0, 9.0))
            ctrl = Controller()
            frames = _run(ctrl, entries, strength, target, speed, dim)
            cls.runs.append(((strength, target, speed, dim), frames))

    def test_per_frame_step_obeys_per_speed_limit(self):
        for (strength, target, speed, dim), frames in self.runs:
            for pos, want, rush, _r, _v, before in frames:
                step = abs(pos - before)
                vmax = MAX_RUSH if rush else max(MAX_FALL, MAX_RISE_FAST)
                self.assertLessEqual(step, vmax * speed * DT + 1e-6,
                                     (strength, target, speed, dim, before, pos))

    def test_per_frame_step_absolute_limits_at_every_speed(self):
        """The task's comfort limits: <= ~1.4 points per frame outside a
        rush and <= ~4.1 in one, whatever speed is chosen."""
        worst = {"normal": 0.0, "rush": 0.0}
        for (strength, target, speed, dim), frames in self.runs:
            for pos, want, rush, _r, _v, before in frames:
                step = abs(pos - before)
                k = "rush" if rush else "normal"
                worst[k] = max(worst[k], step)
        self.assertLessEqual(worst["normal"], STEP_LIMIT, worst)
        self.assertLessEqual(worst["rush"], RUSH_LIMIT, worst)

    def test_offsets_stay_within_strength(self):
        for (strength, target, speed, dim), frames in self.runs:
            for pos, want, *_ in frames:
                self.assertLessEqual(abs(want), strength + 1e-9)
                self.assertLessEqual(abs(pos), strength + 1e-6)
                if not dim:
                    self.assertGreaterEqual(want, 0.0)
                    self.assertGreaterEqual(pos, -1e-9, "dim=False went negative")

    def test_no_overshoot_past_the_goal(self):
        """While the goal stays the same, the offset never goes past it
        (approached from below it stays below; from above, above)."""
        worst = 0.0
        for (strength, target, speed, dim), frames in self.runs:
            seg_want, side = None, 0
            for pos, want, rush, _r, vel, before in frames:
                if want != seg_want:
                    seg_want = want
                    side = (before > want) - (before < want)
                if side:
                    over = (want - pos) * side      # > 0 means past the goal
                    worst = max(worst, over)
        self.assertLessEqual(worst, 0.05, "overshoot of %.3f points" % worst)

    def test_converges_within_bounded_time(self):
        for (strength, target, speed, dim), frames in self.runs:
            seg_start, seg_want = 0, None
            for i, (pos, want, *_rest) in enumerate(frames):
                if want != seg_want:
                    seg_start, seg_want = i, want
                    start_gap = abs(want - frames[i][5])
                    # the slowest spring (RISE_S) needs ~10 time constants
                    # to get within 0.01 of the goal, plus the speed limit
                    bound = (start_gap / (MAX_RISE * speed) + 10 * RISE_S / (2 * speed)
                             + 1.0) * FADER_HZ
                if i - seg_start > bound:
                    self.assertLess(abs(pos - want), 0.01,
                                    (strength, speed, start_gap, i - seg_start))


class SceneScenarioTests(unittest.TestCase):

    def _settle(self, ctrl, level, strength, target, seconds=8.0, speed=1.0, dim=True,
                rng=None, noise=1):
        rng = rng or random.Random(level)
        entries = [PICS.get(level, rng.randrange(VARIANTS), noise)
                   for _ in range(int(seconds / INTERVAL_S))]
        return _run(ctrl, entries, strength, target, speed, dim)

    def test_no_breathing_under_small_pixel_noise(self):
        rng = random.Random(7)
        for _ in range(150):
            level = rng.randint(8, 230)
            strength = rng.choice([15.0, 30.0, 60.0])
            target = rng.uniform(*TARGET_LIMITS)
            amp = rng.randint(1, 3)
            ctrl = Controller()
            self._settle(ctrl, level, strength, target, 3.0, rng=rng, noise=amp)
            goal = ctrl.want
            entries = [PICS.get(level, rng.randrange(VARIANTS), amp) for _ in range(60)]
            frames = _run(ctrl, entries, strength, target)
            changes = sum(1 for a, b in zip(frames, frames[1:]) if a[1] != b[1])
            self.assertEqual(changes, 0, (level, strength, amp))
            self.assertEqual(ctrl.want, goal)

    def test_no_breathing_under_one_level_global_jitter(self):
        """The whole picture wobbling one level up and down (compression,
        dithering) must not keep changing the goal, even in dark scenes."""
        rng = random.Random(8)
        failures = []
        for level in range(4, 240, 3):
            for strength in (30.0, 60.0):
                ctrl = Controller()
                target = 0.12
                entries = [PICS.get(level + rng.randint(-1, 1), rng.randrange(VARIANTS))
                           for _ in range(15)]
                _run(ctrl, entries, strength, target)
                entries = [PICS.get(level + rng.randint(-1, 1), rng.randrange(VARIANTS))
                           for _ in range(75)]
                frames = _run(ctrl, entries, strength, target)
                changes = sum(1 for a, b in zip(frames, frames[1:]) if a[1] != b[1])
                if changes:
                    failures.append((level, strength, changes))
        self.assertEqual(failures, [], "goal kept changing (level, strength, changes)")

    def test_flicker_never_pumps_upward(self):
        rng = random.Random(9)
        for _ in range(200):
            dark = rng.randint(3, 60)
            bright = min(255, dark * rng.randint(2, 5))
            period = rng.randint(2, 5)       # bright at least once a second
            strength = rng.choice([15.0, 30.0, 60.0])
            target = rng.uniform(*TARGET_LIMITS)
            ctrl = Controller()
            self._settle(ctrl, bright, strength, target, 3.0, rng=rng)
            settled = ctrl.offset
            entries = [PICS.get(bright if i % period == 0 else dark, rng.randrange(VARIANTS))
                       for i in range(50)]
            frames = _run(ctrl, entries, strength, target)
            top = max(f[0] for f in frames)
            self.assertLessEqual(top, settled + max(0.5, strength * 0.08) + 1e-6,
                                 (dark, bright, period, strength))

    def test_flash_rushes_down_within_rush_limit(self):
        rng = random.Random(10)
        for _ in range(100):
            level = rng.randint(3, 8)
            ctrl = Controller()
            self._settle(ctrl, level, 30.0, 0.12, 6.0, rng=rng)
            self.assertGreater(ctrl.offset, 10)
            frames = _run(ctrl, [PICS.get(255, 0, 0)] * 3, 30.0, 0.12)
            self.assertTrue(frames[0][2], "flash should rush")
            for pos, want, rush, _r, _v, before in frames:
                self.assertLessEqual(abs(pos - before), RUSH_LIMIT)
            # it is well on its way down within half a second
            self.assertLess(frames[29][0], 0.0)

    def test_dim_false_never_negative(self):
        rng = random.Random(11)
        for _ in range(300):
            strength, target, speed, _dim = _random_params(rng)
            ctrl = Controller()
            frames = _run(ctrl, _scene(rng, 5.0), strength, target, speed, dim=False)
            self.assertGreaterEqual(min(f[0] for f in frames), -1e-9)

    def test_strength_zero_keeps_offset_zero(self):
        rng = random.Random(12)
        for _ in range(200):
            _s, target, speed, dim = _random_params(rng)
            ctrl = Controller()
            frames = _run(ctrl, _scene(rng, 5.0), 0.0, target, speed, dim)
            self.assertTrue(all(f[0] == 0.0 and f[1] == 0.0 for f in frames))

    def test_frozen_picture_eases_back_then_recovers(self):
        rng = random.Random(13)
        for _ in range(60):
            level = rng.randint(3, 25)
            strength = rng.choice([15.0, 30.0, 60.0])
            ctrl = Controller()
            self._settle(ctrl, level, strength, 0.12, 6.0, rng=rng)
            goal = ctrl.want
            self.assertGreater(goal, 1.0)
            same = PICS.get(level, 0)
            secs = FROZEN_S + LOST_S + 3.0
            _run(ctrl, [same] * int(secs / INTERVAL_S), strength, 0.12)
            self.assertTrue(ctrl.frozen)
            self.assertEqual(ctrl.want, 0.0)
            self.assertAlmostEqual(ctrl.offset, 0.0, places=6)
            self._settle(ctrl, level, strength, 0.12, 10.0, rng=rng)
            self.assertFalse(ctrl.frozen)
            self.assertAlmostEqual(ctrl.want, goal, delta=max(0.5, strength * 0.08))
            self.assertAlmostEqual(ctrl.offset, ctrl.want, delta=0.01)

    def test_lost_picture_holds_then_eases_back(self):
        rng = random.Random(14)
        for _ in range(60):
            level = rng.randint(3, 25)
            ctrl = Controller()
            self._settle(ctrl, level, 30.0, 0.12, 6.0, rng=rng)
            goal = ctrl.want
            frames = _run(ctrl, [None] * int((LOST_S - 0.4) / INTERVAL_S), 30.0, 0.12)
            self.assertEqual(frames[-1][1], goal, "held while briefly lost")
            _run(ctrl, [None] * int(4.0 / INTERVAL_S), 30.0, 0.12)
            self.assertEqual(ctrl.want, 0.0)
            self.assertAlmostEqual(ctrl.offset, 0.0, places=6)

    def test_short_black_screen_holds(self):
        ctrl = Controller()
        self._settle(ctrl, 10, 30.0, 0.12, 6.0)
        goal = ctrl.want
        _run(ctrl, [PICS.get(0, 0, 0)] * 20, 30.0, 0.12)
        self.assertEqual(ctrl.want, goal)

    def test_long_black_loading_screen_holds(self):
        """A 10 s pure-black loading screen is pixel-identical every time,
        so it counts as frozen after FROZEN_S and eases back to the preset,
        then has to climb again afterwards ("black hold" is lost)."""
        ctrl = Controller()
        self._settle(ctrl, 10, 30.0, 0.12, 6.0)
        goal = ctrl.want
        _run(ctrl, [PICS.get(0, 0, 0)] * 50, 30.0, 0.12)
        self.assertEqual(ctrl.want, goal)

    def test_very_long_black_eases_back_and_recovers(self):
        """Black for longer than BLACK_HOLD_S (capture blocked for good)
        eases back to the preset; a live picture afterwards steers again."""
        ctrl = Controller()
        self._settle(ctrl, 10, 30.0, 0.12, 6.0)
        goal = ctrl.want
        black = PICS.get(0, 0, 0)
        _run(ctrl, [black] * int((BLACK_HOLD_S - 1.0) / INTERVAL_S), 30.0, 0.12)
        self.assertEqual(ctrl.want, goal)
        self.assertFalse(ctrl.frozen)
        _run(ctrl, [black] * int(3.0 / INTERVAL_S), 30.0, 0.12)
        self.assertEqual(ctrl.want, 0.0)
        self._settle(ctrl, 10, 30.0, 0.12, 6.0)
        self.assertAlmostEqual(ctrl.want, goal, delta=30.0 * 0.08)

    def test_black_then_same_picture_is_not_frozen_at_once(self):
        """The picture from before a loading screen coming back unchanged
        starts the frozen count afresh."""
        ctrl = Controller()
        same = PICS.get(10, 0)
        _run(ctrl, [same] * int((FROZEN_S - 0.4) / INTERVAL_S), 30.0, 0.12)
        _run(ctrl, [PICS.get(0, 0, 0)] * 10, 30.0, 0.12)
        _run(ctrl, [same] * int((FROZEN_S - 0.4) / INTERVAL_S), 30.0, 0.12)
        self.assertFalse(ctrl.frozen)

    def test_aim_and_strength_changes_apply_at_once_in_dark_scenes(self):
        """The noise allowance near black never swallows a change of Aim
        for, How much or dim (each hotkey press must be felt)."""
        for level in (3, 4, 6, 10):
            for strength in (30.0, 60.0):
                ctrl = Controller()
                self._settle(ctrl, level, strength, 0.04, 4.0)
                goal = ctrl.want
                aim = nudge_aim(nudge_aim(0.04, True), True)
                _run(ctrl, [PICS.get(level, 1)], strength, aim)
                expect = target_offset(ctrl._recent[-1][1], strength, aim)
                if abs(expect - goal) >= max(0.5, strength * 0.08):
                    self.assertNotEqual(ctrl.want, goal, (level, strength))
                _run(ctrl, [PICS.get(level, 2)], strength / 2, aim)
                self.assertLessEqual(ctrl.want, strength / 2 + 1e-9)

    def test_real_change_near_black_still_moves_the_goal(self):
        """Twice as bright (level 4 -> 8) is a real change, not noise."""
        ctrl = Controller()
        self._settle(ctrl, 4, 60.0, 0.04, 4.0)
        goal = ctrl.want
        self._settle(ctrl, 8, 60.0, 0.04, 2.0)
        self.assertLess(ctrl.want, goal - 5.0)

    def test_speeds_scale_monotonically(self):
        def time_to(goal_level, start_level, speed, strength=30.0):
            ctrl = Controller()
            self._settle(ctrl, start_level, strength, 0.12, 8.0)
            entries = [PICS.get(goal_level, i % VARIANTS) for i in range(100)]
            frames = _run(ctrl, entries, strength, 0.12, speed)
            want = frames[-1][1]
            for i, f in enumerate(frames):
                if abs(f[0] - want) < 0.5:
                    return i
            return len(frames)
        for start, goal in ((60, 5), (5, 200), (200, 5), (20, 60)):
            t = [time_to(goal, start, SPEEDS[s]) for s in ("slow", "normal", "fast")]
            self.assertGreater(t[0], t[1], (start, goal, t))
            self.assertGreater(t[1], t[2], (start, goal, t))

    def test_spring_from_rest_never_overshoots(self):
        rng = random.Random(15)
        for _ in range(1500):
            ctrl = Controller()
            ctrl.offset = rng.uniform(-60, 60)
            want = rng.uniform(-60, 60)
            ctrl.rush = rng.random() < 0.3 and want < ctrl.offset
            speed = SPEEDS[rng.choice(list(SPEEDS))]
            side = (ctrl.offset > want) - (ctrl.offset < want)
            # A glide held back by the speed limit now ends with a gentle
            # exponential approach instead of sliding past the goal, so at
            # "slow" it can take longer than 15 s to land exactly: allow 30 s.
            for _ in range(1800):
                pos = ctrl.follow(want, 60.0, DT, speed)
                self.assertGreaterEqual((pos - want) * side, 0.0)
                if pos == want:
                    break
            self.assertEqual(ctrl.offset, want)

    def test_goal_reversal_mid_glide_never_overshoots_new_goal(self):
        """A goal change while already moving (velocity carried over) must
        not shoot past the new goal."""
        rng = random.Random(16)
        worst = 0.0
        for _ in range(600):
            ctrl = Controller()
            speed = SPEEDS[rng.choice(list(SPEEDS))]
            first = rng.uniform(-60, 60)
            for _ in range(rng.randint(1, 60)):
                ctrl.follow(first, 60.0, DT, speed)
            want = rng.uniform(-60, 60)
            side = (ctrl.offset > want) - (ctrl.offset < want)
            for _ in range(900):
                pos = ctrl.follow(want, 60.0, DT, speed)
                worst = max(worst, -(pos - want) * side)
        self.assertLessEqual(worst, 0.05, "overshoot %.3f points" % worst)


class PureFunctionTests(unittest.TestCase):

    def test_split_offset_preserves_total_and_range(self):
        rng = random.Random(21)
        lo, hi = SLIDER_RANGE
        for _ in range(20000):
            key = rng.choice(list(SPILL))
            base = {"shadow_boost": rng.uniform(lo, hi), "exposure": rng.uniform(lo, hi)}
            if rng.random() < 0.2:
                base[rng.choice(list(base))] = rng.choice([lo, hi])
            offset = rng.uniform(-60, 60)
            out = split_offset(key, offset, base)
            for name, amount in out.items():
                self.assertGreaterEqual(base[name] + amount, lo - 1e-9)
                self.assertLessEqual(base[name] + amount, hi + 1e-9)
            other, ratio = SPILL[key]
            main = out[key]
            room = max(lo - base[key], min(hi - base[key], offset))
            self.assertAlmostEqual(main, room)
            extra = out.get(other, 0.0)
            spill_room = (hi - base[other]) if offset > 0 else (base[other] - lo)
            if abs(offset - main) * ratio <= spill_room - 1e-6:
                # everything fits: the total (in main-slider points) is kept
                self.assertAlmostEqual(main + extra / ratio, offset,
                                       delta=0.05 / ratio + 1e-9)
            self.assertGreaterEqual(math.copysign(1, offset) * main, -1e-12)
            self.assertGreaterEqual(math.copysign(1, offset) * extra, -1e-12)

    def test_aim_round_trip(self):
        rng = random.Random(22)
        for _ in range(20000):
            key = math.exp(rng.uniform(math.log(1 / 255), 0))
            strength = rng.uniform(0.5, 60)
            offset = rng.uniform(-strength, strength)
            aim = aim_for_current_look(key, offset, strength)
            self.assertAlmostEqual(target_offset(key, strength, aim), offset, places=6)
            # and target_offset -> aim -> target_offset
            target = rng.uniform(*TARGET_LIMITS)
            off = target_offset(key, strength, target)
            aim2 = aim_for_current_look(key, off, strength)
            self.assertAlmostEqual(target_offset(key, strength, aim2), off, places=6)

    def test_aim_for_current_look_with_zero_strength(self):
        self.assertEqual(aim_for_current_look(0.2, 5.0, 0.0), 0.2)

    def test_target_offset_monotone_and_bounded(self):
        rng = random.Random(23)
        for _ in range(3000):
            strength = rng.uniform(0, 60)
            target = rng.uniform(*TARGET_LIMITS)
            keys = sorted(rng.uniform(0, 1) for _ in range(20))
            offs = [target_offset(k, strength, target) for k in keys]
            for a, b in zip(offs, offs[1:]):
                self.assertGreaterEqual(a + 1e-12, b)
            self.assertTrue(all(-strength - 1e-9 <= o <= strength + 1e-9 for o in offs))
            self.assertTrue(all(target_offset(k, strength, target, False) >= 0
                                for k in keys))

    def test_nudge_aim_stays_in_limits(self):
        rng = random.Random(24)
        lo, hi = TARGET_LIMITS
        for _ in range(2000):
            aim = rng.uniform(lo, hi)
            for _ in range(30):
                aim = nudge_aim(aim, rng.random() < 0.5)
                self.assertGreaterEqual(aim, lo)
                self.assertLessEqual(aim, hi)
        self.assertEqual(nudge_aim(hi, True), hi)
        self.assertEqual(nudge_aim(lo, False), lo)
        # out-of-range stored values are brought back in
        self.assertEqual(nudge_aim(5.0, True), hi)
        self.assertEqual(nudge_aim(0.0, False), lo)

    def test_nudge_aim_up_then_down_returns(self):
        rng = random.Random(25)
        lo, hi = TARGET_LIMITS
        for _ in range(2000):
            aim = round(rng.uniform(lo * 1.3, hi / 1.3), 3)
            self.assertAlmostEqual(nudge_aim(nudge_aim(aim, True), False), aim, delta=0.002)


class FaderTickTests(unittest.TestCase):

    def test_tick_never_applies_when_nothing_moved_and_lands_exactly(self):
        rng = random.Random(31)
        for _ in range(150):
            ctrls = {}
            for i in range(rng.randint(1, 3)):
                c = Controller()
                c.offset = rng.uniform(-30, 30)
                c.want = rng.choice([c.offset, rng.uniform(-30, 30)])
                c.strength = 30.0
                ctrls["d%d" % i] = c
            applied = []
            fader = Fader(ctrls, applied.append)
            fader.speed = SPEEDS[rng.choice(list(SPEEDS))]
            for _ in range(900):
                fader.tick(DT)
            self.assertTrue(fader.settled())
            n = len(applied)
            for _ in range(200):
                self.assertFalse(fader.tick(DT, DT * rng.uniform(0, 1)))
            self.assertEqual(len(applied), n)
            self.assertTrue(applied)
            self.assertEqual(applied[-1], {d: c.want for d, c in ctrls.items()})
            for a, b in zip(applied, applied[1:]):
                self.assertTrue(any(abs(a[d] - b[d]) > 0 for d in b))

    def test_tick_applies_on_display_add_and_remove(self):
        c = Controller()
        ctrls = {"a": c}
        applied = []
        fader = Fader(ctrls, applied.append)
        self.assertTrue(fader.tick(DT))
        self.assertFalse(fader.tick(DT))
        ctrls["b"] = Controller()
        self.assertTrue(fader.tick(DT))
        del ctrls["a"]
        self.assertTrue(fader.tick(DT))
        self.assertEqual(set(applied[-1]), {"b"})

    def test_tick_step_sizes_with_rest_time(self):
        """A long rest (dt big, motion_dt small) must not jump."""
        rng = random.Random(32)
        for _ in range(500):
            c = Controller()
            c.want = rng.uniform(-60, 60)
            c.strength = 60.0
            fader = Fader({"a": c}, lambda p: None)
            before = c.offset
            fader.tick(rng.uniform(0.1, 0.25), DT)
            self.assertLessEqual(abs(c.offset - before), MAX_FALL * DT + 1e-6)


class FaderThreadStressTests(unittest.TestCase):

    def _faders_alive(self):
        return [t for t in threading.enumerate()
                if t.name == "catvision-auto-fader" and t.is_alive()]

    def test_random_start_stop_wake_and_displays(self):
        rng = random.Random(41)
        ctrls = {}
        seen = []
        lock = threading.Lock()

        def apply(positions):
            with lock:
                seen.append(dict(positions))

        fader = Fader(ctrls, apply)
        baseline = len(self._faders_alive())
        deadline = time.monotonic() + 0.6
        ops = 0
        while time.monotonic() < deadline:
            op = rng.choice(["start", "stop", "wake", "add", "remove", "goal", "speed",
                             "observe"])
            if op == "start":
                fader.start()
            elif op == "stop":
                fader.stop()
                self.assertFalse(fader.running)
            elif op == "wake":
                fader.wake()
            elif op == "add":
                c = Controller()
                c.strength = 30.0
                ctrls["d%d" % rng.randint(0, 5)] = c
            elif op == "remove" and ctrls:
                ctrls.pop(rng.choice(list(ctrls)), None)
            elif op == "goal" and ctrls:
                c = ctrls.get(rng.choice(list(ctrls)))
                if c is not None:
                    c.want = rng.uniform(-30, 30)
                    fader.wake()
            elif op == "speed":
                fader.speed = SPEEDS[rng.choice(list(SPEEDS))]
            elif op == "observe" and ctrls:
                c = ctrls.get(rng.choice(list(ctrls)))
                if c is not None:
                    c.observe(PICS.get(rng.randint(0, 255), rng.randrange(VARIANTS)),
                              30.0, 0.12)
            self.assertLessEqual(len(self._faders_alive()) - baseline, 1)
            ops += 1
            time.sleep(rng.uniform(0, 0.01))
        fader.stop()
        self.assertEqual(fader.last_error, "")
        end = time.monotonic() + 3.0
        while len(self._faders_alive()) > baseline and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(len(self._faders_alive()), baseline)
        self.assertGreater(ops, 50)

    def test_wake_starts_motion_promptly_and_lands_exactly(self):
        c = Controller()
        c.strength = 30.0
        got = []
        fader = Fader({"a": c}, lambda p: got.append(p["a"]))
        fader.speed = SPEEDS["fast"]
        fader.start()
        try:
            end = time.monotonic() + 2.0
            while not fader.settled() or not got:
                self.assertLess(time.monotonic(), end)
                time.sleep(0.01)
            time.sleep(0.3)    # let it go idle
            c.want = -4.0
            fader.wake()
            end = time.monotonic() + 0.5
            while c.offset == 0.0:
                self.assertLess(time.monotonic(), end, "did not start moving after wake()")
                time.sleep(0.005)
            end = time.monotonic() + 10.0
            while c.offset != -4.0:
                self.assertLess(time.monotonic(), end)
                time.sleep(0.02)
            time.sleep(0.3)
            self.assertEqual(got[-1], -4.0)
            self.assertEqual(fader.last_error, "")
        finally:
            fader.stop()


if __name__ == "__main__":
    unittest.main()
