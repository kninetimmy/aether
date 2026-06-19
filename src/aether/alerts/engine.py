"""Stateful alert-rule evaluation engine (M4.6b, PRD §20.3, §20.5).

The engine sits on top of the stateless condition core (:mod:`aether.alerts.
conditions`). The core answers "does this *one record snapshot* satisfy the rule's
AND-of-leaves right now?"; the engine answers the questions a single snapshot
cannot: *did this just change* (transition edges), *have we already fired*
(cooldown / dedup / still-open), *is the rule active* (schedule / quiet hours),
and *can we close it* (auto-resolution). Its output is a list of
:class:`~aether.schema.records.AlertRecord`s — new ``open`` alerts and
``resolved`` closures — that the caller publishes into live state (PRD §22.4).

**Wiring (PRD §13.3, §5).** The engine is a pure observer of state changes: the
backend registers :meth:`evaluate` as a :class:`~aether.backend.hub.Hub` observer,
and whatever alerts it returns are published back through the hub. It holds an
*in-memory* copy of the operator's rules, kept current by the CRUD path
(:meth:`set_rules`/:meth:`upsert_rule`/:meth:`remove_rule`) rather than re-reading
SQLite on the hot path — so evaluation never does blocking I/O and never gates
serving live state. With persistence off there are simply no rules and the engine
is an inert no-op.

**Scope.** Stateless rules are evaluated by the level core
(:func:`~aether.alerts.conditions.evaluate_conditions`); contextual rules (geofence
containment, distance/elevation, time windows, ``changed_to/from``) are collapsed to
the SAME boolean ``(level, discrete)`` by the :class:`~aether.alerts.contextual.
ContextualEvaluator` (M4.6c) and fed through the *identical* edge/cooldown/dedup/
auto-resolve path — one firing model, no duplication. The evaluator holds the prior
per-subject state and an in-memory mirror of the geofence set (synced here exactly
like the ruleset), so evaluation stays off the hot-path disk and never blocks.
Notification *delivery* (email/Discord/browser drivers) is a later slice (M4.7): an
emitted alert records its selected channels as ``pending`` in ``delivery_status`` and
appears in the dashboard alert centre, which *is* the dashboard channel.

**Transition semantics** over the boolean level a rule's conditions define for a
subject:

- ``enter`` (and ``None``, the default): fire when the level goes False→True;
  auto-resolve the open alert when it goes True→False.
- ``exit``: fire when the level goes True→False; auto-resolve when it returns to
  True.
- ``change``: a *discrete* trigger on either flip — like an event, no open-alert
  dedup and no auto-resolution (each change is its own point-in-time alert).
- Event records are inherently discrete: a matching new event fires (discrete),
  regardless of ``transition``.

A subject's firing identity is ``(rule.id, dedup_key or subject)`` — so a static
``dedup_key`` collapses many subjects into one alert (e.g. one "source offline"
alert), while the default keys per subject. (Field-path/templated dedup keys are a
later refinement; here ``dedup_key`` is a literal grouping label.)
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aether.alerts.conditions import evaluate_conditions, is_stateless
from aether.alerts.contextual import ContextualEvaluator, StationRef
from aether.schema.alert_rule import AlertRule, TimeWindow
from aether.schema.geofence import Geofence
from aether.schema.records import (
    AlertRecord,
    EventRecord,
    Record,
    SourceStatusRecord,
    TrackRecord,
)
from aether.schema.validation import dump_record
from aether.state.live import StateChange, StateKind

log = logging.getLogger(__name__)

#: Synthetic ``source`` tag for engine-emitted alerts — they are derived by the
#: backend, not received from a feed, so they get their own provenance source name
#: rather than borrowing the triggering record's.
ALERT_SOURCE = "alert-engine"

#: State kinds that can drive a rule. Alerts and features never do (a rule reacts to
#: the world, not to its own output), so feeding an alert change back in terminates
#: the observer→publish→observer loop immediately (PRD §37 — no runaway).
_DRIVING_KINDS: frozenset[StateKind] = frozenset({"track", "source_status", "event"})


@dataclass
class _Firing:
    """Per-(rule, subject) engine memory: prior level, last fire time, open alert.

    ``level`` is the last evaluated truth of the rule's conditions for this subject
    (drives edge detection). ``last_fired`` backs the cooldown. ``open_alert`` is the
    currently-open alert this firing produced, if any — held so an exit/remove edge
    can auto-resolve it and so a still-open alert dedups repeat fires.
    """

    level: bool = False
    last_fired: datetime | None = None
    open_alert: AlertRecord | None = None


def subject_type_of(record: Record) -> str:
    """The rule ``subject_types`` token a record matches against (PRD §20.1).

    Tracks match on their ``track_type`` (``aircraft``/``vessel``/…), source-status
    records on the literal ``"source"``, and events on their free-form
    ``event_type``; anything else falls back to its ``kind`` so a future record type
    is still addressable rather than silently unmatchable.
    """
    if isinstance(record, TrackRecord):
        return record.track_type
    if isinstance(record, SourceStatusRecord):
        return "source"
    if isinstance(record, EventRecord):
        return record.event_type
    return record.kind


def subject_of(record: Record) -> str:
    """The subject identity an alert is attributed to (its ``subject_id``).

    A track is its own (fused) id; a source-status record is its ``source``; an event
    prefers its ``subject_id`` (the thing the event is *about*) and falls back to its
    own id when the event has no subject.
    """
    if isinstance(record, SourceStatusRecord):
        return record.source
    if isinstance(record, EventRecord):
        return record.subject_id or record.id
    return record.id


def _minute_of_day(now: datetime) -> int:
    """Minutes since 00:00 UTC for ``now`` — the basis schedule/quiet windows use."""
    return now.hour * 60 + now.minute


def _in_window(minute: int, window: TimeWindow) -> bool:
    """Whether ``minute`` (of day) falls in a possibly-midnight-wrapping window.

    End is exclusive. A non-wrapping window (``start <= end``) is the half-open
    ``[start, end)``; a wrapping one (``start > end``, e.g. 22:00–06:00) is the union
    ``[start, 24:00) ∪ [00:00, end)``. ``start == end`` is an empty window.
    """
    start = _hhmm_to_minute(window.start)
    end = _hhmm_to_minute(window.end)
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def _hhmm_to_minute(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def _schedule_active(rule: AlertRule, now: datetime) -> bool:
    """Whether ``rule``'s schedule (day-of-week + optional window) admits ``now``.

    No schedule ⇒ always active. ``days_of_week`` uses Python's 0=Monday convention
    (matching the model). An optional ``window`` further restricts each active day.
    """
    schedule = rule.schedule
    if schedule is None:
        return True
    if now.weekday() not in schedule.days_of_week:
        return False
    if schedule.window is None:
        return True
    return _in_window(_minute_of_day(now), schedule.window)


def _suppressed_by_quiet_hours(rule: AlertRule, now: datetime) -> bool:
    """Whether ``now`` falls inside the rule's quiet-hours window (firing suppressed)."""
    return rule.quiet_hours is not None and _in_window(_minute_of_day(now), rule.quiet_hours)


def preview_rule(rule: AlertRule, records: Iterable[Record]) -> dict[str, Any]:
    """Dry-run a rule against a set of records without firing (PRD §21.4 test).

    Backs ``POST /api/v2/alert-rules/{id}/test`` (rule preview): for every record
    whose subject type the rule targets, report whether its conditions match *right
    now*. Stateless and side-effect free — it touches no firing state, emits no
    alerts, and honours no transition/cooldown/schedule (it answers "what currently
    matches", not "what would fire next"). A rule with a contextual operator can't be
    previewed by the level core yet, so ``evaluable`` is False and each ``matched`` is
    ``None`` (honest "unknown", never a misleading False — PRD §37).
    """
    evaluable = is_stateless(rule.conditions)
    matches: list[dict[str, Any]] = []
    for record in records:
        stype = subject_type_of(record)
        if stype not in rule.subject_types:
            continue
        matched: bool | None = (
            evaluate_conditions(rule.conditions, dump_record(record)) if evaluable else None
        )
        matches.append(
            {"subject_id": subject_of(record), "subject_type": stype, "matched": matched}
        )
    return {
        "rule_id": rule.id,
        "evaluable": evaluable,
        "subject_types": list(rule.subject_types),
        "evaluated": len(matches),
        "matched": sum(1 for m in matches if m["matched"] is True),
        "matches": matches,
    }


class AlertEngine:
    """Evaluates the operator's rules against live-state changes, emitting alerts.

    The engine owns an in-memory ruleset (synced by the CRUD path) and per-(rule,
    subject) firing memory. It is single-threaded with the asyncio loop that owns
    live state, so it needs no locks. Inject ``clock`` (and, in tests, ``id_factory``)
    so evaluation is deterministic and clock-driven gating is testable.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
        station_lat: float = 0.0,
        station_lon: float = 0.0,
    ) -> None:
        self._rules: dict[str, AlertRule] = {}
        self._firings: dict[tuple[str, str], _Firing] = {}
        self._clock = clock
        self._id_factory = id_factory or (lambda: f"alert-{uuid.uuid4().hex[:12]}")
        # The contextual evaluator computes the firing *level* for any rule the
        # stateless core can't (geometry/state/time). It is given the canonical
        # station (0,0 ⇒ unconfigured, so station-relative leaves degrade visibly,
        # never measure from null island — PRD §5).
        configured = not (station_lat == 0.0 and station_lon == 0.0)
        self._contextual = ContextualEvaluator(
            station=StationRef(lon=station_lon, lat=station_lat, configured=configured)
        )

    # -- ruleset sync (kept current by the CRUD path, never re-read on the hot path) --

    def set_rules(self, rules: Iterable[AlertRule]) -> None:
        """Replace the whole ruleset (startup load from the store)."""
        self._rules = {rule.id: rule for rule in rules}
        self._contextual.set_rules(frozenset(self._rules))

    def upsert_rule(self, rule: AlertRule) -> None:
        """Add or replace one rule (after a successful create/patch).

        Re-baselines the rule's contextual per-subject state, so an edit treats the
        next observation of each subject as a fresh first sighting — symmetric with
        :meth:`remove_rule`/:meth:`set_rules`, and the only way a changed/condition
        edit can't be confused by a stale prior level (the firing memory, keyed by id,
        is re-derived from the next edge regardless).
        """
        self._rules[rule.id] = rule
        self._contextual.forget_rule(rule.id)

    def remove_rule(self, rule_id: str) -> None:
        """Drop a rule and forget every firing it owned (after a successful delete)."""
        self._rules.pop(rule_id, None)
        self._firings = {key: f for key, f in self._firings.items() if key[0] != rule_id}
        self._contextual.forget_rule(rule_id)

    # -- geofence sync (mirrors the ruleset sync; feeds contextual containment math) --

    def set_geofences(self, geofences: Iterable[Geofence]) -> None:
        """Replace the whole geofence set the contextual evaluator references."""
        self._contextual.set_geofences(geofences)

    def upsert_geofence(self, geofence: Geofence) -> None:
        """Add or replace one geofence (after a successful create/patch)."""
        self._contextual.upsert_geofence(geofence)

    def remove_geofence(self, geofence_id: str) -> None:
        """Drop one geofence (after a successful delete)."""
        self._contextual.remove_geofence(geofence_id)

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    # -- evaluation --

    def evaluate(self, change: StateChange) -> list[AlertRecord]:
        """React to one live-state change; return the alerts to publish (may be empty).

        Drives only track/source-status/event changes (an alert/feature never drives a
        rule). A ``remove`` auto-resolves any open alert for the gone subject; an
        ``upsert``/``event`` runs every matching, enabled, *stateless* rule. Wrapped by
        the caller's observer error-isolation, but kept total here regardless.
        """
        if change.kind not in _DRIVING_KINDS:
            return []
        now = self._clock()
        if change.op == "remove":
            return self._on_remove(change.kind, change.id, now)
        record = change.record
        if record is None:  # pragma: no cover - upsert/event always carry a record
            return []
        subject_type = subject_type_of(record)
        subject = subject_of(record)
        dump = dump_record(record)
        out: list[AlertRecord] = []
        for rule in self._rules.values():
            if not rule.enabled or subject_type not in rule.subject_types:
                continue
            if is_stateless(rule.conditions):
                out.extend(
                    self._apply_rule(
                        rule, record, subject, dump, now, discrete=change.op == "event"
                    )
                )
                continue
            # Contextual rule: the evaluator collapses it to the same (level, discrete)
            # the stateless path produces, then the identical _drive machinery fires it.
            res = self._contextual.evaluate(rule, subject, record, dump, now)
            if not res.evaluable:
                continue  # honest 'unknown': no fire, no false resolve (PRD §37)
            out.extend(
                self._drive(
                    rule,
                    subject,
                    record,
                    now,
                    level=res.level,
                    discrete=res.discrete or change.op == "event",
                )
            )
        return out

    def _apply_rule(
        self,
        rule: AlertRule,
        record: Record,
        subject: str,
        dump: dict[str, Any],
        now: datetime,
        *,
        discrete: bool,
    ) -> list[AlertRecord]:
        """Compute a stateless rule's level and drive it through the firing machinery.

        A thin wrapper over :meth:`_drive`: the only stateless-specific work is
        evaluating the level core. Contextual rules reach :meth:`_drive` directly with
        an evaluator-computed level, so both share one firing model.
        """
        return self._drive(
            rule,
            subject,
            record,
            now,
            level=evaluate_conditions(rule.conditions, dump),
            discrete=discrete,
        )

    def _drive(
        self,
        rule: AlertRule,
        subject: str,
        record: Record,
        now: datetime,
        *,
        level: bool,
        discrete: bool,
    ) -> list[AlertRecord]:
        """Turn a precomputed ``(level, discrete)`` for one (rule, subject) into alerts.

        ``discrete`` (an event record, or a ``change``-transition rule) means a
        point-in-time trigger: cooldown-gated, no open-alert dedup, no auto-resolve.
        Otherwise the rule has a continuous level and we detect enter/exit edges,
        dedup against a still-open alert, and auto-resolve on the closing edge. This is
        the single firing path the stateless and contextual evaluation both feed.
        """
        key = (rule.id, rule.dedup_key if rule.dedup_key is not None else subject)
        firing = self._firings.setdefault(key, _Firing())
        prev = firing.level
        current = level
        transition = rule.transition or "enter"

        if discrete or transition == "change":
            firing.level = current
            fired = (current if discrete else prev != current) and self._can_fire_discrete(
                firing, rule, now
            )
            if fired:
                alert = self._open_alert(rule, subject, record, now)
                firing.last_fired = now
                return [alert]
            return []

        firing.level = current
        if transition == "exit":
            fire_edge, resolve_edge = (prev and not current), (not prev and current)
        else:  # enter / None
            fire_edge, resolve_edge = (not prev and current), (prev and not current)

        if fire_edge and self._can_fire_level(firing, rule, now):
            alert = self._open_alert(rule, subject, record, now)
            firing.last_fired = now
            firing.open_alert = alert
            return [alert]
        if resolve_edge and firing.open_alert is not None:
            resolved = self._resolve(firing.open_alert, now)
            firing.open_alert = None
            return [resolved]
        return []

    def _on_remove(self, kind: StateKind, removed_id: str, now: datetime) -> list[AlertRecord]:
        """Auto-resolve open alerts for a removed track and forget its firing memory.

        Only tracks are continuous subjects that get removed (expiry/handoff, PRD
        §15.4). For each rule, the removed subject's level drops to False: an open
        enter-rule alert auto-resolves, and a per-subject firing entry is forgotten so
        the firing map stays bounded (PRD §37). A shared ``dedup_key`` entry is kept
        (other subjects may still hold the group) but its level/open is cleared.
        """
        if kind != "track":
            return []
        out: list[AlertRecord] = []
        for rule in self._rules.values():
            key = (rule.id, rule.dedup_key if rule.dedup_key is not None else removed_id)
            firing = self._firings.get(key)
            if firing is None:
                continue
            if firing.open_alert is not None and (rule.transition or "enter") == "enter":
                out.append(self._resolve(firing.open_alert, now))
                firing.open_alert = None
            if rule.dedup_key is None:
                self._firings.pop(key, None)
            else:
                firing.level = False
        # Forget the gone subject's contextual state once — it is keyed by the raw
        # subject, so this clears every rule's entry for it (bounded maps, PRD §37).
        self._contextual.forget_subject(removed_id)
        return out

    def _can_fire_level(self, firing: _Firing, rule: AlertRule, now: datetime) -> bool:
        """A level rule may fire: no alert already open, cooldown elapsed, rule active."""
        return firing.open_alert is None and self._can_fire_discrete(firing, rule, now)

    def _can_fire_discrete(self, firing: _Firing, rule: AlertRule, now: datetime) -> bool:
        """A discrete trigger may fire: cooldown elapsed and the rule is active now."""
        if firing.last_fired is not None:
            if (now - firing.last_fired).total_seconds() < rule.cooldown_s:
                return False
        return _schedule_active(rule, now) and not _suppressed_by_quiet_hours(rule, now)

    def _open_alert(
        self, rule: AlertRule, subject: str, record: Record, now: datetime
    ) -> AlertRecord:
        """Build a fresh ``open`` alert for a rule firing on ``subject``.

        Selected channels start ``pending`` in ``delivery_status`` (the delivery
        drivers land in M4.7; the dashboard alert centre is the dashboard channel and
        needs no driver). The three timestamps collapse to ``now`` — an alert is
        *derived* at evaluation time, not received from a feed.
        """
        return AlertRecord(
            id=self._id_factory(),
            source=ALERT_SOURCE,
            observed_at=now,
            received_at=now,
            published_at=now,
            rule_id=rule.id,
            subject_id=subject,
            state="open",
            severity=rule.severity,
            title=rule.name,
            summary=_summary(rule, subject, record),
            triggered_at=now,
            delivery_status={channel: "pending" for channel in rule.channels},
        )

    def _resolve(self, alert: AlertRecord, now: datetime) -> AlertRecord:
        """Return a ``resolved`` copy of an open alert (auto-resolution, PRD §20.5)."""
        return alert.model_copy(
            update={"state": "resolved", "resolved_at": now, "published_at": now}
        )


def _summary(rule: AlertRule, subject: str, record: Record) -> str:
    """A one-line human summary for an alert: the rule plus the subject's label/id."""
    label = getattr(record, "label", None)
    return f"{rule.name} — {label or subject}"
