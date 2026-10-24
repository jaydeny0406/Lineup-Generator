"""Serialize editable lineups and rescore manual changes."""

from __future__ import annotations

import math
from typing import Any

from .domain import (
    ACTIVE_MEET_CONFIG,
    DISTANCE_EVENTS,
    FIELD_EVENTS,
    LineupResult,
    MAX_EVENTS_PER_ATHLETE,
    MAX_INDIVIDUAL_ENTRIES,
    Performance,
    RelayPerformance,
    RelaySelection,
    active_relay_events,
    clean_text,
    current_meet_config,
    event_sort_value,
    meet_config_for,
)
from .optimizer import (
    best_relay_leg_candidates,
)
from .records import (
    parse_mark,
)
from .scoring import (
    evaluate_lineup,
)

def attach_edit_context(
    result: LineupResult,
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_history: list[RelayPerformance] | None = None,
    school_relay_splits: list[Performance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
    team_event_limit: int = MAX_EVENTS_PER_ATHLETE,
) -> LineupResult:
    """Attach compact source data needed for coach edits and rescoring."""
    result.edit_context = build_edit_context(
        school,
        opponents,
        school_relay_history or [],
        opponent_relay_history or [],
        school_relay_splits or [],
        opponent_relay_splits or [],
        athlete_event_limits or {},
        team_event_limit,
    )
    return result


def build_edit_context(
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
    team_event_limit: int = MAX_EVENTS_PER_ATHLETE,
) -> dict[str, Any]:
    """Build the data package the UI uses for manual lineup edits."""
    relay_leg_values = {
        event: {
            athlete: round(value, 4)
            for athlete, value in best_relay_leg_candidates(event, school, school_relay_history, school_relay_splits)
        }
        for event in sorted(active_relay_events(), key=event_sort_value)
    }
    return {
        "meet_config": {
            "season_type": current_meet_config().season_type,
            "indoor_sprint_distance": current_meet_config().indoor_sprint_distance,
        },
        "events": list(current_meet_config().events),
        "schedule_order": list(current_meet_config().schedule_order),
        "distance_order": list(current_meet_config().distance_order),
        "running_order": current_meet_config().running_order,
        "relay_events": sorted(current_meet_config().relay_events),
        "field_events": sorted(FIELD_EVENTS),
        "distance_events": sorted(DISTANCE_EVENTS),
        "max_events_per_athlete": team_event_limit,
        "team_event_limit": team_event_limit,
        "athlete_event_limits": athlete_event_limits or {},
        "max_individual_entries": MAX_INDIVIDUAL_ENTRIES,
        "school_performances": [performance_to_dict(perf) for perf in school],
        "opponent_performances": [performance_to_dict(perf) for perf in opponents],
        "school_relay_history": [relay_performance_to_dict(relay) for relay in school_relay_history],
        "opponent_relay_history": [relay_performance_to_dict(relay) for relay in opponent_relay_history],
        "school_relay_splits": [performance_to_dict(perf) for perf in school_relay_splits],
        "opponent_relay_splits": [performance_to_dict(perf) for perf in opponent_relay_splits],
        "relay_leg_values": relay_leg_values,
    }


def performance_to_dict(perf: Performance) -> dict[str, Any]:
    """Serialize one performance for browser-side coach edits."""
    return {
        "athlete": perf.athlete,
        "event": perf.event,
        "mark": perf.mark,
        "value": perf.value,
        "is_time": perf.is_time,
        "source": perf.source,
        "team_role": perf.team_role,
    }


def performance_from_dict(data: dict[str, Any]) -> Performance:
    """Restore a serialized performance from an edit payload."""
    return Performance(
        clean_text(data.get("athlete", "")),
        clean_text(data.get("event", "")),
        clean_text(data.get("mark", "")),
        float(data.get("value", 0.0)),
        bool(data.get("is_time", True)),
        clean_text(data.get("source", "")),
        clean_text(data.get("team_role", "")),
    )


def relay_performance_to_dict(relay: RelayPerformance) -> dict[str, Any]:
    """Serialize one recorded relay for edit rescoring."""
    return {
        "event": relay.event,
        "athletes": list(relay.athletes),
        "mark": relay.mark,
        "value": relay.value,
        "source": relay.source,
        "team_role": relay.team_role,
        "splits": list(relay.splits),
        "method": relay.method,
    }


def relay_performance_from_dict(data: dict[str, Any]) -> RelayPerformance:
    """Restore a serialized recorded relay from an edit payload."""
    athletes = tuple(clean_text(name) for name in (data.get("athletes") or [])[:4])
    if len(athletes) != 4:
        athletes = ("", "", "", "")
    splits = tuple(
        None if value is None else float(value)
        for value in (list(data.get("splits") or []) + [None, None, None, None])[:4]
    )
    return RelayPerformance(
        clean_text(data.get("event", "")),
        athletes,  # type: ignore[arg-type]
        clean_text(data.get("mark", "")),
        float(data.get("value", 0.0)),
        clean_text(data.get("source", "")),
        clean_text(data.get("team_role", "")),
        splits,  # type: ignore[arg-type]
        clean_text(data.get("method", "historic")) or "historic",
    )


def rescore_edited_result(payload: dict[str, Any]) -> LineupResult:
    """Rescore a browser-edited lineup under the meet profile that created it."""
    context = payload.get("edit_context") or {}
    meet_data = context.get("meet_config") or {}
    meet_config = meet_config_for(
        meet_data.get("season_type", "outdoor"),
        meet_data.get("indoor_sprint_distance", "55"),
    )
    token = ACTIVE_MEET_CONFIG.set(meet_config)
    try:
        return _rescore_edited_result_active(payload)
    finally:
        ACTIVE_MEET_CONFIG.reset(token)


def _rescore_edited_result_active(payload: dict[str, Any]) -> LineupResult:
    """Rescore a browser-edited lineup with its meet profile already active."""
    from .service import (
        event_limit_violations,
        normalize_athlete_event_limits,
        normalize_team_event_limit,
    )

    context = payload.get("edit_context") or {}
    school = [performance_from_dict(item) for item in context.get("school_performances", [])]
    opponents = [performance_from_dict(item) for item in context.get("opponent_performances", [])]
    opponent_relay_history = [
        relay_performance_from_dict(item)
        for item in context.get("opponent_relay_history", [])
    ]
    opponent_relay_splits = [
        performance_from_dict(item)
        for item in context.get("opponent_relay_splits", [])
    ]
    school_relay_history = [
        relay_performance_from_dict(item)
        for item in context.get("school_relay_history", [])
    ]
    school_relay_splits = [
        performance_from_dict(item)
        for item in context.get("school_relay_splits", [])
    ]
    athlete_event_limits = normalize_athlete_event_limits(context.get("athlete_event_limits", {}))
    team_event_limit = normalize_team_event_limit(
        context.get("team_event_limit", context.get("max_events_per_athlete", MAX_EVENTS_PER_ATHLETE))
    )
    lineup = edited_lineup_from_payload(payload.get("lineup") or {})
    relays = edited_relays_from_payload(payload.get("relays") or {})
    violations = event_limit_violations(lineup, relays, athlete_event_limits)
    if violations:
        raise ValueError("Athlete event limit exceeded: " + "; ".join(violations))
    result = evaluate_lineup(
        lineup,
        relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    return attach_edit_context(
        result,
        school,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
        athlete_event_limits,
        team_event_limit,
    )


def edited_lineup_from_payload(data: dict[str, Any]) -> dict[str, list[str]]:
    """Extract individual event athlete names from an edited payload."""
    lineup: dict[str, list[str]] = {}
    for event, entries in data.items():
        if event in active_relay_events():
            continue
        athletes: list[str] = []
        for entry in entries or []:
            athlete = clean_text(entry.get("athlete", "") if isinstance(entry, dict) else str(entry))
            if athlete:
                athletes.append(athlete)
        lineup[event] = athletes[:MAX_INDIVIDUAL_ENTRIES]
    return lineup


def edited_relays_from_payload(data: dict[str, Any]) -> dict[str, RelaySelection]:
    """Extract fixed projected relay selections from an edited payload."""
    relays: dict[str, RelaySelection] = {}
    for event, relay in data.items():
        if event not in active_relay_events() or not isinstance(relay, dict):
            continue
        athletes = tuple(clean_text(name) for name in (relay.get("athletes") or [])[:4])
        if len(athletes) != 4 or not all(athletes):
            continue
        seconds = relay.get("projected_seconds")
        if seconds is None:
            parsed = parse_mark(clean_text(relay.get("projected_mark", "")), event)
            seconds = parsed[0] if parsed else math.inf
        relays[event] = RelaySelection(
            event,
            athletes,  # type: ignore[arg-type]
            float(seconds),
            "coach edited",
            clean_text(relay.get("source_mark", "coach edited")) or "coach edited",
        )
    return relays


__all__ = [
    "attach_edit_context",
    "build_edit_context",
    "edited_lineup_from_payload",
    "edited_relays_from_payload",
    "performance_from_dict",
    "performance_to_dict",
    "relay_performance_from_dict",
    "relay_performance_to_dict",
    "rescore_edited_result",
]
