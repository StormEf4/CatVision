"""Auto levels (beta): the Windows calls of a fading update and the small
per-update helpers stay cheap without changing what they do.

The Windows backend runs here against a stand-in for gdi32 that keeps
track of device contexts and refuses ramps that stray too far, like a PC
where Windows limits the strength.
"""

import ctypes
import random
import threading
import unittest
from dataclasses import fields, replace
from unittest import mock

from catvision import autolevels, backends, curves
from catvision.autolevels import FADER_MIN_STEP, Controller, Fader
from catvision.backends import NullColorBackend, NullGammaBackend, WindowsGammaBackend
from catvision.curves import Settings
from catvision.engine import Engine

SEED = 20261030


class FakeGdi:
    """Device contexts, gamma ramps and refusals, the way gdi32 has them."""

    def __init__(self, limit=None):
        self.limit = limit            # largest deviation taken (None: anything)
        self.next = 100
        self.open: set[int] = set()
        self.stale: set[int] = set()  # contexts outdated by a display change
        self.gone: set[str] = set()   # displays no longer connected
        self.created = self.deleted = self.sets = 0
        self.shown: dict[str, list] = {}
        self.names: dict[int, str] = {}

    def CreateDCW(self, name, _a, _b, _c):
        if name in self.gone:
            return None
        self.created += 1
        self.next += 1
        self.open.add(self.next)
        self.names[self.next] = name
        return self.next

    def DeleteDC(self, hdc):
        assert hdc in self.open, "a device context closed twice (or never opened)"
        self.open.discard(hdc)
        self.deleted += 1
        return True

    def SetDeviceGammaRamp(self, hdc, ref):
        assert hdc in self.open, "a closed device context was used"
        self.sets += 1
        if hdc in self.stale:
            return False
        ramp = [list(ch) for ch in ref._obj]
        if self.limit is not None and backends.ramp_deviation(ramp) > self.limit:
            return False
        self.shown[self.names[hdc]] = ramp
        return True


def windows_backend(gdi: FakeGdi) -> WindowsGammaBackend:
    """The real WindowsGammaBackend methods on top of the stand-in."""
    b = object.__new__(WindowsGammaBackend)
    b._ctypes = ctypes
    b._gdi32 = gdi
    b._RAMP = (ctypes.c_ushort * 256) * 3
    b._dcs = {}
    b._dc_lock = threading.Lock()
    b._layout_seen = {}
    return b


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def ramp_for(boost: float):
    return curves.build_ramp(Settings(shadow_boost=boost))


class SetRampQuickTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        patcher = mock.patch.object(backends, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_fading_updates_reuse_one_context(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        for i in range(120):
            self.assertTrue(b.set_ramp_quick("D1", ramp_for(20 + i * 0.1)))
            self.clock.now += 1 / 240
        self.assertEqual(gdi.created, 1)
        self.assertEqual(gdi.sets, 120)
        self.assertEqual(gdi.shown["D1"], ramp_for(20 + 119 * 0.1))
        b.close()
        self.assertEqual(gdi.open, set(), "close() must close the kept context")

    def test_context_is_renewed_after_reuse_time(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        b.set_ramp_quick("D1", ramp_for(10))
        self.clock.now += backends.DC_REUSE_S + 0.01
        self.assertTrue(b.set_ramp_quick("D1", ramp_for(11)))
        self.assertEqual((gdi.created, gdi.deleted), (2, 1))
        self.assertEqual(len(gdi.open), 1)

    def test_outdated_context_never_causes_a_refusal(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        b.set_ramp_quick("D1", ramp_for(10))
        gdi.stale |= gdi.open                 # a display change outdated it
        self.assertTrue(b.set_ramp_quick("D1", ramp_for(12)))
        self.assertEqual(gdi.shown["D1"], ramp_for(12))
        self.assertEqual(len(gdi.open), 1)

    def test_answers_are_set_ramps_on_a_limited_pc(self):
        rng = random.Random(SEED)
        fresh, quick = FakeGdi(limit=96), FakeGdi(limit=96)
        a, b = windows_backend(fresh), windows_backend(quick)
        for i in range(400):
            ramp = ramp_for(rng.uniform(0, 100))
            if rng.random() < 0.3:
                ramp = curves.limit_ramp_deviation(ramp, rng.choice([90, 96, 100, 127]))
            self.assertEqual(a.set_ramp("D1", ramp), b.set_ramp_quick("D1", ramp))
            self.assertEqual(fresh.shown.get("D1"), quick.shown.get("D1"))
            self.clock.now += rng.choice([1 / 120, 1 / 60, 0.3])
        # A refusal is answered by a fresh context, which is then kept: a
        # refused full ramp and the accepted weaker one cost one context.
        self.assertLess(quick.created, fresh.created / 2)
        b.close()
        self.assertEqual(quick.open, set())

    def test_refused_ramp_then_weaker_one(self):
        gdi = FakeGdi(limit=96)
        b = windows_backend(gdi)
        strong = ramp_for(100)
        b.set_ramp_quick("D1", curves.limit_ramp_deviation(strong, 96))
        created = gdi.created
        self.assertFalse(b.set_ramp_quick("D1", strong))
        self.assertTrue(b.set_ramp_quick("D1", curves.limit_ramp_deviation(strong, 90)))
        self.assertEqual(gdi.created - created, 1)
        self.assertEqual(len(gdi.open), 1)

    def test_display_gone(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        b.set_ramp_quick("D1", ramp_for(10))
        gdi.gone.add("D1")
        gdi.stale |= gdi.open
        self.assertFalse(b.set_ramp_quick("D1", ramp_for(11)))
        self.assertFalse(b.set_ramp("D1", ramp_for(11)))
        self.assertEqual(gdi.open, set())
        self.assertEqual(b._dcs, {})

    def test_other_thread_inside_uses_a_fresh_context(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        b.set_ramp_quick("D1", ramp_for(10))
        with b._dc_lock:                       # (another thread is in there)
            self.assertTrue(b.set_ramp_quick("D1", ramp_for(11)))
        self.assertEqual((gdi.created, gdi.deleted), (2, 1))
        self.assertEqual(gdi.shown["D1"], ramp_for(11))

    def test_displays_listed_again_drops_kept_contexts(self):
        gdi = FakeGdi()
        b = windows_backend(gdi)
        for name in ("D1", "D2"):
            b.set_ramp_quick(name, ramp_for(10))
        b._drop_dcs()
        self.assertEqual(gdi.open, set())
        self.assertTrue(b.set_ramp_quick("D1", ramp_for(12)))


class RecordingBackend(NullGammaBackend):
    """Takes ramps up to a limit; records which call each one came by."""

    def __init__(self, limit=None, quick=True):
        super().__init__()
        self.limit = limit
        self.calls: list[str] = []
        if quick:
            self.set_ramp_quick = lambda mid, ramp: self._set("quick", mid, ramp)

    def _set(self, how, mid, ramp):
        self.calls.append(how)
        if self.limit is not None and backends.ramp_deviation(ramp) > self.limit:
            return False
        return super().set_ramp(mid, ramp)

    def set_ramp(self, mid, ramp):
        return self._set("set", mid, ramp)


class FadingUsesQuickTest(unittest.TestCase):
    def run_fade(self, quick: bool, limit):
        backend = RecordingBackend(limit, quick)
        engine = Engine(backend, NullColorBackend())
        engine.set_settings(Settings(shadow_boost=30, gamma=1.2))
        engine.set_enabled(True)
        did = engine.monitors()[0].id
        backend.calls.clear()
        shown = []
        for i in range(150):
            engine.set_auto_offsets({did: {"shadow_boost": 50 * abs((i % 100) - 50) / 50}})
            shown.append((backend.last.get(did), engine.status.gamma_ok,
                          engine.status.gamma_limited))
        return backend.calls, shown

    def test_same_looks_with_or_without_quick(self):
        for limit in (None, 96, 60):
            calls, shown = self.run_fade(True, limit)
            plain_calls, plain_shown = self.run_fade(False, limit)
            self.assertEqual(shown, plain_shown)
            self.assertEqual(len(calls), len(plain_calls))
            # Moving displays go through set_ramp_quick; only a display
            # coming back to rest gets a normal (fresh) apply.
            self.assertGreater(calls.count("quick"), 0.9 * len(calls))

    def test_normal_apply_never_uses_quick(self):
        backend = RecordingBackend(96)
        result = backends.apply_ramp_with_fallback(backend, "preview", ramp_for(100))
        self.assertTrue(result.ok)
        self.assertNotIn("quick", backend.calls)


class FaderMinStepTest(unittest.TestCase):
    """Moves too small to see are gathered up, never lost."""

    def test_applied_moves_and_resting_values(self):
        rng = random.Random(SEED)
        for _ in range(150):
            ctrls = {}
            for i in range(rng.randint(1, 3)):
                c = Controller(offset=rng.uniform(-30, 30), strength=30.0)
                c.want = rng.choice([c.offset, c.offset + rng.uniform(-0.05, 0.05),
                                     rng.uniform(-30, 30)])
                c.rush = rng.random() < 0.2 and c.want < c.offset
                ctrls[f"d{i}"] = c
            applied = []
            fader = Fader(ctrls, applied.append)
            fader.speed = rng.choice(list(autolevels.SPEEDS.values()))
            hz = rng.choice([60, 120])
            fader.tick(1 / hz)
            for _ in range(hz * 15):
                fader.tick(1 / hz)
                # What is shown is never further than the minimum step
                # behind where a display really is.
                for did, c in ctrls.items():
                    self.assertLess(abs(applied[-1][did] - c.offset), FADER_MIN_STEP + 1e-9)
                if fader.settled():
                    break
            self.assertTrue(fader.settled())
            self.assertEqual(applied[-1], {d: c.want for d, c in ctrls.items()})
            # Each call carries a move worth showing, or a display's exact
            # resting value.
            for a, b in zip(applied, applied[1:]):
                self.assertTrue(any(abs(a[d] - b[d]) >= FADER_MIN_STEP
                                    or (a[d] != b[d] and b[d] == ctrls[d].want) for d in b))

    def test_fast_fades_pass_on_every_step(self):
        c = Controller(offset=0.0, want=40.0, strength=30.0)
        applied = []
        fader = Fader({"a": c}, applied.append)
        for _ in range(60):
            fader.tick(1 / 120)
        moving = [b["a"] - a["a"] for a, b in zip(applied, applied[1:])]
        self.assertGreater(len(applied), 50)
        self.assertTrue(all(m > 0 for m in moving))


class SmallHelpersExactTest(unittest.TestCase):
    def test_settings_for_matches_replace_and_clamp(self):
        rng = random.Random(SEED)
        engine = Engine(NullGammaBackend(), NullColorBackend())
        for _ in range(3000):
            base = Settings(shadow_boost=rng.uniform(0, 100), exposure=rng.uniform(0, 100),
                            gamma=rng.uniform(0.5, 3.0),
                            color_filter=rng.choice(list(curves.COLOR_FILTERS)))
            engine.set_settings(base)
            offsets = {k: rng.choice([0.0, rng.uniform(-120, 120), round(rng.uniform(-60, 60), 2),
                                      100.0, -100.0])
                       for k in rng.sample(["shadow_boost", "exposure"], rng.randint(1, 2))}
            engine.auto_offsets = {"d": offsets}
            amounts = {k: v for k, v in offsets.items() if v}
            want = (replace(engine.settings, **{k: getattr(engine.settings, k) + v
                                               for k, v in amounts.items()}).clamped()
                    if amounts else engine.settings)
            got = engine.settings_for("d")
            for f in fields(Settings):
                a, b = getattr(got, f.name), getattr(want, f.name)
                self.assertEqual((type(a), a), (type(b), b), f.name)
            if amounts:   # a copy: the preset itself never changes
                self.assertIsNot(got, engine.settings)
            self.assertEqual(engine.settings, base.clamped(), "the preset must not change")

    def test_plan_tint_matches_plan_color(self):
        rng = random.Random(SEED)
        custom_tints = {"custom:a": (0.3, 1.0, 0.4), "custom:b": (1.0, 1.0, 1.0)}
        keys = list(curves.COLOR_FILTERS) + list(custom_tints) + [
            "custom:", "bogus", 7, None, "custom:missing"]
        for _ in range(3000):
            s = Settings(color_filter=rng.choice(keys),
                         filter_strength=rng.choice([0.0, 1.0, -0.5, 1.5, 2, rng.random(),
                                                     rng.uniform(-1, 2)]))
            for every in (True, False):
                tint, _m = curves.plan_color(s, {}, custom_tints, all_displays=every)
                self.assertEqual(curves.plan_tint(s, custom_tints), tint, repr(s))


class ReadableGdi(FakeGdi):
    """The stand-in, with GetDeviceGammaRamp as well."""

    def GetDeviceGammaRamp(self, hdc, ref):
        assert hdc in self.open, "a closed device context was used"
        shown = self.shown.get(self.names[hdc])
        if shown is None:
            return False
        for c in range(3):
            ref._obj[c][:] = shown[c]
        return True


class RatedGdi(FakeGdi):
    """The stand-in, with a display refresh rate (GetDeviceCaps VREFRESH)."""

    hz = 144

    def GetDeviceCaps(self, hdc, index):
        assert hdc in self.open, "a closed device context was used"
        return self.hz if index == 116 else 0


class DisplayChangeTest(unittest.TestCase):
    """A display change seen while reading the layout (refresh rate or
    positions) closes the contexts kept for fading updates at once."""

    def test_unchanged_readings_keep_the_context(self):
        gdi = RatedGdi()
        b = windows_backend(gdi)
        self.assertEqual(b._refresh_rate("D1"), 144)
        self.assertTrue(b.set_ramp_quick("D1", ramp_for(10)))
        for _ in range(3):
            self.assertEqual(b._refresh_rate("D1"), 144)
            b._note_layout("rects", {"D1": (0, 0, 2560, 1440)})
        self.assertIn("D1", b._dcs)
        self.assertEqual(len(gdi.open), 1)

    def test_changed_refresh_rate_closes_kept_contexts(self):
        gdi = RatedGdi()
        b = windows_backend(gdi)
        b._refresh_rate("D1")
        b.set_ramp_quick("D1", ramp_for(10))
        gdi.hz = 60
        self.assertEqual(b._refresh_rate("D1"), 60)
        self.assertEqual(b._dcs, {})
        self.assertEqual(gdi.open, set())
        created = gdi.created
        self.assertTrue(b.set_ramp_quick("D1", ramp_for(11)))   # a fresh one
        self.assertEqual(gdi.created, created + 1)
        self.assertEqual(gdi.shown["D1"], ramp_for(11))

    def test_changed_positions_close_kept_contexts(self):
        gdi = RatedGdi()
        b = windows_backend(gdi)
        b._note_layout("rects", {"D1": (0, 0, 1920, 1080)})
        b.set_ramp_quick("D1", ramp_for(10))
        b.set_ramp_quick("D2", ramp_for(10))
        rects = {"D1": (0, 0, 2560, 1440)}
        self.assertIs(b._note_layout("rects", rects), rects)
        self.assertEqual(b._dcs, {})
        self.assertEqual(gdi.open, set())

    @unittest.skipUnless(hasattr(Engine, "refresh_layout"), "needs Engine.refresh_layout")
    def test_engine_layout_refresh_closes_kept_contexts(self):
        gdi = RatedGdi()
        b = windows_backend(gdi)
        did = "\\\\.\\DISPLAY1"
        rects = {did: (0, 0, 2560, 1440)}
        b._monitor_rects = lambda: b._note_layout("rects", dict(rects))  # (no user32 here)
        b.monitors = lambda: [backends.Monitor(did, "Display 1", True, rect=rects[did],
                                               hz=b._refresh_rate(did))]
        b.get_ramp = lambda _id: None
        e = Engine(b, NullColorBackend())
        e.monitors()
        self.assertFalse(e.refresh_layout())
        b.set_ramp_quick(did, ramp_for(10))
        self.assertFalse(e.refresh_layout())
        self.assertIn(did, b._dcs, "nothing changed: the context is kept")
        gdi.hz = 60                                  # a game switched the refresh rate
        self.assertTrue(e.refresh_layout())
        self.assertEqual((b._dcs, gdi.open), ({}, set()))
        b.set_ramp_quick(did, ramp_for(11))
        rects[did] = (0, 0, 1920, 1080)              # ...or the resolution
        self.assertTrue(e.refresh_layout())
        self.assertEqual((b._dcs, gdi.open), ({}, set()))

    def test_unreported_rate_reads_as_60(self):
        gdi = RatedGdi()
        b = windows_backend(gdi)
        gdi.hz = 1   # "the hardware default"
        self.assertEqual(b._refresh_rate("D1"), 60)
        gdi.hz = 0
        self.assertEqual(b._refresh_rate("D1"), 60)


class SelftestGammaCallsTest(unittest.TestCase):
    """The self-test's gamma_calls check on the real backend methods: the
    kept-context way is measured and read back, every context is closed
    again and the display gets its own ramp back."""

    def run_check(self, limit=None):
        from catvision import selftest
        gdi = ReadableGdi(limit=limit)
        b = windows_backend(gdi)
        b.monitors = lambda: [backends.Monitor("\\\\.\\DISPLAY1", "Display 1", True)]
        own = ramp_for(4)
        gdi.shown["\\\\.\\DISPLAY1"] = own
        with mock.patch("time.sleep") as sleep:
            out = selftest.gamma_call_cost(b, calls=6)
        sleep.assert_called_once()
        self.assertNotIn("error", out)
        self.assertEqual(gdi.open, set(), "a device context was left open")
        self.assertEqual(gdi.shown["\\\\.\\DISPLAY1"], own)
        self.assertTrue(out["restored"])
        return out

    def test_quick_is_measured_and_read_back(self):
        out = self.run_check()
        for key in ("dc_ms", "set_ramp_ms", "set_only_ms", "set_ramp_quick_ms"):
            self.assertIsInstance(out[key], float, key)
        self.assertTrue(out["quick_ok"])
        self.assertEqual(out["quick_readback_max_diff"], 0)
        self.assertGreater(out["quick_look_vs_own_max_diff"], 0)
        self.assertTrue(out["quick_renewed_ok"])
        self.assertTrue(out["layout_reread_keeps_dc"])
        self.assertEqual(out["refresh_hz"], 60)   # the stand-in doesn't say
        self.assertTrue(out["strong_accepted"])

    def test_limited_pc(self):
        out = self.run_check(limit=96)
        self.assertFalse(out["strong_accepted"])
        self.assertIn("refused_ms", out)
        self.assertTrue(out["quick_ok"])


if __name__ == "__main__":
    unittest.main()
