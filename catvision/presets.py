"""Built-in looks for common situations.

The first nine presets are reachable with Ctrl+Alt+1 .. Ctrl+Alt+9, so the
most useful ones for dark games come first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .curves import Settings

# Outside detail for the strong looks: without it their lift and contrast
# turn everything brighter than a lamp-lit wall white, so the view out of a
# window from a dark room is one white block.  This much keeps the sky,
# walls and trees apart and leaves the dark room's tones as they were.
HIGHLIGHT_ROOM = 40.0


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    settings: Settings
    builtin: bool = True


BUILTIN_PRESETS: list[Preset] = [
    Preset(
        "Gentle Boost",
        "A light lift for games that are just a bit too dark.",
        Settings(shadow_boost=40, exposure=6, anti_glare=100, gamma=1.1),
    ),
    Preset(
        "Dark Areas",
        "Opens up dark rooms, caves and night scenes. A good everyday choice.",
        Settings(shadow_boost=66, exposure=3, anti_glare=100, gamma=1.0, black_floor=0.02,
                 contrast=1.34, highlight_room=HIGHLIGHT_ROOM),
    ),
    Preset(
        "Pitch Black",
        "Maximum shadow lift for nearly black areas. Image will look washed out.",
        Settings(shadow_boost=85, gamma=1.0, black_floor=0.04, brightness=1.05,
                 highlight_room=HIGHLIGHT_ROOM),
    ),
    Preset(
        "Night Vision",
        "Like night-vision goggles: amplifies dim light evenly, deep green shadows "
        "and pale highlights. Bright lights are held back so they never blind you "
        "or hide someone standing in front of them.",
        Settings(exposure=40, anti_glare=100, gamma=1.3, contrast=1.1, black_floor=0.015,
                 white_cap=0.88, color_filter="night_vision", highlight_room=HIGHLIGHT_ROOM),
    ),
    Preset(
        "Night Vision (Blue)",
        "Night vision in cool blue. Bright lights are held back, like the green one.",
        Settings(exposure=50, anti_glare=74, gamma=1.25, contrast=1.1, black_floor=0.015,
                 white_cap=0.88, color_filter="night_vision_blue",
                 highlight_room=HIGHLIGHT_ROOM),
    ),
    Preset(
        "Fog & Haze Cutter",
        "Adds contrast and color to murky, foggy or smoky scenes.",
        Settings(shadow_boost=30, contrast=1.35, saturation=1.25),
    ),
    Preset(
        "Vivid Pop",
        "Slight lift with richer colors and contrast.",
        Settings(shadow_boost=25, contrast=1.15, saturation=1.4),
    ),
    Preset(
        "Glare Guard",
        "No shadow lift; tones down very bright scenes like snow or deserts.",
        Settings(gamma=0.9, brightness=0.92, contrast=1.1, white_cap=0.8),
    ),
]


def builtin_by_name(name: str) -> Preset | None:
    for p in BUILTIN_PRESETS:
        if p.name == name:
            return p
    return None


@dataclass(frozen=True)
class PresetEntry:
    name: str
    settings: Settings
    builtin: bool
    changed: bool        # a built-in preset the user saved their own version of
    description: str


def all_presets(saved: dict[str, Settings]) -> list[PresetEntry]:
    """Built-in presets (with the user's saved changes) then the user's own.

    ``saved`` holds the user's presets.  An entry with the same name as a
    built-in preset replaces that built-in's settings.
    """
    rows = []
    for p in BUILTIN_PRESETS:
        mine = saved.get(p.name)
        changed = mine is not None and mine != p.settings
        rows.append(PresetEntry(p.name, mine if mine is not None else p.settings,
                                True, changed, p.description))
    builtin_names = {p.name for p in BUILTIN_PRESETS}
    for name in sorted((n for n in saved if n not in builtin_names), key=str.lower):
        rows.append(PresetEntry(name, saved[name], False, False, "Your own preset."))
    return rows
