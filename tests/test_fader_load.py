"""Auto levels (beta): fader step timing while other threads run Python
(the self-test's measurement of it)."""

import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from catvision import autolevels, selftest


class SwitchIntervalTests(unittest.TestCase):
    """Python's thread switch interval is left as it is: a 1 ms interval
    made fader steps no more even on Windows (the self-test's fader_load)."""

    def setUp(self):
        self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())

    def test_app_start_leaves_it_alone(self):
        from catvision import __main__ as entry
        sys.setswitchinterval(0.005)
        seen = []

        def fake_run(path):
            seen.append(sys.getswitchinterval())
            return 0

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"CATVISION_CONFIG_DIR": tmp}), \
                mock.patch("catvision.selftest.run", fake_run):
            self.assertEqual(entry.main(["--selftest", os.path.join(tmp, "r.json")]), 0)
        self.assertEqual(len(seen), 1)
        self.assertAlmostEqual(seen[0], 0.005)
        self.assertFalse(hasattr(autolevels, "quick_thread_switches"))


class GapStatsTests(unittest.TestCase):
    def test_even_steps(self):
        stamps = [i / 120 for i in range(121)]
        out = selftest._gap_stats(stamps, 120)
        self.assertEqual(out["steps"], 121)
        self.assertAlmostEqual(out["hz"], 120.0, places=1)
        self.assertAlmostEqual(out["median_ms"], 8.33, places=2)
        self.assertAlmostEqual(out["max_ms"], 8.33, places=2)
        self.assertEqual(out["late_steps"], 0)

    def test_one_late_step_shows_in_max_and_late_count(self):
        stamps = [i / 120 for i in range(60)]
        stamps[30] += 0.006          # 6 ms late, the next one back on the beat
        out = selftest._gap_stats(stamps, 120)
        self.assertAlmostEqual(out["max_ms"], 14.33, places=2)
        self.assertEqual(out["late_steps"], 1)
        self.assertAlmostEqual(out["median_ms"], 8.33, places=2)

    def test_too_few_steps(self):
        self.assertEqual(selftest._gap_stats([1.0], 120), {"steps": 1})
        self.assertEqual(selftest._gap_stats([], 120), {"steps": 0})


class StepsUnderLoadTests(unittest.TestCase):
    """The self-test measurement: informational, quick and never raises."""

    def setUp(self):
        self.addCleanup(sys.setswitchinterval, sys.getswitchinterval())
        sys.setswitchinterval(0.005)

    def _threads(self):
        return {t.name for t in threading.enumerate()}

    def test_thumbnail_is_a_real_capture_size(self):
        pic = selftest._thumbnail()
        self.assertEqual(len(pic), autolevels.THUMB_W * autolevels.THUMB_H * 4)
        m = autolevels.measure(pic, autolevels.THUMB_W)
        self.assertFalse(m.black)
        self.assertTrue(0.0 < m.key < 1.0)

    def test_watcher_load(self):
        out = selftest.fader_steps_under_load(0.3, "watcher")
        self.assertNotIn("error", out)
        self.assertEqual(out["switch_ms"], 5.0)
        self.assertGreater(out["steps"], 10)
        self.assertGreaterEqual(out["measurements"], 1)
        for key in ("hz", "median_ms", "p95_ms", "max_ms", "late_steps"):
            self.assertIn(key, out)
        # A loose check (busy test machines): the fader keeps its beat.
        self.assertGreater(out["hz"], 60)
        self.assertAlmostEqual(sys.getswitchinterval(), 0.005)   # never changed

    def test_busy_load(self):
        out = selftest.fader_steps_under_load(0.2, "busy")
        self.assertNotIn("error", out)
        self.assertEqual(out["switch_ms"], 5.0)
        self.assertGreater(out["measure_ms"], 0)
        self.assertGreater(out["steps"], 5)

    def test_leaves_no_threads_running(self):
        before = self._threads()
        selftest.fader_steps_under_load(0.15, "watcher")
        selftest.fader_steps_under_load(0.15, "busy")
        extra = {n for n in self._threads() - before
                 if n.startswith("catvision-")}
        self.assertEqual(extra, set())

    def test_problems_are_reported_not_raised(self):
        with mock.patch.object(autolevels, "Fader", side_effect=RuntimeError("boom")):
            out = selftest.fader_steps_under_load(0.1, "watcher")
        self.assertIn("boom", out["error"])
        self.assertAlmostEqual(sys.getswitchinterval(), 0.005)


if __name__ == "__main__":
    unittest.main()
