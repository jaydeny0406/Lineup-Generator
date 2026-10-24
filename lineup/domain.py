"""Meet profiles, event rules, shared data models, and small value helpers."""

from __future__ import annotations

import html
import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any

INDIVIDUAL_POINTS = [10, 8, 6, 5, 4, 3, 2, 1]
RELAY_POINTS = [10, 8, 6, 4, 2]
APP_VERSION = "2026.09.13-team-limits-score-delta-v31"
MAX_EVENTS_PER_ATHLETE = 4
MAX_INDIVIDUAL_ENTRIES = 3
ELITE_ATHLETE_COUNT = 5
RUNNER_UTILIZATION_COUNT = 10
MAX_ELITE_REPLACEMENTS = 15
MAX_DISTANCE_REPLACEMENTS = 15
ELITE_UTILIZATION_POINT_TOLERANCE = 2.0

RUNNING_ORDER = {
    "4x800 relay": 1,
    "4x100 relay": 2,
    "3200m": 3,
    "110h": 4,
    "100m": 5,
    "800m": 6,
    "4x200 relay": 7,
    "400m": 8,
    "300h": 9,
    "1600m": 10,
    "200m": 11,
    "4x400 relay": 12,
}

INDOOR_RUNNING_ORDERS = {
    distance: {
        "4x800 relay": 1,
        "3200m": 2,
        f"{distance}h": 3,
        f"{distance}m": 4,
        "800m": 5,
        "4x200 relay": 6,
        "400m": 7,
        "1600m": 8,
        "200m": 9,
        "4x400 relay": 10,
    }
    for distance in ("55", "60")
}

FIELD_EVENTS = {"high jump", "pole vault", "discus", "shot put", "long jump", "triple jump"}
TRACK_EVENTS = set(RUNNING_ORDER).union(
    *(set(order) for order in INDOOR_RUNNING_ORDERS.values())
)
RELAY_EVENTS = {"4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"}
DISTANCE_EVENTS = {"4x800 relay", "800m", "1600m", "3200m"}
LONG_DISTANCE_EVENTS = {"1600m", "3200m"}
DISTANCE_THREE_EVENT_ALLOWED_EVENTS = {"4x800 relay", "800m", "400m", "4x400 relay"}
DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS = {"400m", "800m", "1600m", "3200m"}
DISTANCE_UTILIZATION_RELAY_EVENTS = {"4x800 relay", "4x400 relay"}
ELITE_INDIVIDUAL_EVENTS = {"100m", "200m", "400m"}
RUNNER_UTILIZATION_INDIVIDUAL_EVENTS = {"100m", "200m", "400m", "110h", "300h"}
SPRINT_RELAY_EVENTS = {"4x100 relay", "4x200 relay", "4x400 relay"}
DISTANCE_UTILIZATION_COUNT = 8
EVENTS = list(RUNNING_ORDER) + ["long jump", "triple jump", "high jump", "pole vault", "shot put", "discus"]
OUTDOOR_SCHEDULE_ORDER = tuple(EVENTS)
OUTDOOR_DISTANCE_ORDER = (
    "100m", "200m", "400m", "800m", "1600m", "3200m", "110h", "300h",
    "4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay",
    "shot put", "discus", "high jump", "pole vault", "long jump", "triple jump",
)
INDOOR_FIELD_ORDER = ("long jump", "triple jump", "high jump", "shot put", "pole vault")
INDOOR_SHORT_EVENTS = frozenset({"55m", "60m", "55h", "60h"})
INDOOR_DASH_CONVERSION_FACTOR = 1.071
INDOOR_HURDLE_CONVERSION_FACTOR = 1.075

RELAY_BASE_EVENT = {
    "4x100 relay": "100m",
    "4x200 relay": "200m",
    "4x400 relay": "400m",
    "4x800 relay": "800m",
}

RELAY_EVENT_FOR_BASE = {base_event: relay_event for relay_event, base_event in RELAY_BASE_EVENT.items()}

RELAY_INDIVIDUAL_EXCHANGE_CREDIT = {
    "4x100 relay": 0.7,
    "4x200 relay": 0.7,
    "4x400 relay": 0.6,
    "4x800 relay": 0.5,
}

INDIVIDUAL_LEG_SOURCE = "individual"
RELAY_SPLIT_LEG_SOURCE = "relay_split"
PROJECTED_OPPONENT_ROLE = "opponent_projected"

HISTORIC_RELAY_IMPROVEMENT = {
    "4x100 relay": 0.2,
    "4x200 relay": 0.2,
    "4x400 relay": 0.2,
    "4x800 relay": 0.0,
}


@dataclass(frozen=True)
class MeetConfig:
    season_type: str
    indoor_sprint_distance: str
    running_order: dict[str, int]
    events: tuple[str, ...]
    schedule_order: tuple[str, ...]
    distance_order: tuple[str, ...]
    relay_events: frozenset[str]
    elite_individual_events: frozenset[str]
    runner_individual_events: frozenset[str]
    sprint_relay_events: frozenset[str]


def meet_config_for(
    season_type: str = "outdoor", indoor_sprint_distance: str = "55"
) -> MeetConfig:
    """Build the active outdoor or indoor meet program."""
    season = str(season_type or "").strip().lower() or "outdoor"
    if season not in {"outdoor", "indoor"}:
        raise ValueError(f"Unknown season type: {season_type}")
    if season == "outdoor":
        return MeetConfig(
            "outdoor",
            "",
            RUNNING_ORDER,
            tuple(EVENTS),
            OUTDOOR_SCHEDULE_ORDER,
            OUTDOOR_DISTANCE_ORDER,
            frozenset(RELAY_EVENTS),
            frozenset(ELITE_INDIVIDUAL_EVENTS),
            frozenset(RUNNER_UTILIZATION_INDIVIDUAL_EVENTS),
            frozenset(SPRINT_RELAY_EVENTS),
        )
    distance = str(indoor_sprint_distance or "").strip().lower().replace("m", "")
    if distance not in {"55", "60"}:
        raise ValueError("Indoor sprint distance must be either 55m or 60m.")
    running_order = INDOOR_RUNNING_ORDERS[distance]
    schedule_order = tuple(running_order) + INDOOR_FIELD_ORDER
    distance_order = (
        f"{distance}m", "200m", "400m", "800m", "1600m", "3200m", f"{distance}h",
        "4x200 relay", "4x400 relay", "4x800 relay",
    ) + INDOOR_FIELD_ORDER
    return MeetConfig(
        "indoor",
        distance,
        running_order,
        schedule_order,
        schedule_order,
        distance_order,
        frozenset({"4x800 relay", "4x200 relay", "4x400 relay"}),
        frozenset({f"{distance}m", "200m", "400m"}),
        frozenset({f"{distance}m", "200m", "400m", f"{distance}h"}),
        frozenset({"4x200 relay", "4x400 relay"}),
    )


OUTDOOR_MEET_CONFIG = meet_config_for()
ACTIVE_MEET_CONFIG: ContextVar[MeetConfig] = ContextVar(
    "active_meet_config", default=OUTDOOR_MEET_CONFIG
)


def current_meet_config() -> MeetConfig:
    """Return the meet profile active for this request or test."""
    return ACTIVE_MEET_CONFIG.get()


def active_events() -> tuple[str, ...]:
    return current_meet_config().events


def active_relay_events() -> frozenset[str]:
    return current_meet_config().relay_events


def active_elite_individual_events() -> frozenset[str]:
    return current_meet_config().elite_individual_events


def active_runner_individual_events() -> frozenset[str]:
    return current_meet_config().runner_individual_events


def active_sprint_relay_events() -> frozenset[str]:
    return current_meet_config().sprint_relay_events


@dataclass(frozen=True)
class Performance:
    athlete: str
    event: str
    mark: str
    value: float
    is_time: bool
    source: str
    team_role: str


@dataclass(frozen=True)
class RelayPerformance:
    event: str
    athletes: tuple[str, str, str, str]
    mark: str
    value: float
    source: str
    team_role: str
    splits: tuple[float | None, float | None, float | None, float | None] = (None, None, None, None)
    method: str = "historic"


@dataclass(frozen=True)
class RelaySelection:
    event: str
    athletes: tuple[str, str, str, str]
    projected_time: float
    method: str
    source_mark: str
    leg_times: tuple[float, float, float, float] | None = None
    leg_sources: tuple[str, str, str, str] | None = None


@dataclass
class ScrapeResult:
    performances: list[Performance]
    relay_history: list[RelayPerformance]
    relay_splits: list[Performance] = field(default_factory=list)


@dataclass
class EventProjection:
    event: str
    entries: list[dict[str, Any]]
    projected_points: float


@dataclass
class LineupResult:
    lineup: dict[str, list[dict[str, Any]]]
    relays: dict[str, dict[str, Any]]
    event_points: dict[str, float]
    total_points: float
    scraped: dict[str, Any]
    errors: list[str]
    event_standings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    edit_context: dict[str, Any] = field(default_factory=dict)
    team_points: dict[str, float] = field(default_factory=dict)


@dataclass
class TeamMeetProjection:
    source: str
    lineup: dict[str, list[str]]
    relays: dict[str, RelaySelection]
    entries: list[Performance]
    relay_entries: list[Performance]


@dataclass
class EliteReplacement:
    lineup: dict[str, list[str]]
    relays: dict[str, RelaySelection]
    event: str
    total_delta: float
    event_delta: float
    speed_delta: float

def clean_text(value: str) -> str:
    """Normalize whitespace and HTML entities."""
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()

def is_better(candidate: float, incumbent: float, is_time: bool) -> bool:
    """Compare times low-to-high and field marks high-to-low."""
    return candidate < incumbent if is_time else candidate > incumbent

def event_sort_value(event: str) -> float:
    """Sort running events by meet order and field events after them."""
    running_order = current_meet_config().running_order
    if event in running_order:
        return float(running_order[event])
    field_order = {"high jump": 20, "pole vault": 21, "discus": 22, "shot put": 23, "long jump": 24, "triple jump": 25}
    return float(field_order.get(event, 99))

def format_time(seconds: float) -> str:
    """Format seconds as a sprint or distance time."""
    if seconds >= 60:
        minutes = int(seconds // 60)
        rest = seconds - minutes * 60
        return f"{minutes}:{rest:05.2f}"
    return f"{seconds:.2f}"

def normalize_athlete_name(name: str) -> str:
    """Normalize an athlete name for case-insensitive injury matching."""
    return re.sub(r"[^a-z0-9]+", " ", clean_text(name).lower()).strip()

def athlete_max_events(
    athlete: str, athlete_event_limits: dict[str, int] | None = None
) -> int:
    """Return one athlete's coach-set maximum, defaulting to the meet maximum."""
    return (athlete_event_limits or {}).get(
        normalize_athlete_name(athlete), MAX_EVENTS_PER_ATHLETE
    )


__all__ = [
    "ACTIVE_MEET_CONFIG",
    "APP_VERSION",
    "DISTANCE_EVENTS",
    "DISTANCE_THREE_EVENT_ALLOWED_EVENTS",
    "DISTANCE_UTILIZATION_COUNT",
    "DISTANCE_UTILIZATION_INDIVIDUAL_EVENTS",
    "DISTANCE_UTILIZATION_RELAY_EVENTS",
    "ELITE_ATHLETE_COUNT",
    "ELITE_INDIVIDUAL_EVENTS",
    "ELITE_UTILIZATION_POINT_TOLERANCE",
    "EVENTS",
    "EliteReplacement",
    "EventProjection",
    "FIELD_EVENTS",
    "HISTORIC_RELAY_IMPROVEMENT",
    "INDIVIDUAL_LEG_SOURCE",
    "INDIVIDUAL_POINTS",
    "INDOOR_DASH_CONVERSION_FACTOR",
    "INDOOR_FIELD_ORDER",
    "INDOOR_HURDLE_CONVERSION_FACTOR",
    "INDOOR_RUNNING_ORDERS",
    "INDOOR_SHORT_EVENTS",
    "LONG_DISTANCE_EVENTS",
    "LineupResult",
    "MAX_DISTANCE_REPLACEMENTS",
    "MAX_ELITE_REPLACEMENTS",
    "MAX_EVENTS_PER_ATHLETE",
    "MAX_INDIVIDUAL_ENTRIES",
    "MeetConfig",
    "OUTDOOR_DISTANCE_ORDER",
    "OUTDOOR_MEET_CONFIG",
    "OUTDOOR_SCHEDULE_ORDER",
    "PROJECTED_OPPONENT_ROLE",
    "Performance",
    "RELAY_BASE_EVENT",
    "RELAY_EVENTS",
    "RELAY_EVENT_FOR_BASE",
    "RELAY_INDIVIDUAL_EXCHANGE_CREDIT",
    "RELAY_POINTS",
    "RELAY_SPLIT_LEG_SOURCE",
    "RUNNER_UTILIZATION_COUNT",
    "RUNNER_UTILIZATION_INDIVIDUAL_EVENTS",
    "RUNNING_ORDER",
    "RelayPerformance",
    "RelaySelection",
    "SPRINT_RELAY_EVENTS",
    "ScrapeResult",
    "TRACK_EVENTS",
    "TeamMeetProjection",
    "active_elite_individual_events",
    "active_events",
    "active_relay_events",
    "active_runner_individual_events",
    "active_sprint_relay_events",
    "athlete_max_events",
    "clean_text",
    "current_meet_config",
    "event_sort_value",
    "format_time",
    "is_better",
    "meet_config_for",
    "normalize_athlete_name",
]
