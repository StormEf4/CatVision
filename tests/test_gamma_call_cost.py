"""The self-test's measurement of where a brightness change's time goes on
Windows (device context, SetDeviceGammaRamp): run against a stand-in for
the Windows backend, it must put the display back and never raise."""

import ctypes
import unittest

from catvision import curves, selftest
from catvision.backends import Monitor, NullGammaBackend, ramp_deviation


class FakeGdi:
    """CreateDCW / SetDeviceGammaRamp / DeleteDC, refusing strong ramps
    like Windows does by default."""

    def __init__(self, backend):
        self.backend = backend
        self.open = 0
        self.next_dc = 1

    def CreateDCW(self, name, *_):
        self.open += 1
        self.next_dc += 1
        return self.next_dc

    def DeleteDC(self, hdc):
        self.open -= 1
        return True

    def SetDeviceGammaRamp(self, hdc, ref):
        buf = ref._obj
        ramp = [list(buf[c]) for c in range(3)]
        return self.backend.store(ramp)


class FakeWindowsGamma:
    def __init__(self, limit=127, broken=None):
        self._ctypes = ctypes
        self._RAMP = (ctypes.c_ushort * 256) * 3
        self._gdi32 = FakeGdi(self)
        self.limit = limit
        self.broken = broken
        self.shown = curves.identity_ramp()
        self.calls = 0

    def store(self, ramp) -> bool:
        self.calls += 1
        if self.broken is not None and self.calls == self.broken:
            raise OSError("driver went away")
        if ramp_deviation(ramp) > self.limit:
            return False
        self.shown = ramp
        return True

    def monitors(self):
        return [Monitor("\\\\.\\DISPLAY1", "Display 1", True)]

    def _with_dc(self, monitor_id, fn):
        hdc = self._gdi32.CreateDCW(monitor_id, None, None, None)
        try:
            return fn(hdc)
        finally:
            self._gdi32.DeleteDC(hdc)

    def set_ramp(self, monitor_id, ramp):
        return self.store([list(ch) for ch in ramp])

    def get_ramp(self, monitor_id):
        return [list(ch) for ch in self.shown]


class GammaCallCostTests(unittest.TestCase):
    def test_measures_and_puts_the_display_back(self):
        g = FakeWindowsGamma()
        out = selftest.gamma_call_cost(g, calls=10)
        self.assertNotIn("error", out)
        for key in ("dc_ms", "dc_cpu_ms", "set_ramp_ms", "set_ramp_cpu_ms",
                    "set_only_ms", "set_only_cpu_ms", "refused_ms"):
            self.assertGreaterEqual(out[key], 0.0, key)
        self.assertEqual(out["set_only_accepted"], 10)
        self.assertFalse(out["strong_accepted"])
        self.assertTrue(out["restored"])
        self.assertEqual(g.shown, curves.identity_ramp())
        self.assertEqual(g._gdi32.open, 0)   # every device context closed

    def test_nudge_is_tiny_and_keeps_rising(self):
        g = FakeWindowsGamma()
        seen = []
        store = g.store
        g.store = lambda ramp: seen.append(ramp) or store(ramp)
        selftest.gamma_call_cost(g, calls=4)
        ident = curves.identity_ramp()
        for ramp in seen:
            if ramp_deviation(ramp) > 1:
                continue   # the strong ramp
            diff = sum(abs(a - b) for ch, ich in zip(ramp, ident) for a, b in zip(ch, ich))
            self.assertLessEqual(diff, 1)
            for ch in ramp:
                self.assertEqual(ch, sorted(ch))

    def test_full_strength_unlocked(self):
        g = FakeWindowsGamma(limit=255)
        out = selftest.gamma_call_cost(g, calls=4)
        self.assertTrue(out["strong_accepted"])
        self.assertNotIn("refused_ms", out)
        self.assertEqual(g.shown, curves.identity_ramp())

    def test_problems_are_reported_not_raised(self):
        g = FakeWindowsGamma(broken=7)
        out = selftest.gamma_call_cost(g, calls=10)
        self.assertIn("driver went away", out["error"])
        self.assertTrue(out["restored"])
        self.assertEqual(g.shown, curves.identity_ramp())

    def test_other_backends_are_skipped(self):
        self.assertIn("skipped", selftest.gamma_call_cost(NullGammaBackend()))
        g = FakeWindowsGamma()
        g.get_ramp = lambda monitor_id: None
        self.assertIn("skipped", selftest.gamma_call_cost(g))


if __name__ == "__main__":
    unittest.main()
