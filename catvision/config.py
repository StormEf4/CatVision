"""Saving and loading user settings, presets, filters and hotkeys."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .curves import Settings
from .filters import CustomFilter, clean_name
from .hotkeys import LEGACY_DEFAULT_HOTKEYS, default_hotkeys, normalize_combo


def app_dir() -> Path:
    override = os.environ.get("CATVISION_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return base / "CatVision"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "catvision"


@dataclass
class Options:
    keep_applied: bool = True       # re-apply if a game or Windows resets gamma
    hotkeys: bool = True
    hotkey_popup: bool = True       # small on-screen popup when using hotkeys
    enable_on_start: bool = False
    close_to_tray: bool = True      # closing the window keeps CatVision in the tray
    displays: list = field(default_factory=list)  # chosen display ids; empty = all
    # With only some displays ticked, still apply saturation / color-mixing
    # filters even though Windows applies them to every display.  Off by
    # default so unticked displays are never changed.  (Renamed from
    # "color_all_displays", which defaulted to on, so old configs reset.)
    color_everywhere: bool = False
    text_size: str = "Large"        # Normal / Large / Extra large
    seen_welcome: bool = False
    unlock_requested_at: float = 0.0  # when the user unlocked full strength

    @classmethod
    def from_dict(cls, data) -> "Options":
        o = cls()
        if isinstance(data, dict):
            for key, default in asdict(o).items():
                if key in data and isinstance(data[key], type(default)):
                    setattr(o, key, data[key])
            o.displays = [d for d in o.displays if isinstance(d, str)][:16]
            # version 1-2 stored a single "monitor" ("all" or an id)
            old = data.get("monitor")
            if "displays" not in data and isinstance(old, str) and old != "all":
                o.displays = [old]
        return o


DEFAULT_PRESET = "Dark Areas"

# Built-in presets that were renamed or replaced: old name -> new name.
LEGACY_PRESETS = {"Night Vision (Blue/Red)": "Night Vision (Blue)"}

# Built-in presets removed for good in 1.6.  Saved edits of them and their
# hotkeys are dropped; if one was active, CatVision falls back to the default.
REMOVED_PRESETS = {
    "Night Vision (Red)", "Night Vision (Grey)", "Thermal", "Horror / Flashlight",
    "Mixed Light", "High-Contrast Mono", "Warm Low-Light", "Amber Night",
    "Red-Green Assist (Deutan)", "Red-Green Assist (Protan)", "Blue-Yellow Assist (Tritan)",
}


def _default_settings() -> Settings:
    from .presets import builtin_by_name
    return builtin_by_name(DEFAULT_PRESET).settings


@dataclass
class Config:
    # A fresh install starts on the default preset's settings.
    current: Settings = field(default_factory=_default_settings)
    active_preset: str = DEFAULT_PRESET
    # The user's presets.  A name matching a built-in preset overrides it.
    custom_presets: dict[str, Settings] = field(default_factory=dict)
    custom_filters: dict[str, CustomFilter] = field(default_factory=dict)
    # action -> "Ctrl+Alt+V" style combo ("" = no hotkey)
    hotkeys: dict[str, str] = field(default_factory=default_hotkeys)
    options: Options = field(default_factory=Options)

    def to_json(self) -> dict:
        return {
            "version": 3,
            "current": self.current.to_dict(),
            "active_preset": self.active_preset,
            "custom_presets": {k: v.to_dict() for k, v in self.custom_presets.items()},
            "custom_filters": {k: v.to_dict() for k, v in self.custom_filters.items()},
            "hotkeys": dict(self.hotkeys),
            "options": asdict(self.options),
        }

    @classmethod
    def from_json(cls, data) -> "Config":
        c = cls()
        if not isinstance(data, dict):
            return c
        if "current" in data:
            c.current = Settings.from_dict(data["current"])
        if isinstance(data.get("active_preset"), str):
            c.active_preset = data["active_preset"]
        customs = data.get("custom_presets", {})
        if isinstance(customs, dict):
            c.custom_presets = {
                str(k)[:60]: Settings.from_dict(v) for k, v in customs.items()
            }
        filters = data.get("custom_filters", {})
        if isinstance(filters, dict):
            for k, v in filters.items():
                name = clean_name(k)
                if name:
                    c.custom_filters[name] = CustomFilter.from_dict(v)
        hotkeys = data.get("hotkeys")
        if isinstance(hotkeys, dict):
            c.hotkeys = {}
            for action, combo in hotkeys.items():
                if not isinstance(action, str) or not isinstance(combo, str):
                    continue
                c.hotkeys[action] = (normalize_combo(combo) or "") if combo else ""
        c.options = Options.from_dict(data.get("options"))
        # Versions 1.1-1.3 came with hotkeys already set.  If they were never
        # changed, clear them: the user now chooses their own.
        if c.hotkeys == LEGACY_DEFAULT_HOTKEYS:
            c.hotkeys = default_hotkeys()
        _migrate_preset_names(c)
        return c


def _migrate_preset_names(c: Config) -> None:
    for name in REMOVED_PRESETS:
        c.custom_presets.pop(name, None)
        c.hotkeys.pop("preset:" + name, None)
    if c.active_preset in REMOVED_PRESETS:
        c.active_preset = DEFAULT_PRESET
        c.current = _default_settings()
    for old, new in LEGACY_PRESETS.items():
        if c.active_preset == old:
            c.active_preset = new
        if old in c.custom_presets:
            saved = c.custom_presets.pop(old)
            c.custom_presets.setdefault(new, saved)
        old_action, new_action = "preset:" + old, "preset:" + new
        if old_action in c.hotkeys:
            combo = c.hotkeys.pop(old_action)
            if combo and not c.hotkeys.get(new_action):
                c.hotkeys[new_action] = combo


def load(path: Path | None = None) -> Config:
    path = path or app_dir() / "config.json"
    try:
        with open(path, encoding="utf-8") as f:
            return Config.from_json(json.load(f))
    except (OSError, ValueError):
        return Config()


def save(config: Config, path: Path | None = None) -> None:
    path = path or app_dir() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config.to_json(), f, indent=2)
    os.replace(tmp, path)
