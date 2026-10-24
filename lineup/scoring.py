"""Simulate placements, fatigue, event standings, and team points."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from .domain import (
    FIELD_EVENTS,
    INDIVIDUAL_POINTS,
    INDOOR_SHORT_EVENTS,
    LineupResult,
    Performance,
    RELAY_EVENTS,
    RELAY_POINTS,
    RelayPerformance,
    RelaySelection,
    active_events,
    active_relay_events,
    clean_text,
    event_sort_value,
    format_time,
)

def evaluate_lineup(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
    opponent_relay_entries: list[Performance] | None = None,
) -> LineupResult:
    """Apply fatigue, simulate every event, and return projected team points."""
    from .optimizer import (
        best_performance_by_athlete_event,
        estimate_opponent_relay_entries,
        relay_selection_time_for_build,
        score_team_points_from_entries,
        sort_event_pool,
    )

    best_perf = best_performance_by_athlete_event(school)
    school_source = school[0].source if school else "Your Team"
    event_points: dict[str, float] = {}
    event_standings: dict[str, list[dict[str, Any]]] = {}
    output_lineup: dict[str, list[dict[str, Any]]] = {}
    athlete_history: dict[str, list[str]] = defaultdict(list)
    school_meet_entries: list[Performance] = []
    relay_opponents_by_event = {
        event: [
            perf
            for perf in (
                opponent_relay_entries
                if opponent_relay_entries is not None
                else estimate_opponent_relay_entries(event, opponents, opponent_relay_history, opponent_relay_splits)
            )
            if perf.event == event
        ]
        for event in sorted(active_relay_events(), key=event_sort_value)
    }

    relay_output: dict[str, dict[str, Any]] = {}
    for event in sorted(active_events(), key=event_sort_value):
        if event in lineup:
            entrants = []
            for athlete in lineup[event]:
                perf = best_perf.get((athlete, event))
                if not perf:
                    continue
                adjusted_value = apply_fatigue(perf.value, perf.is_time, len(athlete_history[athlete]))
                entrants.append(make_adjusted_perf(perf, adjusted_value))
            event_points[event], athlete_details = score_event_details(event, entrants, opponents)
            event_standings[event] = projected_event_standings(event, entrants, opponents)
            school_meet_entries.extend(entrants)
            output_lineup[event] = [
                entry_to_dict(perf, athlete_details.get(perf.athlete))
                for perf in sort_event_pool(entrants, event)
            ]
            for perf in entrants:
                athlete_history[perf.athlete].append(event)

        relay = relays.get(event)
        if relay:
            time_value = relay_selection_time_for_build(relay, athlete_history)
            if math.isfinite(time_value):
                relay_entry = Performance(
                    f"{school_source} Relay",
                    event,
                    format_time(time_value),
                    time_value,
                    True,
                    school_source,
                    "school",
                )
                points, _relay_details = score_event_details(event, [relay_entry], relay_opponents_by_event.get(event, []))
                event_standings[event] = projected_event_standings(event, [relay_entry], relay_opponents_by_event.get(event, []))
                school_meet_entries.append(relay_entry)
            else:
                points = 0.0
                event_standings[event] = []
            event_points[event] = points
            relay_output[event] = {
                "athletes": list(relay.athletes),
                "projected_mark": format_time(time_value) if math.isfinite(time_value) else "n/a",
                "projected_seconds": round(time_value, 4) if math.isfinite(time_value) else None,
                "raw_projected_seconds": round(relay.projected_time, 4) if math.isfinite(relay.projected_time) else None,
                "projected_points": points,
                "method": relay.method,
                "source_mark": relay.source_mark,
                "leg_times": list(relay.leg_times) if relay.leg_times else [],
                "leg_sources": list(relay.leg_sources) if relay.leg_sources else [],
            }
            for athlete in relay.athletes:
                athlete_history[athlete].append(event)

    total = round(sum(event_points.values()), 2)
    team_point_entries = school_meet_entries + opponents
    if opponent_relay_entries is not None:
        team_point_entries += opponent_relay_entries
    else:
        for event in sorted(active_relay_events(), key=event_sort_value):
            team_point_entries += relay_opponents_by_event.get(event, [])
    team_points = score_team_points_from_entries(team_point_entries)
    team_points[school_source] = total
    team_points = {
        source: round(points, 2)
        for source, points in sorted(team_points.items(), key=lambda item: (-item[1], item[0].lower()))
    }
    return LineupResult(
        lineup=output_lineup,
        relays=relay_output,
        event_points={event: round(points, 2) for event, points in sorted(event_points.items(), key=lambda item: event_sort_value(item[0]))},
        total_points=total,
        scraped={"school_records": len(school), "opponent_records": len(opponents)},
        errors=[],
        event_standings=event_standings,
        team_points=team_points,
    )

def apply_fatigue(value: float, is_time: bool, prior_events: int) -> float:
    """Add a small fatigue adjustment to times after multiple prior events."""
    return value * fatigue_factor(prior_events) if is_time else value


def fatigue_factor(prior_events: int) -> float:
    """Return the fatigue multiplier based on already-completed events."""
    if prior_events >= 3:
        return 1.01
    if prior_events == 2:
        return 1.005
    return 1.0


def make_adjusted_perf(perf: Performance, adjusted_value: float) -> Performance:
    """Create a temporary performance with an adjusted value."""
    return Performance(
        athlete=perf.athlete,
        event=perf.event,
        mark=perf.mark,
        value=adjusted_value,
        is_time=perf.is_time,
        source=perf.source,
        team_role=perf.team_role,
    )


def score_event(event: str, school_entries: list[Performance], opponents: list[Performance]) -> float:
    """Score a simulated event with school entries and all opponent entries."""
    total, _details = score_event_details(event, school_entries, opponents)
    return total


def score_event_details(
    event: str, school_entries: list[Performance], opponents: list[Performance]
) -> tuple[float, dict[str, dict[str, Any]]]:
    """Score an event and return each school athlete's projected place and points."""
    from .optimizer import select_opponent_entries, sort_event_pool

    points = RELAY_POINTS if event in RELAY_EVENTS else INDIVIDUAL_POINTS
    pool = school_entries + select_opponent_entries(opponents, event)
    ranked = sort_event_pool(pool, event)
    total = 0.0
    details: dict[str, dict[str, Any]] = {
        perf.athlete: {"place": None, "place_label": "unplaced", "points": 0.0}
        for perf in school_entries
    }
    for place, perf in enumerate(ranked, start=1):
        if perf.team_role == "school":
            earned = float(points[place - 1]) if place <= len(points) else 0.0
            total += earned
            details[perf.athlete] = {
                "place": place,
                "place_label": ordinal(place),
                "points": earned,
            }
    return float(total), details


def projected_event_standings(
    event: str, school_entries: list[Performance], opponents: list[Performance]
) -> list[dict[str, Any]]:
    """Return the top eight projected places for display without changing scoring."""
    from .optimizer import select_opponent_entries, sort_event_pool

    points = RELAY_POINTS if event in RELAY_EVENTS else INDIVIDUAL_POINTS
    ranked = sort_event_pool(school_entries + select_opponent_entries(opponents, event), event)
    standings: list[dict[str, Any]] = []
    for place, perf in enumerate(ranked[: len(points)], start=1):
        standings.append(
            {
                "place": place,
                "place_label": ordinal(place),
                "athlete": perf.athlete,
                "school": perf.source or ("Your Team" if perf.team_role == "school" else "Opponent"),
                "team_role": perf.team_role,
                "projected_mark": format_projected_mark(event, perf),
                "mark_origin": short_event_mark_origin(event, perf.mark),
                "projected_points": float(points[place - 1]),
            }
        )
    return standings


def format_projected_mark(event: str, perf: Performance) -> str:
    """Format the value used in event projections for the UI standings popover."""
    if not math.isfinite(perf.value):
        return format_display_mark(perf.mark)
    if perf.is_time:
        return format_time(perf.value)
    if event in FIELD_EVENTS:
        return format_inches(perf.value)
    return format_display_mark(perf.mark)


def entry_to_dict(perf: Performance, projection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a lineup entry."""
    display_mark = format_display_mark(perf.mark)
    data = {
        "athlete": perf.athlete,
        "mark": display_mark,
        "adjusted_mark": display_mark,
        "mark_origin": short_event_mark_origin(perf.event, perf.mark),
    }
    if projection is not None:
        data["projected_place"] = projection.get("place")
        data["projected_place_label"] = projection.get("place_label")
        data["projected_points"] = round(float(projection.get("points", 0.0)), 2)
    return data


def short_event_mark_origin(event: str, mark: str) -> str:
    """Label selected indoor short marks as recorded history or converted projections."""
    if event not in INDOOR_SHORT_EVENTS:
        return ""
    return "predicted" if re.search(r"(?<=\d)c\b", clean_text(mark), flags=re.I) else "historical"


def format_display_mark(mark: str) -> str:
    """Remove Athletic.net timing/conversion suffixes from a displayed mark."""
    return re.sub(r"(?<=\d)(?:fat|ht|a|h|c)\b", "", clean_text(mark), flags=re.I)


def ordinal(value: int) -> str:
    """Format a place number as an ordinal label."""
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"






def title_event(event: str) -> str:
    """Return a readable event label for diagnostics."""
    if event in {"55h", "60h"}:
        return f"{event[:-1]}m Hurdles"
    return event.replace("m", "m").replace(" relay", " Relay").title()


def format_inches(value: float) -> str:
    """Format inches as feet-inches."""
    feet = int(value // 12)
    inches = value - feet * 12
    return f"{feet}' {inches:.2f}\""


__all__ = [
    "apply_fatigue",
    "entry_to_dict",
    "evaluate_lineup",
    "fatigue_factor",
    "format_display_mark",
    "format_inches",
    "format_projected_mark",
    "make_adjusted_perf",
    "ordinal",
    "projected_event_standings",
    "score_event",
    "score_event_details",
    "short_event_mark_origin",
    "title_event",
]
