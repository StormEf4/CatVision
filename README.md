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
- It does **not** draw an overlay on the game. It doesn't look at your screen
  either, unless you turn on **Auto levels (beta)**, which only measures how
  bright the picture is (see [Auto levels](#auto-levels-beta)).
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
| **Home** | Preset picker (◀ ▶ to flip through), **Shadow boost** and **Exposure**, color filter, which **displays** to change, and **Auto levels (beta)** |
| **Tune** | Every slider, and a big **Test pattern** to tune against. **Save** keeps your changes in the current preset (built-in ones too), **Save as new...** makes your own preset |
| **Presets** | Every preset in one list: **Use it**, **New...** (from your current settings), **Copy...**, **Rename...**, **Delete** |
| **Filters** | Make and edit your own color filters |
| **Hotkeys** | Set a hotkey for any action, and an on/off hotkey for every preset |
| **Settings** | Start with Windows, tray icon, text size, full strength, reset display |

The header shows the ON/OFF switch and a **Quit** button (quitting always puts
your screen back to normal). When something needs your attention, a **⚠**
appears next to them: click it to see what's wrong, what to do, and buttons
that fix it. Click it again to close it; it also closes by itself once the
problem is fixed.

**Quick start:** pick **Dark Areas**, click **OFF** so it turns green, then
open **Test pattern...** in the Tune tab and move **Shadow boost** until the
dark squares are all visible. Leave the pattern open while you adjust; it has
a **Full screen** button (or press F11) for an even bigger picture. Not sure what something does? Rest the mouse on it for a short
explanation.

## Choosing displays (multiple monitors)

With two or more monitors, the Home tab shows a checkbox for each display:
🖥️ and its number, with ⭐ on your main display (rest the mouse on one for its
full name). CatVision only changes the **ticked** displays, so you can brighten the
game monitor and leave the one with Discord or a browser alone. **Identify**
shows each display's name on that screen so you know which is which.

- **Brightness, Shadow boost, contrast, glare limit and tints** only change
  the ticked displays.
- **Tint filters** (Night vision, Amber, Ice and your own *Monochrome tint*
  filters) also stay on the ticked displays. There they show as a colored
  tint rather than full monochrome.
- **Unticked displays are never changed.** Saturation and color-mixing
  filters (Grayscale and your own *Channel mixer* filters) can only be
  applied by Windows to **every display at once**, so while only some
  displays are ticked they're left off. The ⚠ tells you when that happens,
  with a **Use it anyway** button if you want them on every display.

## The tray icon (by the clock)

Closing the window keeps CatVision running in the **system tray**, so your
hotkeys keep working. Click the cat-eye icon to open the window, or
right-click it for quick controls:

- **CatVision on**: turn it on or off
- **Auto levels (beta)** and **keep this look**
- **Preset**, **Color filter**, **Shadow boost** (+10 / -10), **Displays**
- **Panic: everything off** and **Reset display to normal**
- **Quit CatVision**: closes normally and saves your settings
- **Force quit**: closes immediately, even if the window has frozen. Your
  screen is put back to normal first.

You can make the close button quit instead in Settings.

## Hotkeys (work while you're in a game)

CatVision starts with **no hotkeys**, so it never takes a key you use for
something else. Set the ones you want in the **Hotkeys** tab: double-click an
action and press the keys.

| Action | What it does |
| --- | --- |
| Turn CatVision on / off | On or off with the current preset |
| **On/off: (any preset)** | Every preset has one: turns that preset on, press again to turn it off |
| Next / previous preset | Flip through presets |
| More / less boost | Shadow boost, or Exposure for night vision |
| Next color filter | Cycle color filters |
| Auto levels on / off (beta) | Turns [auto levels](#auto-levels-beta) on or off |
| Auto levels: keep this look (beta) | Auto levels aims for how the game looks right now |
| Auto levels: aim brighter / darker (beta) | Nudge Aim for up or down while you play |
| Peek (hold to see in the dark) | Hold it for a strong, short lift to check a dark corner; let go and it glides back |
| Show the CatVision window | Brings the window up |
| Panic: everything off | Everything back to normal |

Want a ready-made set? **Use suggested keys** fills in Ctrl+Alt+V for on/off,
Ctrl+Alt+1-8 for the eight presets, Ctrl+Alt+0 for panic and a few more. **Clear
all** removes them again. Everything is also in the tray menu.

<img src="docs/hotkeys.png" width="300" align="right" alt="Hotkeys tab">

- Every built-in preset has its own on/off row. **Add preset hotkey...** gives
  your own presets one too.
- CatVision won't let you pick a plain letter key like `Q`, because that key
  would stop working in your games. Use Ctrl or Alt with it, or an F-key.
- If another program already uses a key combination, the list marks it
  *"taken by another app"*. Pick a different one.
- A small popup confirms each hotkey at the top of every ticked display. It
  can't be clicked and never takes focus away from your game, and you can
  turn it off in Settings.
- **Peek** works while you hold its key (suggested: Ctrl+Alt+P). It brightens
  the dark parts of the picture a lot, on top of your preset and auto levels,
  and glides back to exactly the look you had when you let go. It raises
  Shadow boost first, since that lifts the darkest corners the most (also with
  the Night Vision presets), and Exposure when Shadow boost is nearly full.
  A quick tap gives a short look. Set how much
  with **Peek strength** on the Tune tab (10 to 60, normally 35). It only
  works while CatVision is on, and ends by itself after 10 seconds. To tell
  when you let go, CatVision asks Windows whether that key is still down
  (a plain read-only check, not a keyboard hook).

<br clear="right">

## Auto levels (beta)

**Beta: new and still being tested.** Tick **Adjust to the game
automatically** under *Auto levels* on the Home tab (or use its hotkey or the
tray menu). CatVision then watches how bright the picture is on each ticked
display and adjusts it live, the way your eyes adapt:

- **Dark scene:** it raises Shadow boost (or Exposure for night vision)
  gradually, so the picture doesn't jump.
- **Bright scene, flash or explosion:** it lowers it again quickly, so you're
  never blinded.
- Your preset stays as it is. Auto levels moves things up or down from your
  preset, and goes back to it when you turn auto levels off.
- It looks mostly at the **middle of the screen**, where you're looking, so
  the HUD and screen edges don't throw it off. It also pays extra attention
  to **dark patches**, so a dark doorway still gets help when the sky is
  bright.
- **No visible steps:** changes glide in tiny steps (as many a second as
  your display refreshes, from 60 up to 120) on their own background
  thread, easing in and out with a speed limit, so you never see the
  picture jump. With screens of different speeds, each display gets steps
  at its own refresh rate (a 60 Hz screen next to a 144 Hz one gets 60 a
  second, not 120), which saves CPU, and the quickest fade (into a dark
  view) goes at each screen's own pace, so the slower one never shows
  bigger steps than it would on its own (also at rates that don't divide
  evenly, a 75 Hz screen next to a 144 Hz one).
  It ignores tiny changes so the picture doesn't keep
  "breathing", and it catches up faster after a big change (stepping from
  daylight into a dark building), keeping that quicker pace all the way, and
  it starts brightening on the way into a cave instead of waiting until you
  are inside. If the scene brightens a little while it is still
  brightening for the dark (a light coming up), it eases to a stop within
  a frame or two instead of carrying on up past the new level first.
  Turning auto levels off glides back to your preset too. At the slow end
  of a glide, where single steps are too small to see, it updates less
  often to save CPU.
- **Quick into the dark:** when you turn or run somewhere darker (into a
  building or a cave, away from the sky, closing a bright menu) it can tell
  the whole view changed, not just a light, and starts brightening within
  about a third of a second, with an even quicker fade (walking into a cave
  gets that quicker fade too, on the way in). A light that only reaches
  part of the view (a lamp in a corner, a muzzle flash lighting the middle,
  lightning through a window) leaves the rest of the picture as it was, so
  it doesn't count as a new view, and the game's HUD (the same in every
  view) doesn't hide a real turn.
- **Quick back into the dark:** after a quick look at something bright from a
  dark place (a glance out of a doorway, a door opened and shut, a menu
  opened for a moment), it goes back to how it was once you have been back
  for just over half a second, with the same quick fade, instead of waiting
  as if a light had flashed. A look at something nearly all white (bright
  enough to count as a white-out, see Flashbangs) still waits, as lightning
  does.
- **Flash guard:** a sudden flash or explosion is toned down within a
  fraction of a second, as a very fast fade rather than a jump. Stepping
  out of the dark into daylight gets the same quick fade, even when it
  brightens over a few steps (a cave mouth, a door swinging open). When it
  can tell the whole view changed (turning toward the sky or a window) that
  fade starts at full speed from the first brighter picture; walking out
  gets the same full-speed start once the view no longer looks like the
  dark one you were in. A light coming on in the same view doesn't count.
- **Flashbangs:** when the whole screen whites out (a flashbang or a blast
  right in front) and then fades back to the room you were in, it follows
  the fade as it goes, so you can see in the room again as soon as the
  white has gone (about a second sooner than waiting as for a flash). This
  works with a HUD drawn over the white and a vignette darkening its
  corners too. Lightning, muzzle flashes and strobes light the view and
  snap straight back off, so they still wait (see below).
- **No pumping:** apart from turning into a new, darker view (or back to the
  one you just looked away from, or a flashbang fading), it only brightens
  once a scene has stayed dark for a moment, so flickering lights, muzzle
  flashes and lightning don't make the picture bounce. When it measures
  twice as often (see How it stays safe), a brighter copy only counts once
  the next one, about 0.07 seconds later, is still bright, so a flash
  shorter than that (a lightning stroke, a muzzle flash) doesn't dim the
  picture more often than at the usual pace and stormy nights aren't left
  darker; one that lasts (a flashbang, a flare, daylight) still dims about
  as soon. Glimpses of
  something bright (running past windows) don't count as a new view or as
  looking away, and if you keep turning back and forth between bright and
  dark it waits the usual way until you stop for a few seconds.
- **See outside from a dark room:** when a window, an open door or a lamp is
  in view, it gives the bright part more **Outside detail** (the more of
  the view it fills, the more it gets, up to 40 points), so the sky, walls
  and trees outside stay apart instead of turning one white block. The room
  comes first: the extra never changes the dark room's own levels, so when
  the room is lifted a lot (Pitch Black, Night Vision, a big boost) it adds
  less, or nothing. It glides in over about a second
  and back out more slowly, with no visible steps. Small bright bits (HUD
  text, a crosshair, sparks) don't count: only bright areas that fill at
  least a tenth of one of the 32 blocks (see *How it stays safe*). CatVision can't change
  parts of the screen, only how bright each brightness level is shown: the
  room is the dark levels and the outside the bright ones, so they can be
  treated differently. A screen that is all bright (a flash, open sky) gets
  nothing extra, and **How much** at 0 turns this off too. The Home tab then
  says *keeping the outside visible*.
- **Cut through haze (beta)** (on by default): when fog, smoke, dust or rain
  wash the picture out (even its darkest parts are well above black and
  every tone is squeezed together), it moves the black point down so the
  view gets some of its contrast back: in thick haze over the whole view the
  spread between the darker and brighter parts grows by roughly 15-30%, and
  the Home tab says *cutting through haze*. Haze rarely covers everything in
  a game: your own character, the HUD or something close by stay dark, and
  the sun or an explosion stays bright. So it also checks the middle of the
  view part by part (a coarse grid, leaving out the top row, mostly sky, and
  the bottom one, mostly HUD) and counts it as hazy when at least half of
  those parts are veiled, some more than others the way haze thickens with
  distance. Then the cut stays small (often around a 5-10% wider spread),
  because its black point never goes past about 60% of how bright the
  darkest few percent of the whole picture are. Light haze that leaves most of
  the view clear isn't caught. It only ever darkens the hazy grey toward
  black: white stays white, Outside detail is kept, and nothing below the
  haze is cut to pure black. It needs the haze in most of the last five pictures (about a
  second), so one puff of smoke or a muzzle flash changes nothing; it glides
  in over about a second and a half and out over about two when the air
  clears, and small changes in a view that stays put leave it alone. It
  leaves dark scenes alone, night fog included (in a dark game the haze is
  usually close to black anyway, and lifting the shadows is what helps
  there), and it gives way while auto levels is lifting a dark view, so it
  never works against the night lift. A flat or all-bright screen (a white
  menu, open sky, a loading screen, a plain wall up close) doesn't count,
  and very thick, bright fog that fills the whole screen looks like one too,
  so it gets nothing. Like the rest of auto levels it only measures
  brightness (how bright the darkest few percent of the picture, and of each
  part of the grid, are and how spread out their tones are) and changes the
  whole screen's brightness levels the same way: it can't tell fog from a
  misty-looking scene or an unevenly lit grey wall, and it never looks for
  anything in the picture.
- **Aim brighter / darker hotkeys:** nudge Aim for while you play, without
  leaving the game (suggested Ctrl+Alt+] and Ctrl+Alt+[). They change the
  game's own kept look if it has one.
- **Keep this look:** when the game looks just right, click **Keep this look**
  (or use its hotkey or the tray menu). Auto levels then aims for that look,
  and brighter or darker scenes are adjusted from there. While CatVision's
  own window is in front, the game's look is held (for up to 30 seconds), so
  the look you keep is the one you were just playing with. Changes to How
  much, Aim for and Also tone down still show on it straight away. Used from
  the tray menu just after leaving the game, it keeps the look from just
  before. If Aim for already gives this look (at full boost, say), it stays
  as it is.
- **Each game remembers its own look.** A look you keep while a full-screen
  game is in front is saved for that game (recognized by its window title)
  and used again automatically next time. The Home tab shows *Game: ...* and
  **Aim for** shows and changes that game's look; **Forget** goes back to the
  normal Aim for.
- **Never runs out of room:** if your preset's Shadow boost is already high
  (Pitch Black is 85), the extra brightening that doesn't fit goes into
  Exposure instead, and the same when dimming. (With Anti-glare on, it never
  takes Exposure from 0 up or down to 0: Anti-glare only works while
  Exposure is above 0, so the picture would jump.)
- **How much** sets how far it may move the slider. **Aim for** sets how
  bright it tries to make scenes: higher brightens dark scenes more.
- **Speed**: Slow, Normal or Fast reaction to darker and brighter scenes.
  Flashes and stepping out into daylight are always toned down much faster
  than other changes.
- **Only while a game fills the screen** (on by default): on the desktop or in
  a windowed app it pauses and eases back to your preset. Games in fullscreen
  or borderless windowed mode count, and so do full-screen videos. A normal
  maximized window with a title bar (a browser, say) doesn't count, and
  neither do minimized or hidden ones. Only the position, style and title of
  the window in front are checked, nothing inside it.
- **Also tone down bright scenes** (on by default): untick it if you only
  ever want it to brighten.
- **Faster screen capture (beta)** (on by default): copies the screen with Windows'
  Desktop Duplication, the route recording apps like OBS use, and lets the
  graphics card shrink the copy, so measuring takes less time. Where that
  doesn't work (HDR or rotated displays, remote desktop, some older PCs) that
  display uses the normal GDI copy and the fast way is tried again now and
  then (after 5 seconds, then less often, up to once a minute). Like
  recording apps, it can add a little input lag to some games in borderless
  mode; untick it if you notice that, to always use the GDI copy.
- **Cut through haze (beta)** (on by default): untick it to leave hazy
  scenes as the game shows them (see above).
- Each ticked display is adjusted on its own, and the Home tab shows what it
  is doing (for example *Display 1: 3% bright · Shadow boost 66 → 88*).

**How it stays safe:** five times a second (ten while the brightness is
changing a lot; twice that when taking the copy is cheap, a few
milliseconds, so it reacts a little sooner for no more CPU than the slower
route) it takes a tiny, low-resolution copy of what Windows is
already showing and measures only how bright it is: overall, how much of it
is bright, and in 32 large blocks of the picture (8 across, 4 down) to tell turning to a
new view from a light going off. It never touches the game, never saves or
sends the pictures, and never looks for anything in them. It only changes
brightness, nothing that would point things out to you. The copy comes from
one of two standard Windows screen-copy routes, both only reading the finished
desktop picture Windows composed for a display: DXGI Desktop Duplication (the
route recording and streaming apps such as OBS use; with *Faster screen
capture*, where it works) or a GDI copy of the screen (StretchBlt, the classic
screenshot route; otherwise). Neither opens, reads or changes the game.

**Things to know:**

- It works best with games in **borderless windowed** mode. Some games in
  exclusive fullscreen can't be seen; the Home tab then says *can't see the
  picture* and auto levels leaves your preset alone.
- A few games and videos block screen capture and show up black. Auto levels
  holds still on a black screen (a loading screen, say) instead of
  over-brightening; after about 30 seconds of black it eases back to your
  preset. If it can't see the picture at all for a couple of seconds it eases
  back to your preset too.
- Some games in exclusive fullscreen leave Windows with a frozen, old
  picture. If the picture doesn't change at all for about three seconds,
  auto levels stops following it, holds for a couple more seconds and then
  eases back to your preset; the Home tab says *the picture isn't changing*.
  Borderless windowed mode avoids this.
- If the screen copies stop arriving for a moment, it holds, and after about
  three seconds it eases back to your preset, so it can never stay boosted
  after you leave a game.
- On PCs where Windows limits how strong a brightness change can be (see
  *Get the full strength*), fades stay smooth and turning auto levels off
  always brings back your full preset.
- While nothing is changing it rests and uses almost no processor time.
- It's off by default, and it stops watching whenever CatVision or auto
  levels is turned off.

## Presets

<img src="docs/presets.png" width="300" align="right" alt="Presets tab">

| Preset | Good for |
| --- | --- |
| Gentle Boost | Games that are just a little too dark |
| Dark Areas | Everyday choice: dark rooms, caves, night |
| Pitch Black | Nearly black areas (looks washed out, but you'll see) |
| Night Vision | Like night-vision goggles: deep green shadows, pale highlights, no blinding bloom |
| Night Vision (Blue) | Night vision in cool blue, with the same anti-glare protection |
| Fog & Haze Cutter | Murky, foggy, smoky scenes |
| Vivid Pop | Richer colors and contrast |
| Glare Guard | Bright snow and desert scenes |

More looks are in the **Color filter** list (Night vision red and grey,
Thermal style, Grayscale, Amber, Ice); pick one and **Save as new...** to make
it a preset.

<br clear="right">

<img src="docs/tune.png" width="300" align="right" alt="Tune tab">

**Changing presets:** pick one, adjust the sliders in **Tune**, and click
**Save**. An edited built-in preset shows *(edited)*; **Restore default** in
the **Presets** tab puts it back. Create, copy, rename and delete your own
presets in the **Presets** tab.

<br clear="right">

### What the sliders do

- **Shadow boost**: the main control. It brightens dark parts a lot and
  bright parts barely at all, so the sky doesn't turn white.
- **Exposure**: for games where the *whole* scene is very dark. It amplifies
  dim light evenly, like night-vision goggles, so shapes keep their depth
  instead of looking foggy.
- **Anti-glare** (with Exposure): stops bright lights from blooming, so lamps
  and windows never hide a player standing in front of them.
- **Gamma (midtones)**: brightens or darkens the middle tones.
- **Black floor**: raises pure black to dark grey.
- **Brightness / Contrast**: as usual.
- **Glare limit**: caps the brightest whites. Useful for flashbangs and
  flashlights.
- **Outside detail (windows, sky)**: keeps bright areas from turning white
  when dark areas are lifted, so you can see out of a window or door from a
  dark room. Higher keeps more detail outside; 0 is off. Dark Areas, Pitch
  Black and both Night Vision presets use 40, which leaves dark rooms about
  as bright as before. Your saved copies of those presets get it too.
- **Saturation / Filter strength / Red-Green-Blue**: color strength, how
  strongly the color filter applies, and an overall tint.

## Night vision

Tuned on dark in-game screenshots. Both night-vision presets use three
settings together:

- **Exposure** spreads a very dark scene's tones across the whole brightness
  range, so shapes keep their depth instead of looking foggy. The color is
  added *after* brightening, like the phosphor screen in real goggles.
- **Anti-glare** keeps bright lights from blooming. Instead of every bright
  thing turning the same white, bright areas get their own range, so a lamp
  or window stays brighter than anyone standing in front of it.
- **Glare limit** caps the brightest output, so night vision never blinds you.

![Night vision anti-glare: players in front of a bright window](docs/night_vision_anti_glare.png)

On the reference screenshot, with two test players placed in front of a lit
window: before Anti-glare, the mid-tone player was *brighter* than the window
and disappeared into the glow. Now both players stand out clearly, bright
areas keep their detail, and the brightest output is capped at 80%. Night
Vision shows about 3x the game's own dark-area detail.

![Night vision before and after](docs/night_vision_before_after.png)

- Raise **Exposure** (or use your *More boost* hotkey) for very dark games,
  lower it for brighter ones. In **Tune**, raise **Anti-glare** if bright
  areas still hide things, or lower it to see more in the darkest corners.
- Green shows the most detail, because our eyes see fine detail best in green.
  Blue is tuned to stay as readable as possible (pure blue would be too dark).
- The **Thermal style** color filter looks like a thermal camera, but it colors the picture by
  *brightness*, not heat: dark things look "cold", bright things look "hot".
  Real heat vision would mean reading the game's data, which is exactly what
  anti-cheat systems watch for and what CatVision never does, so it stays a
  display-only effect like everything else.
- With only some displays ticked, night vision stays on those displays, but
  as a colored tint rather than full monochrome (Windows limit).

## Making your own color filters

<img src="docs/filters.png" width="300" align="right" alt="Filters tab">

In the **Filters** tab, click **New...** (or select a built-in filter and
click **Copy...** to start from it). There are two kinds:

- **Monochrome tint**: the picture turns grayscale and is tinted one color,
  like the built-in Night vision and Amber. Use the sliders or
  **Pick a color...**.
- **Channel mixer**: full control. Each output color is a mix of the input
  colors. For example, *Red out = 1.0 × Red* is normal, and moving some of
  Red into Green shifts reds toward yellow.

Your filter is used straight away so you can see changes live (turn
CatVision on first), and the preview shows colors before and after. Your
filters appear in the **Color filter** list on the Home tab, can be saved
into any preset, and can be cycled with the *Next color filter* hotkey.

<br clear="right">

## Get the full strength

By default Windows only lets apps change brightness a limited amount, which
weakens strong looks like night vision. When that happens, the ⚠ in the
header says *"Windows is holding back the strength of this look"* and has an
**Unlock full strength...** button.

1. Click **Unlock full strength...** and confirm. Windows asks for permission.
2. Restart your PC.

This changes one documented Windows setting (`GdiIcmGammaRange`), the same
one display-calibration software uses. **Settings → Full strength** shows
whether it's unlocked and has an **Undo** button. If the ⚠ still says so
after the restart, it tells you your graphics driver has its own limit, and
CatVision is already as strong as your PC allows.

(Running from source instead? `windows/enable_full_strength.reg` and
`windows/undo_full_strength.reg` do the same thing.)

## Tips and troubleshooting

When something needs your attention, a **⚠** appears in the header. Click it
for what's wrong, what to do, and a button that does it (for example **Use it
anyway** when a color effect can only cover every display). The line at the
bottom of the window just says what CatVision is doing.

- **Game in exclusive fullscreen and the color filter doesn't show?**
  Brightness works in every mode, but color filters need **Borderless** or
  **Windowed fullscreen**.
- **The effect disappears when a game starts.** Some games reset brightness
  on launch. Leave *Keep applied* on (Settings) and CatVision puts it back
  within about 2 seconds. It also puts back a display you unticked while
  Windows wasn't taking changes (during a fullscreen switch, say), as soon
  as Windows allows it.
- **Nothing happens with HDR on.** Windows ignores brightness tables while
  HDR is on. Turn HDR off in *Settings → Display* while you use CatVision.
- **Windows' own Color filters are on.** Turn them off; they conflict with
  CatVision's color filters.
- **Screen stuck bright or dark?** Quitting CatVision always restores it. If
  it crashed, just start it again; it notices and cleans up. You can also use
  **Reset display now** in Settings, **Reset display to normal** or
  **Force quit** in the tray menu, your *Panic* hotkey if you set one, or run
  `CatVision.exe --reset`.
- **Looking out of a window from a dark room is too bright?** Raise
  **Outside detail** (Tune tab). With auto levels (beta) on it also adds
  some by itself while a window is in view.
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
