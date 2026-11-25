# CatVision handoff

The current state of the project, for whoever picks it up next. Rules and the
test and build workflow are in `CLAUDE.md`; the user manual is `README.md`.

## Where things are

- **Repository:** `github.com/StormEf4/CatVision`.
  - All work is on branch **`claude/beautiful-gates-rbuyej`**, which is also
    the repository's default branch. There are no other branches and no open
    PRs.
- **Latest version: 1.22.0** (Auto mode: quicker lamps and lasting
  lightning, 240 Hz fades, a daylight lift keyed to the shadows).
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

## What the app does now (1.22.0)

- **Presets:** Gentle Boost, Moderate Boost, Dark Areas, Night Vision, Pitch Black and others.
  - Tune sliders: Shadow boost, Exposure, Gamma, Contrast, Brightness and so
    on.
  - Colour filters, plus the user's own filters.
- **Auto (beta) mode:** a mode of its own, the first entry in the preset list.
  - It starts from a neutral look and moves the settings itself; it is not
    blended with a preset.
  - **Dark scenes** are lifted until they show at about 3.2x *Aim for*.
  - **Bright scenes** get a moderate shadow-only daylight lift, which makes way
    for the haze cutter. Only very bright views are toned down.
    - Since 1.22 the lift is keyed to the darkest quarter of the picture
      (`Measurement.shadows`, `day_lift_for_shadows`): just enough to show
      those shadows at the night look's level, at most 30 (How much 30).
      Deep shade keeps the full 30; a sunlit view with light shadows gets
      about 10, so less glare. It fades out over 0.5 stops above the
      tone-down point. The controller uses the brightest recent shadows,
      the same anti-pump rule as the key.
  - **Speed limits** are paced on screen: each step stays as visible as Dark
    Areas' worst step. Steps can be up to 1.5x larger for 0.5 s after a
    confirmed new view.
  - **On real frames** (1.20 era) it settled light→dark in about 0.43 s and
    dark→light in about 0.29 s.
  - **Lights that stay on** (1.22, `_lasting_light`): a lamp, a light
    switched on, lasting lightning start dimming at the normal pace; once a
    picture `LASTING_S` (0.06 s) later still calls for the same goal, the
    rest of the fade runs at the new-view pace with the normal `CEILING`
    (not `CEILING_RUSH`). Flicker and storms are unchanged.
  - **Fader rate:** up to 240 Hz (`FADER_MAX_HZ`), with view-rise steps
    counted at no more than 120 a second (`CEILING_STEP_HZ`), so speeds are
    the same and a 240 Hz screen gets half-size steps. If a step costs over
    30% of its interval (mean over 240 steps, `note_step_cost`), the fader
    drops to 120 (`FADER_SAFE_HZ`) until restarted.
  - **Fader cost on Windows** (CI runner, 1.22.0): one update is about
    0.15 ms for the engine (`update_cost.engine_update_ms`) plus about
    0.23 ms CPU for SetDeviceGammaRamp (`gamma_calls.set_ramp_quick_cpu_ms`)
    per display, so about 10% of one core at 240 a second while a fade
    moves (5% at 120), and nothing at rest. The fader reached 239.8 Hz
    (max gap 4.6 ms) and 238 Hz with the Watcher running. The self-test's
    `fader_cpu` check times each step directly (`step_ms`); its CPU totals
    are rough because Windows counts thread CPU in 15.6 ms ticks.
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
| 1.21.0 | New **Moderate Boost** preset (the old Gentle Boost values: SB 40, Exposure 6, Anti-glare 100, Gamma 1.1), right after Gentle Boost. Gentle Boost softened to SB 23, Exposure 3.5, Gamma 1.05: about 40% less shadow lift. |
| 1.22.0 | Auto mode: lights that stay on dim at the new-view pace (lamp 0.55→0.39 s, night dark→light 0.46→0.31 s, light on 0.31→0.22 s on the synthetic bench); 240 Hz fader with a slow-PC guard; daylight lift keyed to the darkest quarter (sunlit view 25→10 points, p95 glare −3 L*). |

## Known limits and possible next steps

- **Recovery after lightning:** after a flash the picture stays dimmed for
  about 1.75 s before it brightens again. That is the anti-pump design
  (a test enforces it); shortening it is a possible next step, but it
  risks pumping in storms.
- **Day lift follows the view:** because it is keyed to the shadows,
  panning between sun and shade now moves the lift (about 10 to 30 points)
  where 1.21 held it at about 25-30. Watch for user feedback on that.
- **Glances:** a quick glance at daylight dims in the larger view-change steps
  (about 2.9 L*). That was a deliberate trade for less glare.
- **Evaluation tooling is not in the repository:** the old scripts and
  screenshots lived in `dist/work`, which is gitignored and gone. 1.22 was
  measured on a synthetic bench (game-like 96x54 frames with noise, camera
  shake, a HUD and a vignette, run through the real Controller and Fader)
  that lived only in that session's scratch folder. The cloud container's
  network policy blocks Steam community screenshots (403), so real frames
  need a network allowance or the user's own captures.
  - Use public screenshots that show the HUD; leave out trailers and
    cinematics.
  - Keep them local; never commit them.
- **Not tested in the game by Claude:** all testing has been unit and GUI
  tests plus the Windows CI self-test. The user's own feedback is the real
  test.
