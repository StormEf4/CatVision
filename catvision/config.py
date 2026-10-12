"""Saving and loading user settings, presets, filters and hotkeys."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .curves import Settings
from .filters import CustomFilter, clean_name
from .hotkeys import default_hotkeys, normalize_combo


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
    monitor: str = "all"            # "all" or a monitor id
    text_size: str = "Large"        # Normal / Large / Extra large

    @classmethod
    def from_dict(cls, data) -> "Options":
        o = cls()
        if isinstance(data, dict):
            for key, default in asdict(o).items():
                if key in data and isinstance(data[key], type(default)):
                    setattr(o, key, data[key])
        return o


@dataclass
class Config:
    current: Settings = field(default_factory=Settings)
    active_preset: str = "Dark Areas"
    # The user's presets.  A name matching a built-in preset overrides it.
    custom_presets: dict[str, Settings] = field(default_factory=dict)
    custom_filters: dict[str, CustomFilter] = field(default_factory=dict)
    # action -> "Ctrl+Alt+V" style combo ("" = no hotkey)
    hotkeys: dict[str, str] = field(default_factory=default_hotkeys)
    options: Options = field(default_factory=Options)

    def to_json(self) -> dict:
        return {
            "version": 2,
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
        c.current = Settings.from_dict(data.get("current", {}))
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
        return c


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
