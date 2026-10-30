"""Turns settings into display changes and always knows how to undo them."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import curves
from .backends import GammaResult, Monitor, apply_ramp_with_fallback, shown_memory
from .curves import Settings

# How far (in 16-bit units) a ramp read back from the driver may differ from
# what we set before we decide something else overwrote it.  Drivers round
# ramps to their own precision.
_DRIFT_TOLERANCE = 768
# Auto levels (beta): a display whose last change Windows refused is offered
# its look again this often (with a fresh search), even once its fade has
# stopped and no new update would come.
_RETRY_S = 0.5


@dataclass
class Status:
    enabled: bool = False
    gamma_ok: bool = True
    gamma_limited: bool = False
    color_ok: bool = True
    color_used: bool = False
    color_all_displays: bool = False   # a color filter is covering every display
    color_skipped: bool = False        # a color filter was left off (displays chosen)
    messages: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if not self.enabled:
            return "Off - your display is back to normal."
        parts = []
        if not self.gamma_ok:
            parts.append("Windows refused the brightness change.")
        elif self.gamma_limited:
            parts.append("On, but Windows is holding back the strength of this look.")
        else:
            parts.append("On.")
        if self.color_used and not self.color_ok:
            parts.append("Color filter could not be applied.")
        if self.color_skipped:
            parts.append("Saturation / color-mixing filters are off so your other displays "
                         "stay untouched.")
        parts.extend(self.messages)
        return " ".join(parts)


class Engine:
    def __init__(self, gamma_backend, color_backend, state_dir: Path | None = None):
        self.gamma = gamma_backend
        self.color = color_backend
        self.settings = Settings()
        self.enabled = False
        self.displays: list[str] = []      # chosen display ids; empty = all displays
        # When only some displays are chosen, saturation and filters that mix
        # colors can only be shown on every display (Windows limit), so they
        # are left off unless the user explicitly allows that.
        self.color_all_displays = False
        self.custom_filters: dict[str, curves.Matrix] = {}  # "custom:<name>" -> matrix
        self.custom_tints: dict[str, tuple] = {}            # "custom:<name>" -> tint
        # Auto levels (beta): per display id, amounts added on top of the
        # preset, e.g. {"shadow_boost": 12.5} or {"shadow_boost": 15, "exposure": 4}.
        self.auto_offsets: dict[str, dict[str, float]] = {}
        # Bumped when auto levels stops, so a late update from a fader that
        # was still finishing can't put a boost back afterwards.
        self.auto_generation = 0
        self._lock = threading.RLock()  # force-quit restores from the tray thread
        self._flag = (state_dir / "display_modified.flag") if state_dir else None
        self._applied: dict[str, list[list[int]]] = {}
        self._fallback: dict = {}   # per display: what fading updates last showed
        # Per display: (preset ramp, what the normal apply showed for it), so a
        # fade coming back to rest shows exactly that in one step.
        self._rest: dict[str, tuple[list, list]] = {}
        # Per display: (ok, limited) of the last brightness table set, so the
        # status follows what is on screen now (a fade that Windows held back
        # must not leave "holding back" showing once it is back to normal).
        self._gamma_state: dict[str, tuple[bool, bool]] = {}
        # Per display: when Windows started refusing it, or it was last
        # offered its look again (see _RETRY_S).
        self._retry_at: dict[str, float] = {}
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
        chosen = [m for m in self._monitors if m.id in self.displays]
        return chosen or self._monitors  # nothing (still) connected: use all

    def target_monitors(self) -> list[Monitor]:
        """The displays CatVision changes (all of them if none are chosen)."""
        return self._targets()

    def all_displays_selected(self) -> bool:
        return len(self._targets()) == len(self._monitors)

    # -- main API ---------------------------------------------------------------

    def set_settings(self, settings: Settings) -> Status:
        self.settings = settings.clamped()
        return self.apply()

    def set_enabled(self, enabled: bool) -> Status:
        self.enabled = enabled
        return self.apply()

    def set_displays(self, display_ids) -> Status:
        """Choose which displays CatVision changes (empty = all)."""
        with self._lock:
            self.displays = [d for d in display_ids]
            if self.enabled:
                self._restore_gamma()
            return self.apply()

    def apply(self) -> Status:
        with self._lock:
            return self._apply()

    def _apply(self) -> Status:
        if not self.enabled:
            self._restore_gamma()
            self.color.reset()
            self.status = Status(enabled=False)
            return self.status

        status = Status(enabled=True)
        per_display = not self.all_displays_selected()
        tint, matrix = curves.plan_color(self.settings, self.custom_filters, self.custom_tints,
                                         all_displays=not per_display)
        self._mark_dirty(True)
        self._gamma_state.clear()
        self._apply_gamma(self._targets(), tint, status)

        if curves.is_identity_matrix(matrix):
            self.color.reset()
        elif per_display and not self.color_all_displays:
            self.color.reset()
            status.color_skipped = True
        else:
            status.color_all_displays = per_display
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

    def retire_auto_offsets(self) -> None:
        """Auto levels has stopped: drop its offsets and ignore any late
        update from the run that just ended."""
        with self._lock:
            self.auto_generation += 1
            if self.auto_offsets:
                self.set_auto_offsets({})

    def set_auto_offsets(self, offsets: dict[str, dict[str, float]],
                         generation: int | None = None) -> None:
        """Auto levels (beta): move Shadow boost / Exposure per display without
        touching the preset.  Only the brightness tables are redone.  A
        display whose last change Windows refused is offered its look again
        every _RETRY_S, even if nothing changed (a fade that has stopped keeps
        sending the same offsets)."""
        with self._lock:
            if generation is not None and generation != self.auto_generation:
                return  # from a fader run that has already been stopped
            def amounts(d):   # {} and all-zero adjustments are the same picture
                return {k: v for k, v in (d or {}).items() if v}
            targets = self._targets()
            changed = [m for m in targets
                       if amounts(offsets.get(m.id)) != amounts(self.auto_offsets.get(m.id))]
            self.auto_offsets = {d: dict(v) for d, v in offsets.items()}
            if not self.enabled:
                return
            now = time.monotonic()
            due = {m.id for m in targets if not self._gamma_state.get(m.id, (True,))[0]
                   and now - self._retry_at.get(m.id, now) >= _RETRY_S}
            redo = changed + [m for m in targets if m.id in due and m not in changed]
            if not redo:
                return
            tint, _matrix = curves.plan_color(self.settings, self.custom_filters,
                                              self.custom_tints,
                                              all_displays=self.all_displays_selected())
            # Displays with no adjustment left get the normal full apply, so
            # they settle exactly where the preset alone would.
            self._apply_gamma(redo, tint, self.status, fading=True, search=due)

    def retry_pending(self) -> bool:
        """Windows refused the last change of a display (a quick check
        without the lock; set_auto_offsets decides when to try again)."""
        return self.enabled and not self.status.gamma_ok

    def settings_for(self, display_id: str) -> Settings:
        """The settings a display really gets (the preset plus auto levels)."""
        amounts = {k: v for k, v in self.auto_offsets.get(display_id, {}).items() if v}
        if not amounts:
            return self.settings
        return replace(self.settings, **{k: getattr(self.settings, k) + v
                                         for k, v in amounts.items()}).clamped()

    def _apply_gamma(self, monitors, tint, status: Status, fading: bool = False,
                     search: set[str] = frozenset()) -> None:
        """``fading``: a frequent auto-levels update.  Displays that carry an
        adjustment reuse what Windows last accepted and remember what they
        set instead of reading every ramp back (the keep-applied check still
        compares); displays with none get the normal full apply.  ``search``:
        displays Windows refused, offered their look again (see _RETRY_S)."""
        if not fading:
            self._fallback.clear()   # a full apply starts from the full ramp again
        ramps: dict[tuple, list] = {}  # displays with the same adjustment share a ramp
        for m in monitors:
            adjusted = fading and any(self.auto_offsets.get(m.id, {}).values())
            which = tuple(sorted(self.auto_offsets.get(m.id, {}).items()))
            ramp = ramps.get(which)
            if ramp is None:
                ramp = ramps[which] = curves.build_ramp(self.settings_for(m.id), tint)
            if adjusted:
                result: GammaResult = apply_ramp_with_fallback(
                    self.gamma, m.id, ramp, self._fallback, search=m.id in search)
            else:
                result = self._apply_rest(m.id, ramp) if fading else None
                if result is None:
                    result = apply_ramp_with_fallback(self.gamma, m.id, ramp, None)
                if result.ok:
                    self._rest[m.id] = (ramp, result.applied or ramp)
                # Remember what is on screen so the next fading update
                # starts from it (no dip when it is refused).
                self._fallback.pop(m.id, None)
                if result.ok:
                    self._fallback[m.id] = shown_memory(result.applied or ramp)
            if result.ok:
                if adjusted:
                    self._applied[m.id] = result.applied or ramp
                else:
                    self._applied[m.id] = self.gamma.get_ramp(m.id) or ramp
            elif m.id in search or self._gamma_state.get(m.id, (True,))[0]:
                self._retry_at[m.id] = time.monotonic()   # refused: try again later
            self._gamma_state[m.id] = (result.ok, result.limited)
        states = [self._gamma_state[m.id] for m in self._targets() if m.id in self._gamma_state]
        status.gamma_ok = all(ok for ok, _ in states)
        status.gamma_limited = any(limited for _, limited in states)

    def _apply_rest(self, display_id: str, ramp) -> GammaResult | None:
        """A fade came back to rest: show exactly what the preset's own apply
        showed, in one step (on a PC where Windows limits the strength, the
        normal search would flash weaker versions first).  The full ramp is
        offered first (a refusal changes nothing on screen).  None if that
        isn't known for this ramp or Windows no longer takes it."""
        cached = self._rest.get(display_id)
        if cached is None or cached[0] != ramp:
            return None
        if self.gamma.set_ramp(display_id, ramp):
            return GammaResult(True, applied=ramp)
        if cached[1] != ramp and self.gamma.set_ramp(display_id, cached[1]):
            return GammaResult(True, limited=True, applied=cached[1])
        return None

    def check_and_reapply(self) -> bool:
        """Re-apply if a game, driver or Windows reset the gamma ramp.

        Returns True if something had to be fixed.
        """
        if not self.enabled or not self.gamma.available:
            return False
        with self._lock:
            return self._check_and_reapply()

    def _check_and_reapply(self) -> bool:
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
                self._apply()
                return True
        return False

    def shutdown(self) -> None:
        with self._lock:
            self.enabled = False
            self._restore_gamma()
            self.color.close()
            self.gamma.close()

    def restore_now(self) -> None:
        """Put the displays back immediately (safe to call from any thread).

        Used by Force quit, which must work even if the window is frozen.
        Color filters end by themselves when the process exits.
        """
        if self._lock.acquire(timeout=1.0):
            try:
                self._restore_gamma()
            finally:
                self._lock.release()
        else:  # something is stuck holding the lock: restore without it
            for m in self._monitors:
                self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())
            self._mark_dirty(False)

    # -- internals ----------------------------------------------------------

    def _restore_gamma(self) -> None:
        for m in self._monitors:
            if m.id in self._applied:
                self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())
        self._applied.clear()
        self._gamma_state.clear()
        self._rest.clear()
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
