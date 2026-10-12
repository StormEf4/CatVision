"""System-tray icon and its right-click menu (needs ``pystray`` + ``Pillow``,
which are bundled into CatVision.exe).

Menu clicks are reported through ``on_action`` from the tray's own thread,
the same way hotkeys are, so the window handles both in one place.

The one exception is **Force quit**: it must work even if the window is
frozen, so it runs ``on_force_quit`` directly on the tray thread.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable

from .winutil import asset


@dataclass
class TrayState:
    enabled: bool = False
    preset: str = ""
    presets: list[str] = field(default_factory=list)
    filter_key: str = "none"
    filters: list[tuple[str, str]] = field(default_factory=list)   # (key, label)
    boost: int = 0
    boost_label: str = "Shadow boost"
    displays: list[tuple[str, str]] = field(default_factory=list)  # (id, label)
    chosen: list[str] = field(default_factory=list)                # empty = all
    auto_levels: bool = False


class Tray:
    def __init__(self, on_action: Callable[[str], None],
                 on_force_quit: Callable[[], None] | None = None):
        self.on_action = on_action
        self.on_force_quit = on_force_quit
        self.state = TrayState()
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

    def build_menu(self):
        ps = self._pystray
        act = self.on_action
        st = lambda: self.state  # noqa: E731  (always read the latest state)

        def do(action):
            return lambda _icon, _item: act(action)

        def presets():
            for name in list(st().presets):
                yield ps.MenuItem(name, do("preset:" + name), radio=True,
                                  checked=lambda _i, n=name: n == st().preset)

        def color_filters():
            for key, label in list(st().filters):
                yield ps.MenuItem(label, do("filter:" + key), radio=True,
                                  checked=lambda _i, k=key: k == st().filter_key)

        def displays():
            yield ps.MenuItem("All displays", do("display:all"), radio=True,
                              checked=lambda _i: not st().chosen)
            for did, label in list(st().displays):
                yield ps.MenuItem(label, do("display:" + did),
                                  checked=lambda _i, d=did: d in st().chosen)
            yield ps.Menu.SEPARATOR
            yield ps.MenuItem("Identify displays", do("identify"))

        return ps.Menu(
            ps.MenuItem("Show CatVision", do("show_window"), default=True),
            ps.MenuItem("CatVision on", do("toggle"), checked=lambda _i: st().enabled),
            ps.MenuItem("Auto levels (beta)", do("auto_levels"),
                        checked=lambda _i: st().auto_levels),
            ps.Menu.SEPARATOR,
            ps.MenuItem(lambda _i: f"Preset: {st().preset}", ps.Menu(presets)),
            ps.MenuItem("Color filter", ps.Menu(color_filters)),
            ps.MenuItem(lambda _i: f"{st().boost_label}: {st().boost}", ps.Menu(
                ps.MenuItem("More (+10)", do("boost_up")),
                ps.MenuItem("Less (-10)", do("boost_down")),
            )),
            ps.MenuItem("Displays", ps.Menu(displays),
                        visible=lambda _i: len(st().displays) > 1),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Panic: everything off", do("off")),
            ps.MenuItem("Reset display to normal", do("reset_display")),
            ps.Menu.SEPARATOR,
            ps.MenuItem("Quit CatVision", do("quit")),
            ps.MenuItem("Force quit (close right now)", lambda _i, _it: self.force_quit()),
        )

    def start(self) -> bool:
        if not self.available or self._icon is not None:
            return self.running
        try:
            self._icon = self._pystray.Icon("CatVision", self._image, self._title(),
                                            self.build_menu())
            threading.Thread(target=self._icon.run, name="catvision-tray", daemon=True).start()
        except Exception:
            self._icon = None
        return self.running

    def update(self, state: TrayState) -> None:
        self.state = state
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

    def force_quit(self) -> None:
        """Restore the display and end the process immediately."""
        try:
            if self.on_force_quit:
                self.on_force_quit()
        finally:
            if self._icon is not None:
                try:
                    self._icon.visible = False  # don't leave a ghost icon behind
                except Exception:
                    pass
            os._exit(0)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def _title(self) -> str:
        s = self.state
        return f"CatVision by StormEf4 - {'ON' if s.enabled else 'OFF'} ({s.preset})"[:127]
