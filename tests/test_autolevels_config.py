"""Auto levels (beta): settings, hotkeys, tray and user-facing text audit.

Plain tests run everywhere (including the Windows CI run of
``python -m unittest``).  The GUI tests only run when CATVISION_GUI_TESTS is
set and tkinter + a display are available.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
import types
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from catvision import autolevels, hotkeys
from catvision.config import Config, Options

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUI_SRC = (ROOT / "catvision" / "gui.py").read_text(encoding="utf-8")
TRAY_SRC = (ROOT / "catvision" / "tray.py").read_text(encoding="utf-8")

AUTO_ACTIONS = ("auto_levels", "auto_keep", "auto_brighter", "auto_darker")
S_LO, S_HI = autolevels.STRENGTH_LIMITS
_REAL_WATCHER = autolevels.Watcher
T_LO, T_HI = autolevels.TARGET_LIMITS


def _readme_section() -> str:
    start = README.index("## Auto levels (beta)")
    end = README.index("\n## ", start + 5)
    return README[start:end]


def _gui_const(name: str) -> float:
    """A module constant from gui.py (read from the source: gui imports
    tkinter, which the plain test run may not have)."""
    m = re.search(rf"^{name} = ([0-9.]+)", GUI_SRC, re.M)
    return float(m.group(1))


def _opts(**auto) -> Options:
    return Options.from_dict(auto)


def _finite_in(value, lo, hi) -> bool:
    return isinstance(value, float) and math.isfinite(value) and lo <= value <= hi


# ---------------------------------------------------------------------------
# Options.from_dict
# ---------------------------------------------------------------------------

class OptionsDefaultsTest(unittest.TestCase):
    def test_defaults_match_engine_constants(self):
        o = Options()
        self.assertFalse(o.auto_levels)
        self.assertEqual(o.auto_strength, autolevels.DEFAULT_STRENGTH)
        self.assertEqual(o.auto_target, autolevels.DEFAULT_TARGET)
        self.assertEqual(o.auto_speed, "normal")
        self.assertIn(o.auto_speed, autolevels.SPEEDS)
        self.assertTrue(o.auto_game_only)
        self.assertTrue(o.auto_dim)
        self.assertTrue(o.auto_haze)
        self.assertEqual(o.auto_game_looks, {})
        self.assertTrue(S_LO <= o.auto_strength <= S_HI)
        self.assertTrue(T_LO <= o.auto_target <= T_HI)

    def test_not_a_dict_gives_defaults(self):
        for data in (None, [], "auto", 3, 1.5, True):
            self.assertEqual(asdict(Options.from_dict(data)), asdict(Options()))

    def test_wrong_types_fall_back_to_defaults(self):
        bad = {"auto_levels": "yes", "auto_strength": "40", "auto_target": [0.2],
               "auto_speed": 2, "auto_game_only": None, "auto_dim": {}, "auto_haze": "no",
               "auto_game_looks": ["Game", 0.2]}
        o = Options.from_dict(bad)
        d = Options()
        for key in bad:
            self.assertEqual(getattr(o, key), getattr(d, key), key)

    def test_int_for_bool_is_not_taken(self):
        o = _opts(auto_levels=1, auto_dim=0)
        self.assertIs(o.auto_levels, False)
        self.assertIs(o.auto_dim, True)

    def test_good_values_round_trip_through_json(self):
        c = Config()
        o = c.options
        o.auto_levels, o.auto_strength, o.auto_target = True, 45.0, 0.2
        o.auto_speed, o.auto_game_only, o.auto_dim = "fast", False, False
        o.auto_haze = False
        o.auto_game_looks = {"ARC Raiders": 0.15, "Hunt: Showdown 1896": 0.25}
        back = Config.from_json(json.loads(json.dumps(c.to_json())))
        self.assertEqual(asdict(back.options), asdict(o))

    def test_bad_auto_values_never_break_loading(self):
        raw = ('{"options": {"auto_strength": NaN, "auto_target": Infinity, '
               '"auto_game_looks": {"A": -Infinity, "B": NaN}}}')
        Config.from_json(json.loads(raw))   # must not raise


class OptionsAutoValuesTest(unittest.TestCase):
    """What a hand-edited or old config can hold.  The controller is fed
    cfg.options.auto_strength / auto_target / auto_game_looks directly (see
    CatVisionApp._auto_tick and _auto_aim), so the stored values themselves
    have to be sane, not just the slider."""

    def test_int_strength_and_target_are_kept(self):
        # A hand-edited config ("auto_strength": 40) is silently reset to 30.
        o = _opts(auto_strength=40, auto_target=0)
        self.assertEqual(o.auto_strength, 40.0)

    def test_int_target_is_taken_and_clamped(self):
        o = _opts(auto_target=1)
        self.assertTrue(_finite_in(float(o.auto_target), T_LO, T_HI), o.auto_target)

    def test_nan_and_inf_strength_become_finite(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(v=v):
                o = _opts(auto_strength=v)
                self.assertTrue(_finite_in(o.auto_strength, S_LO, S_HI), o.auto_strength)

    def test_nan_and_inf_target_become_finite(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(v=v):
                o = _opts(auto_target=v)
                self.assertTrue(_finite_in(o.auto_target, T_LO, T_HI), o.auto_target)

    def test_out_of_range_strength_is_clamped(self):
        for v, want in ((1000.0, S_HI), (-50.0, S_LO), (60.5, S_HI)):
            with self.subTest(v=v):
                self.assertEqual(_opts(auto_strength=v).auto_strength, want)

    def test_out_of_range_target_is_clamped(self):
        for v, want in ((5.0, T_HI), (0.0, T_LO), (-1.0, T_LO), (0.31, T_HI)):
            with self.subTest(v=v):
                self.assertEqual(_opts(auto_target=v).auto_target, want)

    def test_unknown_speed_becomes_normal(self):
        for v in ("turbo", "", "Fast", "SLOW", "x" * 10000):
            with self.subTest(v=v[:10]):
                self.assertIn(_opts(auto_speed=v).auto_speed, autolevels.SPEEDS)

    def test_known_speeds_are_kept(self):
        for v in autolevels.SPEEDS:
            self.assertEqual(_opts(auto_speed=v).auto_speed, v)


class OptionsGameLooksTest(unittest.TestCase):
    def test_non_numeric_looks_are_dropped(self):
        o = _opts(auto_game_looks={"A": "0.2", "B": None, "C": [0.2], "D": 0.2})
        self.assertEqual(o.auto_game_looks, {"D": 0.2})

    def test_bool_looks_are_dropped(self):
        # True is an int in Python, so {"A": true} became an Aim for of 100%.
        o = _opts(auto_game_looks={"A": True, "B": False})
        self.assertEqual(o.auto_game_looks, {})

    def test_nan_and_inf_looks_are_dropped_or_clamped(self):
        o = _opts(auto_game_looks={"A": float("nan"), "B": float("inf"),
                                   "C": float("-inf")})
        for title, aim in o.auto_game_looks.items():
            self.assertTrue(_finite_in(aim, T_LO, T_HI), (title, aim))

    def test_out_of_range_looks_are_clamped(self):
        o = _opts(auto_game_looks={"A": 5.0, "B": -1.0, "C": 0, "D": 1})
        for title, aim in o.auto_game_looks.items():
            self.assertTrue(_finite_in(aim, T_LO, T_HI), (title, aim))

    def test_int_looks_become_floats(self):
        o = _opts(auto_game_looks={"A": 0})
        for aim in o.auto_game_looks.values():
            self.assertIsInstance(aim, float)

    def test_huge_titles_are_cut_to_window_title_length(self):
        o = _opts(auto_game_looks={"G" * 100000: 0.2})
        (title,) = o.auto_game_looks
        self.assertLessEqual(len(title), 60)
        # and match what window_in_front would report for that window
        self.assertEqual(title, autolevels.clean_title("G" * 100000))

    def test_titles_are_cleaned_like_window_titles(self):
        # window_in_front() reports clean_title(...) (whitespace collapsed),
        # so a saved key with odd whitespace could never match a game again.
        raw = "  ARC   Raiders\t"
        o = _opts(auto_game_looks={raw: 0.2})
        self.assertEqual(list(o.auto_game_looks), [autolevels.clean_title(raw)])

    def test_empty_title_is_not_a_game(self):
        # "" means "no game in front"; a look saved under "" would apply to
        # the desktop / windowed apps when auto_game_only is off.
        o = _opts(auto_game_looks={"": 0.2, "   ": 0.2})
        self.assertEqual(o.auto_game_looks, {})

    def test_too_many_looks_are_capped(self):
        looks = {f"Game {i}": 0.1 for i in range(5000)}
        o = _opts(auto_game_looks=looks)
        self.assertLessEqual(len(o.auto_game_looks), 1000)

    def test_non_string_keys_are_strings_after_load(self):
        o = _opts(auto_game_looks={7: 0.2, (1, 2): 0.15})
        for title in o.auto_game_looks:
            self.assertIsInstance(title, str)


# ---------------------------------------------------------------------------
# Hotkeys
# ---------------------------------------------------------------------------

class AutoHotkeysTest(unittest.TestCase):
    def test_every_auto_action_has_a_beta_label(self):
        for action in AUTO_ACTIONS:
            with self.subTest(action=action):
                label = hotkeys.ACTION_LABELS.get(action)
                self.assertTrue(label, f"{action} has no label")
                self.assertIn("(beta)", label)
                self.assertIn("Auto ", label)   # Auto mode / Auto levels
                self.assertEqual(hotkeys.action_label(action), label)

    def test_no_other_action_mentions_auto_levels(self):
        for action, label in hotkeys.ACTION_LABELS.items():
            if "auto" in label.lower():
                self.assertIn(action, AUTO_ACTIONS)

    def test_suggested_auto_keys_are_valid_and_allowed(self):
        for action in AUTO_ACTIONS:
            with self.subTest(action=action):
                combo = hotkeys._SUGGESTED_KEYS.get(action)
                self.assertTrue(combo, f"{action} has no suggested key")
                self.assertIsNotNone(hotkeys.parse_combo(combo))
                self.assertEqual(hotkeys.normalize_combo(combo), combo)
                self.assertIsNone(hotkeys.combo_problem(combo))

    def test_suggested_keys_are_unique(self):
        keys = hotkeys.suggested_hotkeys()
        parsed = [hotkeys.parse_combo(c) for c in keys.values()]
        self.assertNotIn(None, parsed)
        self.assertEqual(len(parsed), len(set(parsed)))
        for action in AUTO_ACTIONS:
            self.assertIn(action, keys)

    def test_suggested_keys_all_bind(self):
        bound = {b.action for b in hotkeys.bindings_from_config(hotkeys.suggested_hotkeys())}
        for action in AUTO_ACTIONS:
            self.assertIn(action, bound)

    def test_auto_actions_do_not_repeat(self):
        for action in AUTO_ACTIONS:
            self.assertNotIn(action, hotkeys.REPEATING_ACTIONS)

    def test_auto_actions_are_not_legacy_defaults(self):
        # The upgrade check compares to LEGACY_DEFAULT_HOTKEYS, which is built
        # from _SUGGESTED_KEYS and so now includes the auto actions that 1.1-1.3
        # never had: a 1.1-1.3 config can no longer match it exactly.
        for action in AUTO_ACTIONS:
            self.assertNotIn(action, hotkeys.LEGACY_DEFAULT_HOTKEYS)

    def test_real_1_3_default_hotkeys_are_still_cleared(self):
        # Exactly what 1.1-1.3 saved (before auto levels existed).
        old = {"toggle": "Ctrl+Alt+V", "off": "Ctrl+Alt+0", "next": "Ctrl+Alt+Page Down",
               "prev": "Ctrl+Alt+Page Up", "boost_up": "Ctrl+Alt+=",
               "boost_down": "Ctrl+Alt+-", "filter_next": "Ctrl+Alt+F",
               "show_window": "Ctrl+Alt+C"}
        for n, name in enumerate([
                "Gentle Boost", "Dark Areas", "Pitch Black", "Night Vision",
                "Horror / Flashlight", "Fog & Haze Cutter", "Mixed Light",
                "High-Contrast Mono", "Warm Low-Light"], start=1):
            old[f"preset:{name}"] = f"Ctrl+Alt+{n}"
        self.assertEqual(Config.from_json({"hotkeys": old}).hotkeys, {})

    def test_gui_handles_every_auto_action(self):
        for action in AUTO_ACTIONS:
            self.assertRegex(GUI_SRC, rf'action (==|in \() ?[^\n]*"{action}"')

    def test_readme_hotkey_table_matches_labels(self):
        for action in ("auto_levels", "auto_keep"):
            self.assertIn(f"| {hotkeys.ACTION_LABELS[action]} |", README)
        self.assertIn("| Auto levels: aim brighter / darker (beta) |", README)

    def test_readme_suggested_keys_match(self):
        sec = _readme_section()
        for action in ("auto_brighter", "auto_darker"):
            self.assertIn(hotkeys._SUGGESTED_KEYS[action], sec)


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

class _Item:
    def __init__(self, text, action=None, checked=None, enabled=True, visible=True,
                 radio=False, default=False):
        self.text, self.action = text, action
        self.checked, self.enabled, self.visible = checked, enabled, visible


class _Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items


def _fake_tray_modules():
    ps = types.ModuleType("pystray")
    ps.MenuItem, ps.Menu = _Item, _Menu
    pil = types.ModuleType("PIL")
    image = types.ModuleType("PIL.Image")
    image.open = lambda *_a, **_k: object()
    pil.Image = image
    return {"pystray": ps, "PIL": pil, "PIL.Image": image}


class AutoTrayTest(unittest.TestCase):
    def setUp(self):
        from catvision import tray
        self.calls = []
        with mock.patch.dict(sys.modules, _fake_tray_modules()):
            self.tray = tray.Tray(self.calls.append)
            self.assertTrue(self.tray.available)
            self.menu = self.tray.build_menu()
        self.items = {i.text: i for i in self.menu.items
                      if isinstance(i, _Item) and isinstance(i.text, str)}

    def _auto_items(self):
        return [i for t, i in self.items.items() if t.lower().startswith("auto")]

    def test_auto_items_present_and_wired(self):
        wanted = {"auto_levels", "auto_keep"}
        for item in self._auto_items():
            item.action(None, item)
        self.assertEqual(set(self.calls), wanted)

    def test_every_auto_item_is_marked_beta(self):
        items = self._auto_items()
        self.assertGreaterEqual(len(items), 2)
        for item in items:
            with self.subTest(text=item.text):
                self.assertIn("(beta)", item.text)

    def test_auto_toggle_check_follows_state(self):
        item = next(i for i in self._auto_items() if i.text.startswith("Auto mode (beta)"))
        self.assertFalse(item.checked(item))
        self.tray.state.auto_levels = True
        self.assertTrue(item.checked(item))

    def test_keep_look_only_enabled_while_running(self):
        item = next(i for i in self._auto_items() if "keep" in i.text.lower())
        st = self.tray.state
        for auto, on, want in ((False, False, False), (True, False, False),
                               (False, True, False), (True, True, True)):
            st.auto_levels, st.enabled = auto, on
            self.assertEqual(bool(item.enabled(item)), want, (auto, on))

    def test_gui_passes_auto_state_to_tray(self):
        self.assertIn("auto_levels=self.cfg.options.auto_levels", GUI_SRC)


# ---------------------------------------------------------------------------
# User-facing text versus the code
# ---------------------------------------------------------------------------

class AutoTextTest(unittest.TestCase):
    def setUp(self):
        self.sec = _readme_section()

    def test_readme_section_is_labelled_beta(self):
        self.assertIn("**Beta: new and still being tested.**", self.sec)
        self.assertIn("**Auto levels (beta)**", README)

    def test_readme_glide_rate_matches_fader(self):
        # One step per refresh of the display, from FADER_HZ up to FADER_MAX_HZ.
        self.assertIn(f"from {autolevels.FADER_HZ} up to {autolevels.FADER_MAX_HZ})", self.sec)
        self.assertEqual(autolevels.fader_rate([30]), autolevels.FADER_HZ)
        self.assertEqual(autolevels.fader_rate([240]), autolevels.FADER_MAX_HZ)

    def test_readme_measure_rate_matches_watcher(self):
        self.assertAlmostEqual(1 / autolevels.INTERVAL_S, 5.0)
        self.assertIn("five times a second", self.sec)

    def test_flash_fade_is_a_fraction_of_a_second_at_every_speed(self):
        # The README promises a flash is "toned down within a fraction of a
        # second" and the Speed setting scales the fade, so check the claim
        # itself at every speed: 90% of the way down well within a second.
        flat = " ".join(self.sec.split())
        self.assertIn("toned down within a fraction of a second", flat)
        dark = autolevels.measure(bytes([4, 4, 4, 255]) * 400)
        flash = autolevels.measure(bytes([250, 250, 250, 255]) * 400)
        for name, speed in autolevels.SPEEDS.items():
            c = autolevels.Controller()
            for _ in range(60):
                c.step(dark, 30, 0.12, 0.2, speed)
            start = c.offset
            c.observe(flash, 30, 0.12)
            goal = start - 0.9 * (start - c.want)
            t = 0.0
            while c.offset > goal and t < 2.0:
                c.advance(1 / 60, speed)
                t += 1 / 60
            self.assertLess(t, 1.0, f"{name}: a flash took {t:.2f} s to fade")

    def test_flash_reaction_including_measure_delay(self):
        # The README promises a flash is "toned down within about a tenth of a
        # second".  A flash is only seen at the next measurement (every
        # INTERVAL_S), then picked up by the next GUI tick (AUTO_TICK_MS),
        # then fades with time constant RUSH_S.  Worst case to get most of
        # the way (2 time constants) down:
        worst = autolevels.INTERVAL_S + _gui_const("AUTO_TICK_MS") / 1000 + 2 * autolevels.RUSH_S
        # Test fixed: the timing is by design, so check the README no longer
        # promises the whole reaction within a tenth of a second.
        flat = " ".join(self.sec.split())
        if worst > 0.2:
            self.assertNotIn("toned down within about a tenth of a second", flat)
            self.assertIn("within a fraction of a second", flat)
        self.assertLess(worst, 1.0, f"worst case is about {worst:.2f} s")

    def test_lost_picture_timing_wording(self):
        # Stale for AUTO_STALE_S, then observe(None) for LOST_S before easing
        # back: about AUTO_STALE_S + LOST_S in total.
        total = _gui_const("AUTO_STALE_S") + autolevels.LOST_S
        # Test fixed: check the README wording against the timing instead of
        # requiring the timing to be short.  Copies that stop arriving take
        # AUTO_STALE_S + LOST_S; a capture that fails outright (None every
        # measurement) takes LOST_S alone ("a couple of seconds").
        flat = " ".join(self.sec.split())
        self.assertIn(f"after about {['zero', 'one', 'two', 'three', 'four'][round(total)]} "
                      "seconds it eases back", flat)
        self.assertLessEqual(autolevels.LOST_S, 2.5)
        self.assertIn("see the picture at all for a couple of seconds", flat)

    def test_frozen_wording_matches_constant(self):
        # "about three seconds" to be called frozen (FROZEN_S), then the
        # lost-picture hold of "a couple more seconds" (LOST_S) before it
        # eases back to the preset.
        flat = " ".join(self.sec.split())
        self.assertIn("doesn't change at all for about three seconds", flat)
        self.assertIn("holds for a couple more seconds", flat)
        self.assertAlmostEqual(autolevels.FROZEN_S, 3.0)
        self.assertAlmostEqual(autolevels.LOST_S, 2.0)
        self.assertIn("the picture isn't changing", GUI_SRC)
        self.assertIn("*the picture isn't changing*", self.sec)

    def test_cant_see_wording_matches_gui(self):
        self.assertIn("can't see the picture", GUI_SRC)
        self.assertIn("*can't see the\n  picture*", self.sec)

    def test_readme_example_numbers_match_presets(self):
        from catvision.presets import builtin_by_name
        self.assertIn("Pitch Black is 85", self.sec)
        self.assertEqual(builtin_by_name("Pitch Black").settings.shadow_boost, 85)
        m = re.search(r"Shadow boost (\d+) → (\d+)", self.sec)
        self.assertIsNotNone(m)
        base, now = int(m.group(1)), int(m.group(2))
        # Auto mode starts from the neutral look, and How much (default)
        # caps its lift.
        self.assertEqual(autolevels.AUTO_BASE.shadow_boost, base)
        self.assertLessEqual(now - base, autolevels.auto_cap(autolevels.DEFAULT_STRENGTH))

    def test_exposure_for_night_vision(self):
        from catvision.presets import builtin_by_name
        s = builtin_by_name("Night Vision").settings
        self.assertGreater(s.exposure, s.shadow_boost)

    def test_full_screen_rules_described(self):
        self.assertIn("A normal\n  maximized window with a title bar", self.sec)
        self.assertIn("position, style and title", self.sec)

    def test_gui_game_only_tip_mentions_style(self):
        # README: "Only the position, style and title of the window in front
        # are checked"; the tooltip says only "position and title".
        self.assertNotIn("Only the position and title of the window in front", GUI_SRC)

    def test_every_auto_tooltip_says_beta(self):
        build = GUI_SRC[GUI_SRC.index("def _build_auto_levels"):
                        GUI_SRC.index("def _on_auto_option")]
        tips = re.findall(r'(?:Tooltip\([^,]+,|tip=)\s*\(?\s*"([^"]*)"', build)
        self.assertGreaterEqual(len(tips), 6)
        for tip in tips:
            if tip.startswith("New and still being tested"):
                continue   # the BETA badge's own tooltip
            self.assertTrue(tip.startswith("Beta."), tip)

    def test_every_auto_status_message_says_beta(self):
        msgs = [m for m in re.findall(r'f?"(Auto levels[^"\n]*)"', GUI_SRC)
                if m != "Auto levels"]          # the section heading next to its BETA badge
        self.assertGreaterEqual(len(msgs), 4)
        for msg in msgs:
            self.assertIn("(beta)", msg)

    def test_no_detection_wording(self):
        lowered = self.sec.lower()
        for word in ("highlight", "detect enem", "aim assist", "outline"):
            self.assertNotIn(word, lowered)


# ---------------------------------------------------------------------------
# GUI: bad settings at build time
# ---------------------------------------------------------------------------

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
class AutoGuiBadConfigTest(unittest.TestCase):
    def _app(self, options: dict):
        import tkinter as tk
        from catvision import config as cm
        from catvision.backends import Monitor, NullColorBackend
        from catvision.engine import Engine
        from catvision.gui import CatVisionApp
        from tests.test_catvision import FakeGamma

        d1 = "\\\\.\\DISPLAY1"

        class One(FakeGamma):
            def monitors(self):
                return [Monitor(d1, "Display 1", True, short="Display 1",
                                rect=(0, 0, 800, 600))]

        tmp = tempfile.mkdtemp()
        env = mock.patch.dict(os.environ, {"CATVISION_CONFIG_DIR": tmp})
        env.start()
        self.addCleanup(env.stop)
        Path(tmp, "config.json").write_text(json.dumps({"options": options}),
                                            encoding="utf-8")
        tick = [0]

        def capture(_rect):
            tick[0] += 1
            n = tick[0] % 2
            return bytes([5, 5, 5, 255]) * 399 + bytes([n, n, n, 255])

        real = _REAL_WATCHER
        for p in (mock.patch.object(autolevels, "Watcher", lambda: real(capture=capture)),
                  mock.patch.object(autolevels, "window_in_front",
                                    lambda rects: ({d1}, "ARC Raiders"))):
            p.start()
            self.addCleanup(p.stop)
        engine = Engine(One(), NullColorBackend(), cm.app_dir())
        root = tk.Tk()
        app = CatVisionApp(root, engine, cm.load())

        def close():
            try:
                app.quit()
            except Exception:
                try:
                    root.destroy()
                except Exception:
                    pass
        self.addCleanup(close)
        return app, root

    def _pump(self, root, seconds):
        import time
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.02)

    def test_sliders_and_speed_are_clamped_at_build(self):
        app, root = self._app({"auto_strength": 1000.0, "auto_target": 5.0,
                               "auto_speed": "turbo"})
        self.assertEqual(app.auto_vars["auto_strength"].get(), S_HI)
        self.assertEqual(app.auto_vars["auto_target"].get(), T_HI)
        self.assertEqual(app.auto_speed_var.get(), "normal")

    def test_nan_settings_give_finite_sliders(self):
        app, root = self._app({"auto_strength": float("nan"), "auto_target": float("nan")})
        s = app.auto_vars["auto_strength"].get()
        t = app.auto_vars["auto_target"].get()
        self.assertTrue(math.isfinite(s) and S_LO <= s <= S_HI, s)
        self.assertTrue(math.isfinite(t) and T_LO <= t <= T_HI, t)

    def test_controller_never_gets_out_of_range_values(self):
        # The slider shows 60 / 30%, but _auto_tick feeds the controller
        # cfg.options.auto_strength and _auto_aim() unclamped.
        app, root = self._app({"auto_levels": True, "auto_strength": 1000.0,
                               "auto_target": 5.0,
                               "auto_game_looks": {"ARC Raiders": 9.0}})
        app._set_enabled(True)
        self._pump(root, 2.5)
        self.assertTrue(app._auto_ctrl)
        for did, ctrl in app._auto_ctrl.items():
            # seen: strength 1000, want 1000, offset 90+ while the slider says 60
            # (Auto mode: How much caps auto_cap points.)
            cap = autolevels.auto_cap(S_HI)
            self.assertLessEqual(ctrl.strength, cap, did)
            self.assertLessEqual(abs(ctrl.want), cap + 1e-6, did)
            self.assertLessEqual(abs(ctrl.offset), cap + 1e-6, did)
        self.assertTrue(S_LO <= app.cfg.options.auto_strength <= S_HI,
                        app.cfg.options.auto_strength)
        self.assertTrue(T_LO <= app._auto_aim() <= T_HI, app._auto_aim())

    def test_nan_strength_does_not_silently_disable(self):
        # seen: strength NaN -> controller want stays 0, auto levels does nothing
        app, root = self._app({"auto_levels": True, "auto_strength": float("nan")})
        app._set_enabled(True)
        self._pump(root, 2.5)
        self.assertTrue(app._auto_ctrl)
        for ctrl in app._auto_ctrl.values():
            self.assertTrue(math.isfinite(ctrl.strength), ctrl.strength)

    def test_game_look_slider_is_clamped(self):
        app, root = self._app({"auto_game_looks": {"ARC Raiders": 9.0},
                               "auto_levels": True})
        app._set_enabled(True)
        self._pump(root, 1.0)
        self.assertEqual(app._auto_game, "ARC Raiders")
        t = app.auto_vars["auto_target"].get()
        self.assertTrue(T_LO <= t <= T_HI, t)



class HugeNumberTests(unittest.TestCase):
    """A hand-typed integer far too long for a float must not stop CatVision
    from starting: it falls back to the default and other settings stay."""

    BIG = 10 ** 400

    def test_numbers_too_big_for_a_float_fall_back_to_defaults(self):
        from catvision import config as cfg_mod
        d = cfg_mod.Options()
        o = cfg_mod.Options.from_dict({
            "auto_strength": self.BIG, "auto_target": self.BIG, "unlock_requested_at": self.BIG,
            "auto_game_looks": {"Game A": self.BIG, "Game B": 0.2}, "text_size": "Normal"})
        self.assertEqual(o.auto_strength, d.auto_strength)
        self.assertEqual(o.auto_target, d.auto_target)
        self.assertEqual(o.unlock_requested_at, d.unlock_requested_at)
        self.assertEqual(o.auto_game_looks, {"Game B": 0.2})
        self.assertEqual(o.text_size, "Normal")

    def test_config_file_with_a_huge_number_still_loads(self):
        import tempfile
        from pathlib import Path
        from catvision import config as cfg_mod
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"options": {"auto_strength": 1' + "0" * 400
                            + ', "text_size": "Normal"}}', encoding="utf-8")
            loaded = cfg_mod.load(path)
        self.assertEqual(loaded.options.text_size, "Normal")
        self.assertEqual(loaded.options.auto_strength, cfg_mod.Options().auto_strength)


if __name__ == "__main__":
    unittest.main()
