"""Built-in looks for common situations.

The first nine presets are reachable with Ctrl+Alt+1 .. Ctrl+Alt+9, so the
most useful ones for dark games come first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .curves import Settings


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
        Settings(shadow_boost=35, gamma=1.1),
    ),
    Preset(
        "Dark Areas",
        "Opens up dark rooms, caves and night scenes. A good everyday choice.",
        Settings(shadow_boost=60, gamma=1.15, black_floor=0.02, contrast=1.05),
    ),
    Preset(
        "Pitch Black",
        "Maximum shadow lift for nearly black areas. Image will look washed out.",
        Settings(shadow_boost=85, gamma=1.25, black_floor=0.04, brightness=1.05),
    ),
    Preset(
        "Night Vision",
        "Like night-vision goggles: amplifies dim light evenly, deep green shadows "
        "and pale highlights. Bright lights are held back so they never blind you "
        "or hide someone standing in front of them.",
        Settings(exposure=70, anti_glare=70, contrast=1.1, black_floor=0.015, white_cap=0.88,
                 color_filter="night_vision"),
    ),
    Preset(
        "Night Vision (Blue/Red)",
        "Night vision in blue and red: blue shadows, purple mid-tones, red highlights.",
        Settings(exposure=75, anti_glare=70, contrast=1.1, black_floor=0.015, white_cap=0.88,
                 color_filter="night_vision_br"),
    ),
    Preset(
        "Horror / Flashlight",
        "Lifts darkness but caps the brightest whites so flashlights and "
        "lamps don't dazzle you.",
        Settings(shadow_boost=70, gamma=1.1, black_floor=0.02, white_cap=0.85),
    ),
    Preset(
        "Fog & Haze Cutter",
        "Adds contrast and color to murky, foggy or smoky scenes.",
        Settings(shadow_boost=30, contrast=1.35, saturation=1.25),
    ),
    Preset(
        "Mixed Light",
        "For games with dark shadows next to bright skies or explosions.",
        Settings(shadow_boost=50, gamma=1.05, contrast=0.95, white_cap=0.82),
    ),
    Preset(
        "High-Contrast Mono",
        "Grayscale with strong contrast. Makes shapes and edges stand out.",
        Settings(shadow_boost=50, contrast=1.3, color_filter="grayscale"),
    ),
    Preset(
        "Warm Low-Light",
        "Shadow lift with less blue light, easier on tired eyes.",
        Settings(shadow_boost=55, gamma=1.1, green=0.92, blue=0.75, saturation=0.9),
    ),
    Preset(
        "Vivid Pop",
        "Slight lift with richer colors and contrast.",
        Settings(shadow_boost=25, contrast=1.15, saturation=1.4),
    ),
    Preset(
        "Amber Night",
        "Strong lift in a warm amber monochrome.",
        Settings(shadow_boost=65, gamma=1.1, black_floor=0.02, color_filter="amber"),
    ),
    Preset(
        "Glare Guard",
        "No shadow lift; tones down very bright scenes like snow or deserts.",
        Settings(gamma=0.9, brightness=0.92, contrast=1.1, white_cap=0.8),
    ),
    Preset(
        "Red-Green Assist (Deutan)",
        "Mild lift plus color correction for deuteranopia.",
        Settings(shadow_boost=30, color_filter="deuteranopia"),
    ),
    Preset(
        "Red-Green Assist (Protan)",
        "Mild lift plus color correction for protanopia.",
        Settings(shadow_boost=30, color_filter="protanopia"),
    ),
    Preset(
        "Blue-Yellow Assist (Tritan)",
        "Mild lift plus color correction for tritanopia.",
        Settings(shadow_boost=30, color_filter="tritanopia"),
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
