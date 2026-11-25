# CatVision: notes for Claude

CatVision is a Windows accessibility app for a **low-vision gamer**. It
brightens dark parts of the screen so games, mainly **Arc Raiders night
raids**, are easier to see. The user plays at 120-240 fps on a 240 Hz monitor.
Read `HANDOFF.md` for status, history and open ideas; `README.md` is the
user-facing manual.

## Hard rules (never break these)

- **Display-level APIs only.** These are the only Windows APIs it may use:
  - the GPU gamma ramp (`SetDeviceGammaRamp`, 3x256, always monotonic);
  - the Magnification API colour matrix;
  - GDI or DXGI Desktop Duplication screen capture. Capture is only for
    brightness and colour statistics of tiny thumbnails.
- **Never** inject into a game, read game memory, open game process handles
  or draw overlays.
- **Never** detect, locate, outline or highlight players, enemies or objects.
  Requests for ESP, outlines or enemy highlighting have been declined and must
  stay declined. Auto features look only at whole-picture brightness and haze
  statistics.
- No keyboard or mouse hooks. Never send or simulate input. Reading keys with
  `GetAsyncKeyState` (read-only) is allowed.
- No new dependencies: the standard library, plus `pystray` and `Pillow` for
  the tray. No default hotkeys.
- Keep the experimental auto features labelled **(beta)**.
- Real game screenshots are copyrighted. Use them for local evaluation only and
  never commit them.
- Do not put model identifiers in commits or code.

## Code map

| File | What it holds |
|---|---|
| `catvision/curves.py` | Pure tone-curve, ramp and colour-matrix maths. Includes the Outside detail ceiling (`highlight_room`), `haze_cut` and `haze_band`. Fast `build_ramp`. |
| `catvision/engine.py` | Turns settings into display changes and can always undo them. Handles refused-change retry and fallback search, the auto offsets and Peek fast path (`_redo_fading`), and the Auto on/off glide (`begin_switch`, `set_switch_amount`, `glide_ramp`). |
| `catvision/backends.py` | The Windows gamma and colour-filter backends (plus null versions), `Monitor`/layout/refresh rate, and `apply_ramp_with_fallback`. |
| `catvision/autolevels.py` | Auto mode. `measure()` turns thumbnails into statistics. The `Controller` handles flash confirmation, settle and calm rules, the view-change rush and the follow spring. The `Fader` thread runs at 60-120 Hz. Also Auto mode mapping (`AUTO_BASE`, `auto_offset`, `auto_amounts`, `auto_pace`, daylight lift), haze, Outside detail and the capture watcher. |
| `catvision/dxgicapture.py` | Faster screen capture (DXGI with GPU mips) with GDI fallback. Both go through the same `resample()`. |
| `catvision/peek.py` | Hold-to-peek lift. |
| `catvision/hotkeys.py` | Hotkeys: `RegisterHotKey`, plus `KeyPoller` for plain keys, Shift combos and mouse buttons. |
| `catvision/gui*.py`, `theme.py`, `tray.py` | Tkinter window, tabs and tray icon. |
| `catvision/selftest.py` | `CatVision --selftest out.json`: runs the real Windows APIs on CI. |
| `catvision/config.py`, `presets.py`, `filters.py` | Settings, built-in presets and user colour filters. |

## Testing

- **Fast suite:** `python3 -m unittest discover -s tests -t .`. GUI tests are
  skipped.
- **Full suite, including GUI tests:** use a Python that has tkinter
  (`python3.12` in the cloud container):
  `CATVISION_GUI_TESTS=1 xvfb-run -a -s "-screen 0 1280x1024x24" python3.12 -m unittest discover -s tests -t .`
  - It runs 705 tests in about 2.5 min with 0 skipped.
  - Plain `python3` there has no tkinter, so the GUI tests silently skip.
- **Lint:** `pyflakes catvision tests` must be clean. pytest is not
  installed; use unittest.

## Build and release

- Every push runs `.github/workflows/build.yml` on `windows-latest`:
  1. unit tests;
  2. the self-test from source;
  3. a PyInstaller one-file exe;
  4. the self-test on the exe;
  5. upload of the artifact (`selftest-*.json`/`png` are in it);
  6. publish to the GitHub release **`latest-build`**.
- **Verify a build:**
  - the run must succeed;
  - `errors` must be `[]` in both `selftest-*.json`;
  - the release's `target_commitish` must equal the pushed commit.
- **Bump the version in three places together:** `catvision/__init__.py`,
  `pyproject.toml`, `packaging/version_info.txt`.
- **Standing user preference:** send the user the new `CatVision.exe` after
  every pushed build, but only once CI is green.
