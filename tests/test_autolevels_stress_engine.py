"""Auto levels (beta): stress tests for the engine, the gamma fallback and
the fader thread.  Deterministic (fixed seeds), fast, no tkinter needed."""
import random
import threading
import time
import unittest

from catvision import autolevels, curves
from catvision.autolevels import Controller, Fader, split_offset
from catvision.backends import NullColorBackend, apply_ramp_with_fallback
from catvision.curves import Settings
from catvision.engine import Engine
from tests.test_catvision import FakeGamma

D1 = "\\\\.\\DISPLAY1"
D2 = "\\\\.\\DISPLAY2"
D3 = "\\\\.\\DISPLAY3"
ALL = (D1, D2, D3)
LIMITS = (None, 127, 96, 60, 30, 10)


def _accepts(limit, ramp) -> bool:
    if limit is None:
        return True
    return all(abs((v >> 8) - i) <= limit for ch in ramp for i, v in enumerate(ch))


def _monotonic(ramp) -> bool:
    return (len(ramp) == 3 and all(len(ch) == 256 for ch in ramp)
            and all(0 <= v <= 65535 for ch in ramp for v in ch)
            and all(a <= b for ch in ramp for a, b in zip(ch, ch[1:])))


class StrictGamma(FakeGamma):
    """FakeGamma with three displays that records every call and checks every
    ramp offered (accepted or not) is a valid, non-decreasing table."""

    def __init__(self, limit=None):
        super().__init__(limit=limit)
        # Slightly unusual originals so a "restore" is distinguishable.
        orig3 = [[min(65535, i * 257 + (i % 3)) for i in range(256)] for _ in range(3)]
        self.ramps[D3] = orig3
        self.originals = {k: [list(c) for c in v] for k, v in self.ramps.items()}
        self.log = []          # (display, accepted)
        self.offered = []      # (display, hash of the ramp offered)
        self.bad = []          # problems found in offered ramps
        self.lock = threading.Lock()

    def set_ramp(self, mid, ramp):
        with self.lock:
            if not _monotonic(ramp):
                self.bad.append(("non-monotonic or malformed ramp", mid))
            ok = super().set_ramp(mid, ramp)
            if ok and not _accepts(self.limit, self.ramps[mid]):
                self.bad.append(("stored a ramp beyond the limit", mid))
            self.log.append((mid, ok))
            self.offered.append((mid, hash(tuple(tuple(c) for c in ramp))))
            return ok

    def get_ramp(self, mid):
        with self.lock:
            return super().get_ramp(mid)

    def calls_to(self, mid):
        return sum(1 for d, _ in self.log if d == mid)


def _engine(limit, settings=None, enabled=True, displays=()):
    g = StrictGamma(limit)
    e = Engine(g, NullColorBackend())
    if settings is not None:
        e.settings = settings.clamped()
    e.set_displays(list(displays))
    e.set_enabled(enabled)
    return g, e


def _random_settings(rng):
    return Settings(shadow_boost=rng.choice([0, 10, 25, 40, 60, 85, 100]),
                    exposure=rng.choice([0, 0, 5, 15, 40, 70]),
                    gamma=rng.choice([1.0, 1.0, 1.2, 0.9]),
                    contrast=rng.choice([1.0, 1.0, 1.3, 0.8]),
                    white_cap=rng.choice([1.0, 1.0, 0.85]),
                    black_floor=rng.choice([0.0, 0.0, 0.08]),
                    red=rng.choice([1.0, 1.0, 0.8]),
                    anti_glare=rng.choice([0, 0, 40]))


def _random_offsets(rng, settings, displays=ALL):
    """Spill-like amounts, as the window's _fader_apply makes them."""
    base = {"shadow_boost": settings.shadow_boost, "exposure": settings.exposure}
    out = {}
    for did in displays:
        if rng.random() < 0.3:
            continue
        key = rng.choice(["shadow_boost", "exposure"])
        offset = rng.uniform(-60, 60)
        amounts = {k: round(v, 2) for k, v in split_offset(key, offset, base).items()}
        amounts = {k: v for k, v in amounts.items() if v}
        out[did] = amounts
    return out


def _expected_full(e, did):
    tint, _ = curves.plan_color(e.settings, e.custom_filters, e.custom_tints,
                                all_displays=e.all_displays_selected())
    return curves.build_ramp(e.settings_for(did), tint)


class RandomEngineSequenceTest(unittest.TestCase):
    """Random sequences of everything the window can do, at several limits."""

    STEPS = 60

    def _run(self, limit, seed):
        rng = random.Random(seed)
        g, e = _engine(limit, _random_settings(rng))
        for step in range(self.STEPS):
            ctx = f"limit={limit} seed={seed} step={step}"
            op = rng.random()
            targets_before = {m.id for m in e.target_monitors()}
            others_before = {d: g.get_ramp(d) for d in ALL if d not in targets_before}
            calls_before = {d: g.calls_to(d) for d in ALL}
            if op < 0.55:
                offs = _random_offsets(rng, e.settings)
                e.set_auto_offsets(offs, generation=e.auto_generation)
                kind = "offsets"
            elif op < 0.65:
                e.set_settings(_random_settings(rng))
                kind = "settings"
            elif op < 0.73:
                e.set_displays(rng.choice([[], [D1], [D2], [D1, D3], [D3], ALL]))
                kind = "displays"
            elif op < 0.80:
                e.set_enabled(not e.enabled if rng.random() < 0.6 else e.enabled)
                kind = "enabled"
            elif op < 0.90:
                had = bool(e.auto_offsets)
                e.retire_auto_offsets()
                kind = "retire"
                if e.enabled and had:
                    for m in e.target_monitors():
                        full = _expected_full(e, m.id)
                        if _accepts(limit, full):
                            self.assertEqual(g.get_ramp(m.id), full,
                                             f"{ctx}: preset not restored after retire")
            else:
                self.assertFalse(e.check_and_reapply(), f"{ctx}: false drift")
                kind = "check"

            self.assertEqual(g.bad, [], ctx)
            # Fading updates never touch displays that are not chosen.
            if kind == "offsets":
                for d, ramp in others_before.items():
                    self.assertEqual(g.get_ramp(d), ramp, f"{ctx}: untargeted {d} changed")
                    self.assertEqual(g.calls_to(d), calls_before[d],
                                     f"{ctx}: untargeted {d} was written")
            if e.enabled:
                # No false drift right after anything.
                self.assertFalse(e.check_and_reapply(), f"{ctx} after {kind}: false drift")
                # Untargeted displays sit at their originals.
                for d in ALL:
                    if d not in {m.id for m in e.target_monitors()}:
                        self.assertEqual(g.get_ramp(d), g.originals[d], f"{ctx}: {d}")
            else:
                for d in ALL:
                    self.assertEqual(g.get_ramp(d), g.originals[d],
                                     f"{ctx}: off but {d} not original")

        e.set_enabled(False)
        for d in ALL:
            self.assertEqual(g.get_ramp(d), g.originals[d], f"limit={limit}: final off")

    def test_random_sequences_all_limits(self):
        for limit in LIMITS:
            for seed in (1,):
                with self.subTest(limit=limit, seed=seed):
                    self._run(limit, seed * 1000 + (limit or 0))


class LateGenerationTest(unittest.TestCase):
    def test_old_generation_never_changes_anything(self):
        rng = random.Random(7)
        for limit in LIMITS:
            g, e = _engine(limit, Settings(shadow_boost=40))
            e.set_auto_offsets({D1: {"shadow_boost": 20}}, generation=e.auto_generation)
            old = e.auto_generation
            e.retire_auto_offsets()
            for _ in range(20):
                before = ({d: g.get_ramp(d) for d in ALL}, len(g.log),
                          {k: dict(v) for k, v in e.auto_offsets.items()})
                e.set_auto_offsets(_random_offsets(rng, e.settings), generation=old)
                after = ({d: g.get_ramp(d) for d in ALL}, len(g.log),
                         {k: dict(v) for k, v in e.auto_offsets.items()})
                self.assertEqual(before, after, f"limit={limit}")
            # A restarted run (new generation) works again.
            e.set_auto_offsets({D1: {"shadow_boost": 10}}, generation=e.auto_generation)
            self.assertEqual(e.auto_offsets, {D1: {"shadow_boost": 10}})


def _fade(e, did, key, frm, to, steps):
    for i in range(steps + 1):
        v = round(frm + (to - frm) * i / steps, 2)
        e.set_auto_offsets({did: {key: v}} if v else {}, generation=e.auto_generation)


class SetRampBudgetTest(unittest.TestCase):
    """How many set_ramp calls one fading update costs (60 a second)."""

    def test_calls_per_fading_update_stay_small(self):
        report = {}
        for limit in LIMITS:
            g, e = _engine(limit, Settings(shadow_boost=30), displays=[D1])
            per_update = []
            rng = random.Random(11)
            v = 0.0
            for _ in range(100):   # a couple of seconds of a wandering fade, 60 Hz steps
                v = max(-30.0, min(70.0, v + rng.uniform(-1.2, 1.2)))
                n = len(g.log)
                e.set_auto_offsets({D1: {"shadow_boost": round(v, 2)}},
                                   generation=e.auto_generation)
                per_update.append(len(g.log) - n)
            report[limit] = (max(per_update), sum(per_update) / len(per_update))
        for limit, (worst, avg) in report.items():
            with self.subTest(limit=limit, worst=worst, avg=round(avg, 2)):
                self.assertLessEqual(worst, 13)
                self.assertLessEqual(avg, 4.0, f"report={report}")

    def test_steady_rising_fade_does_not_redo_the_full_search_every_frame(self):
        """A smooth brighten (the common case: into a cave) with a tight
        Windows limit: the remembered blend should follow the fade instead of
        falling through to the 12-call search over and over."""
        for limit in (60, 30, 10):
            g, e = _engine(limit, Settings(shadow_boost=20), displays=[D1])
            full_searches = 0
            for i in range(121):   # 0 -> 60 over two seconds at 60 Hz
                n = len(g.log)
                v = round(60 * i / 120, 2)
                e.set_auto_offsets({D1: {"shadow_boost": v}} if v else {},
                                   generation=e.auto_generation)
                if len(g.log) - n >= 11:
                    full_searches += 1
            with self.subTest(limit=limit):
                self.assertLessEqual(full_searches, 12,
                                     f"limit={limit}: {full_searches} full searches in 121 frames")


class NoRepeatedOfferTest(unittest.TestCase):
    def test_same_ramp_never_offered_twice_in_one_fading_update(self):
        """With fallback memory, apply_ramp_with_fallback first offers the full
        ramp; if the remembered version is then refused too it falls through
        and offers the very same full ramp again (a wasted call that Windows
        is certain to refuse).  Only the final re-apply of the best blend may
        repeat an earlier candidate."""
        repeats = {}
        for limit in (60, 30, 10):
            g, e = _engine(limit, Settings(shadow_boost=30), displays=[D1])
            rng = random.Random(11)
            v, count = 0.0, 0
            for _ in range(100):
                v = max(-30.0, min(70.0, v + rng.uniform(-1.2, 1.2)))
                n = len(g.offered)
                e.set_auto_offsets({D1: {"shadow_boost": round(v, 2)}},
                                   generation=e.auto_generation)
                offered = [h for _, h in g.offered[n:]]
                body = offered[:-1] if len(offered) >= 11 else offered   # allow the final re-apply
                count += len(body) - len(set(body))
            repeats[limit] = count
        self.assertEqual(repeats, {lim: 0 for lim in repeats},
                         "identical ramps offered twice within one fading update")


class VisibleStepsTest(unittest.TestCase):
    def test_one_visible_look_per_fading_update(self):
        """Each fading update (60 a second) should put at most one new look
        on screen.  The blend search applies every accepted candidate on the
        way (and the best one again at the end), so during a fade with a tight
        Windows limit the display can pass through several weaker looks within
        one update -- a possible flicker source on real drivers."""
        worst = {}
        for limit in (60, 30, 10):
            g, e = _engine(limit, Settings(shadow_boost=30), displays=[D1])
            rng = random.Random(11)
            v, most = 0.0, 0
            for _ in range(100):
                v = max(-30.0, min(70.0, v + rng.uniform(-1.2, 1.2)))
                n = len(g.log)
                e.set_auto_offsets({D1: {"shadow_boost": round(v, 2)}},
                                   generation=e.auto_generation)
                most = max(most, sum(1 for _, ok in g.log[n:] if ok))
            worst[limit] = most
        self.assertEqual(worst, {lim: 1 for lim in worst},
                         "most accepted set_ramp calls within one fading update")


def _strongest_blend(limit, ramp):
    """The strongest blend (1/128 resolution) a fresh search would find."""
    if _accepts(limit, ramp):
        return 1.0
    for steps in (127, 120, 96):
        if _accepts(limit, curves.limit_ramp_deviation(ramp, steps)):
            return None   # a tier, not a blend
    best = 0.0
    for k in range(1, 129):
        if _accepts(limit, curves.blend_ramp(ramp, k / 128)):
            best = k / 128
    return best


def _deviation(ramp):
    return max(abs((v >> 8) - i) for ch in ramp for i, v in enumerate(ch))


class FallbackMemoryConvergenceTest(unittest.TestCase):
    def test_repeated_same_request_reaches_strongest_accepted(self):
        """apply_ramp_with_fallback with memory: after a strong request the
        memory holds a low blend; then a weaker request repeated N times must
        reach (about) the strongest accepted blend."""
        for limit in (60, 30, 10):
            g = StrictGamma(limit)
            mem: dict = {}
            apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=100)), mem)
            weaker = curves.build_ramp(Settings(shadow_boost=35))
            best = _strongest_blend(limit, weaker)
            for _ in range(80):
                r = apply_ramp_with_fallback(g, D1, weaker, mem)
                self.assertTrue(r.ok)
            got = r.strength if r.limited else 1.0
            with self.subTest(limit=limit):
                self.assertGreaterEqual(got, best - 1 / 64, f"limit={limit} best={best}")

    def test_fade_coming_to_rest_is_not_left_weaker_than_accepted(self):
        """Through the engine: a fade down from a strong boost comes to rest
        (the fader then sends nothing more, as the offsets don't change).  The
        display should not be left noticeably weaker than what Windows would
        accept for that final request."""
        problems = []
        for limit in (60, 30, 10):
            g, e = _engine(limit, Settings(shadow_boost=10), displays=[D1])
            _fade(e, D1, "shadow_boost", 90, 30, 12)    # quick fall, 12 frames
            # Fader at rest: the window sends the same offsets again -> no-op.
            for _ in range(30):
                e.set_auto_offsets({D1: {"shadow_boost": 30}}, generation=e.auto_generation)
            want = _expected_full(e, D1)
            best = _strongest_blend(limit, want)
            have = g.get_ramp(D1)
            best_ramp = want if best == 1.0 else curves.blend_ramp(want, best)
            if _deviation(have) < _deviation(best_ramp) - 4:
                problems.append((limit, _deviation(have), _deviation(best_ramp)))
        self.assertEqual(problems, [], "(limit, shown deviation, strongest accepted deviation)")

    def test_tier_memory_recovers_full_ramp(self):
        g, e = _engine(127, Settings(shadow_boost=10), displays=[D1])
        _fade(e, D1, "shadow_boost", 0, 90, 20)
        self.assertNotEqual(g.get_ramp(D1), _expected_full(e, D1))   # clamped
        _fade(e, D1, "shadow_boost", 90, 1, 20)
        self.assertEqual(g.get_ramp(D1), _expected_full(e, D1))
        e.retire_auto_offsets()
        self.assertEqual(g.get_ramp(D1), curves.build_ramp(e.settings))
        self.assertFalse(e.check_and_reapply())


class StatusAfterFadeTest(unittest.TestCase):
    def test_limited_status_clears_after_fading_back(self):
        """A fade that Windows held back sets status.gamma_limited; once
        auto levels fades back to the (accepted) preset and retires, the
        status must not keep saying Windows is holding the look back."""
        g, e = _engine(127, Settings(shadow_boost=10), displays=[D1])
        self.assertFalse(e.status.gamma_limited)
        _fade(e, D1, "shadow_boost", 0, 90, 20)
        self.assertTrue(e.status.gamma_limited)
        _fade(e, D1, "shadow_boost", 90, 0, 20)
        e.retire_auto_offsets()
        self.assertEqual(g.get_ramp(D1), curves.build_ramp(e.settings))
        self.assertFalse(e.status.gamma_limited, e.status.summary())

    def test_refused_status_follows_the_current_fade(self):
        """Likewise for a refusal: once Windows takes the tables again the
        status must not keep saying it refused the change."""
        g, e = _engine(None, Settings(shadow_boost=10), displays=[D1])
        g.limit = -1                      # refuses everything
        e.set_auto_offsets({D1: {"shadow_boost": 20}}, generation=e.auto_generation)
        self.assertFalse(e.status.gamma_ok)
        g.limit = None
        e.set_auto_offsets({D1: {"shadow_boost": 21}}, generation=e.auto_generation)
        self.assertTrue(e.status.gamma_ok, e.status.summary())
        self.assertEqual(e.status.summary(), "On.")


class UntargetedDisplayTest(unittest.TestCase):
    def test_offsets_for_unchosen_displays_never_written(self):
        for limit in LIMITS:
            g, e = _engine(limit, Settings(shadow_boost=50), displays=[D2])
            n1, n3 = g.calls_to(D1), g.calls_to(D3)
            rng = random.Random(3)
            for _ in range(25):
                e.set_auto_offsets(_random_offsets(rng, e.settings), generation=e.auto_generation)
            e.retire_auto_offsets()
            self.assertEqual((g.calls_to(D1), g.calls_to(D3)), (n1, n3), f"limit={limit}")
            self.assertEqual(g.get_ramp(D1), g.originals[D1])
            self.assertEqual(g.get_ramp(D3), g.originals[D3])

    def test_switching_displays_while_offsets_pending(self):
        g, e = _engine(None, Settings(shadow_boost=30), displays=[D1, D2])
        e.set_auto_offsets({D1: {"shadow_boost": 20}, D2: {"exposure": 10}},
                           generation=e.auto_generation)
        e.set_displays([D1])
        self.assertEqual(g.get_ramp(D2), g.originals[D2])
        self.assertEqual(g.get_ramp(D1), _expected_full(e, D1))
        e.set_auto_offsets({D1: {"shadow_boost": 25}, D2: {"exposure": 30}},
                           generation=e.auto_generation)
        self.assertEqual(g.get_ramp(D2), g.originals[D2])
        e.set_displays([D1, D2])   # D2 comes back with its pending offset
        self.assertEqual(g.get_ramp(D2), _expected_full(e, D2))
        self.assertFalse(e.check_and_reapply())


class FaderThreadStressTest(unittest.TestCase):
    """A real fader thread drives the engine while the 'window' thread
    randomly changes everything and stops/restarts runs."""

    def _apply_for(self, e, key_box):
        def apply(positions, gen):
            settings = e.settings
            base = {k: getattr(settings, k) for k in ("shadow_boost", "exposure")}
            offsets = {}
            for did, off in positions.items():
                amounts = {k: round(v, 2) for k, v in
                           split_offset(key_box[0], off, base).items()}
                amounts = {k: v for k, v in amounts.items() if v}
                if amounts or did in e.auto_offsets:
                    offsets[did] = amounts
            if offsets != e.auto_offsets:
                e.set_auto_offsets(offsets, generation=gen)
        return apply

    def test_fader_vs_window_thread(self):
        for limit in (None, 96, 30):
            with self.subTest(limit=limit):
                self._stress(limit, seed=100 + (limit or 0))

    def _stress(self, limit, seed):
        rng = random.Random(seed)
        g, e = _engine(limit, Settings(shadow_boost=40))
        key_box = ["shadow_boost"]
        apply = self._apply_for(e, key_box)
        ctrls = {d: Controller() for d in ALL}
        for c in ctrls.values():
            c.strength = 40.0
        fader = Fader(ctrls, apply)
        errors = []

        def start_run():
            gen = e.auto_generation
            fader.apply = lambda pos, gen=gen: apply(pos, gen)
            fader.start()

        start_run()
        deadline = time.monotonic() + 0.25
        try:
            while time.monotonic() < deadline:
                op = rng.random()
                try:
                    if op < 0.45:
                        for c in list(ctrls.values()):
                            c.want = rng.uniform(-40, 40)
                        fader.wake()
                    elif op < 0.55:
                        e.set_settings(_random_settings(rng))
                    elif op < 0.63:
                        e.set_displays(rng.choice([[], [D1], [D2, D3], ALL]))
                    elif op < 0.70:
                        e.set_enabled(not e.enabled)
                    elif op < 0.76:
                        key_box[0] = rng.choice(["shadow_boost", "exposure"])
                    elif op < 0.84:
                        # Stop the run (like _auto_finish), then maybe restart.
                        fader.stop()
                        for c in ctrls.values():
                            c.offset = c.want = c.velocity = 0.0
                        e.retire_auto_offsets()
                        if rng.random() < 0.8:
                            start_run()
                    elif op < 0.92:
                        e.check_and_reapply()
                    else:
                        e.restore_now()
                        e.apply()
                except Exception as exc:  # pragma: no cover - reported
                    errors.append(repr(exc))
                time.sleep(rng.uniform(0.0, 0.01))
        finally:
            fader.stop()
        self.assertFalse(fader.running, "fader thread did not stop within its timeout")
        self.assertEqual(errors, [])
        self.assertEqual(fader.last_error, "")
        self.assertEqual(g.bad, [])

        # Wind down like the window does and check the end state.
        for c in ctrls.values():
            c.offset = c.want = c.velocity = 0.0
        e.retire_auto_offsets()
        # A late update from the finished run must be dropped.
        e.set_auto_offsets({D1: {"shadow_boost": 30}}, generation=e.auto_generation - 1)
        self.assertEqual(e.auto_offsets, {})
        e.set_enabled(True)
        self.assertFalse(e.check_and_reapply())
        for m in e.target_monitors():
            full = curves.build_ramp(e.settings)
            if _accepts(limit, full):
                self.assertEqual(g.get_ramp(m.id), full)
        targets = {m.id for m in e.target_monitors()}
        for d in ALL:
            if d not in targets:
                self.assertEqual(g.get_ramp(d), g.originals[d])
        e.set_enabled(False)
        for d in ALL:
            self.assertEqual(g.get_ramp(d), g.originals[d])

    def test_stopped_run_thread_cannot_write_after_retire(self):
        """Stop while the fader is mid-fade: once retire_auto_offsets has
        returned, no set_ramp call may come from the old run."""
        g, e = _engine(None, Settings(shadow_boost=30))
        ctrls = {D1: Controller()}
        ctrls[D1].strength = 40.0
        ctrls[D1].want = 40.0
        apply = self._apply_for(e, ["shadow_boost"])
        gen = e.auto_generation
        fader = Fader(ctrls, lambda pos: apply(pos, gen))
        fader.start()
        t0 = time.monotonic()
        while not e.auto_offsets and time.monotonic() - t0 < 5:
            time.sleep(0.005)
        self.assertTrue(e.auto_offsets, "fader never applied anything")
        # Simulate a stop() whose join timed out: the thread keeps going.
        fader._stop.set()
        e.retire_auto_offsets()
        n = len(g.log)
        time.sleep(0.15)
        fader.stop()
        self.assertEqual(len(g.log), n)
        self.assertEqual(g.get_ramp(D1), curves.build_ramp(e.settings))


class AutolevelsConstantsSanity(unittest.TestCase):
    def test_spill_amounts_stay_in_slider_range(self):
        rng = random.Random(5)
        lo, hi = autolevels.SLIDER_RANGE
        for _ in range(2000):
            base = {"shadow_boost": rng.uniform(lo, hi), "exposure": rng.uniform(lo, hi)}
            key = rng.choice(["shadow_boost", "exposure"])
            out = split_offset(key, rng.uniform(-150, 150), base)
            for k, v in out.items():
                self.assertGreaterEqual(base[k] + v, lo - 1e-9)
                self.assertLessEqual(base[k] + v, hi + 1e-9)


if __name__ == "__main__":
    unittest.main()


def _shown_after_each(g, e, did, values, key="shadow_boost"):
    """Send each offset as a fading update; the deviation on screen and the
    (calls, accepted calls) each update cost."""
    shown, costs = [], []
    for v in values:
        n = len(g.log)
        e.set_auto_offsets({did: {key: v}}, generation=e.auto_generation)
        shown.append(_deviation(g.get_ramp(did)))
        costs.append((len(g.log) - n, sum(1 for _, ok in g.log[n:] if ok)))
    return shown, costs


class NoBlinkAtTheLimitTest(unittest.TestCase):
    """A fade crossing the Windows limit must not dip and climb back
    (reviewers saw 40 -> 32 -> 36 -> 38 -> 39 -> 40 with limit 40)."""

    def test_rising_fade_across_the_limit_never_dips(self):
        for limit in (40, 20):
            g, e = _engine(limit, Settings(shadow_boost=10), displays=[D1])
            values = [round(i, 2) for i in range(1, 71)]   # slow rise to 70
            shown, costs = _shown_after_each(g, e, D1, values)
            wanted = [_deviation(curves.build_ramp(Settings(shadow_boost=10 + v)))
                      for v in values]
            with self.subTest(limit=limit):
                for i in range(1, len(shown)):
                    if wanted[i] >= wanted[i - 1]:
                        self.assertGreaterEqual(shown[i], shown[i - 1],
                                                f"dip at frame {i}: {shown[max(0, i - 3):i + 3]}")
                self.assertEqual(shown[-1], limit)            # holds at the limit
                self.assertLessEqual(max(a for _, a in costs), 1)
                self.assertLessEqual(max(c for c, _ in costs[1:]), 3)
                self.assertEqual(g.bad, [])

    def test_memory_crossing_the_limit_holds_the_shown_strength(self):
        g = StrictGamma(40)
        mem: dict = {}
        shown = []
        for boost in range(20, 60):
            r = apply_ramp_with_fallback(g, D1, curves.build_ramp(Settings(shadow_boost=boost)),
                                         mem)
            self.assertTrue(r.ok)
            shown.append(_deviation(g.get_ramp(D1)))
        crossed = [s for s in shown if s >= 38]
        self.assertTrue(crossed)
        self.assertEqual(shown, sorted(shown), shown)
        self.assertEqual(shown[-1], 40)

    def test_first_fading_update_after_a_full_apply_does_not_dip(self):
        """A full apply (settings change, display switch, drift fix) forgets
        the fading memory; the next refused fading update must start from
        what that apply put on screen, not from far below it."""
        for limit in (40, 30):
            g, e = _engine(limit, Settings(shadow_boost=60), displays=[D1])
            preset_shown = _deviation(g.get_ramp(D1))
            for k in range(5):
                e.set_settings(Settings(shadow_boost=60 + k))     # full apply
                before = _deviation(g.get_ramp(D1))
                n = len(g.log)
                e.set_auto_offsets({D1: {"shadow_boost": 1.0 + k}},
                                   generation=e.auto_generation)
                after = _deviation(g.get_ramp(D1))
                with self.subTest(limit=limit, k=k):
                    self.assertGreaterEqual(after, before, (preset_shown, before, after))
                    self.assertLessEqual(len(g.log) - n, 3)
                    self.assertLessEqual(sum(1 for _, ok in g.log[n:] if ok), 1)


class RecoveryAfterRefusalBurstTest(unittest.TestCase):
    """A brief burst of refusals (a fullscreen switch) must not leave the
    display weaker for the rest of the session."""

    def test_climbs_back_smoothly_within_two_seconds(self):
        for limit, burst_limit in ((40, 12), (30, -1)):
            g, e = _engine(limit, Settings(shadow_boost=60), displays=[D1])
            wobble = [40.0 + (i % 2) * 0.5 for i in range(400)]   # fader keeps sending
            shown, _ = _shown_after_each(g, e, D1, wobble[:12])
            steady = shown[-1]
            self.assertEqual(steady, limit)
            g.limit = burst_limit
            _shown_after_each(g, e, D1, wobble[12:18])               # 6 refusing frames
            g.limit = limit
            shown, costs = _shown_after_each(g, e, D1, wobble[18:18 + 100])
            with self.subTest(limit=limit, burst=burst_limit):
                back = next((i for i, s in enumerate(shown) if s >= steady), None)
                self.assertIsNotNone(back, shown)
                self.assertLessEqual(back, 90, shown)                # within ~1.5 s
                self.assertEqual(shown[-1], steady)
                for a, b in zip(shown, shown[1:]):                   # small steps up
                    self.assertGreaterEqual(b, a, shown)
                    self.assertLessEqual(b - a, max(2, a // 5), shown)
                self.assertLessEqual(max(a for _, a in costs), 1)
                self.assertLessEqual(max(c for c, _ in costs), 3)
                self.assertEqual(g.bad, [])


class PresetExactAtRestTest(unittest.TestCase):
    def test_display_back_to_zero_matches_the_preset_apply(self):
        """While auto levels runs for another display, a display that fades
        back to no adjustment gets exactly the ramp the preset alone gives."""
        for limit in (40, 127, None):
            for rest in ({}, {"shadow_boost": 0.0}, {"shadow_boost": 0.0, "exposure": 0.0}):
                g0, e0 = _engine(limit, Settings(shadow_boost=70), displays=[D1, D2])
                preset = g0.get_ramp(D1)
                g, e = _engine(limit, Settings(shadow_boost=70), displays=[D1, D2])
                _fade(e, D1, "shadow_boost", 0, 25, 10)
                e.set_auto_offsets({D1: {"shadow_boost": 3.0}, D2: {"exposure": 5.0}},
                                   generation=e.auto_generation)
                e.set_auto_offsets({D1: dict(rest), D2: {"exposure": 6.0}},
                                   generation=e.auto_generation)
                with self.subTest(limit=limit, rest=rest):
                    self.assertEqual(g.get_ramp(D1), preset)
                    self.assertFalse(e.check_and_reapply())
                    self.assertEqual(g.bad, [])


class RefusingDisplayBudgetTest(unittest.TestCase):
    def test_display_that_refuses_everything_costs_one_call(self):
        g, e = _engine(None, Settings(shadow_boost=30), displays=[D1])
        g.limit = -1
        _, costs = _shown_after_each(g, e, D1, [10.0 + 0.1 * i for i in range(125)])
        calls = [c for c, _ in costs]
        self.assertFalse(e.status.gamma_ok)
        self.assertLessEqual(sum(calls) / len(calls), 1.5, calls)
        self.assertLessEqual(max(calls), 13)
        # ...and recovers on the very next update once Windows takes it.
        g.limit = None
        e.set_auto_offsets({D1: {"shadow_boost": 50.0}}, generation=e.auto_generation)
        self.assertEqual(g.get_ramp(D1), _expected_full(e, D1))
        self.assertTrue(e.status.gamma_ok)

    def test_one_or_two_levels_allowed_is_remembered(self):
        for limit in (1, 2):
            g, e = _engine(limit, Settings(shadow_boost=30), displays=[D1])
            shown, costs = _shown_after_each(g, e, D1, [10.0 + 0.1 * i for i in range(80)])
            calls = [c for c, _ in costs[1:]]
            with self.subTest(limit=limit):
                self.assertTrue(all(s == limit for s in shown), shown)
                self.assertLessEqual(max(a for _, a in costs), 1)
                self.assertLessEqual(max(calls), 3)
                self.assertLessEqual(sum(calls) / len(calls), 2.2)
                self.assertEqual(g.bad, [])
