"""Select individual entries, relays, and post-build lineup improvements."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations
from typing import Any

from .domain import (
    DISTANCE_EVENTS,
    DISTANCE_THREE_EVENT_ALLOWED_EVENTS,
    DISTANCE_UTILIZATION_COUNT,
    DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS,
    DISTANCE_UTILIZATION_RELAY_EVENTS,
    ELITE_ATHLETE_COUNT,
    ELITE_UTILIZATION_POINT_TOLERANCE,
    EliteReplacement,
    HISTORIC_RELAY_IMPROVEMENT,
    INDIVIDUAL_LEG_SOURCE,
    INDIVIDUAL_POINTS,
    LONG_DISTANCE_EVENTS,
    LineupResult,
    MAX_DISTANCE_REPLACEMENTS,
    MAX_ELITE_REPLACEMENTS,
    MAX_EVENTS_PER_ATHLETE,
    MAX_INDIVIDUAL_ENTRIES,
    PROJECTED_OPPONENT_ROLE,
    Performance,
    RELAY_BASE_EVENT,
    RELAY_EVENTS,
    RELAY_INDIVIDUAL_EXCHANGE_CREDIT,
    RELAY_POINTS,
    RELAY_SPLIT_LEG_SOURCE,
    RUNNER_UTILIZATION_COUNT,
    RelayPerformance,
    RelaySelection,
    ScrapeResult,
    TRACK_EVENTS,
    TeamMeetProjection,
    active_elite_individual_events,
    active_events,
    active_relay_events,
    active_runner_individual_events,
    active_sprint_relay_events,
    athlete_max_events,
    current_meet_config,
    event_sort_value,
    format_time,
    is_better,
)
from .scoring import (
    apply_fatigue,
    evaluate_lineup,
    fatigue_factor,
)

def compute_scores(school: list[Performance], opponents: list[Performance]) -> dict[tuple[str, str], float]:
    """Estimate each school athlete's points by simulating their PR against all opponents."""
    potentials: dict[tuple[str, str], float] = {}
    for event in active_events():
        pool = [perf for perf in school if perf.event == event]
        pool.extend(select_opponent_entries(opponents, event))
        if not pool:
            continue
        ranked = sort_event_pool(pool, event)
        points = RELAY_POINTS if event in RELAY_EVENTS else INDIVIDUAL_POINTS
        for place, perf in enumerate(ranked[: len(points)], start=1):
            if perf.team_role == "school":
                potentials[(perf.athlete, event)] = float(points[place - 1])
        for perf in ranked[len(points) :]:
            if perf.team_role == "school":
                potentials.setdefault((perf.athlete, event), 0.0)
    return potentials


def sort_event_pool(pool: list[Performance], event: str) -> list[Performance]:
    """Sort a mixed school/opponent event pool by the event's scoring direction."""
    is_time = event in TRACK_EVENTS
    return sorted(pool, key=lambda perf: perf.value, reverse=not is_time)


def select_opponent_entries(
    opponents: list[Performance], event: str, max_entries_per_team: int = MAX_INDIVIDUAL_ENTRIES
) -> list[Performance]:
    """Keep only each opponent team's best allowed entries for one individual event."""
    by_team: dict[str, list[Performance]] = defaultdict(list)
    for perf in opponents:
        if perf.event == event:
            by_team[perf.source].append(perf)
    selected: list[Performance] = []
    for team_entries in by_team.values():
        selected.extend(sort_event_pool(team_entries, event)[:max_entries_per_team])
    return selected


def build_lineup(
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_history: list[RelayPerformance] | None = None,
    school_relay_splits: list[Performance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build and locally improve a lineup while respecting event limits and race spacing."""
    school_relay_history = school_relay_history or []
    opponent_relay_history = opponent_relay_history or []
    school_relay_splits = school_relay_splits or []
    opponent_relay_splits = opponent_relay_splits or []
    athlete_event_limits = athlete_event_limits or {}
    potentials = compute_scores(school, opponents)
    by_athlete = group_school_events(school, potentials)
    athlete_order = rank_athletes_by_value(by_athlete)
    lineup: dict[str, list[str]] = {
        event: [] for event in active_events() if event not in active_relay_events()
    }
    relays: dict[str, RelaySelection] = {}
    athlete_events: dict[str, list[str]] = defaultdict(list)

    stacked_relay = choose_stacked_relay_anchor(
        school,
        school_relay_history,
        school_relay_splits,
        athlete_event_limits,
    )
    if stacked_relay:
        relays[stacked_relay.event] = stacked_relay
        for athlete in stacked_relay.athletes:
            athlete_events[athlete].append(stacked_relay.event)

    for athlete in athlete_order:
        for event, _mark, score in by_athlete[athlete]:
            if len(athlete_events[athlete]) >= athlete_max_events(
                athlete, athlete_event_limits
            ):
                break
            if event in RELAY_EVENTS or score <= 0:
                continue
            try_add_entry(lineup, athlete_events, athlete, event, athlete_event_limits=athlete_event_limits)

    for relay_event in sorted(active_relay_events(), key=event_sort_value):
        if relay_event in relays:
            continue
        team = choose_relay_team(
            relay_event,
            school,
            opponents,
            athlete_events,
            school_relay_history,
            opponent_relay_history,
            school_relay_splits,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if team:
            relays[relay_event] = team
            for athlete in team.athletes:
                athlete_events[athlete].append(relay_event)

    fill_remaining_spots(lineup, school, athlete_events, athlete_event_limits)
    priority_runners = set(
        rank_priority_running_athletes(
            school,
            potentials,
            relays,
            opponents,
            opponent_relay_history,
            opponent_relay_splits,
        )
    )
    lineup, relays = optimize_lineup(
        lineup,
        relays,
        school,
        opponents,
        athlete_events,
        opponent_relay_history,
        opponent_relay_splits,
        priority_runners,
        athlete_event_limits,
    )
    lineup, relays = ensure_complete_lineup(
        lineup,
        relays,
        school,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
        athlete_event_limits,
    )
    lineup, relays = optimize_elite_sprint_utilization(
        lineup,
        relays,
        school,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
        athlete_event_limits,
    )
    lineup, relays = optimize_distance_runner_utilization(
        lineup,
        relays,
        school,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
        athlete_event_limits,
    )
    missing_events = [
        event
        for event in active_events()
        if (event in active_relay_events() and event not in relays)
        or (event not in active_relay_events() and not lineup.get(event))
    ]
    return {"lineup": lineup, "relays": relays, "missing_events": missing_events}


def ensure_complete_lineup(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Fill every supported event, using depth athletes for non-scoring relays."""
    athlete_events = collect_athlete_events(lineup, relays)
    potentials = compute_scores(school, opponents)
    athlete_event_limits = athlete_event_limits or {}
    fill_remaining_spots(lineup, school, athlete_events, athlete_event_limits)
    force_empty_individual_events(lineup, school, athlete_events, potentials, athlete_event_limits)

    for relay_event in sorted(active_relay_events(), key=event_sort_value):
        if relay_event in relays:
            continue
        selection = choose_relay_team(
            relay_event,
            school,
            opponents,
            athlete_events,
            school_relay_history,
            opponent_relay_history,
            school_relay_splits,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if not selection:
            selection = force_depth_relay(
                relay_event,
                lineup,
                school,
                athlete_events,
                school_relay_history,
                school_relay_splits,
                potentials,
                athlete_event_limits,
            )
        if selection:
            relays[relay_event] = selection
            for athlete in selection.athletes:
                athlete_events[athlete].append(relay_event)

    fill_remaining_spots(lineup, school, athlete_events, athlete_event_limits)
    force_empty_individual_events(lineup, school, athlete_events, potentials, athlete_event_limits)
    return lineup, relays


def collect_athlete_events(
    lineup: dict[str, list[str]], relays: dict[str, RelaySelection]
) -> dict[str, list[str]]:
    """Rebuild athlete event assignments from the finalized lineup structures."""
    athlete_events: dict[str, list[str]] = defaultdict(list)
    for event, athletes in lineup.items():
        for athlete in athletes:
            athlete_events[athlete].append(event)
    for event, relay in relays.items():
        for athlete in relay.athletes:
            athlete_events[athlete].append(event)
    return athlete_events


def minimum_event_removals(
    existing_events: list[str], new_event: str, max_events: int = MAX_EVENTS_PER_ATHLETE
) -> list[str] | None:
    """Find the fewest removable individual events needed to make a new event valid."""
    removable = [event for event in existing_events if event not in RELAY_EVENTS]
    for count in range(len(removable) + 1):
        for removed in combinations(removable, count):
            remaining = list(existing_events)
            for event in removed:
                remaining.remove(event)
            if can_take_event(remaining, new_event, max_events):
                return list(removed)
    return None


def remove_athlete_events(
    lineup: dict[str, list[str]],
    athlete_events: dict[str, list[str]],
    athlete: str,
    events: list[str],
) -> None:
    """Remove selected individual assignments before placing an athlete elsewhere."""
    for event in events:
        if athlete in lineup.get(event, []):
            lineup[event].remove(athlete)
        if event in athlete_events[athlete]:
            athlete_events[athlete].remove(event)


def force_depth_relay(
    relay_event: str,
    lineup: dict[str, list[str]],
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance],
    potentials: dict[tuple[str, str], float],
    athlete_event_limits: dict[str, int] | None = None,
) -> RelaySelection | None:
    """Free low-cost individual assignments to guarantee a four-person relay."""
    candidates = best_relay_leg_candidates_with_sources(relay_event, school, relay_history, relay_splits)
    options: list[tuple[float, int, float, str, str, list[str]]] = []
    for athlete, leg_time, leg_source in candidates:
        removals = minimum_event_removals(
            athlete_events[athlete],
            relay_event,
            athlete_max_events(athlete, athlete_event_limits),
        )
        if removals is None:
            continue
        cost = sum(potentials.get((athlete, event), 0.0) for event in removals)
        options.append((cost, len(removals), leg_time, athlete, leg_source, removals))
    if len(options) < 4:
        return None
    chosen = sorted(options, key=lambda item: (item[0], item[1], item[2]))[:4]
    team: list[tuple[str, float, str]] = []
    for _cost, _count, leg_time, athlete, leg_source, removals in chosen:
        remove_athlete_events(lineup, athlete_events, athlete, removals)
        team.append((athlete, leg_time, leg_source))
    ordered_team = order_synthetic_relay_legs(team)
    leg_times = tuple(item[1] for item in ordered_team)
    leg_sources = tuple(item[2] for item in ordered_team)
    return RelaySelection(
        event=relay_event,
        athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
        projected_time=synthetic_relay_time(relay_event, leg_times, leg_sources),
        method="synthetic",
        source_mark="completion depth relay",
        leg_times=leg_times,  # type: ignore[arg-type]
        leg_sources=leg_sources,  # type: ignore[arg-type]
    )


def force_empty_individual_events(
    lineup: dict[str, list[str]],
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    potentials: dict[tuple[str, str], float],
    athlete_event_limits: dict[str, int] | None = None,
) -> None:
    """Guarantee at least one athlete in each individual event when a recorded athlete exists."""
    for event in (
        event
        for event in active_events()
        if event not in active_relay_events() and not lineup.get(event)
    ):
        options: list[tuple[float, float, Performance, list[str]]] = []
        for perf in sort_event_pool([item for item in school if item.event == event], event):
            removals = minimum_event_removals(
                athlete_events[perf.athlete],
                event,
                athlete_max_events(perf.athlete, athlete_event_limits),
            )
            if removals is None:
                continue
            if any(len(lineup.get(old_event, [])) <= 1 for old_event in removals):
                continue
            cost = sum(potentials.get((perf.athlete, old_event), 0.0) for old_event in removals)
            performance_rank = perf.value if perf.is_time else -perf.value
            options.append((cost, performance_rank, perf, removals))
        if not options:
            continue
        _cost, _rank, perf, removals = min(options, key=lambda item: (item[0], item[1]))
        remove_athlete_events(lineup, athlete_events, perf.athlete, removals)
        try_add_entry(
            lineup,
            athlete_events,
            perf.athlete,
            event,
            athlete_event_limits=athlete_event_limits,
        )


def optimize_elite_sprint_utilization(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Push top flat sprinters toward four legal running events when it does not cost points."""
    potentials = compute_scores(school, opponents)
    elite_order = rank_priority_running_athletes(
        school,
        potentials,
        relays,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    protected: set[str] = set()
    replacements = 0
    for athlete in elite_order:
        while replacements < MAX_ELITE_REPLACEMENTS:
            athlete_events = collect_athlete_events(lineup, relays)
            if len(athlete_events[athlete]) >= athlete_max_events(athlete, athlete_event_limits):
                break
            replacement = find_best_elite_replacement(
                athlete,
                protected,
                lineup,
                relays,
                school,
                opponents,
                school_relay_history,
                opponent_relay_history,
                school_relay_splits,
                opponent_relay_splits,
                athlete_event_limits,
            )
            if not replacement:
                break
            lineup = replacement.lineup
            relays = replacement.relays
            replacements += 1
        protected.add(athlete)
    return lineup, relays


def optimize_distance_runner_utilization(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Let valuable distance runners take point-gaining legal swaps at the end."""
    potentials = compute_scores(school, opponents)
    distance_order = rank_priority_distance_athletes(
        school,
        potentials,
        relays,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
    )
    protected: set[str] = set()
    replacements = 0
    for athlete in distance_order:
        while replacements < MAX_DISTANCE_REPLACEMENTS:
            athlete_events = collect_athlete_events(lineup, relays)
            if len(athlete_events[athlete]) >= athlete_max_events(athlete, athlete_event_limits):
                break
            replacement = find_best_distance_replacement(
                athlete,
                protected,
                lineup,
                relays,
                school,
                opponents,
                school_relay_history,
                opponent_relay_history,
                school_relay_splits,
                opponent_relay_splits,
                athlete_event_limits,
            )
            if not replacement:
                break
            lineup = replacement.lineup
            relays = replacement.relays
            replacements += 1
        protected.add(athlete)
    return lineup, relays


def rank_priority_distance_athletes(
    school: list[Performance],
    potentials: dict[tuple[str, str], float],
    relays: dict[str, RelaySelection],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
) -> list[str]:
    """Rank athletes who have a real distance/mid-distance scoring path."""
    possible_events = possible_distance_events_by_athlete(school, relays, school_relay_history, school_relay_splits)
    relay_bonus = distance_relay_bonus_by_athlete(
        relays,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    grouped: dict[str, list[float]] = defaultdict(list)
    for perf in school:
        if perf.event in DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS:
            grouped[perf.athlete].append(potentials.get((perf.athlete, perf.event), 0.0))

    ranked = []
    for athlete, events in possible_events.items():
        if not events.intersection(DISTANCE_EVENTS):
            continue
        if len(events) < 2:
            continue
        value_limit = 2 if events.intersection(LONG_DISTANCE_EVENTS) else 3
        distance_value = sum(sorted(grouped.get(athlete, []), reverse=True)[:value_limit])
        bonus = relay_bonus.get(athlete, 0.0)
        total_value = distance_value + bonus
        if total_value <= 0:
            continue
        ranked.append((total_value, bonus, athlete))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].lower()))
    return [athlete for _value, _bonus, athlete in ranked[:DISTANCE_UTILIZATION_COUNT]]


def possible_distance_events_by_athlete(
    school: list[Performance],
    relays: dict[str, RelaySelection],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
) -> dict[str, set[str]]:
    """Return distance/mid-distance events where each athlete can be considered."""
    possible: dict[str, set[str]] = defaultdict(set)
    for perf in school:
        if perf.event in DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS:
            possible[perf.athlete].add(perf.event)
    for relay_event in sorted(DISTANCE_UTILIZATION_RELAY_EVENTS, key=event_sort_value):
        for athlete, _value, _source in best_relay_leg_candidates_with_sources(
            relay_event,
            school,
            school_relay_history,
            school_relay_splits,
        ):
            possible[athlete].add(relay_event)
        relay = relays.get(relay_event)
        if relay:
            for athlete in relay.athletes:
                possible[athlete].add(relay_event)
    return possible


def distance_relay_bonus_by_athlete(
    relays: dict[str, RelaySelection],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
) -> dict[str, float]:
    """Give value credit for current 4x800/4x400 relay scoring potential."""
    bonuses: dict[str, float] = defaultdict(float)
    athlete_events = collect_athlete_events({}, relays)
    for event, relay in relays.items():
        if event not in DISTANCE_UTILIZATION_RELAY_EVENTS:
            continue
        time_value = relay_selection_time_for_build(relay, athlete_events)
        points = projected_relay_points(
            event,
            time_value,
            opponents,
            opponent_relay_history,
            opponent_relay_splits,
        )
        if points <= 0:
            continue
        for athlete in relay.athletes:
            bonuses[athlete] += points / 4.0
    return bonuses


def find_best_distance_replacement(
    athlete: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Find the best legal distance addition that increases projected team score."""
    base_result = evaluate_lineup(
        lineup,
        relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    best_perf = best_performance_by_athlete_event(school)
    replacements: list[EliteReplacement] = []
    for event in sorted(DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS, key=event_sort_value):
        replacement = try_distance_individual_replacement(
            athlete,
            event,
            protected,
            lineup,
            relays,
            school,
            opponents,
            best_perf,
            base_result,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            replacements.append(replacement)
    for event in sorted(DISTANCE_UTILIZATION_RELAY_EVENTS, key=event_sort_value):
        replacement = try_distance_relay_replacement(
            athlete,
            event,
            protected,
            lineup,
            relays,
            school,
            opponents,
            school_relay_history,
            school_relay_splits,
            base_result,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            replacements.append(replacement)
    if not replacements:
        return None
    return max(
        replacements,
        key=lambda item: (
            item.total_delta,
            item.event_delta,
            item.speed_delta,
            -event_sort_value(item.event),
        ),
    )


def try_distance_individual_replacement(
    athlete: str,
    event: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    best_perf: dict[tuple[str, str], Performance],
    base_result: LineupResult,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Try placing a distance athlete into an individual event by replacing the weakest entry."""
    if event not in lineup or athlete in lineup[event]:
        return None
    perf = best_perf.get((athlete, event))
    if not perf:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(
        athlete_events[athlete], event, athlete_max_events(athlete, athlete_event_limits)
    ):
        return None
    trial_lineup = clone_lineup(lineup)
    speed_delta = 0.0
    if len(trial_lineup[event]) >= MAX_INDIVIDUAL_ENTRIES:
        target = worst_individual_entry(event, trial_lineup[event], best_perf, protected)
        if not target or not is_better(perf.value, target.value, perf.is_time):
            return None
        speed_delta = target.value - perf.value if perf.is_time else perf.value - target.value
        trial_lineup[event][trial_lineup[event].index(target.athlete)] = athlete
    else:
        trial_lineup[event].append(athlete)
    return evaluated_distance_replacement(
        trial_lineup,
        dict(relays),
        event,
        school,
        opponents,
        base_result,
        speed_delta,
        opponent_relay_history,
        opponent_relay_splits,
        athlete_event_limits,
    )


def try_distance_relay_replacement(
    athlete: str,
    relay_event: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    base_result: LineupResult,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Try replacing the slowest leg in a synthetic distance relay."""
    relay = relays.get(relay_event)
    if not relay or relay.method != "synthetic" or athlete in relay.athletes:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(
        athlete_events[athlete], relay_event, athlete_max_events(athlete, athlete_event_limits)
    ):
        return None
    leg_candidates = relay_leg_candidate_map(relay_event, school, school_relay_history, school_relay_splits)
    candidate_leg = leg_candidates.get(athlete)
    if candidate_leg is None:
        return None
    candidate_time, candidate_source = candidate_leg
    current_team = []
    for current_athlete in relay.athletes:
        current_leg = leg_candidates.get(current_athlete)
        if current_leg is None:
            return None
        current_time, current_source = current_leg
        current_team.append((current_athlete, current_time, current_source))
    replaceable_team = [item for item in current_team if item[0] not in protected]
    if not replaceable_team:
        return None
    target_athlete, target_time, _target_source = max(replaceable_team, key=lambda item: item[1])
    if candidate_time >= target_time:
        return None
    new_team = [(name, value, source) for name, value, source in current_team if name != target_athlete]
    new_team.append((athlete, candidate_time, candidate_source))
    ordered_team = order_synthetic_relay_legs(new_team)
    ordered_times = tuple(item[1] for item in ordered_team)
    ordered_sources = tuple(item[2] for item in ordered_team)
    trial_relays = dict(relays)
    trial_relays[relay_event] = RelaySelection(
        event=relay_event,
        athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
        projected_time=synthetic_relay_time(relay_event, ordered_times, ordered_sources),
        method="synthetic",
        source_mark="distance replacement using best individual PR/relay split",
        leg_times=ordered_times,  # type: ignore[arg-type]
        leg_sources=ordered_sources,  # type: ignore[arg-type]
    )
    return evaluated_distance_replacement(
        clone_lineup(lineup),
        trial_relays,
        relay_event,
        school,
        opponents,
        base_result,
        target_time - candidate_time,
        opponent_relay_history,
        opponent_relay_splits,
        athlete_event_limits,
    )


def evaluated_distance_replacement(
    trial_lineup: dict[str, list[str]],
    trial_relays: dict[str, RelaySelection],
    event: str,
    school: list[Performance],
    opponents: list[Performance],
    base_result: LineupResult,
    speed_delta: float,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Keep a distance replacement only when total projected team points rise."""
    if not lineup_is_valid(trial_lineup, trial_relays, athlete_event_limits):
        return None
    trial_result = evaluate_lineup(
        trial_lineup,
        trial_relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    total_delta = trial_result.total_points - base_result.total_points
    event_delta = trial_result.event_points.get(event, 0.0) - base_result.event_points.get(event, 0.0)
    if total_delta <= 0.01:
        return None
    return EliteReplacement(
        lineup=trial_lineup,
        relays=trial_relays,
        event=event,
        total_delta=total_delta,
        event_delta=event_delta,
        speed_delta=speed_delta,
    )


def rank_priority_running_athletes(
    school: list[Performance],
    potentials: dict[tuple[str, str], float],
    relays: dict[str, RelaySelection],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
) -> list[str]:
    """Rank pure sprinters first, then high-value non-distance runners for utilization."""
    priority = rank_elite_sprint_jump_athletes(
        school,
        potentials,
        relays,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    seen = set(priority)
    possible_events = possible_elite_events_by_athlete(school, relays)
    relay_bonus = elite_relay_bonus_by_athlete(
        relays,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    runner_grouped: dict[str, list[float]] = defaultdict(list)
    for perf in school:
        if perf.event in active_runner_individual_events():
            runner_grouped[perf.athlete].append(potentials.get((perf.athlete, perf.event), 0.0))

    runner_ranked = []
    for athlete, scores in runner_grouped.items():
        if athlete in seen:
            continue
        if len(possible_events.get(athlete, set())) < MAX_EVENTS_PER_ATHLETE:
            continue
        bonus = relay_bonus.get(athlete, 0.0)
        runner_value = sum(sorted(scores, reverse=True)[:MAX_EVENTS_PER_ATHLETE]) + bonus
        if runner_value <= 0:
            continue
        runner_ranked.append((runner_value, bonus, athlete))

    runner_ranked.sort(key=lambda item: (-item[0], -item[1], item[2].lower()))
    for _value, _bonus, athlete in runner_ranked:
        if len(priority) >= RUNNER_UTILIZATION_COUNT:
            break
        priority.append(athlete)
        seen.add(athlete)
    return priority


def rank_elite_sprint_jump_athletes(
    school: list[Performance],
    potentials: dict[tuple[str, str], float],
    relays: dict[str, RelaySelection],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
) -> list[str]:
    """Rank top flat sprinters using only 100m, 200m, 400m, and sprint relay value."""
    priority_grouped: dict[str, list[float]] = defaultdict(list)
    all_grouped: dict[str, list[float]] = defaultdict(list)
    for perf in school:
        if perf.event in active_elite_individual_events():
            priority_grouped[perf.athlete].append(potentials.get((perf.athlete, perf.event), 0.0))
        if perf.event in active_elite_individual_events():
            all_grouped[perf.athlete].append(potentials.get((perf.athlete, perf.event), 0.0))
    relay_bonus = elite_relay_bonus_by_athlete(
        relays,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    possible_events = possible_elite_events_by_athlete(school, relays)
    ranked = []
    for athlete, scores in all_grouped.items():
        if len(possible_events.get(athlete, set())) < MAX_EVENTS_PER_ATHLETE:
            continue
        bonus = relay_bonus.get(athlete, 0.0)
        priority_value = sum(sorted(priority_grouped.get(athlete, []), reverse=True)[:MAX_EVENTS_PER_ATHLETE]) + bonus
        top_four_value = sum(sorted(scores, reverse=True)[:MAX_EVENTS_PER_ATHLETE])
        if priority_value > 0 or top_four_value > 0:
            ranked.append((priority_value, top_four_value, bonus, athlete))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3].lower()))
    return [athlete for _priority, _top_four, _bonus, athlete in ranked[:ELITE_ATHLETE_COUNT]]


def possible_elite_events_by_athlete(
    school: list[Performance], relays: dict[str, RelaySelection]
) -> dict[str, set[str]]:
    """Return flat-sprint and sprint-relay events where each athlete can be considered."""
    possible: dict[str, set[str]] = defaultdict(set)
    individual_events_by_athlete: dict[str, set[str]] = defaultdict(set)
    for perf in school:
        individual_events_by_athlete[perf.athlete].add(perf.event)
        if perf.event in active_elite_individual_events():
            possible[perf.athlete].add(perf.event)
    for relay_event in sorted(active_sprint_relay_events(), key=event_sort_value):
        base_event = RELAY_BASE_EVENT[relay_event]
        for athlete, events in individual_events_by_athlete.items():
            if base_event in events:
                possible[athlete].add(relay_event)
        relay = relays.get(relay_event)
        if relay:
            for athlete in relay.athletes:
                possible[athlete].add(relay_event)
    return possible


def elite_relay_bonus_by_athlete(
    relays: dict[str, RelaySelection],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
) -> dict[str, float]:
    """Give relay-value credit to athletes on the two best current sprint relays."""
    bonuses: dict[str, float] = defaultdict(float)
    athlete_events = collect_athlete_events({}, relays)
    scored_relays = []
    for event, relay in relays.items():
        if event not in active_sprint_relay_events():
            continue
        time_value = relay_selection_time_for_build(relay, athlete_events)
        points = projected_relay_points(
            event,
            time_value,
            opponents,
            opponent_relay_history,
            opponent_relay_splits,
        )
        scored_relays.append((points, time_value, relay))
    scored_relays.sort(key=lambda item: (-item[0], item[1]))
    for points, _time_value, relay in scored_relays[:2]:
        if points <= 0:
            continue
        for athlete in relay.athletes:
            bonuses[athlete] += points / 4.0
    return bonuses


def find_best_elite_replacement(
    athlete: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Find the best legal non-losing event addition for one elite athlete."""
    base_result = evaluate_lineup(
        lineup,
        relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    best_perf = {(perf.athlete, perf.event): perf for perf in school}
    replacements: list[EliteReplacement] = []
    for event in sorted(active_elite_individual_events(), key=event_sort_value):
        replacement = try_elite_individual_replacement(
            athlete,
            event,
            protected,
            lineup,
            relays,
            school,
            school_relay_history,
            school_relay_splits,
            opponents,
            best_perf,
            base_result,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            replacements.append(replacement)
    for event in sorted(active_sprint_relay_events(), key=event_sort_value):
        replacement = try_elite_relay_replacement(
            athlete,
            event,
            protected,
            lineup,
            relays,
            school,
            opponents,
            school_relay_history,
            school_relay_splits,
            base_result,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            replacements.append(replacement)
    if not replacements:
        return None
    return max(
        replacements,
        key=lambda item: (
            item.total_delta,
            item.event_delta,
            item.speed_delta,
            -event_sort_value(item.event),
        ),
    )


def try_elite_individual_replacement(
    athlete: str,
    event: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponents: list[Performance],
    best_perf: dict[tuple[str, str], Performance],
    base_result: LineupResult,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Try adding an elite athlete to an individual event by replacing the weakest entry."""
    if event not in lineup or athlete in lineup[event]:
        return None
    perf = best_perf.get((athlete, event))
    if not perf:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(
        athlete_events[athlete], event, athlete_max_events(athlete, athlete_event_limits)
    ):
        return None
    trial_lineup = clone_lineup(lineup)
    trial_relays = dict(relays)
    speed_delta = 0.0
    if len(trial_lineup[event]) >= MAX_INDIVIDUAL_ENTRIES:
        target = worst_individual_entry(event, trial_lineup[event], best_perf)
        if not target:
            return None
        if not is_better(perf.value, target.value, perf.is_time):
            return None
        if target.athlete in protected:
            compensated = try_compensated_protected_individual_replacement(
                athlete,
                perf,
                event,
                target,
                protected,
                lineup,
                relays,
                school,
                school_relay_history,
                school_relay_splits,
                opponents,
                best_perf,
                base_result,
                opponent_relay_history,
                opponent_relay_splits,
                athlete_event_limits,
            )
            if compensated:
                return compensated
            target = worst_individual_entry(event, trial_lineup[event], best_perf, protected)
            if not target or not is_better(perf.value, target.value, perf.is_time):
                return None
        speed_delta = target.value - perf.value if perf.is_time else perf.value - target.value
        slot = trial_lineup[event].index(target.athlete)
        trial_lineup[event][slot] = athlete
    else:
        trial_lineup[event].append(athlete)
    return evaluated_elite_replacement(
        trial_lineup,
        trial_relays,
        event,
        school,
        opponents,
        base_result,
        speed_delta,
        opponent_relay_history,
        opponent_relay_splits,
        athlete_event_limits,
    )


def try_compensated_protected_individual_replacement(
    athlete: str,
    perf: Performance,
    event: str,
    target: Performance,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponents: list[Performance],
    best_perf: dict[tuple[str, str], Performance],
    base_result: LineupResult,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Let a faster elite take a protected athlete's slot only if the protected athlete is re-used."""
    trial_lineup = clone_lineup(lineup)
    slot = trial_lineup[event].index(target.athlete)
    trial_lineup[event][slot] = athlete
    base_speed_delta = target.value - perf.value if perf.is_time else perf.value - target.value
    options = compensated_individual_options(
        target.athlete,
        event,
        trial_lineup,
        relays,
        school,
        opponents,
        best_perf,
        base_result,
        base_speed_delta,
        protected,
        opponent_relay_history,
        opponent_relay_splits,
        athlete_event_limits,
    )
    options.extend(
        compensated_relay_options(
            target.athlete,
            event,
            trial_lineup,
            relays,
            school,
            school_relay_history,
            school_relay_splits,
            opponents,
            base_result,
            base_speed_delta,
            protected,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
    )
    if not options:
        return None
    return max(
        options,
        key=lambda item: (
            item.total_delta,
            item.event_delta,
            item.speed_delta,
            -event_sort_value(item.event),
        ),
    )


def compensated_individual_options(
    protected_athlete: str,
    replaced_event: str,
    trial_lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    best_perf: dict[tuple[str, str], Performance],
    base_result: LineupResult,
    base_speed_delta: float,
    protected: set[str],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> list[EliteReplacement]:
    """Try moving a displaced protected athlete into another individual flat sprint event."""
    options: list[EliteReplacement] = []
    athlete_events = collect_athlete_events(trial_lineup, relays)
    for new_event in sorted(active_elite_individual_events(), key=event_sort_value):
        if new_event == replaced_event or protected_athlete in trial_lineup.get(new_event, []):
            continue
        protected_perf = best_perf.get((protected_athlete, new_event))
        if not protected_perf or not can_take_event(
            athlete_events[protected_athlete],
            new_event,
            athlete_max_events(protected_athlete, athlete_event_limits),
        ):
            continue
        option_lineup = clone_lineup(trial_lineup)
        speed_delta = base_speed_delta
        if len(option_lineup.get(new_event, [])) >= MAX_INDIVIDUAL_ENTRIES:
            target = worst_individual_entry(new_event, option_lineup[new_event], best_perf, protected)
            if not target or not is_better(protected_perf.value, target.value, protected_perf.is_time):
                continue
            speed_delta += target.value - protected_perf.value if protected_perf.is_time else protected_perf.value - target.value
            option_lineup[new_event][option_lineup[new_event].index(target.athlete)] = protected_athlete
        else:
            option_lineup.setdefault(new_event, []).append(protected_athlete)
        replacement = evaluated_elite_replacement(
            option_lineup,
            dict(relays),
            replaced_event,
            school,
            opponents,
            base_result,
            speed_delta,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            options.append(replacement)
    return options


def compensated_relay_options(
    protected_athlete: str,
    replaced_event: str,
    trial_lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponents: list[Performance],
    base_result: LineupResult,
    base_speed_delta: float,
    protected: set[str],
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> list[EliteReplacement]:
    """Try moving a displaced protected athlete into a synthetic sprint relay."""
    options: list[EliteReplacement] = []
    athlete_events = collect_athlete_events(trial_lineup, relays)
    for relay_event in sorted(active_sprint_relay_events(), key=event_sort_value):
        relay = relays.get(relay_event)
        if not relay or relay.method != "synthetic" or protected_athlete in relay.athletes:
            continue
        if not can_take_event(
            athlete_events[protected_athlete],
            relay_event,
            athlete_max_events(protected_athlete, athlete_event_limits),
        ):
            continue
        leg_candidates = relay_leg_candidate_map(relay_event, school, school_relay_history, school_relay_splits)
        protected_leg = leg_candidates.get(protected_athlete)
        if protected_leg is None:
            continue
        protected_time, protected_source = protected_leg
        current_team = []
        for current_athlete in relay.athletes:
            current_leg = leg_candidates.get(current_athlete)
            if current_leg is None:
                break
            current_time, current_source = current_leg
            current_team.append((current_athlete, current_time, current_source))
        if len(current_team) != 4:
            continue
        replaceable_team = [item for item in current_team if item[0] not in protected]
        if not replaceable_team:
            continue
        target_athlete, target_time, _target_source = max(replaceable_team, key=lambda item: item[1])
        if protected_time >= target_time:
            continue
        new_team = [(name, value, source) for name, value, source in current_team if name != target_athlete]
        new_team.append((protected_athlete, protected_time, protected_source))
        ordered_team = order_synthetic_relay_legs(new_team)
        ordered_times = tuple(item[1] for item in ordered_team)
        ordered_sources = tuple(item[2] for item in ordered_team)
        option_relays = dict(relays)
        option_relays[relay_event] = RelaySelection(
            event=relay_event,
            athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
            projected_time=synthetic_relay_time(relay_event, ordered_times, ordered_sources),
            method="synthetic",
            source_mark="protected elite compensation using best individual PR/relay split",
            leg_times=ordered_times,  # type: ignore[arg-type]
            leg_sources=ordered_sources,  # type: ignore[arg-type]
        )
        replacement = evaluated_elite_replacement(
            clone_lineup(trial_lineup),
            option_relays,
            replaced_event,
            school,
            opponents,
            base_result,
            base_speed_delta + target_time - protected_time,
            opponent_relay_history,
            opponent_relay_splits,
            athlete_event_limits,
        )
        if replacement:
            options.append(replacement)
    return options


def try_elite_relay_replacement(
    athlete: str,
    relay_event: str,
    protected: set[str],
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    base_result: LineupResult,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Try replacing the slowest leg in a synthetic sprint relay with an elite athlete."""
    relay = relays.get(relay_event)
    if not relay or relay.method != "synthetic" or athlete in relay.athletes:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(
        athlete_events[athlete], relay_event, athlete_max_events(athlete, athlete_event_limits)
    ):
        return None
    leg_candidates = relay_leg_candidate_map(relay_event, school, school_relay_history, school_relay_splits)
    candidate_leg = leg_candidates.get(athlete)
    if candidate_leg is None:
        return None
    candidate_time, candidate_source = candidate_leg
    current_team = []
    for current_athlete in relay.athletes:
        current_leg = leg_candidates.get(current_athlete)
        if current_leg is None:
            return None
        current_time, current_source = current_leg
        current_team.append((current_athlete, current_time, current_source))
    replaceable_team = [item for item in current_team if item[0] not in protected]
    if not replaceable_team:
        return None
    target_athlete, target_time, _target_source = max(replaceable_team, key=lambda item: item[1])
    if candidate_time >= target_time:
        return None
    new_team = [(name, value, source) for name, value, source in current_team if name != target_athlete]
    new_team.append((athlete, candidate_time, candidate_source))
    ordered_team = order_synthetic_relay_legs(new_team)
    ordered_times = tuple(item[1] for item in ordered_team)
    ordered_sources = tuple(item[2] for item in ordered_team)
    trial_relays = dict(relays)
    trial_relays[relay_event] = RelaySelection(
        event=relay_event,
        athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
        projected_time=synthetic_relay_time(relay_event, ordered_times, ordered_sources),
        method="synthetic",
        source_mark="elite replacement using best individual PR/relay split",
        leg_times=ordered_times,  # type: ignore[arg-type]
        leg_sources=ordered_sources,  # type: ignore[arg-type]
    )
    return evaluated_elite_replacement(
        clone_lineup(lineup),
        trial_relays,
        relay_event,
        school,
        opponents,
        base_result,
        target_time - candidate_time,
        opponent_relay_history,
        opponent_relay_splits,
        athlete_event_limits,
    )


def evaluated_elite_replacement(
    trial_lineup: dict[str, list[str]],
    trial_relays: dict[str, RelaySelection],
    event: str,
    school: list[Performance],
    opponents: list[Performance],
    base_result: LineupResult,
    speed_delta: float,
    opponent_relay_history: list[RelayPerformance],
    opponent_relay_splits: list[Performance],
    athlete_event_limits: dict[str, int] | None = None,
) -> EliteReplacement | None:
    """Score a trial elite replacement and keep it only when team points do not drop."""
    if not lineup_is_valid(trial_lineup, trial_relays, athlete_event_limits):
        return None
    trial_result = evaluate_lineup(
        trial_lineup,
        trial_relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    total_delta = trial_result.total_points - base_result.total_points
    event_delta = trial_result.event_points.get(event, 0.0) - base_result.event_points.get(event, 0.0)
    if total_delta < -ELITE_UTILIZATION_POINT_TOLERANCE:
        return None
    if total_delta < -0.01 and speed_delta <= 0.01:
        return None
    if total_delta <= 0.01 and event_delta <= 0.01 and speed_delta <= 0.01:
        return None
    return EliteReplacement(
        lineup=trial_lineup,
        relays=trial_relays,
        event=event,
        total_delta=total_delta,
        event_delta=event_delta,
        speed_delta=speed_delta,
    )


def worst_individual_entry(
    event: str,
    athletes: list[str],
    best_perf: dict[tuple[str, str], Performance],
    protected: set[str] | None = None,
) -> Performance | None:
    """Return the weakest seeded athlete currently entered in an individual event."""
    protected = protected or set()
    entries = [best_perf.get((athlete, event)) for athlete in athletes if athlete not in protected]
    entries = [entry for entry in entries if entry is not None]
    if not entries:
        return None
    return sort_event_pool(entries, event)[-1]


def relay_leg_time_map(
    relay_event: str,
    school: list[Performance],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance],
) -> dict[str, float]:
    """Map athletes to their best comparable relay-leg time."""
    return dict(best_relay_leg_candidates(relay_event, school, relay_history, relay_splits))


def relay_leg_candidate_map(
    relay_event: str,
    school: list[Performance],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance],
) -> dict[str, tuple[float, str]]:
    """Map athletes to their best comparable relay-leg time and source type."""
    return {
        athlete: (value, source)
        for athlete, value, source in best_relay_leg_candidates_with_sources(
            relay_event,
            school,
            relay_history,
            relay_splits,
        )
    }


def group_school_events(
    school: list[Performance], potentials: dict[tuple[str, str], float]
) -> dict[str, list[tuple[str, Performance, float]]]:
    """Group each school athlete's events by projected scoring potential."""
    grouped: dict[str, list[tuple[str, Performance, float]]] = defaultdict(list)
    for perf in school:
        if perf.event in RELAY_EVENTS:
            continue
        grouped[perf.athlete].append((perf.event, perf, potentials.get((perf.athlete, perf.event), 0.0)))
    for athlete in grouped:
        grouped[athlete].sort(key=lambda item: (-item[2], event_sort_value(item[0]), item[1].value))
    return grouped


def rank_athletes_by_value(grouped: dict[str, list[tuple[str, Performance, float]]]) -> list[str]:
    """Rank athletes by the sum of their best three scoring chances."""
    return sorted(grouped, key=lambda athlete: sum(item[2] for item in grouped[athlete][:3]), reverse=True)


def try_add_entry(
    lineup: dict[str, list[str]],
    athlete_events: dict[str, list[str]],
    athlete: str,
    event: str,
    max_entries: int = MAX_INDIVIDUAL_ENTRIES,
    athlete_event_limits: dict[str, int] | None = None,
) -> bool:
    """Add an athlete to an individual event if all constraints allow it."""
    if event not in lineup:
        return False
    if athlete in lineup[event] or len(lineup[event]) >= max_entries:
        return False
    if not can_take_event(
        athlete_events[athlete], event, athlete_max_events(athlete, athlete_event_limits)
    ):
        return False
    lineup[event].append(athlete)
    athlete_events[athlete].append(event)
    return True


def can_take_event(
    existing_events: list[str], event: str, max_events: int = MAX_EVENTS_PER_ATHLETE
) -> bool:
    """Limit athletes to four events and avoid consecutive running races."""
    if event in existing_events:
        return False
    if not can_event_set_stand(existing_events + [event], max_events):
        return False
    running_order = current_meet_config().running_order
    if event in running_order:
        event_order = running_order[event]
        for existing in existing_events:
            if existing in running_order and abs(running_order[existing] - event_order) == 1:
                return False
    return True


def choose_relay_team(
    relay_event: str,
    school: list[Performance],
    opponents: list[Performance],
    athlete_events: dict[str, list[str]],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
) -> RelaySelection | None:
    """Prefer a scoring relay, but always return the best valid relay candidate."""
    candidates: list[RelaySelection] = []
    for relay in sorted([item for item in school_relay_history if item.event == relay_event], key=lambda item: item.value):
        if all(
            can_take_event(
                athlete_events[athlete],
                relay_event,
                athlete_max_events(athlete, athlete_event_limits),
            )
            for athlete in relay.athletes
        ):
            candidates.append(
                RelaySelection(
                    event=relay_event,
                    athletes=relay.athletes,
                    projected_time=historic_relay_time(relay),
                    method="historic",
                    source_mark=relay.mark,
                )
            )

    synthetic = synthesize_relay(
        relay_event,
        school,
        athlete_events,
        school_relay_history,
        school_relay_splits,
        athlete_event_limits=athlete_event_limits,
    )
    if synthetic:
        candidates.append(synthetic)
    if not candidates:
        return None

    scored = []
    for candidate in candidates:
        candidate_time = relay_selection_time_for_build(candidate, athlete_events)
        points = projected_relay_points(
            relay_event,
            candidate_time,
            opponents,
            opponent_relay_history,
            opponent_relay_splits,
        )
        scored.append((points, candidate_time, candidate))
    scored.sort(key=lambda item: (-item[0], item[1]))
    scoring_candidates = [item for item in scored if item[0] >= 5]
    if scoring_candidates:
        return scoring_candidates[0][2]

    depth_relay = synthesize_relay(
        relay_event,
        school,
        athlete_events,
        school_relay_history,
        school_relay_splits,
        prefer_depth=True,
        athlete_event_limits=athlete_event_limits,
    )
    return depth_relay or scored[0][2]


def choose_stacked_relay_anchor(
    school: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
) -> RelaySelection | None:
    """Reserve the earliest sprint relay's fastest full-strength team before individuals."""
    for relay_event in sorted(active_sprint_relay_events(), key=event_sort_value):
        selection = choose_fully_stacked_relay(
            relay_event,
            school,
            school_relay_history,
            school_relay_splits,
            athlete_event_limits,
        )
        if selection:
            return selection
    return None


def choose_fully_stacked_relay(
    relay_event: str,
    school: list[Performance],
    school_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
) -> RelaySelection | None:
    """Choose the fastest historic relay unless an unrestricted synthetic team is faster."""
    empty_events: dict[str, list[str]] = defaultdict(list)
    historic: RelaySelection | None = None
    for relay in sorted(
        (item for item in school_relay_history if item.event == relay_event),
        key=lambda item: item.value,
    ):
        if len(set(relay.athletes)) != 4 or not all(relay.athletes):
            continue
        if not all(
            can_take_event(
                [],
                relay_event,
                athlete_max_events(athlete, athlete_event_limits),
            )
            for athlete in relay.athletes
        ):
            continue
        historic = RelaySelection(
            event=relay_event,
            athletes=relay.athletes,
            projected_time=historic_relay_time(relay),
            method="historic",
            source_mark=relay.mark,
        )
        break

    synthetic = synthesize_relay(
        relay_event,
        school,
        empty_events,
        school_relay_history,
        school_relay_splits,
        athlete_event_limits=athlete_event_limits,
    )
    if synthetic and (not historic or synthetic.projected_time < historic.projected_time - 0.01):
        return synthetic
    return historic or synthetic


def relay_selection_time_for_build(
    selection: RelaySelection, athlete_events: dict[str, list[str]]
) -> float:
    """Apply current known fatigue to a relay candidate during lineup construction."""
    if selection.method == "historic":
        avg_fatigue = sum(
            fatigue_factor(prior_event_count(athlete_events[athlete], selection.event))
            for athlete in selection.athletes
        ) / 4
        return selection.projected_time * avg_fatigue
    if selection.leg_times:
        adjusted_leg_times = tuple(
            leg_time * fatigue_factor(prior_event_count(athlete_events[athlete], selection.event))
            for athlete, leg_time in zip(selection.athletes, selection.leg_times)
        )
        return synthetic_relay_time(selection.event, adjusted_leg_times, selection.leg_sources)
    return selection.projected_time


def prior_event_count(existing_events: list[str], event: str) -> int:
    """Count only events that occur before the candidate event in the meet order."""
    event_rank = event_sort_value(event)
    return sum(1 for existing in existing_events if event_sort_value(existing) < event_rank)


def synthesize_relay(
    relay_event: str,
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    school_relay_history: list[RelayPerformance] | None = None,
    school_relay_splits: list[Performance] | None = None,
    prefer_depth: bool = False,
    athlete_event_limits: dict[str, int] | None = None,
) -> RelaySelection | None:
    """Create a relay using each athlete's faster individual PR or recorded relay split."""
    candidates = best_relay_leg_candidates_with_sources(
        relay_event,
        school,
        school_relay_history or [],
        school_relay_splits or [],
    )
    available = [
        (athlete, leg_time, leg_source)
        for athlete, leg_time, leg_source in candidates
        if can_take_event(
            athlete_events[athlete], relay_event, athlete_max_events(athlete, athlete_event_limits)
        )
    ]
    if prefer_depth and len(available) >= 4:
        candidate_pool = available[:8]
        team = candidate_pool[-4:]
    else:
        team = available[:4]
    if len(team) < 4:
        return None
    for athlete, _leg_time, _leg_source in team:
        if can_take_event(
            athlete_events[athlete], relay_event, athlete_max_events(athlete, athlete_event_limits)
        ):
            continue
    ordered_team = order_synthetic_relay_legs(team)
    athletes = tuple(item[0] for item in ordered_team)
    leg_times = tuple(item[1] for item in ordered_team)
    leg_sources = tuple(item[2] for item in ordered_team)
    time_value = synthetic_relay_time(relay_event, leg_times, leg_sources)
    return RelaySelection(
        event=relay_event,
        athletes=athletes,  # type: ignore[arg-type]
        projected_time=time_value,
        method="synthetic",
        source_mark=(
            "depth runners using best individual PR/relay split"
            if prefer_depth
            else "best individual PR/relay split"
        ),
        leg_times=leg_times,  # type: ignore[arg-type]
        leg_sources=leg_sources,  # type: ignore[arg-type]
    )


def best_relay_leg_candidates(
    relay_event: str,
    school: list[Performance],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance] | None = None,
) -> list[tuple[str, float]]:
    """Return athletes ranked by their fastest comparable individual time or relay split."""
    return [
        (athlete, value)
        for athlete, value, _source in best_relay_leg_candidates_with_sources(
            relay_event,
            school,
            relay_history,
            relay_splits,
        )
    ]


def best_relay_leg_candidates_with_sources(
    relay_event: str,
    school: list[Performance],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance] | None = None,
) -> list[tuple[str, float, str]]:
    """Return athletes ranked by comparable relay-leg time with value source."""
    base_event = RELAY_BASE_EVENT[relay_event]
    best: dict[str, tuple[str, float, str]] = {}
    for perf in school:
        if perf.event != base_event:
            continue
        key = perf.athlete.lower()
        current = best.get(key)
        if not current or perf.value < current[1]:
            best[key] = (perf.athlete, perf.value, INDIVIDUAL_LEG_SOURCE)
    for relay in relay_history:
        if relay.event != relay_event:
            continue
        for athlete, split in zip(relay.athletes, relay.splits):
            if split is None:
                continue
            key = athlete.lower()
            current = best.get(key)
            if not current or split < current[1] or (split == current[1] and current[2] == INDIVIDUAL_LEG_SOURCE):
                best[key] = (athlete, split, RELAY_SPLIT_LEG_SOURCE)
    for split in relay_splits or []:
        if split.event != base_event:
            continue
        key = split.athlete.lower()
        current = best.get(key)
        if not current or split.value < current[1] or (split.value == current[1] and current[2] == INDIVIDUAL_LEG_SOURCE):
            best[key] = (split.athlete, split.value, RELAY_SPLIT_LEG_SOURCE)
    return sorted(best.values(), key=lambda item: item[1])


def order_synthetic_relay_legs(team: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Order four synthetic legs as second, third, slowest, fastest."""
    ranked = sorted(team, key=lambda item: item[1])
    if len(ranked) != 4:
        return ranked
    return [ranked[1], ranked[2], ranked[3], ranked[0]]


def synthetic_relay_credit(relay_event: str, leg_sources: tuple[str, ...] | None, leg_count: int) -> float:
    """Credit only relay legs based on individual PRs, not already-recorded relay splits."""
    sources = leg_sources or tuple(INDIVIDUAL_LEG_SOURCE for _ in range(leg_count))
    per_individual = RELAY_INDIVIDUAL_EXCHANGE_CREDIT[relay_event]
    return sum(per_individual for source in sources[:leg_count] if source == INDIVIDUAL_LEG_SOURCE)


def synthetic_relay_time(
    relay_event: str,
    leg_times: tuple[float, ...] | list[float],
    leg_sources: tuple[str, ...] | None = None,
) -> float:
    """Estimate synthetic relay time from leg values and individual-leg exchange credit."""
    return sum(leg_times) - synthetic_relay_credit(relay_event, leg_sources, len(leg_times))


def historic_relay_time(relay: RelayPerformance) -> float:
    """Use a recorded relay time with a small improvement assumption for sprint relays."""
    if relay.method == "projected":
        return relay.value
    return relay.value - HISTORIC_RELAY_IMPROVEMENT.get(relay.event, 0.0)


def relay_time(relay_event: str, legs: list[Performance], adjustments: dict[str, float] | None = None) -> float:
    """Estimate relay time as four PRs minus exchange credit."""
    leg_times = []
    for perf in legs[:4]:
        factor = adjustments.get(perf.athlete, 1.0) if adjustments else 1.0
        leg_times.append(perf.value * factor)
    return synthetic_relay_time(
        relay_event,
        leg_times,
        tuple(INDIVIDUAL_LEG_SOURCE for _ in leg_times),
    )


def projected_relay_points(
    relay_event: str,
    school_time: float,
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
) -> float:
    """Compare a school relay estimate against opponent relay estimates or recorded relay marks."""
    opponent_times = estimate_opponent_relays(
        relay_event,
        opponents,
        opponent_relay_history or [],
        opponent_relay_splits or [],
    )
    ranked = sorted([("school", school_time)] + [("opponent", value) for value in opponent_times], key=lambda row: row[1])
    for place, (role, _value) in enumerate(ranked[: len(RELAY_POINTS)], start=1):
        if role == "school":
            return float(RELAY_POINTS[place - 1])
    return 0.0


def estimate_opponent_relays(
    relay_event: str,
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
) -> list[float]:
    """Return exactly one fastest relay estimate for each opponent school."""
    return [
        entry.value
        for entry in estimate_opponent_relay_entries(
            relay_event,
            opponents,
            opponent_relay_history,
            opponent_relay_splits,
        )
    ]


def estimate_opponent_relay_entries(
    relay_event: str,
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
) -> list[Performance]:
    """Return one displayable historic relay entry for each opponent school."""
    recorded_by_source: dict[str, Performance] = {}
    for relay in opponent_relay_history or []:
        if relay.event != relay_event:
            continue
        source = relay.source or "Opponent"
        entry = Performance(
            f"{source} Relay",
            relay_event,
            relay.mark,
            relay.value,
            True,
            source,
            PROJECTED_OPPONENT_ROLE,
        )
        current = recorded_by_source.get(source)
        if not current or entry.value < current.value:
            recorded_by_source[source] = entry
    for perf in opponents:
        if perf.event != relay_event:
            continue
        source = perf.source or "Opponent"
        entry = Performance(
            f"{source} Relay",
            relay_event,
            perf.mark,
            perf.value,
            True,
            source,
            PROJECTED_OPPONENT_ROLE,
        )
        current = recorded_by_source.get(source)
        if not current or entry.value < current.value:
            recorded_by_source[source] = entry

    return sorted(recorded_by_source.values(), key=lambda perf: perf.value)


def fastest_historic_relay_entries(
    relay_history: list[RelayPerformance],
    fallback_source: str,
    team_role: str,
) -> list[Performance]:
    """Convert relay history into one fastest raw relay mark per source and event."""
    entries: dict[tuple[str, str], Performance] = {}
    for relay in relay_history:
        if relay.event not in active_relay_events():
            continue
        if relay.method not in ("", "historic"):
            continue
        source = relay.source or fallback_source
        entry = Performance(
            f"{source} Relay",
            relay.event,
            relay.mark,
            relay.value,
            True,
            source,
            team_role,
        )
        key = (source, relay.event)
        current = entries.get(key)
        if not current or entry.value < current.value:
            entries[key] = entry
    return sorted(entries.values(), key=lambda perf: (event_sort_value(perf.event), perf.value, perf.source.lower()))


def top_opponent_individual_entries(data: ScrapeResult, fallback_source: str) -> list[Performance]:
    """Return top-three raw PR entries per individual event for one opponent school."""
    source = scrape_result_source(data, fallback_source)
    entries: list[Performance] = []
    for event in (event for event in active_events() if event not in active_relay_events()):
        event_entries = [perf for perf in data.performances if perf.event == event]
        for perf in sort_event_pool(event_entries, event)[:MAX_INDIVIDUAL_ENTRIES]:
            entries.append(
                Performance(
                    perf.athlete,
                    perf.event,
                    perf.mark,
                    perf.value,
                    perf.is_time,
                    perf.source or source,
                    PROJECTED_OPPONENT_ROLE,
                )
            )
    return entries


def fill_remaining_spots(
    lineup: dict[str, list[str]],
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    athlete_event_limits: dict[str, int] | None = None,
) -> None:
    """Fill open individual entries by PR after the main scoring assignments."""
    for event in lineup:
        event_perfs = sort_event_pool([perf for perf in school if perf.event == event], event)
        for perf in event_perfs:
            if len(lineup[event]) >= MAX_INDIVIDUAL_ENTRIES:
                break
            try_add_entry(
                lineup,
                athlete_events,
                perf.athlete,
                event,
                athlete_event_limits=athlete_event_limits,
            )


def optimize_lineup(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    athlete_events: dict[str, list[str]],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
    protected_athletes: set[str] | None = None,
    athlete_event_limits: dict[str, int] | None = None,
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Try simple one-athlete replacements and keep changes that raise projected points."""
    protected_athletes = protected_athletes or set()
    best_lineup = clone_lineup(lineup)
    best_relays = dict(relays)
    best_score = evaluate_lineup(
        best_lineup,
        best_relays,
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    ).total_points
    school_by_event = {
        event: sort_event_pool([perf for perf in school if perf.event == event], event)
        for event in active_events()
    }

    improved = True
    passes = 0
    while improved and passes < 4:
        improved = False
        passes += 1
        for event, athletes in list(best_lineup.items()):
            for slot, old_athlete in enumerate(list(athletes)):
                if old_athlete in protected_athletes:
                    continue
                for candidate in school_by_event.get(event, [])[:10]:
                    if candidate.athlete in athletes:
                        continue
                    trial_lineup = clone_lineup(best_lineup)
                    trial_lineup[event][slot] = candidate.athlete
                    if not lineup_is_valid(trial_lineup, best_relays, athlete_event_limits):
                        continue
                    trial_score = evaluate_lineup(
                        trial_lineup,
                        best_relays,
                        school,
                        opponents,
                        opponent_relay_history,
                        opponent_relay_splits,
                    ).total_points
                    if trial_score > best_score + 0.01:
                        best_lineup = trial_lineup
                        best_score = trial_score
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
    return best_lineup, best_relays


def clone_lineup(lineup: dict[str, list[str]]) -> dict[str, list[str]]:
    """Copy a lineup dictionary."""
    return {event: list(athletes) for event, athletes in lineup.items()}


def lineup_is_valid(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    athlete_event_limits: dict[str, int] | None = None,
) -> bool:
    """Validate athlete event-count and running-adjacency constraints."""
    athlete_events: dict[str, list[str]] = defaultdict(list)
    for event, athletes in lineup.items():
        if len(athletes) > MAX_INDIVIDUAL_ENTRIES or len(set(athletes)) != len(athletes):
            return False
        for athlete in athletes:
            athlete_events[athlete].append(event)
    for event, relay in relays.items():
        if len(relay.athletes) != 4 or len(set(relay.athletes)) != 4:
            return False
        for athlete in relay.athletes:
            athlete_events[athlete].append(event)
    return all(
        can_event_set_stand(events, athlete_max_events(athlete, athlete_event_limits))
        for athlete, events in athlete_events.items()
    )


def can_event_set_stand(events: list[str], max_events: int = MAX_EVENTS_PER_ATHLETE) -> bool:
    """Check the full set of events for one athlete."""
    if len(events) > max_events:
        return False
    if {"400m", "4x400 relay"}.issubset(set(events)) and not is_under_1600_distance_load(events):
        return False
    if distance_limit_exceeded(events):
        return False
    running_order = current_meet_config().running_order
    ordered = sorted(running_order[event] for event in events if event in running_order)
    return all(b - a > 1 for a, b in zip(ordered, ordered[1:]))


def distance_limit_exceeded(events: list[str]) -> bool:
    """Apply distance-load caps, with three events allowed only below 1600m."""
    event_set = set(events)
    if not event_set.intersection(DISTANCE_EVENTS):
        return False
    if event_set.intersection(LONG_DISTANCE_EVENTS):
        return len(events) > 2
    if len(events) <= 2:
        return False
    return len(events) > 3 or not is_under_1600_distance_load(events)


def is_under_1600_distance_load(events: list[str]) -> bool:
    """Return whether a distance load is one of the allowed under-1600m combinations."""
    event_set = set(events)
    return (
        bool(event_set.intersection(DISTANCE_EVENTS))
        and not event_set.intersection(LONG_DISTANCE_EVENTS)
        and event_set.issubset(DISTANCE_THREE_EVENT_ALLOWED_EVENTS)
    )


def scrape_result_source(data: ScrapeResult, fallback: str) -> str:
    """Infer a team display name from scraped performances or relays."""
    for perf in data.performances:
        if perf.source:
            return perf.source
    for relay in data.relay_history:
        if relay.source:
            return relay.source
    for split in data.relay_splits:
        if split.source:
            return split.source
    return fallback


def build_independent_team_projection(
    data: ScrapeResult, fallback_source: str, projected_role: str
) -> TeamMeetProjection:
    """Generate one team's lineup as if it were entered by itself."""
    source = scrape_result_source(data, fallback_source)
    raw_lineup = build_lineup(
        data.performances,
        [],
        data.relay_history,
        [],
        data.relay_splits,
        [],
    )
    entries, _generated_relay_entries = build_projected_meet_entries(
        raw_lineup["lineup"],
        raw_lineup["relays"],
        data.performances,
        source,
        projected_role,
    )
    relay_entries = fastest_historic_relay_entries(data.relay_history, source, projected_role)
    return TeamMeetProjection(
        source=source,
        lineup=raw_lineup["lineup"],
        relays=raw_lineup["relays"],
        entries=entries,
        relay_entries=relay_entries,
    )


def build_projected_meet_entries(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    performances: list[Performance],
    source: str,
    team_role: str,
) -> tuple[list[Performance], list[Performance]]:
    """Apply the lineup's fatigue model and return projected meet entries."""
    best_perf = best_performance_by_athlete_event(performances)
    athlete_history: dict[str, list[str]] = defaultdict(list)
    individual_entries: list[Performance] = []
    relay_entries: list[Performance] = []

    for event in sorted(active_events(), key=event_sort_value):
        if event in lineup:
            event_entries: list[Performance] = []
            for athlete in lineup[event]:
                perf = best_perf.get((athlete, event))
                if not perf:
                    continue
                adjusted_value = apply_fatigue(perf.value, perf.is_time, len(athlete_history[athlete]))
                event_entries.append(
                    Performance(
                        perf.athlete,
                        perf.event,
                        perf.mark,
                        adjusted_value,
                        perf.is_time,
                        source or perf.source,
                        team_role,
                    )
                )
            individual_entries.extend(event_entries)
            for perf in event_entries:
                athlete_history[perf.athlete].append(event)

        relay = relays.get(event)
        if relay:
            time_value = relay_selection_time_for_build(relay, athlete_history)
            if math.isfinite(time_value):
                relay_entries.append(
                    Performance(
                        f"{source} Relay",
                        event,
                        format_time(time_value),
                        time_value,
                        True,
                        source,
                        team_role,
                    )
                )
            for athlete in relay.athletes:
                athlete_history[athlete].append(event)
    return individual_entries, relay_entries


def best_performance_by_athlete_event(performances: list[Performance]) -> dict[tuple[str, str], Performance]:
    """Keep each athlete's best official individual mark for each event."""
    best: dict[tuple[str, str], Performance] = {}
    for perf in performances:
        key = (perf.athlete, perf.event)
        current = best.get(key)
        if not current or is_better(perf.value, current.value, perf.is_time):
            best[key] = perf
    return best


def score_team_points_from_entries(entries: list[Performance]) -> dict[str, float]:
    """Score all projected meet entries and total points by school name."""
    entries = dedupe_meet_entries(entries)
    sources = {perf.source for perf in entries if perf.source}
    totals: dict[str, float] = {source: 0.0 for source in sources}
    for event in active_events():
        event_entries = [perf for perf in entries if perf.event == event]
        if not event_entries:
            continue
        points = RELAY_POINTS if event in RELAY_EVENTS else INDIVIDUAL_POINTS
        for place, perf in enumerate(sort_event_pool(event_entries, event)[: len(points)], start=1):
            totals[perf.source] = totals.get(perf.source, 0.0) + float(points[place - 1])
    return {
        source: round(points, 2)
        for source, points in sorted(totals.items(), key=lambda item: (-item[1], item[0].lower()))
    }


def dedupe_meet_entries(entries: list[Performance]) -> list[Performance]:
    """Remove exact duplicate projected entries before team scoring."""
    seen: set[tuple[str, str, str, str, float]] = set()
    unique: list[Performance] = []
    for perf in entries:
        key = (perf.team_role, perf.source, perf.athlete, perf.event, round(perf.value, 4))
        if key in seen:
            continue
        seen.add(key)
        unique.append(perf)
    return unique


__all__ = [
    "best_performance_by_athlete_event",
    "best_relay_leg_candidates",
    "best_relay_leg_candidates_with_sources",
    "build_independent_team_projection",
    "build_lineup",
    "build_projected_meet_entries",
    "can_event_set_stand",
    "can_take_event",
    "choose_fully_stacked_relay",
    "choose_relay_team",
    "choose_stacked_relay_anchor",
    "clone_lineup",
    "collect_athlete_events",
    "compensated_individual_options",
    "compensated_relay_options",
    "compute_scores",
    "dedupe_meet_entries",
    "distance_limit_exceeded",
    "distance_relay_bonus_by_athlete",
    "elite_relay_bonus_by_athlete",
    "ensure_complete_lineup",
    "estimate_opponent_relay_entries",
    "estimate_opponent_relays",
    "evaluated_distance_replacement",
    "evaluated_elite_replacement",
    "fastest_historic_relay_entries",
    "fill_remaining_spots",
    "find_best_distance_replacement",
    "find_best_elite_replacement",
    "force_depth_relay",
    "force_empty_individual_events",
    "group_school_events",
    "historic_relay_time",
    "is_under_1600_distance_load",
    "lineup_is_valid",
    "minimum_event_removals",
    "optimize_distance_runner_utilization",
    "optimize_elite_sprint_utilization",
    "optimize_lineup",
    "order_synthetic_relay_legs",
    "possible_distance_events_by_athlete",
    "possible_elite_events_by_athlete",
    "prior_event_count",
    "projected_relay_points",
    "rank_athletes_by_value",
    "rank_elite_sprint_jump_athletes",
    "rank_priority_distance_athletes",
    "rank_priority_running_athletes",
    "relay_leg_candidate_map",
    "relay_leg_time_map",
    "relay_selection_time_for_build",
    "relay_time",
    "remove_athlete_events",
    "score_team_points_from_entries",
    "scrape_result_source",
    "select_opponent_entries",
    "sort_event_pool",
    "synthesize_relay",
    "synthetic_relay_credit",
    "synthetic_relay_time",
    "top_opponent_individual_entries",
    "try_add_entry",
    "try_compensated_protected_individual_replacement",
    "try_distance_individual_replacement",
    "try_distance_relay_replacement",
    "try_elite_individual_replacement",
    "try_elite_relay_replacement",
    "worst_individual_entry",
]
