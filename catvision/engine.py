"""Turns settings into display changes and always knows how to undo them."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import curves
from .backends import GammaResult, Monitor, apply_ramp_with_fallback
from .curves import Settings

# How far (in 16-bit units) a ramp read back from the driver may differ from
# what we set before we decide something else overwrote it.  Drivers round
# ramps to their own precision.
_DRIFT_TOLERANCE = 768


@dataclass
class Status:
    enabled: bool = False
    gamma_ok: bool = True
    gamma_limited: bool = False
    color_ok: bool = True
    color_used: bool = False
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.enabled:
            return "Off - your display is back to normal."
        parts = []
        if not self.gamma_ok:
            parts.append("Windows refused the brightness change.")
        elif self.gamma_limited:
            parts.append("On (Windows limited the strength - see README for the fix).")
        else:
            parts.append("On.")
        if self.color_used and not self.color_ok:
            parts.append("Color filter could not be applied.")
        parts.extend(self.messages)
        return " ".join(parts)


class Engine:
    def __init__(self, gamma_backend, color_backend, state_dir: Path | None = None):
        self.gamma = gamma_backend
        self.color = color_backend
        self.settings = Settings()
        self.enabled = False
        self.monitor_choice = "all"
        self.custom_filters: dict[str, curves.Matrix] = {}  # "custom:<name>" -> matrix
        self._flag = (state_dir / "display_modified.flag") if state_dir else None
        self._applied: dict[str, list[list[int]]] = {}
        self.status = Status()

        unclean = self._flag is not None and self._flag.exists()
        self._monitors = self.gamma.monitors()
        for m in self._monitors:
            original = None if unclean else self.gamma.get_ramp(m.id)
            # If the last session crashed while active, the "current" ramp is
            # ours, not the user's -- fall back to a plain straight line.
            m.original = original or curves.identity_ramp()
        if unclean:
            for m in self._monitors:
                self.gamma.set_ramp(m.id, m.original)
            self._mark_dirty(False)

    # -- monitors -----------------------------------------------------------

    def monitors(self) -> list[Monitor]:
        return list(self._monitors)

    def _targets(self) -> list[Monitor]:
        if self.monitor_choice == "all":
            return self._monitors
        chosen = [m for m in self._monitors if m.id == self.monitor_choice]
        return chosen or self._monitors

    # -- main API ---------------------------------------------------------------

    def set_settings(self, settings: Settings) -> Status:
        self.settings = settings.clamped()
        return self.apply()

    def set_enabled(self, enabled: bool) -> Status:
        self.enabled = enabled
        return self.apply()

    def set_monitor_choice(self, choice: str) -> Status:
        self.monitor_choice = choice
        if self.enabled:
            self._restore_gamma()
        return self.apply()

    def apply(self) -> Status:
        if not self.enabled:
            self._restore_gamma()
            self.color.reset()
            self.status = Status(enabled=False)
            return self.status

        status = Status(enabled=True)
        ramp = curves.build_ramp(self.settings)
        self._mark_dirty(True)
        for m in self._targets():
            result: GammaResult = apply_ramp_with_fallback(self.gamma, m.id, ramp)
            if result.ok:
                self._applied[m.id] = self.gamma.get_ramp(m.id) or ramp
            status.gamma_ok = status.gamma_ok and result.ok
            status.gamma_limited = status.gamma_limited or result.limited

        matrix = curves.color_matrix(self.settings, self.custom_filters)
        if curves.is_identity_matrix(matrix):
            self.color.reset()
        else:
            status.color_used = True
            if not self.color.available:
                status.color_ok = False
                status.messages.append(self.color.reason)
            else:
                status.color_ok = self.color.apply(curves.to_mag_color_effect(matrix))
        if not self.gamma.available:
            status.messages.append(self.gamma.reason)
        self.status = status
        return status

    def check_and_reapply(self) -> bool:
        """Re-apply if a game, driver or Windows reset the gamma ramp.

        Returns True if something had to be fixed.
        """
        if not self.enabled or not self.gamma.available:
            return False
        for m in self._targets():
            want = self._applied.get(m.id)
            have = self.gamma.get_ramp(m.id)
            if want is None or have is None:
                continue
            drifted = any(
                abs(a - b) > _DRIFT_TOLERANCE
                for cw, ch in zip(want, have)
                for a, b in zip(cw, ch)
            )
            if drifted:
                self.apply()
                return True
        return False

    def shutdown(self) -> None:
        self.enabled = False
        self._restore_gamma()
        self.color.close()
        self.gamma.close()

    # -- internals ----------------------------------------------------------

    def _restore_gamma(self) -> None:
        for m in self._monitors:
            if m.id in self._applied:
                self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())
        self._applied.clear()
        self._mark_dirty(False)

    def _mark_dirty(self, dirty: bool) -> None:
        if self._flag is None:
            return
        try:
            if dirty:
                self._flag.parent.mkdir(parents=True, exist_ok=True)
                self._flag.touch()
            elif self._flag.exists():
                self._flag.unlink()
        except OSError:
            pass


def emergency_reset(gamma_backend, color_backend, state_dir: Path | None = None) -> int:
    """Put every display back to a plain, unmodified gamma ramp.

    Returns how many displays were reset.
    """
    count = 0
    for m in gamma_backend.monitors():
        if gamma_backend.set_ramp(m.id, curves.identity_ramp()):
            count += 1
    color_backend.reset()
    color_backend.close()
    if state_dir is not None:
        flag = state_dir / "display_modified.flag"
        try:
            flag.unlink()
        except OSError:
            pass
    return count
