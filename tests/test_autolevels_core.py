"""Auto levels (beta): whole-picture metering and the fader's idle backoff."""

import threading
import time
import unittest

from catvision.autolevels import (FADER_HZ, IDLE_S, THUMB_H, THUMB_W, Controller, Fader,
                                  measure)


def _frame(level_at, w=THUMB_W, h=THUMB_H) -> bytes:
    """A BGRA picture whose grey level at (x, y) is level_at(x, y)."""
    out = bytearray()
    for y in range(h):
        for x in range(w):
            v = level_at(x, y)
            out += bytes((v, v, v, 255))
    return bytes(out)


class WholePictureTests(unittest.TestCase):
    """The whole picture is always measured: dark edges (a night sky, a
    ceiling, letterbox bars) are never cut away, so the measured area never
    depends on what is on screen and dark scenes keep their boost."""

    def test_dark_edges_still_count(self):
        plain = measure(_frame(lambda x, y: 40), THUMB_W)
        dark_top = measure(_frame(lambda x, y: 0 if y < THUMB_H // 4 else 40), THUMB_W)
        self.assertLess(dark_top.key, plain.key)
        self.assertGreater(dark_top.dark, plain.dark)

    def test_a_level_of_noise_in_dark_edges_barely_moves_the_reading(self):
        # Pure black vs. one level up in a dark band must not flip the goal.
        a = measure(_frame(lambda x, y: 0 if y < 13 else 20), THUMB_W)
        b = measure(_frame(lambda x, y: 1 if y < 13 else 20), THUMB_W)
        self.assertLess(abs(a.key - b.key) / b.key, 0.05)

    def test_whole_black_picture_is_still_black(self):
        self.assertTrue(measure(_frame(lambda x, y: 0), THUMB_W).black)


class FaderIdleTests(unittest.TestCase):
    def _wait(self, cond, seconds=5.0):
        deadline = time.time() + seconds
        while not cond() and time.time() < deadline:
            time.sleep(0.01)
        return cond()

    def test_tick_skips_apply_when_nothing_moved(self):
        calls = []
        ctrls = {"a": Controller()}
        f = Fader(ctrls, calls.append)
        self.assertTrue(f.tick(1 / 60))          # first time: always applied
        self.assertFalse(f.tick(1 / 60))         # at rest: nothing to apply
        self.assertTrue(f.tick(1 / 60, force=True))
        ctrls["a"].want = 10.0
        # A new goal is passed on as soon as the move is worth showing
        # (FADER_MIN_STEP): within a few steps of the spring's gentle start.
        moved = [f.tick(1 / 60) for _ in range(5)]
        self.assertIn(True, moved)
        self.assertEqual(len(calls), 2 + moved.count(True))

    def test_final_resting_value_is_applied(self):
        calls = []
        ctrls = {"a": Controller(want=3.0, strength=30.0)}
        f = Fader(ctrls, calls.append)
        for _ in range(2000):
            f.tick(1 / 60)
        self.assertTrue(f.settled())
        self.assertEqual(calls[-1]["a"], 3.0)

    def test_clock_keeps_running_while_resting(self):
        ctrls = {"a": Controller()}
        f = Fader(ctrls, lambda pos: None)
        f.tick(0.1, motion_dt=1 / 60)
        self.assertAlmostEqual(ctrls["a"].clock, 0.1)

    def test_fade_after_rest_starts_like_a_normal_step(self):
        steady, rested = Controller(strength=30.0), Controller(strength=30.0)
        steady.want = rested.want = 20.0
        f = Fader({"a": rested}, lambda pos: None)
        f.tick(IDLE_S, motion_dt=1 / FADER_HZ)
        steady.advance(1 / FADER_HZ)
        self.assertAlmostEqual(rested.offset, steady.offset)

    def test_thread_backs_off_when_settled_and_resumes_on_a_new_goal(self):
        lock = threading.Lock()
        calls = []

        def apply(pos):
            with lock:
                calls.append((time.perf_counter(), pos["a"]))

        ctrl = Controller(strength=30.0)
        f = Fader({"a": ctrl}, apply)
        f.start()
        try:
            time.sleep(0.6)
            with lock:
                resting = len(calls)
            # 60 a second would be ~36 calls; resting refreshes ~10 a second.
            self.assertLess(resting, 15)
            ctrl.want = 20.0
            self.assertTrue(self._wait(lambda: ctrl.offset > 5))
            with lock:
                moving = [p for _t, p in calls if p > 0]
            steps = [b - a for a, b in zip(moving, moving[1:])]
            self.assertGreater(len(moving), 5)
            # Still a smooth 60 Hz fade (allowing for a busy test machine).
            self.assertLess(sorted(steps)[int(len(steps) * 0.9)], 1.4)
            self.assertTrue(self._wait(f.settled, 10.0))
        finally:
            f.stop()
        self.assertFalse(f.running)
        self.assertEqual(f.last_error, "")



class FaderLifecycleTests(unittest.TestCase):
    def test_quick_off_and_on_never_leaves_two_faders_running(self):
        f = Fader({"a": Controller(want=5.0, strength=30.0)}, lambda pos: None)
        for _ in range(3):
            f.start()
            f.stop()
        f.start()
        time.sleep(0.2)
        alive = [t for t in threading.enumerate() if t.name == "catvision-auto-fader"]
        f.stop()
        self.assertEqual(len(alive), 1)

    def test_display_removed_mid_fade_is_not_an_error(self):
        ctrls = {"a": Controller(want=20.0, strength=30.0), "b": Controller(want=20.0)}
        f = Fader(ctrls, lambda pos: None)
        f.start()
        for _ in range(20):
            ctrls.pop("b", None)
            ctrls["b"] = Controller(want=10.0)
            time.sleep(0.01)
        f.stop()
        self.assertEqual(f.last_error, "")


class FaderRunCallbackTests(unittest.TestCase):
    def test_a_run_keeps_the_callback_it_started_with(self):
        first, second = [], []
        ctrls = {"a": Controller(want=20.0, strength=30.0)}
        f = Fader(ctrls, first.append)
        f.tick(1 / 60, apply=first.append)
        f.apply = second.append            # a later run would bring its own callback
        f.tick(1 / 60, apply=first.append)
        self.assertEqual(len(first), 2)    # this run kept reporting through its own
        self.assertEqual(second, [])
        f.tick(1 / 60)                     # without one, the current callback is used
        self.assertEqual(len(second), 1)


if __name__ == "__main__":
    unittest.main()
