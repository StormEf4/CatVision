"""Turns settings into display changes and always knows how to undo them."""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import autolevels, curves
from .backends import GammaResult, Monitor, apply_ramp_with_fallback, shown_memory
from .curves import Settings

# How far (in 16-bit units) a ramp read back from the driver may differ from
# what we set before we decide something else overwrote it.  Drivers round
# ramps to their own precision.
_DRIFT_TOLERANCE = 768
# Auto levels (beta): a display whose last change Windows refused is offered
# its look again this often (with a fresh search; see _SEARCH_EVERY_S), even
# once its fade has stopped and no new update would come.
_RETRY_S = 0.5
# A display Windows keeps refusing is still offered its look at every retry
# (auto levels every _RETRY_S, the keep-applied check every 2 s), but after
# this long refusing it is searched for a weaker version Windows would take
# (about a dozen refused calls) only this often; the retries in between offer
# just its full look, or that and the weaker version Windows last took for
# it: one or two calls.  A short refusal (a fullscreen switch) is searched at
# every retry as before, so the look comes back as soon as Windows takes it.
_SEARCH_EVERY_S = 10.0


def peek_lift(settings: Settings, amounts: dict[str, float], lift: float) -> dict[str, float]:
    """What a Peek of ``lift`` points adds on top of the preset plus auto
    levels' ``amounts``: Shadow boost goes up first, because it lifts the
    darkest parts the most (even with the night vision presets, where it
    beats Exposure in the dark corners a Peek is for), and whatever doesn't
    fit spills to Exposure (autolevels.split_offset).  Only ever brightens."""
    base = {k: getattr(settings, k) + amounts.get(k, 0.0)
            for k in ("shadow_boost", "exposure", "anti_glare")}
    out = autolevels.split_offset("shadow_boost", lift, base)
    return {k: v for k, v in out.items() if v > 0}


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


def menu_ramp(ramp: list, original: list, amount: float) -> list:
    """Normal display in menus: ``ramp`` glided ``amount`` (0-1) of the way
    to the display's own brightness table (1 = exactly that table)."""
    if amount >= 1.0:
        return [list(ch) for ch in original]
    keep = 1.0 - amount
    return [[int(round(o + (v - o) * keep)) for v, o in zip(ch, och)]
            for ch, och in zip(ramp, original)]


def menu_matrix(matrix: list, amount: float) -> list:
    """The color filter glided ``amount`` (0-1) of the way to none."""
    if amount <= 0.0:
        return matrix
    if amount >= 1.0:
        return [row[:] for row in curves.IDENTITY3]
    return [[v + (i - v) * amount for v, i in zip(row, irow)]
            for row, irow in zip(matrix, curves.IDENTITY3)]


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
        # Peek: points added on top of everything (see peek_lift) on every
        # chosen display while the Peek hotkey is held (0 = not peeking).
        self.peek_amount = 0.0
        # Bumped when a peek is cut short (CatVision off, quit), so a late
        # step from that peek can't put the lift back.
        self.peek_generation = 0
        # "Normal display in menus" (beta): how far every chosen display has
        # glided back to normal (0 = the full look, 1 = exactly as if
        # CatVision were off: the displays' own brightness tables and no
        # color filter) while the game shows one of its menus.
        self.menu_amount = 0.0
        # Bumped when CatVision goes off or quits, so a late step from a
        # menu fade can't leave a display half-normal afterwards.
        self.menu_generation = 0
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
        # Per display Windows keeps refusing: (first retry, last search) times
        # (see _SEARCH_EVERY_S).
        self._refusing: dict[str, tuple[float, float]] = {}
        self.status = Status()

        unclean = self._flag is not None and self._flag.exists()
        self._monitors = self.gamma.monitors()
        for m in self._monitors:
            original = None if unclean else self.gamma.get_ramp(m.id)
            # If the last session crashed while active, the "current" ramp is
            # ours, not the user's -- fall back to a plain straight line.
            m.original = original or curves.identity_ramp()
        if unclean:
            # Keep the mark if Windows refused one (try again next start).
            refused = [m for m in self._monitors if not self.gamma.set_ramp(m.id, m.original)]
            self._mark_dirty(bool(refused))

    # -- monitors -----------------------------------------------------------

    def monitors(self) -> list[Monitor]:
        return list(self._monitors)

    def _targets(self) -> list[Monitor]:
        chosen = [m for m in self._monitors if m.id in self.displays]
        return chosen or self._monitors  # nothing (still) connected: use all

    def refresh_layout(self) -> bool:
        """Re-read where each display is and how fast it refreshes (the
        resolution, arrangement or refresh rate changed while CatVision
        runs).  True if anything changed.  A backend that can't tell, or a
        display Windows no longer lists, keeps what it had."""
        read = getattr(self.gamma, "layout", None)
        if read is None:
            return False
        try:
            now = read()
        except Exception:   # a ctypes error too: this must never stop auto levels
            return False
        changed = False
        with self._lock:
            for m in self._monitors:
                rect, hz = now.get(m.id, (m.rect, m.hz))
                if (rect, hz) != (m.rect, m.hz):
                    m.rect, m.hz = rect, hz
                    changed = True
        return changed

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
            self._end_peek()   # a peek never outlives CatVision being on
            self._end_menu()   # ...nor does a menu's normal display
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

        matrix = menu_matrix(matrix, self.menu_amount)
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
        sending the same offsets); see _SEARCH_EVERY_S for how hard."""
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
            self._redo_fading(changed)

    def set_peek(self, amount: float, generation: int | None = None) -> None:
        """Peek: lift by ``amount`` (see peek_lift) on every chosen display, on
        top of the preset and auto levels (0 = back to exactly what showed
        before).  Takes the same quick path as auto levels updates."""
        with self._lock:
            if generation is not None and generation != self.peek_generation:
                return   # from a peek that was cut short
            try:
                amount = max(0.0, float(amount))
            except (TypeError, ValueError):
                amount = 0.0
            if not math.isfinite(amount):
                amount = 0.0
            changed = amount != self.peek_amount
            self.peek_amount = amount
            if not self.enabled:
                return
            self._redo_fading(self._targets() if changed else [])

    def set_menu_amount(self, amount: float, generation: int | None = None) -> None:
        """Normal display in menus (beta): glide every chosen display toward
        normal (1 = as if CatVision were off) or back (0 = exactly what
        showed before).  Takes the same quick path as auto levels updates,
        and moves the color filter along with it."""
        with self._lock:
            if generation is not None and generation != self.menu_generation:
                return   # from a menu fade that was cut short
            try:
                amount = min(1.0, max(0.0, float(amount)))
            except (TypeError, ValueError):
                amount = 0.0
            if not math.isfinite(amount):
                amount = 0.0
            changed = amount != self.menu_amount
            self.menu_amount = amount
            if not self.enabled:
                return
            self._redo_fading(self._targets() if changed else [])
            if changed:
                self._menu_color(amount)

    def _menu_color(self, amount: float) -> None:
        """Move the color filter (if the look has one that shows) along with
        a menu fade, as _apply would show it."""
        per_display = not self.all_displays_selected()
        _tint, matrix = curves.plan_color(self.settings, self.custom_filters, self.custom_tints,
                                          all_displays=not per_display)
        if (curves.is_identity_matrix(matrix) or not self.color.available
                or per_display and not self.color_all_displays):
            return
        matrix = menu_matrix(matrix, amount)
        if curves.is_identity_matrix(matrix):
            self.color.reset()
            self.status.color_used = False
        else:
            # (The status as _apply would give it: the notices follow it.)
            self.status.color_ok = self.color.apply(curves.to_mag_color_effect(matrix))
            self.status.color_used, self.status.color_all_displays = True, per_display

    def _end_menu(self) -> None:
        self.menu_generation += 1
        self.menu_amount = 0.0

    def end_peek(self) -> None:
        """Stop any peek at once and ignore its late steps."""
        with self._lock:
            lifted = self.peek_amount
            self._end_peek()
            if lifted and self.enabled:
                self._redo_fading(self._targets())

    def _end_peek(self) -> None:
        self.peek_generation += 1
        self.peek_amount = 0.0

    def _redo_fading(self, changed: list) -> None:
        """Redo the brightness tables of the displays whose adjustment
        changed (auto levels or Peek), plus those Windows refused that are
        due to be offered their look again (see _RETRY_S)."""
        targets = self._targets()
        now = time.monotonic()
        due = {m.id for m in targets if not self._gamma_state.get(m.id, (True,))[0]
               and now - self._retry_at.get(m.id, now) >= _RETRY_S}
        redo = changed + [m for m in targets if m.id in due and m not in changed]
        if not redo:
            return
        # Only the tint is needed (the color matrix is not redone).
        tint = curves.plan_tint(self.settings, self.custom_tints)
        search, offer_only = self._plan_retry(due)
        # Displays with no adjustment left get the normal full apply, so
        # they settle exactly where the preset alone would.
        self._apply_gamma(redo, tint, self.status, fading=True, search=search,
                          offer_only=offer_only)

    def _plan_retry(self, refused: set[str]) -> tuple[set[str], set[str]]:
        """Displays Windows refused, about to be offered their look again:
        (those to search for a weaker version Windows would take, those only
        offered their look).  See _SEARCH_EVERY_S."""
        now = time.monotonic()
        search, offer_only = set(), set()
        for did in refused:
            first, searched = self._refusing.get(did, (now, now))
            if now - first < _SEARCH_EVERY_S or now - searched >= _SEARCH_EVERY_S:
                search.add(did)
                searched = now
            else:
                offer_only.add(did)
            self._refusing[did] = (first, searched)
        return search, offer_only

    def retry_pending(self) -> bool:
        """Windows refused the last change of a display (a quick check
        without the lock; set_auto_offsets decides when to try again)."""
        return self.enabled and not self.status.gamma_ok

    def settings_for(self, display_id: str, peek: bool = True) -> Settings:
        """The settings a display really gets (the preset plus auto levels,
        plus a Peek in progress unless ``peek`` is False):
        replace(...).clamped() in one pass (this runs on every auto levels
        update)."""
        s = self.settings
        amounts = {k: v for k, v in self.auto_offsets.get(display_id, {}).items() if v}
        lift = self.peek_amount
        if peek and lift:   # Peek goes on top of everything
            for k, v in peek_lift(s, amounts, lift).items():
                amounts[k] = amounts.get(k, 0.0) + v
            if amounts.get("haze_cut"):
                # Auto levels' haze cut (a lower black point) gives way to
                # the Peek's lift as to its own (autolevels.HAZE_LIFT_OFF),
                # or it would swallow the dark corners a Peek is for.
                amounts["haze_cut"] *= max(0.0, 1.0 - lift / autolevels.HAZE_LIFT_OFF)
            if amounts.get("haze_band"):
                # (So does the haze band, though it never darkens.)
                amounts["haze_band"] *= max(0.0, 1.0 - lift / autolevels.HAZE_LIFT_OFF)
        if not amounts:
            return s
        return s.clamped({k: getattr(s, k) + v for k, v in amounts.items()})

    def _apply_gamma(self, monitors, tint, status: Status, fading: bool = False,
                     search: set[str] = frozenset(),
                     offer_only: set[str] = frozenset()) -> None:
        """``fading``: a frequent auto-levels update.  Displays that carry an
        adjustment reuse what Windows last accepted and remember what they
        set instead of reading every ramp back (the keep-applied check still
        compares); displays with none get the normal full apply.  ``search``:
        displays Windows refused, offered their look again (see _RETRY_S).
        ``offer_only``: displays Windows keeps refusing that get no search
        this time, only their look offered again (see _SEARCH_EVERY_S)."""
        if not fading:
            self._fallback.clear()   # a full apply starts from the full ramp again
        ramps: dict[tuple, list] = {}  # displays with the same adjustment share a ramp
        menu = self.menu_amount
        for m in monitors:
            lifted = bool(any(self.auto_offsets.get(m.id, {}).values()) or self.peek_amount
                          or menu)
            adjusted = fading and lifted
            which = (tuple(sorted(self.auto_offsets.get(m.id, {}).items())), self.peek_amount,
                     menu, m.id if menu else None)   # (toward each display's own table)
            ramp = ramps.get(which)
            if ramp is None:
                # (A Peek held as a menu opens fades out with the rest, so
                # nothing jumps; in the menu itself the display is normal.)
                ramp = curves.build_ramp(self.settings_for(m.id), tint)
                if menu:
                    ramp = menu_ramp(ramp, m.original or curves.identity_ramp(), menu)
                ramps[which] = ramp
            if adjusted:
                result: GammaResult = apply_ramp_with_fallback(
                    self.gamma, m.id, ramp, self._fallback, search=m.id in search)
                if m.id in search and result.ok and result.limited:
                    # Windows may have started taking changes again partway
                    # through that search, so what it found can be weaker than
                    # Windows allows now, and a fade at rest sends nothing
                    # more to climb back with: look once for the strongest.
                    full = apply_ramp_with_fallback(self.gamma, m.id, ramp, None)
                    if full.ok:
                        result = full
                        self._fallback[m.id] = shown_memory(full.applied or ramp)
            else:
                result = None
                if m.id in offer_only:
                    result = self._offer_again(m.id, ramp)
                elif fading:
                    result = self._apply_rest(m.id, ramp)
                if result is None:
                    result = apply_ramp_with_fallback(self.gamma, m.id, ramp, None)
                if result.ok and not lifted:
                    # Only the preset's own look (a full apply during a boost
                    # shows the boosted one).
                    self._rest[m.id] = (ramp, result.applied or ramp)
                # Remember what is on screen so the next fading update
                # starts from it (no dip when it is refused).
                self._fallback.pop(m.id, None)
                if result.ok:
                    self._fallback[m.id] = shown_memory(result.applied or ramp)
            if result.ok:
                self._refusing.pop(m.id, None)   # a later refusal is searched afresh
                if adjusted:
                    self._applied[m.id] = result.applied or ramp
                else:
                    self._applied[m.id] = self.gamma.get_ramp(m.id) or ramp
            elif (m.id in search or m.id in offer_only
                  or self._gamma_state.get(m.id, (True,))[0]):
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

    def _offer_again(self, display_id: str, ramp) -> GammaResult:
        """A display Windows keeps refusing, between searches (see
        _SEARCH_EVERY_S): offer its full look, then the weaker version
        Windows last took for that look (if known), and nothing weaker."""
        result = self._apply_rest(display_id, ramp)
        if result is not None:
            return result
        cached = self._rest.get(display_id)
        if (cached is None or cached[0] != ramp) and self.gamma.set_ramp(display_id, ramp):
            return GammaResult(True, applied=ramp)
        return GammaResult(False, failed_monitors=[display_id])

    def check_and_reapply(self) -> bool:
        """Re-apply if a game, driver or Windows reset the gamma ramp, or
        offer a change Windows refused again (nothing else would).

        Returns True if something had to be fixed.
        """
        if not self.gamma.available:
            return False
        with self._lock:
            if not self.enabled:
                if not self._applied:
                    return False
                self._restore_gamma()   # Windows refused putting a display back
                return True
            return self._check_and_reapply()

    def _check_and_reapply(self) -> bool:
        put_back = self._restore_unchosen()
        refused = [m for m in self._targets() if not self._gamma_state.get(m.id, (True,))[0]]
        retried = set()
        if refused and not self.auto_offsets:
            # Auto levels offers refused changes again while it runs; once it
            # has stopped (or was never on), this is the only retry.  The
            # screen may still show an old look, such as an ending boost.
            tint, _matrix = curves.plan_color(self.settings, self.custom_filters,
                                              self.custom_tints,
                                              all_displays=self.all_displays_selected())
            _search, offer_only = self._plan_retry({m.id for m in refused})
            self._apply_gamma(refused, tint, self.status, offer_only=offer_only)
            retried = {m.id for m in refused}
        for m in self._targets():
            if m.id in retried:
                continue   # (a display that always refuses mustn't stop the others' check)
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
        return bool(retried) or put_back

    def _restore_unchosen(self) -> bool:
        """Displays no longer chosen whose restore Windows refused (they are
        still in _applied, showing CatVision's look): offer them their own
        look again (one call each).  True if one was put back."""
        chosen = {m.id for m in self._targets()}
        back = [m for m in self._monitors if m.id in self._applied and m.id not in chosen
                and self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())]
        for m in back:
            del self._applied[m.id]
        return bool(back)

    def shutdown(self) -> None:
        with self._lock:
            self.enabled = False
            self._end_peek()
            self._end_menu()
            self._restore_gamma()
            self.color.close()
            self.gamma.close()

    def restore_now(self) -> None:
        """Put the displays back immediately (safe to call from any thread).

        Used by Force quit, which must work even if the window is frozen.
        Color filters end by themselves when the process exits.  CatVision
        counts as off from here on, so an auto levels fade that is still
        running (the fader thread isn't frozen with the window) can't put a
        boost back before the process ends: the "display changed" mark is
        already gone, so the next start would take that boost as the
        display's own look.
        """
        if self._lock.acquire(timeout=1.0):
            try:
                self.enabled = False
                self.auto_generation += 1
                self._end_peek()
                self._end_menu()
                self._restore_gamma()
            finally:
                self._lock.release()
        else:  # something is stuck holding the lock: restore without it
            self.enabled = False
            self.auto_generation += 1
            self._end_peek()
            self._end_menu()
            refused = [m for m in self._monitors
                       if not self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())]
            self._mark_dirty(bool(refused))

    # -- internals ----------------------------------------------------------

    def _restore_gamma(self) -> None:
        # A display Windows refused to put back (a fullscreen switch, say)
        # stays ours: it is tried again, and the "display changed" mark stays
        # so the next start doesn't take our look as the display's own.
        self._applied = {m.id: self._applied[m.id] for m in self._monitors
                         if m.id in self._applied
                         and not self.gamma.set_ramp(m.id, m.original or curves.identity_ramp())}
        self._gamma_state.clear()
        self._refusing.clear()
        self._rest.clear()
        self._mark_dirty(bool(self._applied))

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
