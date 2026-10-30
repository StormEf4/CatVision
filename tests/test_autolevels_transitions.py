"""Auto levels (beta): walking into and out of dark places, as the eye sees it.

A small simulation: flat grey pictures are measured five times a second
(each reaching the controller three frames later, like the window's tick),
the fader moves 60 times a second, and what the screen shows is worked out
from the "Dark Areas" preset's brightness table with the current offset.
"Too dark" / "too bright" means more than 15% away from what the screen
shows once auto levels has settled for that picture.
"""

import unittest

from catvision.autolevels import (CALM_S, SETTLE_FAST_S, SETTLE_S, Controller, measure,
                                  target_offset)
from catvision.curves import Settings, build_ramp

FRAME = 1 / 60
STRENGTH, AIM = 30.0, 0.12
DARK, BRIGHT = 5, 120          # a dark room and a daylit street (grey levels)

_measured: dict = {}
_ramps: dict = {}


def _m(level):
    if level not in _measured:
        _measured[level] = measure(bytes([level, level, level, 255]) * 400)
    return _measured[level]


def _settled(level):
    return target_offset(_m(level).key, STRENGTH, AIM)


def _shown(level, offset):
    """Brightness the screen shows for this grey level (0-1)."""
    q = round(offset, 1)
    if q not in _ramps:
        _ramps[q] = build_ramp(Settings(shadow_boost=66 + q, exposure=3, anti_glare=100,
                                        black_floor=0.02, contrast=1.34))[1]
    return _ramps[q][level] / 65535


def _run(level_at, seconds, start=None, latency_frames=3):
    """[(time, offset, level)] for each 1/60 s frame; starts settled."""
    c = Controller()
    first = level_at(0.0) if start is None else start
    c.offset = c.want = _settled(first)
    c.observe(_m(first), STRENGTH, AIM)
    pending, frames = [], []
    for i in range(int(seconds * 60)):
        t = i * FRAME
        if i % 12 == 0:                                   # five measurements a second
            pending.append((i + latency_frames, _m(level_at(t))))
        while pending and pending[0][0] <= i:
            c.observe(pending.pop(0)[1], STRENGTH, AIM)
        c.advance(FRAME)
        frames.append((t, c.offset, level_at(t)))
    return frames


def _too_dark_s(frames, since):
    return sum(FRAME for t, off, lv in frames
               if t >= since and _shown(lv, off) < 0.85 * _shown(lv, _settled(lv)))


def _too_bright_s(frames, since):
    return sum(FRAME for t, off, lv in frames
               if t >= since and _shown(lv, off) > 1.15 * _shown(lv, _settled(lv)))


def _largest_step(frames):
    return max(abs(b[1] - a[1]) for a, b in zip(frames, frames[1:]))


def _reversals(frames, since, leg=0.5):
    """How often the offset changes direction (legs of more than `leg` points)."""
    count, direction, anchor = 0, 0, None
    for t, off, _lv in frames:
        if t < since:
            continue
        if anchor is None:
            anchor = off
        if direction >= 0 and off < anchor - leg:
            count += direction == 1
            direction, anchor = -1, off
        elif direction <= 0 and off > anchor + leg:
            count += direction == -1
            direction, anchor = 1, off
        elif (direction == 1 and off > anchor) or (direction == -1 and off < anchor):
            anchor = off
    return count


def _gradual(start, end, t0, seconds):
    """Brightness changing evenly (in stops) from start to end over seconds."""
    def level_at(t):
        if t < t0:
            return start
        if t >= t0 + seconds:
            return end
        return int(round(start * (end / start) ** ((t - t0) / seconds)))
    return level_at


class EnteringDarkTests(unittest.TestCase):
    def test_walking_into_a_dark_room_is_not_left_too_dark(self):
        frames = _run(lambda t: BRIGHT if t < 2 else DARK, 6)
        # It used to wait a full second and then rise slowly (about 2.4 s
        # too dark); a steady dark scene now brightens after SETTLE_FAST_S.
        self.assertLess(_too_dark_s(frames, 2), 1.8)
        self.assertGreater(frames[-1][1], _settled(DARK) - 1)

    def test_walking_into_a_cave_starts_brightening_on_the_way(self):
        frames = _run(_gradual(BRIGHT, DARK, 2, 1.2), 7)
        self.assertLess(_too_dark_s(frames, 2), 2.0)       # was about 2.7 s
        self.assertEqual(_reversals(frames, 2), 0)          # one smooth move, no wobble

    def test_brightening_stays_smooth(self):
        for frames in (_run(lambda t: BRIGHT if t < 2 else DARK, 6),
                       _run(_gradual(BRIGHT, DARK, 2, 1.2), 7)):
            self.assertLess(_largest_step(frames), 1.4)      # never a visible step
            shown = [_shown(lv, off) for _t, off, lv in frames]
            jumps = [abs(b - a) for (a, b, (_t, _o, la), (_t2, _o2, lb))
                     in zip(shown, shown[1:], frames, frames[1:]) if la == lb]
            self.assertLess(max(jumps), 0.01)                # under 1% of the screen's range

    def test_a_big_change_catches_up_all_the_way(self):
        c = Controller()
        frames = 0
        while c.offset < 29 and frames < 600:
            c.follow(30.0, STRENGTH, FRAME)
            frames += 1
        # Without the slow last stretch it gets there in about a second (was ~2 s).
        self.assertLess(frames * FRAME, 1.3)

    def test_returning_from_a_quick_peek_outside(self):
        frames = _run(lambda t: BRIGHT if 3 <= t < 3.6 else DARK, 8, start=DARK)
        self.assertLess(_too_dark_s(frames, 3.6), 2.0)      # was about 2.4 s
        self.assertLess(_too_bright_s(frames, 3.0), 0.3)    # dims for the daylight at once


class LeavingDarkTests(unittest.TestCase):
    def test_walking_out_into_daylight_dims_without_lingering_glare(self):
        frames = _run(_gradual(DARK, BRIGHT, 3, 1.0), 7, start=DARK)
        self.assertLess(_too_bright_s(frames, 3), 0.6)
        self.assertLess(abs(frames[-1][1] - _settled(BRIGHT)), 1.0)

    def test_door_opening_onto_daylight_dims_like_a_flash(self):
        frames = _run(lambda t: DARK if t < 3 else BRIGHT, 6, start=DARK)
        self.assertLess(_too_bright_s(frames, 3), 0.45)
        self.assertLess(_largest_step(frames), 4.1)

    def test_coming_out_of_a_cave_in_steps_fades_like_a_flash(self):
        # Brightening over 1.5 s: each measurement is under FLASH_RATIO
        # brighter, but it keeps brightening while the display is still
        # boosted far too much, so it fades at the flash pace (it used to
        # glare for about 0.7 s).
        frames = _run(_gradual(DARK, BRIGHT, 3, 1.5), 8, start=DARK)
        self.assertLess(_too_bright_s(frames, 3), 0.45)
        self.assertEqual(_reversals(frames, 3), 0)
        self.assertLess(_largest_step(frames), 4.1)
        self.assertLess(abs(frames[-1][1] - _settled(BRIGHT)), 1.0)

    def test_one_brighter_picture_still_dims_at_the_normal_pace(self):
        def settled_at(level):
            c = Controller()
            c.offset = c.want = _settled(level)
            c.observe(_m(level), STRENGTH, AIM)
            return c

        def wait(c):
            for _ in range(12):
                c.advance(FRAME)
        # A light flickering on (less than a flash) dims as far as before,
        # at the normal pace, so flicker never dims more than it used to...
        for ratio in (1.25, 1.4, 1.6, 1.9):
            c = settled_at(20)
            wait(c)
            c.observe(_m(int(round(20 * ratio))), STRENGTH, AIM)
            self.assertLess(c.want, c.offset)
            self.assertFalse(c.rush, ratio)
        # ...but brightening again while that fade is still going is the
        # way out of the dark: the rest fades at the flash pace.
        c = settled_at(20)
        wait(c)
        c.observe(_m(32), STRENGTH, AIM)
        wait(c)
        self.assertFalse(c.rush)
        c.observe(_m(51), STRENGTH, AIM)
        self.assertTrue(c.rush)


class NoPumpingTests(unittest.TestCase):
    def test_a_strobe_never_lets_it_brighten_between_flashes(self):
        # A light flashing on for 0.2 s every 0.8 s: a light went on less
        # than CALM_S ago, so the steady-dark shortcut stays off, it waits
        # the full settle time and never pumps up in between.
        self.assertGreater(CALM_S, 0.8)
        self.assertLessEqual(SETTLE_FAST_S, SETTLE_S)
        frames = _run(lambda t: BRIGHT if t >= 2 and (t - 2) % 0.8 < 0.2 else DARK, 8,
                      start=DARK)
        later = [off for t, off, _lv in frames if t > 2.6]
        self.assertLess(max(later) - min(later), 1.0)

    def test_a_soft_flicker_does_not_pump_either(self):
        # Only 1.4x brighter when on (a lamp buzzing, a TV in the room):
        # still counts as a light going on, so it never aims brighter.
        c = Controller()
        goals = []
        for i in range(45):                                  # 9 s, five measurements a second
            t = i * 0.2
            c.observe(_m(14 if t >= 2 and (t - 2) % 1.2 < 0.4 else 10), STRENGTH, AIM)
            for _ in range(12):
                c.advance(FRAME)
            if t >= 2:
                goals.append(c.want)
        self.assertEqual(max(goals), goals[0])               # never aims brighter in between

    def test_flicker_every_measurement_does_not_pump(self):
        frames = _run(lambda t: BRIGHT if int(t / 0.2) % 2 else DARK, 8, start=DARK)
        later = [off for t, off, _lv in frames if t > 1.0]
        self.assertLess(max(later) - min(later), 1.0)

    def test_early_brightening_is_not_taken_back(self):
        # A scene that settles a little unevenly after going dark: once it
        # has started brightening early, the older bright pictures can't
        # pull it back down.
        levels = [BRIGHT] * 10 + [12, 7, 6, 7, 6, 8, 6, 7, 6, 6, 7, 6] + [6] * 15
        c = Controller()
        c.offset = c.want = _settled(BRIGHT)
        wants = []
        for level in levels:
            c.observe(_m(level), STRENGTH, AIM)
            for _ in range(12):
                c.advance(FRAME)
            wants.append(c.want)
        rising = wants[10:]
        self.assertEqual(rising, sorted(rising))
        self.assertGreater(rising[-1], _settled(BRIGHT) + 20)

    def test_lights_on_still_dims_at_once(self):
        c = Controller()
        for _ in range(30):
            c.step(_m(DARK), STRENGTH, AIM, 0.2)
        c.observe(_m(BRIGHT), STRENGTH, AIM)
        self.assertAlmostEqual(c.want, _settled(BRIGHT))
        self.assertTrue(c.rush)


if __name__ == "__main__":
    unittest.main()
