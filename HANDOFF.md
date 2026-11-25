# CatVision handoff

The current state of the project, for whoever picks it up next. Rules and the
test and build workflow are in `CLAUDE.md`; the user manual is `README.md`.

## Where things are

- **Repository:** `github.com/StormEf4/CatVision`.
  - All work is on branch **`claude/beautiful-gates-rbuyej`**, which is also
    the repository's default branch. There are no other branches and no open
    PRs.
- **Latest version: 1.20.2.** The release commit is `43b35e8`; this handoff
  commit follows it and adds only `CLAUDE.md` and `HANDOFF.md`.
  - Download: the GitHub release `latest-build`
    (`https://github.com/StormEf4/CatVision/releases/download/latest-build/CatVision.exe`).
  - Windows CI is green, and the self-test reports no errors on the source
    build or the exe.
- **The handoff zip** holds:
  - a full-history git bundle;
  - the 1.20.2 exe;
  - these notes.

  To restore the repository from the bundle: `git clone CatVision.bundle CatVision`.

## The user and what they care about

- **Who:** a low-vision player of Arc Raiders.
  - Night raids are the main use case.
  - They play at 120-240 fps on a 240 Hz monitor.
- **What they want:**
  - fast, smooth switches between dark and light scenes, with no flicker or
    pumping;
  - readable shadows;
  - no glare when looking out of a window or walking into daylight.
- **Working style:** short iterations, an exe after every green build, and
  time-boxed agent rounds when they ask for them.

## What the app does now (1.20.2)

- **Presets:** Gentle Boost, Dark Areas, Night Vision, Pitch Black and others.
  - Tune sliders: Shadow boost, Exposure, Gamma, Contrast, Brightness and so
    on.
  - Colour filters, plus the user's own filters.
- **Auto (beta) mode:** a mode of its own, the first entry in the preset list.
  - It starts from a neutral look and moves the settings itself; it is not
    blended with a preset.
  - **Dark scenes** are lifted until they show at about 3.2x *Aim for*.
  - **Bright scenes** get a moderate shadow-only daylight lift, which makes way
    for the haze cutter. Only very bright views are toned down.
  - **Speed limits** are paced on screen: each step stays as visible as Dark
    Areas' worst step. Steps can be up to 1.5x larger for 0.5 s after a
    confirmed new view.
  - **On real frames** it settles light→dark in about 0.43 s and dark→light in
    about 0.29 s.
  - **Switching:** turning Auto on or off glides over 0.4 s. The preset sliders
    are greyed out while Auto is on.
  - **Options** (all beta):
    - *Outside detail*: a bright window seen from a dark room is not blown out.
    - *Cut through haze*: a black-point cut, plus a band-contrast stretch for
      light haze.
    - *Faster screen capture*: DXGI.
    - *Tone down bright scenes.*
- **Peek:** hold a hotkey for a strong, short lift. It raises Shadow boost
  first, then Exposure.
- **Hotkeys:**
  - any key or combination, including Middle mouse, Mouse 4 and Mouse 5;
  - Left and Right mouse only with a modifier;
  - keys that can't be registered are marked "shared with game".
- **Multiple displays:** per-display choice. Refused gamma changes are retried
  with a fallback search. The "Get the full strength" registry option lifts
  Windows' gamma limit.

## Recent history

| Version | Change |
|---|---|
| 1.15.0 | Refused-change retry, fader timed for 240 Hz screens, faster dark→light. |
| 1.16.0 | Agent review round focused on fast scene switches. |
| 1.17.0 | Outside detail: see out of windows from dark rooms. |
| 1.18.0 | Faster DXGI capture, Peek hotkey, auto haze cutter, auto-mode tuning. |
| 1.19.0 | Brightness parity between the two capture methods; any key or mouse button as a hotkey. |
| 1.20.0 | Auto became a standalone mode; haze band for light haze; menu detection added. |
| 1.20.1 | Auto on/off glide; preset sliders greyed out in Auto. |
| 1.20.2 | **Menu and inventory detection removed.** The user found the fade to normal in menus too jarring. Do not re-add it unless they ask. |

## Known limits and possible next steps

- **Slower cases:** some scene changes still ease at the normal pace, up to
  about 0.7 s. These are changes the controller reads as a flash or a new
  light rather than a new view, such as lightning or a lamp coming into view.
  Synthetic dark→light switches are also slower than in 1.19 (about 0.41 s vs
  0.30 s).
- **Day lift:** the daylight lift is keyed to overall brightness, not to the
  darkest quarter of the picture.
- **Glances:** a quick glance at daylight dims in the larger view-change steps
  (about 2.9 L*). That was a deliberate trade for less glare.
- **240 Hz fader:** `FADER_MAX_HZ` is 120. Raising it on 240 Hz screens would
  allow faster fades at the same step size, but the CPU cost on Windows has
  not been measured.
- **Evaluation tooling is gone:** the benchmark and evaluation scripts
  (`opt_bench*.py`, `automode_eval.py`, `haze_eval.py`) and the local
  screenshot set lived in `dist/work`, which is gitignored and no longer
  exists. Rebuild them if you need numbers again.
  - Build them from public Steam community screenshots that show the HUD.
  - Leave out trailers and cinematics.
  - Keep them local; never commit them.
- **Not tested in the game by Claude:** all testing has been unit and GUI
  tests plus the Windows CI self-test. The user's own feedback is the real
  test.
