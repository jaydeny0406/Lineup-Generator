"""Validate requests, prepare teams, and orchestrate each division."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .domain import (
    ACTIVE_MEET_CONFIG,
    LineupResult,
    MAX_EVENTS_PER_ATHLETE,
    MeetConfig,
    PROJECTED_OPPONENT_ROLE,
    Performance,
    RelayPerformance,
    RelaySelection,
    ScrapeResult,
    athlete_max_events,
    clean_text,
    current_meet_config,
    meet_config_for,
    normalize_athlete_name,
)
from .edits import (
    attach_edit_context,
)
from .optimizer import (
    build_independent_team_projection,
    build_lineup,
    collect_athlete_events,
    fastest_historic_relay_entries,
    scrape_result_source,
    top_opponent_individual_entries,
)
from .records import (
    parse_mark,
    prepare_scrape_result_for_meet,
    scrape_team_data,
    validate_meet_urls,
)
from .scoring import (
    evaluate_lineup,
    title_event,
)

def normalize_athlete_event_limits(raw_limits: Any) -> dict[str, int]:
    """Normalize coach-entered athlete limits into a case-insensitive lookup."""
    if not raw_limits:
        return {}
    rows = (
        [{"athlete": name, "maxEvents": limit} for name, limit in raw_limits.items()]
        if isinstance(raw_limits, dict)
        else raw_limits
    )
    normalized: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        athlete = clean_text(row.get("athlete", row.get("name", "")))
        key = normalize_athlete_name(athlete)
        if not key:
            continue
        raw_limit = row.get("maxEvents", row.get("max_events", row.get("limit")))
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Choose a valid event limit for {athlete}.") from exc
        if not 1 <= limit <= MAX_EVENTS_PER_ATHLETE:
            raise ValueError(
                f"{athlete}'s event limit must be between 1 and {MAX_EVENTS_PER_ATHLETE}."
            )
        normalized[key] = min(limit, normalized.get(key, MAX_EVENTS_PER_ATHLETE))
    return normalized


def normalize_team_event_limit(raw_limit: Any) -> int:
    """Normalize the school-wide event cap, with four events as the standard default."""
    if raw_limit is None or raw_limit == "":
        return MAX_EVENTS_PER_ATHLETE
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Choose a valid team-wide event limit.") from exc
    if limit not in {2, 3, MAX_EVENTS_PER_ATHLETE}:
        raise ValueError("The team-wide event limit must be 2, 3, or 4 events.")
    return limit


def apply_team_event_limit_to_school(
    data: ScrapeResult,
    athlete_event_limits: dict[str, int] | None,
    team_event_limit: Any,
) -> dict[str, int]:
    """Apply a global cap only to athletes contained in the user's school data."""
    limit = normalize_team_event_limit(team_event_limit)
    effective_limits = dict(athlete_event_limits or {})
    if limit == MAX_EVENTS_PER_ATHLETE:
        return effective_limits
    athlete_names = {perf.athlete for perf in data.performances}
    athlete_names.update(split.athlete for split in data.relay_splits)
    athlete_names.update(athlete for relay in data.relay_history for athlete in relay.athletes)
    for athlete in athlete_names:
        key = normalize_athlete_name(athlete)
        if key:
            effective_limits[key] = min(limit, effective_limits.get(key, MAX_EVENTS_PER_ATHLETE))
    return effective_limits




def event_limit_violations(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    athlete_event_limits: dict[str, int] | None = None,
) -> list[str]:
    """Describe any athletes whose generated or edited assignment exceeds a manual cap."""
    athlete_events = collect_athlete_events(lineup, relays)
    return [
        f"{athlete} has {len(events)} events (maximum {athlete_max_events(athlete, athlete_event_limits)})"
        for athlete, events in sorted(athlete_events.items())
        if len(events) > athlete_max_events(athlete, athlete_event_limits)
    ]


def filter_injured_athletes(data: ScrapeResult, injured_athletes: list[str]) -> ScrapeResult:
    """Remove injured athletes from individual marks, relay splits, and historic relays."""
    injured = {
        normalize_athlete_name(name)
        for name in injured_athletes
        if normalize_athlete_name(name)
    }
    if not injured:
        return data
    performances = [
        perf for perf in data.performances if normalize_athlete_name(perf.athlete) not in injured
    ]
    relay_splits = [
        split for split in data.relay_splits if normalize_athlete_name(split.athlete) not in injured
    ]
    relay_history = [
        relay
        for relay in data.relay_history
        if all(normalize_athlete_name(athlete) not in injured for athlete in relay.athletes)
    ]
    return ScrapeResult(performances, relay_history, relay_splits)


def run_optimizer(
    school_url: str,
    opponent_urls: list[str],
    gender: str = "mens",
    injured_athletes: list[str] | None = None,
    athlete_event_limits: Any = None,
    season_type: str = "outdoor",
    indoor_sprint_distance: str = "55",
    team_event_limit: Any = MAX_EVENTS_PER_ATHLETE,
    *, scrape_team_data_fn=None,
) -> LineupResult:
    """Activate a meet profile, validate its links, and run the optimizer."""
    meet_config = meet_config_for(season_type, indoor_sprint_distance)
    normalized_team_event_limit = normalize_team_event_limit(team_event_limit)
    token = ACTIVE_MEET_CONFIG.set(meet_config)
    try:
        entered_urls = [school_url, *opponent_urls]
        if any("athletic.net" in url.lower() for url in entered_urls):
            url_errors = validate_meet_urls(school_url, opponent_urls, meet_config.season_type)
            if url_errors:
                return LineupResult(
                    {},
                    {},
                    {},
                    0.0,
                    {
                        "school_records": 0,
                        "opponent_records": 0,
                        "season_type": meet_config.season_type,
                        "indoor_sprint_distance": meet_config.indoor_sprint_distance,
                    },
                    url_errors,
                )
        return _run_optimizer_active(
            school_url,
            opponent_urls,
            gender,
            injured_athletes,
            athlete_event_limits,
            normalized_team_event_limit,
            scrape_team_data_fn=scrape_team_data_fn,
        )
    finally:
        ACTIVE_MEET_CONFIG.reset(token)


def _run_optimizer_active(
    school_url: str,
    opponent_urls: list[str],
    gender: str = "mens",
    injured_athletes: list[str] | None = None,
    athlete_event_limits: Any = None,
    team_event_limit: Any = MAX_EVENTS_PER_ATHLETE,
    *, scrape_team_data_fn=None,
) -> LineupResult:
    """Scrape inputs, build a lineup, and evaluate the final projection."""
    scrape = scrape_team_data_fn or scrape_team_data
    errors: list[str] = []
    school: list[Performance] = []
    raw_opponents: list[Performance] = []
    competition_opponents: list[Performance] = []
    opponent_relay_entries: list[Performance] = []
    school_relay_history: list[RelayPerformance] = []
    school_relay_splits: list[Performance] = []
    opponent_results: list[tuple[int, ScrapeResult]] = []
    normalized_event_limits = normalize_athlete_event_limits(athlete_event_limits)
    normalized_team_event_limit = normalize_team_event_limit(team_event_limit)
    effective_event_limits = dict(normalized_event_limits)
    try:
        school_result = scrape(school_url, "school", "Your Team", gender)
        school_result = filter_injured_athletes(school_result, injured_athletes or [])
        school_result = prepare_scrape_result_for_meet(school_result, current_meet_config())
        school = school_result.performances
        school_relay_history = school_result.relay_history
        school_relay_splits = school_result.relay_splits
        effective_event_limits = apply_team_event_limit_to_school(
            school_result,
            normalized_event_limits,
            normalized_team_event_limit,
        )
    except Exception as exc:
        errors.append(str(exc))
    for index, url in enumerate(opponent_urls, start=1):
        if not url.strip():
            continue
        try:
            opponent_result = scrape(url, "opponent", f"Opponent {index}", gender)
            opponent_result = filter_injured_athletes(opponent_result, injured_athletes or [])
            opponent_result = prepare_scrape_result_for_meet(
                opponent_result, current_meet_config()
            )
            raw_opponents.extend(opponent_result.performances)
            opponent_results.append((index, opponent_result))
        except Exception as exc:
            errors.append(str(exc))
    if not school:
        return LineupResult(
            {},
            {},
            {},
            0.0,
            {
                "school_records": 0,
                "opponent_records": len(raw_opponents),
                "season_type": current_meet_config().season_type,
                "indoor_sprint_distance": current_meet_config().indoor_sprint_distance,
                "team_event_limit": normalized_team_event_limit,
            },
            errors,
        )

    opponent_teams: list[str] = []
    for index, opponent_result in opponent_results:
        source = scrape_result_source(opponent_result, f"Opponent {index}")
        opponent_teams.append(source)
        individual_entries = top_opponent_individual_entries(opponent_result, source)
        relay_entries = fastest_historic_relay_entries(
            opponent_result.relay_history,
            source,
            PROJECTED_OPPONENT_ROLE,
        )
        competition_opponents.extend(individual_entries)
        competition_opponents.extend(relay_entries)
        opponent_relay_entries.extend(relay_entries)

    raw_lineup = build_lineup(
        school,
        competition_opponents,
        school_relay_history,
        [],
        school_relay_splits,
        [],
        effective_event_limits,
    )
    violations = event_limit_violations(
        raw_lineup["lineup"], raw_lineup["relays"], effective_event_limits
    )
    if violations:
        errors.append(
            "The lineup was not published because an athlete event limit was exceeded: "
            + "; ".join(violations)
        )
        return LineupResult(
            {},
            {},
            {},
            0.0,
            {
                "school_records": len(school),
                "opponent_records": len(raw_opponents),
                "season_type": current_meet_config().season_type,
                "indoor_sprint_distance": current_meet_config().indoor_sprint_distance,
                "team_event_limit": normalized_team_event_limit,
            },
            errors,
        )
    result = evaluate_lineup(
        raw_lineup["lineup"],
        raw_lineup["relays"],
        school,
        competition_opponents,
        [],
        [],
        opponent_relay_entries,
    )
    result.scraped = {
        "school_records": len(school),
        "opponent_records": len(raw_opponents),
        "school_name": school[0].source if school else "Your Team",
        "opponent_teams": opponent_teams,
        "season_type": current_meet_config().season_type,
        "indoor_sprint_distance": current_meet_config().indoor_sprint_distance,
        "team_event_limit": normalized_team_event_limit,
    }
    missing_events = raw_lineup.get("missing_events", [])
    if missing_events:
        errors.append(
            "No eligible recorded athletes were available for: "
            + ", ".join(title_event(event) for event in missing_events)
        )
    result.errors = errors
    return attach_edit_context(
        result,
        school,
        competition_opponents,
        school_relay_history,
        [],
        school_relay_splits,
        [],
        effective_event_limits,
        normalized_team_event_limit,
    )


def run_optimizer_both(
    school_url: str,
    opponent_urls: list[str],
    injured_athletes: list[str] | None = None,
    athlete_event_limits: Any = None,
    season_type: str = "outdoor",
    indoor_sprint_distance: str = "55",
    team_event_limit: Any = MAX_EVENTS_PER_ATHLETE,
    *, optimize_fn=None,
) -> dict[str, Any]:
    """Generate independent men's and women's lineups without combining their athlete pools."""
    optimize = optimize_fn or run_optimizer
    return {
        "mode": "both",
        "division_results": {
            "mens": asdict(
                optimize(
                    school_url,
                    opponent_urls,
                    "mens",
                    injured_athletes,
                    athlete_event_limits,
                    season_type,
                    indoor_sprint_distance,
                    team_event_limit,
                )
            ),
            "womens": asdict(
                optimize(
                    school_url,
                    opponent_urls,
                    "womens",
                    injured_athletes,
                    athlete_event_limits,
                    season_type,
                    indoor_sprint_distance,
                    team_event_limit,
                )
            ),
        },
    }


def demo_result(
    athlete_event_limits: Any = None,
    season_type: str = "outdoor",
    indoor_sprint_distance: str = "55",
) -> LineupResult:
    """Run the optimizer on built-in sample marks for the selected meet profile."""
    meet_config = meet_config_for(season_type, indoor_sprint_distance)
    token = ACTIVE_MEET_CONFIG.set(meet_config)
    try:
        return _demo_result_active(athlete_event_limits)
    finally:
        ACTIVE_MEET_CONFIG.reset(token)


def _demo_result_active(athlete_event_limits: Any = None) -> LineupResult:
    """Build a demo lineup with the requested meet profile already active."""
    normalized_event_limits = normalize_athlete_event_limits(athlete_event_limits)
    school, opponents, school_relays, opponent_relays = sample_data_for_meet(
        current_meet_config()
    )
    opponent_projection = build_independent_team_projection(
        ScrapeResult(opponents, opponent_relays, []),
        "Opponent A",
        PROJECTED_OPPONENT_ROLE,
    )
    competition_opponents = opponent_projection.entries + opponent_projection.relay_entries
    raw_lineup = build_lineup(
        school,
        competition_opponents,
        school_relays,
        [],
        athlete_event_limits=normalized_event_limits,
    )
    result = evaluate_lineup(
        raw_lineup["lineup"],
        raw_lineup["relays"],
        school,
        competition_opponents,
        [],
        [],
        opponent_projection.relay_entries,
    )
    result.scraped = {
        "school_records": len(school),
        "opponent_records": len(opponents),
        "school_name": "Your Team",
        "opponent_teams": ["Opponent A"],
        "season_type": current_meet_config().season_type,
        "indoor_sprint_distance": current_meet_config().indoor_sprint_distance,
    }
    return attach_edit_context(
        result,
        school,
        competition_opponents,
        school_relays,
        [],
        athlete_event_limits=normalized_event_limits,
    )


def sample_data() -> tuple[list[Performance], list[Performance], list[RelayPerformance], list[RelayPerformance]]:
    """Provide deterministic data so the app can be tested without a network request."""
    rows = [
        ("school", "Your Team", "Alex Carter", "100m", "10.92"),
        ("school", "Your Team", "Alex Carter", "200m", "22.30"),
        ("school", "Your Team", "Malik Reed", "100m", "11.05"),
        ("school", "Your Team", "Malik Reed", "400m", "50.40"),
        ("school", "Your Team", "Noah Smith", "800m", "1:59.20"),
        ("school", "Your Team", "Noah Smith", "1600m", "4:29.40"),
        ("school", "Your Team", "Drew Hayes", "3200m", "9:53.00"),
        ("school", "Your Team", "Drew Hayes", "1600m", "4:34.00"),
        ("school", "Your Team", "Evan Kim", "110h", "15.12"),
        ("school", "Your Team", "Evan Kim", "300h", "40.88"),
        ("school", "Your Team", "Jalen Brooks", "long jump", "21' 4"),
        ("school", "Your Team", "Jalen Brooks", "triple jump", "42' 2"),
        ("school", "Your Team", "Sam Lee", "shot put", "48' 8"),
        ("school", "Your Team", "Sam Lee", "discus", "139' 6"),
        ("school", "Your Team", "Cole Diaz", "400m", "51.10"),
        ("school", "Your Team", "Cole Diaz", "200m", "22.80"),
        ("opponent", "Opponent A", "Ryan West", "100m", "10.98"),
        ("opponent", "Opponent A", "Ike Torres", "200m", "22.10"),
        ("opponent", "Opponent A", "Paul Green", "400m", "49.90"),
        ("opponent", "Opponent A", "Miles King", "800m", "1:58.80"),
        ("opponent", "Opponent A", "Owen Fox", "1600m", "4:27.00"),
        ("opponent", "Opponent A", "Liam Ray", "3200m", "9:44.00"),
        ("opponent", "Opponent A", "Trey Hill", "110h", "15.30"),
        ("opponent", "Opponent A", "Trey Hill", "300h", "40.20"),
        ("opponent", "Opponent A", "Max Stone", "long jump", "20' 10"),
        ("opponent", "Opponent A", "Max Stone", "triple jump", "43' 0"),
        ("opponent", "Opponent A", "Ben North", "shot put", "50' 1"),
        ("opponent", "Opponent A", "Ben North", "discus", "145' 4"),
    ]
    perfs = []
    for role, source, athlete, event, mark in rows:
        parsed = parse_mark(mark, event)
        if parsed:
            perfs.append(Performance(athlete, event, mark, parsed[0], parsed[1], source, role))
    school_relays = [
        RelayPerformance(
            "4x100 relay",
            ("Alex Carter", "Malik Reed", "Cole Diaz", "Evan Kim"),
            "43.60",
            43.60,
            "Your Team",
            "school",
        )
    ]
    opponent_relays = [
        RelayPerformance(
            "4x100 relay",
            ("Ryan West", "Ike Torres", "Paul Green", "Trey Hill"),
            "43.80",
            43.80,
            "Opponent A",
            "opponent",
        )
    ]
    return (
        [perf for perf in perfs if perf.team_role == "school"],
        [perf for perf in perfs if perf.team_role == "opponent"],
        school_relays,
        opponent_relays,
    )


def sample_data_for_meet(
    meet_config: MeetConfig,
) -> tuple[list[Performance], list[Performance], list[RelayPerformance], list[RelayPerformance]]:
    """Adapt the offline demo data to the selected outdoor or indoor program."""
    school, opponents, school_relays, opponent_relays = sample_data()
    if meet_config.season_type == "outdoor":
        return school, opponents, school_relays, opponent_relays

    supplemental_rows = [
        ("school", "Your Team", "Alex Carter", "55m", "6.55"),
        ("school", "Your Team", "Malik Reed", "55m", "6.63"),
        ("school", "Your Team", "Cole Diaz", "55m", "6.78"),
        ("school", "Your Team", "Evan Kim", "55m", "6.88"),
        ("school", "Your Team", "Evan Kim", "55h", "7.98"),
        ("school", "Your Team", "Jalen Brooks", "55h", "8.15"),
        ("school", "Your Team", "Malik Reed", "200m", "22.70"),
        ("school", "Your Team", "Evan Kim", "200m", "23.10"),
        ("school", "Your Team", "Alex Carter", "400m", "49.80"),
        ("school", "Your Team", "Evan Kim", "400m", "52.40"),
        ("school", "Your Team", "Drew Hayes", "800m", "2:04.00"),
        ("school", "Your Team", "Cole Diaz", "800m", "2:05.50"),
        ("school", "Your Team", "Jalen Brooks", "800m", "2:08.00"),
        ("opponent", "Opponent A", "Ryan West", "55m", "6.58"),
        ("opponent", "Opponent A", "Ike Torres", "55m", "6.65"),
        ("opponent", "Opponent A", "Paul Green", "55m", "6.73"),
        ("opponent", "Opponent A", "Trey Hill", "55m", "6.85"),
        ("opponent", "Opponent A", "Trey Hill", "55h", "8.05"),
        ("opponent", "Opponent A", "Max Stone", "55h", "8.20"),
        ("opponent", "Opponent A", "Ryan West", "200m", "22.55"),
        ("opponent", "Opponent A", "Paul Green", "200m", "22.75"),
        ("opponent", "Opponent A", "Trey Hill", "200m", "23.05"),
        ("opponent", "Opponent A", "Ryan West", "400m", "50.20"),
        ("opponent", "Opponent A", "Ike Torres", "400m", "50.80"),
        ("opponent", "Opponent A", "Trey Hill", "400m", "52.10"),
        ("opponent", "Opponent A", "Owen Fox", "800m", "2:03.00"),
        ("opponent", "Opponent A", "Liam Ray", "800m", "2:04.00"),
        ("opponent", "Opponent A", "Max Stone", "800m", "2:07.00"),
    ]
    supplemental: list[Performance] = []
    for role, source, athlete, event, mark in supplemental_rows:
        parsed = parse_mark(mark, event)
        if parsed:
            supplemental.append(
                Performance(athlete, event, mark, parsed[0], parsed[1], source, role)
            )

    school_indoor_relays = [
        RelayPerformance(
            "4x200 relay",
            ("Alex Carter", "Malik Reed", "Cole Diaz", "Evan Kim"),
            "1:31.20",
            91.2,
            "Your Team",
            "school",
        ),
        RelayPerformance(
            "4x400 relay",
            ("Alex Carter", "Malik Reed", "Cole Diaz", "Evan Kim"),
            "3:25.00",
            205.0,
            "Your Team",
            "school",
        ),
        RelayPerformance(
            "4x800 relay",
            ("Noah Smith", "Drew Hayes", "Cole Diaz", "Jalen Brooks"),
            "8:18.00",
            498.0,
            "Your Team",
            "school",
        ),
    ]
    opponent_indoor_relays = [
        RelayPerformance(
            "4x200 relay",
            ("Ryan West", "Ike Torres", "Paul Green", "Trey Hill"),
            "1:30.80",
            90.8,
            "Opponent A",
            "opponent",
        ),
        RelayPerformance(
            "4x400 relay",
            ("Ryan West", "Ike Torres", "Paul Green", "Trey Hill"),
            "3:24.00",
            204.0,
            "Opponent A",
            "opponent",
        ),
        RelayPerformance(
            "4x800 relay",
            ("Miles King", "Owen Fox", "Liam Ray", "Max Stone"),
            "8:14.00",
            494.0,
            "Opponent A",
            "opponent",
        ),
    ]
    school_result = prepare_scrape_result_for_meet(
        ScrapeResult(
            school + [perf for perf in supplemental if perf.team_role == "school"],
            school_indoor_relays,
            [],
        ),
        meet_config,
    )
    opponent_result = prepare_scrape_result_for_meet(
        ScrapeResult(
            opponents + [perf for perf in supplemental if perf.team_role == "opponent"],
            opponent_indoor_relays,
            [],
        ),
        meet_config,
    )
    return (
        school_result.performances,
        opponent_result.performances,
        school_result.relay_history,
        opponent_result.relay_history,
    )


__all__ = [
    "apply_team_event_limit_to_school",
    "demo_result",
    "event_limit_violations",
    "filter_injured_athletes",
    "normalize_athlete_event_limits",
    "normalize_team_event_limit",
    "run_optimizer",
    "run_optimizer_both",
    "sample_data",
    "sample_data_for_meet",
]
