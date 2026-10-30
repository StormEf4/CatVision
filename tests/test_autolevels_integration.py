"""Auto levels (beta): how the app drives the engine and the gamma backend."""
import tempfile
import unittest
from pathlib import Path

from catvision import curves
from catvision.backends import NullColorBackend, apply_ramp_with_fallback, ramp_deviation
from catvision.curves import Settings
from catvision.engine import Engine
from tests.test_catvision import FakeGamma

try:
    from catvision.gui import AUTO_FRONT_S, AUTO_STALE_S, auto_tick_plan
except ImportError:  # no tkinter here
    auto_tick_plan = None

D1 = "\\\\.\\DISPLAY1"


class CountingGamma(FakeGamma):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.reads = 0
        self.sets = []

    def set_ramp(self, mid, ramp):
        ok = super().set_ramp(mid, ramp)
        self.sets.append((mid, ok))
        return ok

    def get_ramp(self, mid):
        self.reads += 1
        return super().get_ramp(mid)


@unittest.skipIf(auto_tick_plan is None, "needs tkinter")
class TickPlanTest(unittest.TestCase):
    def test_window_rechecked_without_fresh_pictures(self):
        recheck, stale = auto_tick_plan(10.0 + AUTO_FRONT_S, False, 10.0, 10.0)
        self.assertTrue(recheck)
        self.assertFalse(stale)
        recheck, _ = auto_tick_plan(10.1, False, 10.0, 10.0)
        self.assertFalse(recheck)

    def test_fresh_picture_always_rechecks_and_is_not_stale(self):
        self.assertEqual(auto_tick_plan(10.0, True, 10.0, 0.0), (True, False))

    def test_stalled_capture_counts_as_lost(self):
        _, stale = auto_tick_plan(10.0 + AUTO_STALE_S, False, 10.0, 10.0)
        self.assertTrue(stale)
        _, stale = auto_tick_plan(10.0 + AUTO_STALE_S / 2, False, 10.0, 10.0)
        self.assertFalse(stale)


class FallbackMemoryTest(unittest.TestCase):
    def test_unlimited_path_unchanged(self):
        g = CountingGamma()
        ramp = curves.build_ramp(Settings(shadow_boost=80))
        mem: dict = {}
        r = apply_ramp_with_fallback(g, D1, ramp, mem)
        self.assertTrue(r.ok)
        self.assertFalse(r.limited)
        self.assertEqual(len(g.sets), 1)
        # Remembers what is shown now (the full ramp), nothing weaker.
        self.assertEqual(mem, {D1: ("shown", ramp_deviation(ramp), None, 1)})
        self.assertEqual(r.applied, ramp)

    def test_limited_tier_is_tried_first_next_time(self):
        g = CountingGamma(limit=127)
        mem: dict = {}
        r = apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=100)), mem)
        self.assertTrue(r.ok and r.limited)
        first = len(g.sets)
        self.assertGreater(first, 1)
        g.sets.clear()
        r = apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=98)), mem)
        self.assertTrue(r.ok)
        self.assertEqual(g.sets, [(D1, True)])
        self.assertEqual(g.ramps[D1], r.applied)

    def test_blend_search_is_remembered(self):
        g = CountingGamma(limit=20)
        mem: dict = {}
        r = apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=100)), mem)
        self.assertTrue(r.ok and r.limited)
        self.assertLess(r.strength, 1.0)
        self.assertEqual(mem[D1][0], "shown")
        self.assertEqual(mem[D1][1], ramp_deviation(r.applied))
        g.sets.clear()
        r2 = apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=99)), mem)
        self.assertTrue(r2.ok)
        # At most a couple of nearby tries, never the full search again.
        self.assertLessEqual(len(g.sets), 3)
        self.assertAlmostEqual(r2.strength, r.strength, delta=0.05)
        self.assertEqual(g.ramps[D1], r2.applied)

    def test_memory_forgotten_when_it_stops_working(self):
        g = CountingGamma(limit=127)
        mem = {D1: ("shown", 3, 4, 0)}
        g.limit = None
        ramp = curves.build_ramp(Settings(shadow_boost=40))
        r = apply_ramp_with_fallback(g, D1, ramp, mem)
        # Windows takes the full ramp again: it is used at once and the
        # remembered weaker version and refusal are forgotten.
        self.assertTrue(r.ok and not r.limited)
        self.assertEqual(r.strength, 1.0)
        self.assertEqual(r.applied, ramp)
        self.assertEqual(mem[D1][:3], ("shown", ramp_deviation(ramp), None))
        # A remembered strength that fails now searches weaker, one look.
        g.limit = 10
        g.sets.clear()
        r = apply_ramp_with_fallback(g, D1, ramp, mem)
        self.assertTrue(r.ok and r.limited)
        self.assertEqual(sum(1 for _, ok in g.sets if ok), 1)
        self.assertEqual(mem[D1][0], "shown")
        self.assertLessEqual(mem[D1][1], 10)
        # Unknown (older) memory entries are ignored safely.
        mem = {D1: ("blend", 0.01)}
        r = apply_ramp_with_fallback(g, D1, ramp, mem)
        self.assertTrue(r.ok and r.limited)


class EngineAutoOffsetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fades_do_not_read_ramps_back(self):
        g = CountingGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=40)
        e.set_enabled(True)
        g.reads = 0
        g.sets.clear()
        for i in range(1, 11):
            e.set_auto_offsets({D1: {"shadow_boost": float(i)}})
        self.assertEqual(g.reads, 0)
        self.assertEqual(len(g.sets), 10)
        # The keep-applied check still sees what was set and finds no drift.
        self.assertFalse(e.check_and_reapply())
        # ...and still notices when something else resets the ramp.
        g.ramps[D1] = curves.identity_ramp()
        self.assertTrue(e.check_and_reapply())
        e.shutdown()

    def test_limited_fades_reuse_the_working_tier(self):
        g = CountingGamma(limit=40)
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=90)
        e.set_enabled(True)
        g.sets.clear()
        e.set_auto_offsets({D1: {"shadow_boost": 5.0}})
        for i in range(6, 12):
            g.sets.clear()
            e.set_auto_offsets({D1: {"shadow_boost": float(i)}})
            # One harmless rejected try of the full ramp, then the
            # remembered strength (a touch stronger, the same, a touch
            # weaker) - never the flickering 7-step search.
            self.assertLessEqual(len(g.sets), 4)
            self.assertEqual(g.sets[0], (D1, False))
            self.assertEqual(g.sets[-1], (D1, True))
        self.assertTrue(e.status.gamma_limited)
        e.shutdown()


class AutoLevelsEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_turning_auto_levels_off_restores_the_full_preset(self):
        g = CountingGamma(limit=60)
        e = Engine(g, NullColorBackend(), self.dir)
        preset = Settings(shadow_boost=20)
        e.settings = preset
        e.set_enabled(True)
        full = curves.build_ramp(preset)
        self.assertEqual(g.ramps[D1], full)               # Windows takes the preset in full
        e.set_auto_offsets({D1: {"shadow_boost": 60.0}})  # too strong: weakened
        e.set_auto_offsets({D1: {"shadow_boost": 30.0}})
        e.retire_auto_offsets()
        self.assertEqual(g.ramps[D1], full)               # nothing weaker lingers
        self.assertFalse(e.check_and_reapply())
        e.shutdown()

    def test_late_update_from_a_stopped_fader_is_ignored(self):
        g = CountingGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=30)
        e.set_enabled(True)
        run = e.auto_generation
        e.set_auto_offsets({D1: {"shadow_boost": 20.0}}, generation=run)
        e.retire_auto_offsets()
        e.set_auto_offsets({D1: {"shadow_boost": 25.0}}, generation=run)   # arrives late
        self.assertEqual(e.auto_offsets, {})
        self.assertEqual(g.ramps[D1], curves.build_ramp(Settings(shadow_boost=30)))
        e.shutdown()

    def test_auto_off_while_windows_refuses_still_ends_the_boost(self):
        g = CountingGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=30)
        e.set_enabled(True)
        preset = g.get_ramp(D1)
        run = e.auto_generation
        e.set_auto_offsets({D1: {"shadow_boost": 30.0}}, generation=run)
        g.limit = -1    # a fullscreen switch: Windows refuses everything for a moment
        e.set_auto_offsets({D1: {}}, generation=run)   # the fade's last step, refused
        g.limit = None
        e.retire_auto_offsets()   # auto levels stops at once: no fader left to retry
        self.assertNotEqual(g.ramps[D1], preset)
        self.assertTrue(e.check_and_reapply())        # keep-applied offers it again
        self.assertEqual(g.ramps[D1], preset)
        self.assertTrue(e.status.gamma_ok)
        self.assertFalse(e.check_and_reapply())
        e.shutdown()

    def test_a_display_that_always_refuses_doesnt_stop_keep_applied(self):
        # Display 2 never takes a change (HDR, say); a game resets display 1.
        class OneRefuses(CountingGamma):
            def set_ramp(self, mid, ramp):
                return mid != D2 and super().set_ramp(mid, ramp)
        D2 = "\\\\.\\DISPLAY2"
        g = OneRefuses()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=30)
        e.set_enabled(True)
        self.assertFalse(e.status.gamma_ok)
        preset = g.get_ramp(D1)
        g.ramps[D1] = curves.identity_ramp()
        self.assertTrue(e.check_and_reapply())
        self.assertEqual(g.ramps[D1], preset)
        e.shutdown()

    def test_refused_restore_keeps_the_display_changed_mark(self):
        flag = self.dir / "display_modified.flag"
        g = CountingGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=30)
        e.set_enabled(True)
        boosted = g.get_ramp(D1)
        g.limit = -1               # Windows refuses everything just as CatVision quits
        e.shutdown()
        self.assertEqual(g.ramps[D1], boosted)
        self.assertTrue(flag.exists())   # so the next start doesn't take it as the user's own
        Engine(g, NullColorBackend(), self.dir)     # still refused: the mark stays
        self.assertTrue(flag.exists())
        g.limit = None
        e2 = Engine(g, NullColorBackend(), self.dir)
        self.assertTrue(curves.is_identity_ramp(g.ramps[D1]))
        self.assertFalse(flag.exists())
        e2.set_enabled(True)
        e2.set_enabled(False)
        self.assertTrue(curves.is_identity_ramp(g.ramps[D1]))
        e2.shutdown()

    def test_off_while_windows_refuses_puts_the_display_back_later(self):
        flag = self.dir / "display_modified.flag"
        g = CountingGamma()
        e = Engine(g, NullColorBackend(), self.dir)
        e.settings = Settings(shadow_boost=30)
        e.set_enabled(True)
        g.limit = -1
        e.set_enabled(False)       # the off hotkey at a bad moment
        self.assertFalse(curves.is_identity_ramp(g.ramps[D1]))
        self.assertTrue(flag.exists())
        g.limit = None
        self.assertTrue(e.check_and_reapply())     # keep-applied tries again
        self.assertTrue(curves.is_identity_ramp(g.ramps[D1]))
        self.assertFalse(flag.exists())
        self.assertFalse(e.check_and_reapply())
        e.shutdown()


if __name__ == "__main__":
    unittest.main()
