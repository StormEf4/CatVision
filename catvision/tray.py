"""System-tray icon (optional: needs the ``pystray`` and ``Pillow`` packages,
which are bundled into CatVision.exe).

Menu clicks are reported through ``on_action`` from the tray's own thread,
the same way hotkeys are, so the GUI handles both in one place.
"""

from __future__ import annotations

import threading
from typing import Callable

from .winutil import asset


class Tray:
    def __init__(self, on_action: Callable[[str], None]):
        self.on_action = on_action
        self.enabled = False
        self.active_preset = ""
        self.preset_names: list[str] = []
        self._icon = None
        try:
            import pystray
            from PIL import Image
        except Exception:  # not installed: the app works without a tray icon
            self._pystray = None
            return
        self._pystray = pystray
        self._image = Image.open(asset("catvision.png"))

    @property
    def available(self) -> bool:
        return self._pystray is not None

    @property
    def running(self) -> bool:
        return self._icon is not None

    def start(self) -> bool:
        if not self.available or self._icon is not None:
            return self.running
        ps = self._pystray
        act = self.on_action

        def preset_items():
            for name in list(self.preset_names):
                yield ps.MenuItem(
                    name, lambda _i, _it, n=name: act("preset:" + n),
                    checked=lambda _it, n=name: n == self.active_preset, radio=True)

        menu = ps.Menu(
            ps.MenuItem("Show CatVision", lambda _i, _it: act("show_window"), default=True),
            ps.MenuItem("On", lambda _i, _it: act("toggle"), checked=lambda _it: self.enabled),
            ps.MenuItem("Presets", ps.Menu(preset_items)),
            ps.MenuItem("Next color filter", lambda _i, _it: act("filter_next")),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Quit CatVision", lambda _i, _it: act("quit")),
        )
        try:
            self._icon = ps.Icon("CatVision", self._image, self._title(), menu)
            threading.Thread(target=self._icon.run, name="catvision-tray", daemon=True).start()
        except Exception:
            self._icon = None
        return self.running

    def update(self, enabled: bool, active_preset: str, preset_names: list[str]) -> None:
        self.enabled = enabled
        self.active_preset = active_preset
        self.preset_names = list(preset_names)
        if self._icon is not None:
            try:
                self._icon.title = self._title()
                self._icon.update_menu()
            except Exception:
                pass

    def notify(self, text: str) -> None:
        if self._icon is not None:
            try:
                self._icon.notify(text, "CatVision")
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def _title(self) -> str:
        return f"CatVision - {'ON' if self.enabled else 'OFF'} ({self.active_preset})"
