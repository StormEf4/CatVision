"""Normal display in menus (beta): the menu detector (synthetic pictures only),
its timing, and the engine's glide to normal and back.

The GUI test only runs when CATVISION_GUI_TESTS is set and tkinter plus a
display are available.
"""

from __future__ import annotations

import os
import random
import shutil
import tempfile
import time
import unittest

from catvision import autolevels, curves
from catvision.autolevels import (MENU_OFF_S, MENU_ON_S, MENU_SCORE, THUMB_H, THUMB_W,
                                  MenuWatch, measure)
from catvision.curves import Settings
from catvision.engine import Engine, menu_matrix, menu_ramp
from tests.test_catvision import FakeColor, FakeGamma

D1, D2 = "\\\\.\\DISPLAY1", "\\\\.\\DISPLAY2"
W, H = THUMB_W, THUMB_H


def picture(pixel) -> bytes:
    """A BGRA thumbnail from ``pixel(x, y) -> (r, g, b)``."""
    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b = pixel(x, y)
            out += bytes([b, g, r, 255])
    return bytes(out)


def navy(x, y):
    return (14, 22, 38)


def text(x, y, x0=2, x1=W - 2):
    """Small bright UI text: short bright strokes with gaps."""
    return x0 <= x < x1 and (x // 2) % 3 != 0


def menu_like(top_bar=True, bottom=(2, W - 2), base=navy):
    """A menu: plain dark background, a line of hints just above the bottom
    edge (two rows tall at this size), optionally a menu bar along the top."""
    def px(x, y):
        if y in (H - 3, H - 2) and text(x, y, *bottom):
            return (220, 220, 220)
        if top_bar and y == 1 and text(x, y):
            return (230, 230, 230)
        return base(x, y)
    return picture(px)


def hud(scene):
    """Gameplay: ``scene`` with the game's HUD in the corners (health bars
    bottom-left, weapon bottom-right) and a narrow compass at the top centre."""
    def px(x, y):
        if 44 <= y <= 50 and (x < 22 and y % 2 == 0 or x >= 78 and text(x, y, 78, 94)):
            return (230, 230, 230)
        if 1 <= y <= 2 and 40 <= x < 56 and text(x, y, 40, 56):
            return (240, 240, 240)
        return scene(x, y)
    return picture(px)


def night(seed=1):
    rnd = random.Random(seed)
    noise = [[rnd.randint(0, 6) for _ in range(W)] for _ in range(H)]

    def scene(x, y):   # dark blue moonlit: a dark, mostly flat picture
        n = noise[y][x]
        return (6 + n, 10 + n, 28 + n + (12 if y < 18 else 0))
    return scene


def fog(x, y):
    v = 120 + (x + y) // 6
    return (v, v, v + 4)


def busy(seed=2):
    rnd = random.Random(seed)
    vals = [[rnd.randint(0, 255) for _ in range(W)] for _ in range(H)]
    return lambda x, y: (vals[y][x],) * 3


def score(bgra: bytes) -> float:
    return measure(bgra, W).menu


class DetectorTest(unittest.TestCase):
    def test_menus_are_seen(self):
        self.assertGreaterEqual(score(menu_like()), MENU_SCORE)
        # The in-raid inventory: hints only at the left, but a dark, flat
        # navy background.
        self.assertGreaterEqual(score(menu_like(top_bar=False, bottom=(2, 34))), MENU_SCORE)
        # A lobby: a bright busy background, full-width bars top and bottom
        # (a dark band along the bottom behind the hints).
        lobby = busy(5)
        self.assertGreaterEqual(score(menu_like(base=lambda x, y: lobby(x, y)
                                                if 6 <= y <= H - 8 else (30, 30, 30))),
                                MENU_SCORE)
        # The inventory full of item icons (not flat), on its navy background.
        icons = busy(6)
        self.assertGreaterEqual(score(menu_like(top_bar=False, base=lambda x, y: (
            tuple(min(255, c + icons(x, y)[0] // 4) for c in navy(x, y))
            if 6 <= y <= H - 8 and (x // 6) % 2 == 0 else navy(x, y)))), MENU_SCORE)

    def test_hint_line_alone_is_not_enough(self):
        # A line of hints with nothing above or below, but no second sign
        # (no menu bar, not dark): a missed menu rather than a gameplay risk.
        self.assertLess(score(menu_like(top_bar=False, base=lambda x, y: (110, 110, 110))),
                        MENU_SCORE)
        # With a second sign (here a dark, plain background) it counts.
        self.assertGreaterEqual(score(menu_like(top_bar=False, base=navy)), MENU_SCORE)

    def test_night_storm_hud_reaching_the_hint_rows_is_not_a_menu(self):
        # A dark, flat night with the HUD stacked several rows tall in both
        # bottom corners right down to the hint rows and a lightning bolt in
        # the middle: plenty of steps on the hint rows and none on the last
        # row (the old single-cue score called this a menu), but nothing is
        # clear just above them, so it never counts.
        scene = night(3)

        def px(x, y):
            if 44 <= y <= H - 2 and (x < 22 and text(x, y, 2, 22)
                                     or x >= 74 and text(x, y, 74, 94)):
                return (230, 230, 230)
            if 4 <= y <= H - 2 and x == 40 + (y // 4) % 3:
                return (240, 240, 255)
            return scene(x, y)
        bgra = picture(px)
        lum = [(54 * r + 183 * g + 19 * b) >> 8
               for b, g, r in zip(bgra[0::4], bgra[1::4], bgra[2::4])]
        line, quiet, _top, flat, avg, _tint = autolevels.menu_parts(lum, W, H, bgra)
        self.assertGreaterEqual(line, autolevels.MENU_LINE)   # the old gate passes...
        self.assertLess(avg, autolevels.MENU_DARK)
        self.assertGreaterEqual(flat, autolevels.MENU_FLAT)   # ...with a second cue
        self.assertLess(quiet, autolevels.MENU_QUIET)
        self.assertLess(score(bgra), MENU_SCORE)

    def test_gameplay_is_never_a_menu(self):
        cases = {
            "night": hud(night(1)),
            "night 2": hud(night(7)),
            "fog": hud(fog),
            "busy day": hud(busy(3)),
            "busy without HUD": picture(busy(4)),
            "black (loading)": picture(lambda x, y: (0, 0, 0)),
            "white flash": picture(lambda x, y: (255, 255, 255)),
            "flat grey": picture(lambda x, y: (60, 60, 60)),
        }
        for name, bgra in cases.items():
            with self.subTest(name):
                self.assertLess(score(bgra), MENU_SCORE)

    def test_hint_line_alone_on_a_bright_busy_scene_is_not_enough(self):
        # A busy scene reaching the last row cancels the hint line.
        scene = busy(9)
        bgra = picture(lambda x, y: (220, 220, 220) if y in (H - 3, H - 2) and text(x, y)
                       else scene(x, y))
        self.assertLess(score(bgra), MENU_SCORE)

    def test_only_with_the_picture_width(self):
        self.assertEqual(measure(menu_like()).menu, 0.0)


def m_at(menu: bool, at: float, black: bool = False):
    return autolevels.Measurement(0.1, 0.1, 0.1, 0.0, 0.0, black, at=at,
                                  menu=MENU_SCORE + 1 if menu else 0.0)


class MenuWatchTest(unittest.TestCase):
    def feed(self, w, pattern, start=0.0, step=0.1):
        out = []
        for i, kind in enumerate(pattern):
            m = None if kind is None else m_at(kind == "menu", start + i * step,
                                               black=kind == "black")
            out.append(w.update(m, start + i * step))
        return out

    def test_needs_several_menu_pictures(self):
        w = MenuWatch()
        states = self.feed(w, ["menu"] * 6)
        first = states.index(True)
        self.assertGreaterEqual(first * 0.1, MENU_ON_S - 1e-9)
        self.assertGreaterEqual(first + 1, autolevels.MENU_ON_COUNT)
        self.assertLessEqual(first * 0.1, 0.4 + 1e-9)

    def test_one_menu_like_picture_never_switches(self):
        w = MenuWatch()
        self.assertFalse(any(self.feed(w, ["game", "menu", "game", "menu", "menu", "game"] * 3)))

    def test_back_to_gameplay_after_a_short_wait(self):
        w = MenuWatch()
        self.feed(w, ["menu"] * 6)
        self.assertTrue(w.active)
        states = self.feed(w, ["game"] * 5, start=1.0)
        first = states.index(False)
        self.assertGreaterEqual(first * 0.1, MENU_OFF_S - 1e-9)
        self.assertLessEqual(first * 0.1, 0.25 + 1e-9)
        # A single gameplay-like picture inside a menu doesn't switch back.
        w2 = MenuWatch()
        self.feed(w2, ["menu"] * 6)
        self.assertTrue(all(self.feed(w2, ["menu", "game", "menu", "menu", "game", "menu"],
                                      start=1.0)))

    def test_black_counts_as_gameplay_and_missing_pictures_change_nothing(self):
        w = MenuWatch()
        self.feed(w, ["menu"] * 6)
        self.assertTrue(all(self.feed(w, [None] * 4, start=0.6)))
        self.assertTrue(self.feed(w, ["menu"], start=1.0)[-1])
        self.assertFalse(self.feed(w, ["black"] * 4, start=3.0)[-1])
        w.reset()
        self.assertFalse(w.active or w.looks)

    def test_no_picture_for_a_while_ends_menu_mode(self):
        # The capture failing in a menu (a raid starting in exclusive full
        # screen, say) must never leave the display normal during play.
        w = MenuWatch()
        self.feed(w, ["menu"] * 6)
        self.assertTrue(w.active)
        states = self.feed(w, [None] * 15, start=0.6)
        first = states.index(False)
        self.assertGreaterEqual(0.6 + first * 0.1 - 0.5, autolevels.MENU_LOST_S - 1e-9)
        self.assertLessEqual(0.6 + first * 0.1 - 0.5, autolevels.MENU_LOST_S + 0.1 + 1e-9)
        self.assertFalse(any(states[first:]))


class EngineMenuTest(unittest.TestCase):
    def setUp(self):
        self.gamma = FakeGamma(start=curves.identity_ramp())
        # A display with its own (non-straight) table: normal = exactly that.
        own = [[min(65535, int(i * 250)) for i in range(256)] for _ in range(3)]
        self.gamma.ramps[D2] = own
        self.own = own
        self.color = FakeColor()
        self.applied = []
        self.color.apply = lambda effect: self.applied.append(list(effect)) or True
        self.resets = []
        self.color.reset = lambda: self.resets.append(1)
        self.engine = Engine(self.gamma, self.color)
        self.engine.settings = Settings(shadow_boost=60, color_filter="night_vision")
        self.engine.set_enabled(True)
        self.full = {d: self.gamma.get_ramp(d) for d in (D1, D2)}

    def test_glides_to_normal_and_back_exactly(self):
        e = self.engine
        lows = []
        for i in range(1, 13):
            e.set_menu_amount(i / 12)
            lows.append(self.gamma.ramps[D1][1][20])
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        self.assertEqual(self.gamma.ramps[D2], self.own)
        # No big jumps: each step a twelfth of the way.
        gaps = [abs(a - b) for a, b in zip(lows, lows[1:])]
        self.assertLess(max(gaps), abs(self.full[D1][1][20] - curves.identity_ramp()[1][20]) / 6)
        for i in range(11, -1, -1):
            e.set_menu_amount(i / 12)
        self.assertEqual(e.menu_amount, 0.0)
        self.assertEqual({d: self.gamma.get_ramp(d) for d in (D1, D2)}, self.full)

    def test_menu_ramp_and_matrix_ends(self):
        ramp = curves.build_ramp(Settings(shadow_boost=50))
        own = curves.identity_ramp()
        self.assertEqual(menu_ramp(ramp, own, 1.0), own)
        self.assertEqual(menu_ramp(ramp, own, 0.0), ramp)
        m = [[0.5, 0.2, 0.1], [0.1, 0.6, 0.2], [0.0, 0.3, 0.9]]
        self.assertEqual(menu_matrix(m, 0.0), m)
        self.assertTrue(curves.is_identity_matrix(menu_matrix(m, 1.0)))

    def test_color_filter_follows_and_comes_back(self):
        e = self.engine
        e.settings = Settings(saturation=0.4)
        e.apply()
        self.assertTrue(e.status.color_used)
        first = self.applied[-1]
        n = len(self.resets)
        e.set_menu_amount(0.5)
        self.assertNotEqual(self.applied[-1], first)
        e.set_menu_amount(1.0)
        self.assertGreater(len(self.resets), n)   # the filter is off in a menu
        e.set_menu_amount(0.0)
        self.assertEqual(self.applied[-1], first)

    def test_off_mid_menu_never_leaves_it_half_normal(self):
        e = self.engine
        gen = e.menu_generation
        e.set_menu_amount(0.5, gen)
        e.set_enabled(False)
        self.assertEqual(e.menu_amount, 0.0)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        e.set_menu_amount(0.7, gen)          # a late step from that fade: ignored
        self.assertEqual(e.menu_amount, 0.0)
        e.set_enabled(True)
        self.assertEqual({d: self.gamma.get_ramp(d) for d in (D1, D2)}, self.full)

    def test_quit_and_force_quit_mid_menu(self):
        e = self.engine
        e.set_menu_amount(0.4)
        e.restore_now()
        self.assertEqual(e.menu_amount, 0.0)
        self.assertEqual(self.gamma.ramps[D2], self.own)

    def test_peek_is_ignored_in_a_menu(self):
        e = self.engine
        e.set_menu_amount(1.0)
        e.set_peek(40)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        e.end_peek()
        e.set_menu_amount(0.0)
        self.assertEqual({d: self.gamma.get_ramp(d) for d in (D1, D2)}, self.full)

    def test_peek_held_as_a_menu_opens_fades_without_a_jump(self):
        e = self.engine
        e.set_peek(40)
        peeked = self.gamma.get_ramp(D1)
        e.set_menu_amount(0.02)   # the first step of the glide
        step = self.gamma.get_ramp(D1)
        self.assertLess(max(abs(a - b) for ca, cb in zip(peeked, step)
                            for a, b in zip(ca, cb)), 0.03 * 65535)
        e.set_menu_amount(1.0)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        e.end_peek()
        e.set_menu_amount(0.0)
        self.assertEqual({d: self.gamma.get_ramp(d) for d in (D1, D2)}, self.full)

    def test_with_auto_levels_offsets(self):
        e = self.engine
        e.set_auto_offsets({D1: {"shadow_boost": 10.0}}, e.auto_generation)
        boosted = self.gamma.get_ramp(D1)
        e.set_menu_amount(1.0)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        e.set_menu_amount(0.0)
        self.assertEqual(self.gamma.get_ramp(D1), boosted)

    def test_refused_change_is_offered_again(self):
        e = self.engine
        real = self.gamma.set_ramp
        self.gamma.set_ramp = lambda mid, ramp: False
        e.set_menu_amount(0.5)
        self.assertFalse(e.status.gamma_ok)
        self.gamma.set_ramp = real
        e._retry_at = {k: 0.0 for k in e._retry_at}   # due now
        e.set_menu_amount(1.0)
        self.assertTrue(e.status.gamma_ok)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())


class ConfigTest(unittest.TestCase):
    def test_option_saved_and_cleaned(self):
        from catvision.config import Options
        self.assertTrue(Options().menu_normal)
        self.assertFalse(Options.from_dict({"menu_normal": False}).menu_normal)
        self.assertTrue(Options.from_dict({"menu_normal": "no"}).menu_normal)
        o = Options()
        o.menu_normal = 3
        o._clean_auto()
        self.assertIs(o.menu_normal, True)
        from catvision.config import Config
        cfg = Config()
        cfg.options.menu_normal = False
        self.assertFalse(Config.from_json(cfg.to_json()).options.menu_normal)


def _gui_ok() -> bool:
    if not os.environ.get("CATVISION_GUI_TESTS"):
        return False
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_gui_ok(), "set CATVISION_GUI_TESTS=1 with tkinter and a display")
class MenuAppTest(unittest.TestCase):
    """The real window with a fake capture: a menu glides the display to
    normal, holds auto levels, and the full look comes back."""

    def setUp(self):
        import tkinter as tk
        from catvision import config as cm
        from catvision.backends import Monitor, NullColorBackend
        from catvision.gui import CatVisionApp
        self.tmp = tempfile.mkdtemp()
        self._old_env = os.environ.get("CATVISION_CONFIG_DIR")
        os.environ["CATVISION_CONFIG_DIR"] = self.tmp
        self.mode = "game"
        pictures = {"game": hud(night(1)), "menu": menu_like(top_bar=False, bottom=(2, 34))}
        self._real_watcher, self._real_front = autolevels.Watcher, autolevels.window_in_front
        real = self._real_watcher
        autolevels.Watcher = lambda: real(capture=lambda rect: pictures.get(self.mode))
        self.front = lambda rects: ({D1} & set(rects), "Game A")
        autolevels.window_in_front = lambda rects: self.front(rects)

        class One(FakeGamma):
            def monitors(self):
                return [Monitor(D1, "Display 1", True, short="Display 1", rect=(0, 0, 800, 600))]
        self.gamma = One()
        del self.gamma.ramps[D2]
        self.engine = Engine(self.gamma, NullColorBackend(), cm.app_dir())
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = CatVisionApp(self.root, self.engine, cm.load())
        self.app.cfg.options.hotkey_popup = False

    def tearDown(self):
        try:
            if not self.app._closed:
                self.app.quit()
        except Exception:
            pass
        autolevels.Watcher, autolevels.window_in_front = self._real_watcher, self._real_front
        if self._old_env is None:
            os.environ.pop("CATVISION_CONFIG_DIR", None)
        else:
            os.environ["CATVISION_CONFIG_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.005)

    def run_menu(self, auto: bool):
        app = self.app
        app.engine.settings = Settings(shadow_boost=60)
        app.set_auto_levels(auto)
        app._set_enabled(True)
        self.pump(1.0)
        full = self.gamma.get_ramp(D1)
        self.assertNotEqual(full, curves.identity_ramp())
        ctrl = app._auto_ctrl.get(D1)
        key = ctrl.key if ctrl is not None else None
        self.mode = "menu"
        self.pump(1.2)
        self.assertTrue(app._menu_on)
        if auto:
            # Held: the menu pictures (a different brightness) taught it nothing.
            self.assertIsNotNone(ctrl)
            self.assertEqual(ctrl.key, key)
        self.assertEqual(self.engine.menu_amount, 1.0)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        self.assertIn("normal display", app.status_var.get()
                      if hasattr(app, "status_var") else "normal display")
        if auto:
            self.assertIn("menu: normal display", app.auto_info.cget("text"))
        self.mode = "game"
        self.pump(1.0)
        self.assertFalse(app._menu_on)
        self.assertEqual(self.engine.menu_amount, 0.0)
        return full

    def test_menu_with_auto_levels_off(self):
        full = self.run_menu(auto=False)
        self.assertEqual(self.gamma.get_ramp(D1), full)

    def test_menu_with_auto_levels_on_holds_the_look(self):
        self.run_menu(auto=True)
        self.assertEqual(self.engine.menu_amount, 0.0)

    def test_off_mid_menu_and_option_off(self):
        app = self.app
        app.engine.settings = Settings(shadow_boost=60)
        app._set_enabled(True)
        self.pump(0.5)
        self.mode = "menu"
        self.pump(0.45)   # part way into the glide, maybe
        app._set_enabled(False)
        self.pump(0.3)
        self.assertEqual(self.engine.menu_amount, 0.0)
        self.assertEqual(self.gamma.ramps[D1], curves.identity_ramp())
        app._set_enabled(True)
        self.pump(1.2)
        self.assertEqual(self.engine.menu_amount, 1.0)
        app.auto_flag_vars["menu_normal"].set(False)
        app._on_auto_option()
        self.pump(0.6)
        self.assertEqual(self.engine.menu_amount, 0.0)
        self.assertNotEqual(self.gamma.get_ramp(D1), curves.identity_ramp())
        self.assertFalse(app.auto_watch.running)   # auto levels is off: nothing to watch

    def test_capture_failing_in_a_menu_brings_the_look_back(self):
        app = self.app
        app.engine.settings = Settings(shadow_boost=60)
        app._set_enabled(True)
        self.pump(0.5)
        full = self.gamma.get_ramp(D1)
        self.mode = "menu"
        self.pump(1.2)
        self.assertEqual(self.engine.menu_amount, 1.0)
        self.mode = "fail"    # the capture returns nothing from now on
        self.pump(1.8)
        self.assertFalse(app._menu_on)
        self.assertEqual(self.engine.menu_amount, 0.0)
        self.assertEqual(self.gamma.get_ramp(D1), full)

    def test_glide_steps_are_small(self):
        from catvision.gui import MENU_STEP_MAX_S
        app = self.app
        app.engine.settings = Settings(shadow_boost=60)
        app._set_enabled(True)
        self.pump(0.5)
        amounts = []
        real = self.engine.set_menu_amount
        self.engine.set_menu_amount = lambda a, g=None: (amounts.append(a), real(a, g))
        self.mode = "menu"
        self.pump(1.2)
        self.mode = "game"
        self.pump(1.0)
        moves = [a for a in amounts if 0.0 < a < 1.0]
        self.assertTrue(moves)
        steps = [abs(b - a) for a, b in zip([0.0] + amounts, amounts)]
        # Eased: at most 1.5x the even share of one capped step.
        most = 1.5 * MENU_STEP_MAX_S / autolevels.MENU_FADE_S + 1e-9
        self.assertLessEqual(max(steps), most)
        self.assertLess(min(moves), 0.05)   # it starts gently
        self.assertEqual(amounts[-1], 0.0)

    def test_refused_glide_back_is_offered_again_with_auto_levels_off(self):
        app = self.app
        app.engine.settings = Settings(shadow_boost=60)
        app._set_enabled(True)
        self.pump(0.5)
        full = self.gamma.get_ramp(D1)
        self.mode = "menu"
        self.pump(1.2)
        self.assertEqual(self.engine.menu_amount, 1.0)
        real = self.gamma.set_ramp
        self.gamma.set_ramp = lambda mid, ramp: False   # Windows refuses for a moment
        self.mode = "game"
        self.pump(0.8)
        self.assertEqual(self.engine.menu_amount, 0.0)
        self.assertNotEqual(self.gamma.get_ramp(D1), full)   # left part way
        self.gamma.set_ramp = real
        self.engine._retry_at = {k: 0.0 for k in self.engine._retry_at}   # due now
        self.pump(0.5)
        self.assertEqual(self.gamma.get_ramp(D1), full)

    def test_menu_only_where_a_game_fills_the_display(self):
        # Auto levels on for every display (not only games): a menu-like
        # picture on a display no full-screen game fills (the desktop,
        # another program) never switches anything.
        app = self.app
        app.cfg.options.auto_game_only = False
        app.engine.settings = Settings(shadow_boost=60)
        self.front = lambda rects: (set(), "")
        app.set_auto_levels(True)
        app._set_enabled(True)
        self.mode = "menu"
        self.pump(1.5)
        self.assertTrue(app.auto_watch.running)
        self.assertFalse(app._menu_on)
        self.assertEqual(self.engine.menu_amount, 0.0)
        # The game comes to the front showing its menu: now it counts.
        self.front = lambda rects: ({D1} & set(rects), "Game A")
        self.pump(1.2)
        self.assertTrue(app._menu_on)
        self.assertEqual(self.engine.menu_amount, 1.0)


if __name__ == "__main__":
    unittest.main()
