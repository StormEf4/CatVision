<img src="catvision/assets/logo_96.png" width="72" align="left" alt="CatVision logo">

# CatVision

<br clear="left">

**See in the dark, in any game.** CatVision is a small accessibility app for
Windows that brightens dark areas of your screen so caves, night scenes and
horror games are easier to see. It has presets for different situations, its
own color filters, and hotkeys that work while you play. You can change all
of these.

<p>
<img src="docs/home.png" width="300" alt="Home tab">
<img src="docs/displays.png" width="300" alt="Home tab with one display chosen">
</p>

## Download

**[⬇ Download CatVision.exe](https://github.com/StormEf4/CatVision/releases/latest/download/CatVision.exe)**
(from the [Releases page](https://github.com/StormEf4/CatVision/releases))

It's one file. Nothing to install and no Python needed. Put it anywhere,
for example on your desktop, and double-click it.

> **"Windows protected your PC"?** That's normal for small apps that haven't
> paid for a code-signing certificate. Click **More info**, then
> **Run anyway**. You only need to do this once.

## Is it safe? Will it get me banned?

CatVision **never touches your games**:

- It does **not** inject anything, read or write game memory, hook DirectX or
  OpenGL, or attach to any process.
- It does **not** draw an overlay on the game or capture your screen.
- It does **not** use keyboard hooks. Hotkeys use the normal Windows
  `RegisterHotKey` feature, the same one Discord and OBS use.

It only uses two standard Windows display features:

1. **The graphics card's brightness table** (gamma ramp). NVIDIA/AMD control
   panels, f.lux and monitor-calibration tools all change this. Your
   monitor's own "black stabilizer" setting does the same job inside the
   monitor.
2. **Windows' accessibility color filters**, the same engine as
   *Settings → Accessibility → Color filters*.

From the game's point of view nothing has changed. The change only happens
on the way to your monitor, like turning your monitor's brightness up.

> One honest caveat: organized tournaments and LAN events sometimes ban *all*
> outside software. Check their rules if you play in one. Normal online play
> is fine: anti-cheat systems don't look at display brightness.

## Using it

The window is small and split into tabs:

| Tab | What's there |
| --- | --- |
| **Home** | The big **ON/OFF** button, preset picker (◀ ▶ to flip through), **Shadow boost**, color filter, which **displays** to change, and a test pattern |
| **Tune** | Every slider. **Save** keeps your changes in the current preset (built-in ones too), **Save as new...** makes your own preset |
| **Filters** | Make and edit your own color filters |
| **Hotkeys** | Change any hotkey, or give any preset its own hotkey |
| **Settings** | Start with Windows, tray icon, text size, reset display, quit |

**Quick start:** pick **Dark Areas**, click **OFF** so it turns green, then
move **Shadow boost** until the dark squares in the test pattern are all
visible. Not sure what something does? Rest the mouse on it for a short
explanation.

## Choosing displays (multiple monitors)

With two or more monitors, the Home tab shows a button for each display.
CatVision only changes the **ticked** displays, so you can brighten the
game monitor and leave the one with Discord or a browser alone. **Identify**
shows each display's name on that screen so you know which is which.

- **Brightness, Shadow boost, contrast, glare limit and tints** only change
  the ticked displays.
- **Tint filters** (Night vision, Amber, Ice and your own *Monochrome tint*
  filters) also stay on the ticked displays. There they show as a colored
  tint rather than full monochrome.
- **Unticked displays are never changed.** Saturation and color-mixing
  filters (Grayscale, the color-blind assists and your own *Channel mixer*
  filters) can only be applied by Windows to **every display at once**, so
  while only some displays are ticked they're left off. A note tells you when
  that happens. Tick *Use it anyway* if you want them on every display.

## The tray icon (by the clock)

Closing the window keeps CatVision running in the **system tray**, so your
hotkeys keep working. Click the cat-eye icon to open the window, or
right-click it for quick controls:

- **CatVision on**: turn it on or off
- **Preset**, **Color filter**, **Shadow boost** (+10 / -10), **Displays**
- **Panic: everything off** and **Reset display to normal**
- **Quit CatVision**: closes normally and saves your settings
- **Force quit**: closes immediately, even if the window has frozen. Your
  screen is put back to normal first.

You can make the close button quit instead in Settings.

## Hotkeys (work while you're in a game)

These are the defaults. Change any of them in the **Hotkeys** tab: double-click
an action and press the new keys.

| Keys | What it does |
| --- | --- |
| **Ctrl+Alt+V** | Turn CatVision on / off |
| **Ctrl+Alt+1 … 9** | Jump to a preset (Gentle Boost … Warm Low-Light) |
| **Ctrl+Alt+Page Down / Page Up** | Next / previous preset |
| **Ctrl+Alt+=** / **Ctrl+Alt+-** | More / less shadow boost |
| **Ctrl+Alt+F** | Next color filter |
| **Ctrl+Alt+C** | Show the CatVision window |
| **Ctrl+Alt+0** | Panic button: everything back to normal |

You can also give hotkeys to any preset, and use the tray menu for the rest.

<img src="docs/hotkeys.png" width="300" align="right" alt="Hotkeys tab">

- **Add preset hotkey...** gives any preset (including your own) a key.
- CatVision won't let you pick a plain letter key like `Q`, because that key
  would stop working in your games. Use Ctrl or Alt with it, or an F-key.
- If another program already uses a key combination, the list marks it
  *"taken by another app"*. Pick a different one.
- A small popup confirms each hotkey. It can't be clicked and never takes
  focus away from your game, and you can turn it off in Settings.

<br clear="right">

## Presets

| Preset | Good for |
| --- | --- |
| Gentle Boost | Games that are just a little too dark |
| Dark Areas | Everyday choice: dark rooms, caves, night |
| Pitch Black | Nearly black areas (looks washed out, but you'll see) |
| Night Vision | Green night-vision look with a strong lift |
| Horror / Flashlight | Lifts darkness, stops flashlights and lamps from dazzling |
| Fog & Haze Cutter | Murky, foggy, smoky scenes |
| Mixed Light | Dark shadows next to bright skies or explosions |
| High-Contrast Mono | Grayscale: shapes and edges stand out |
| Warm Low-Light | Shadow lift with less blue light for tired eyes |
| Vivid Pop | Richer colors and contrast |
| Amber Night | Strong lift in warm amber monochrome |
| Glare Guard | Bright snow and desert scenes |
| Red-Green Assist (Deutan / Protan) | Color correction for red-green color blindness |
| Blue-Yellow Assist (Tritan) | Color correction for blue-yellow color blindness |

<img src="docs/tune.png" width="300" align="right" alt="Tune tab">

**Changing presets:** pick one, adjust the sliders in **Tune**, and click
**Save**. An edited built-in preset shows *(edited)*, and **Restore default**
puts it back. **Save as new...** creates your own preset, which you can
rename or delete.

<br clear="right">

### What the sliders do

- **Shadow boost**: the main control. It brightens dark parts a lot and
  bright parts barely at all, so the sky doesn't turn white.
- **Gamma (midtones)**: brightens or darkens the middle tones.
- **Black floor**: raises pure black to dark grey.
- **Brightness / Contrast**: as usual.
- **Glare limit**: caps the brightest whites. Useful for flashbangs and
  flashlights.
- **Saturation / Filter strength / Red-Green-Blue**: color strength, how
  strongly the color filter applies, and an overall tint.

## Making your own color filters

<img src="docs/filters.png" width="300" align="right" alt="Filters tab">

In the **Filters** tab, click **New...** (or select a built-in filter and
click **Copy...** to start from it). There are two kinds:

- **Monochrome tint**: the picture turns grayscale and is tinted one color,
  like the built-in Night vision and Amber. Use the sliders or
  **Pick a color...**.
- **Channel mixer**: full control. Each output color is a mix of the input
  colors. For example, *Red out = 1.0 × Red* is normal, and moving some of
  Red into Green helps red-green color blindness.

Your filter is used straight away so you can see changes live (turn
CatVision on first), and the preview shows colors before and after. Your
filters appear in the **Color filter** list on the Home tab, can be saved
into any preset, and can be cycled with **Ctrl+Alt+F**.

<br clear="right">

## Get the full strength (recommended)

By default Windows only lets programs change brightness a limited amount.
CatVision works within that limit, and the status bar says *"Windows limited
the strength"* when it applies. To lift the limit:

1. Download [`windows/enable_full_strength.reg`](windows/enable_full_strength.reg),
   double-click it and click *Yes*.
2. Sign out and back in (or restart).

This sets one documented registry value (`GdiIcmGammaRange`), the same one
display-calibration software uses. `windows/undo_full_strength.reg` undoes it.

## Tips and troubleshooting

- **Game in exclusive fullscreen and the color filter doesn't show?**
  Brightness works in every mode, but color filters need **Borderless** or
  **Windowed fullscreen**.
- **The effect disappears when a game starts.** Some games reset brightness
  on launch. Leave *Keep applied* on (Settings) and CatVision puts it back
  within about 2 seconds.
- **Nothing happens with HDR on.** Windows ignores brightness tables while
  HDR is on. Turn HDR off in *Settings → Display* while you use CatVision.
- **Windows' own Color filters are on.** Turn them off; they conflict with
  CatVision's color filters.
- **Screen stuck bright or dark?** Quitting CatVision always restores it. If
  it crashed, just start it again; it notices and cleans up. You can also use
  **Reset display now** in Settings, **Reset display to normal** or
  **Force quit** in the tray menu, the panic hotkey **Ctrl+Alt+0**, or run
  `CatVision.exe --reset`.
- **Text too small?** Settings → Text size → *Extra large*.
- **Opened CatVision twice?** The second copy just brings up the first one's
  window.

Your settings, presets, filters and hotkeys are saved in
`%APPDATA%\CatVision\config.json` (Settings → *Open settings folder*).

## For developers

The `.exe` is built automatically by GitHub Actions
(`.github/workflows/build.yml`) on a Windows machine. Each build runs the unit
tests, then a self-test (`CatVision.exe --selftest report.json`) against the
real Windows display, color-filter, hotkey and tray APIs, both from source and
on the built exe, then publishes the exe to the
[Releases page](https://github.com/StormEf4/CatVision/releases). To build it
yourself on Windows, run `build_exe.bat`.

To run from source: Python 3.9+, `pip install pystray pillow` (optional, for
the tray icon), then `python run_catvision.py` (or `CatVision.bat`).

- `catvision/curves.py`: brightness curve and color-matrix math
- `catvision/filters.py`: user color filters
- `catvision/backends.py`: the two Windows display APIs (plus no-op versions)
- `catvision/engine.py`: applies settings and always knows how to undo them
- `catvision/hotkeys.py`: hotkey parsing and the `RegisterHotKey` listener
- `catvision/gui*.py`, `theme.py`, `tray.py`: the window, tabs and tray icon

Run the tests with `python -m unittest -v`.
