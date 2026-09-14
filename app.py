from __future__ import annotations

import html
import json
import math
import os
import re
import sys
import traceback
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import combinations
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen


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


def fetch_html(url: str) -> str:
    """Download a page, falling back to reader text when Athletic.net blocks static HTML."""
    url = normalize_athletic_url(url)
    direct_error: Exception | None = None
    try:
        page = fetch_text_url(url)
        if is_cloudflare_challenge(page):
            direct_error = RuntimeError("Athletic.net returned a JavaScript challenge")
        else:
            return page
    except Exception as exc:
        direct_error = exc

    if is_athletic_url(url):
        reader_errors = []
        for reader_url in athletic_reader_urls(url):
            try:
                reader_page = fetch_text_url(reader_url)
                if reader_page and not is_cloudflare_challenge(reader_page):
                    return reader_page
            except Exception as reader_exc:
                reader_errors.append(str(reader_exc))
        raise RuntimeError(
            f"Could not fetch Athletic.net page {url}. Direct request failed with: {direct_error}. "
            f"Reader fallbacks also failed: {'; '.join(reader_errors)}"
        )
    raise RuntimeError(str(direct_error))


def normalize_athletic_url(url: str) -> str:
    """Return a canonical Athletic.net URL, unwrapping Reader URLs without losing the path."""
    value = html.unescape(clean_text(url)).strip().strip("\"'")
    reader_prefixes = (
        "https://r.jina.ai/http://",
        "https://r.jina.ai/https://",
        "http://r.jina.ai/http://",
        "http://r.jina.ai/https://",
    )
    lowered = value.lower()
    for prefix in reader_prefixes:
        if lowered.startswith(prefix):
            target_scheme = "https://" if "/https://" in prefix else "http://"
            value = target_scheme + value[len(prefix) :]
            break
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value.lstrip("/")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host == "athletic.net":
        host = "www.athletic.net"
    if not host.endswith("athletic.net"):
        return value
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    return urlunparse(("https", host, path, "", "", ""))


def is_athletic_url(url: str) -> bool:
    """Return true when a URL belongs to Athletic.net."""
    host = (urlparse(url).hostname or "").lower()
    return host == "athletic.net" or host.endswith(".athletic.net")


def athletic_reader_urls(url: str) -> list[str]:
    """Build non-nested Reader URL variants from one canonical Athletic.net URL."""
    canonical = normalize_athletic_url(url)
    http_target = "http://" + canonical.split("://", 1)[1]
    return [f"https://r.jina.ai/{canonical}", f"https://r.jina.ai/{http_target}"]


def parse_athletic_team_url(url: str) -> tuple[str, int, str]:
    """Extract team ID, API season ID, and canonical URL from an event-records link."""
    canonical = normalize_athletic_url(url)
    match = re.search(
        r"/team/(\d+)/track-and-field-(outdoor|indoor)/(\d{4})(?:/event-records)?(?:/|$)",
        urlparse(canonical).path,
        flags=re.I,
    )
    if not match:
        raise RuntimeError(
            "Use an Athletic.net event-records URL like "
            "https://www.athletic.net/team/16546/track-and-field-outdoor/2026/event-records"
        )
    team_id, season_type, year = match.groups()
    season_id = int(year) + (10000 if season_type.lower() == "indoor" else 0)
    if not urlparse(canonical).path.rstrip("/").endswith("/event-records"):
        canonical = canonical.rstrip("/") + "/event-records"
    return team_id, season_id, canonical


def athletic_url_season_type(url: str) -> str:
    """Return whether an Athletic.net team URL points to an indoor or outdoor season."""
    _team_id, season_id, _canonical = parse_athletic_team_url(url)
    return "indoor" if season_id >= 10000 else "outdoor"


def validate_meet_urls(
    school_url: str, opponent_urls: list[str], expected_season: str
) -> list[str]:
    """Require every entered Athletic.net link to match the selected season type."""
    errors: list[str] = []
    labeled_urls = [("School", school_url)] + [
        (f"Opponent {index}", url)
        for index, url in enumerate(opponent_urls, start=1)
        if url.strip()
    ]
    for label, url in labeled_urls:
        try:
            actual_season = athletic_url_season_type(url)
        except Exception as exc:
            errors.append(f"{label} URL is invalid: {exc}")
            continue
        if actual_season != expected_season:
            errors.append(
                f"{label} URL is for the {actual_season} season, but {expected_season.title()} is selected."
            )
    return errors


def athletic_api_url(team_id: str, season_id: int) -> str:
    """Build Athletic.net's first-party event-records API URL."""
    return (
        "https://www.athletic.net/api/v1/TeamHome/GetTeamEventRecords"
        f"?teamId={team_id}&seasonId={season_id}"
    )


def fetch_text_url(url: str) -> str:
    """Fetch a URL and return decoded text."""
    req = Request(
        url.strip(),
        headers={
            "User-Agent": "Mozilla/5.0 track-lineup-optimizer/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(req, timeout=20) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"Could not fetch {url}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not fetch {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"Timed out fetching {url}") from exc


def is_cloudflare_challenge(page: str) -> bool:
    """Detect challenge pages that contain no usable event records."""
    lowered = page.lower()
    return "enable javascript and cookies to continue" in lowered or "cf_chl_opt" in lowered


def scrape_data(
    url: str, team_role: str = "school", source: str | None = None, gender: str = "mens"
) -> list[Performance]:
    """Scrape athlete, event, and mark records from an Athletic.net event-records page."""
    return scrape_team_data(url, team_role, source, gender).performances


def scrape_team_data(
    url: str, team_role: str = "school", source: str | None = None, gender: str = "mens"
) -> ScrapeResult:
    """Scrape individual records and historic relay teams from an event-records page."""
    team_id, season_id, canonical_url = parse_athletic_team_url(url)
    source_name = resolve_athletic_team_name(canonical_url) or source or canonical_url
    errors: list[str] = []

    try:
        payload = json.loads(fetch_text_url(athletic_api_url(team_id, season_id)))
        result = parse_athletic_api_data(payload, source_name, team_role, gender)
        if result.performances:
            return result
        errors.append("Athletic.net API returned no matching individual event records.")
    except Exception as exc:
        errors.append(f"Athletic.net API failed: {exc}")

    page_candidates: list[str] = []
    try:
        page_candidates.append(fetch_text_url(canonical_url))
    except Exception as exc:
        errors.append(f"Direct page failed: {exc}")
    for reader_url in athletic_reader_urls(canonical_url):
        try:
            page_candidates.append(fetch_text_url(reader_url))
        except Exception as exc:
            errors.append(f"Reader fallback failed: {exc}")

    for page in page_candidates:
        page_source = extract_team_name(page) or source or canonical_url
        performances, relay_history = parse_athletic_records_html(page, page_source, team_role, gender)
        if performances:
            return ScrapeResult(performances, relay_history)

    raise RuntimeError(
        f"No event records could be loaded for {canonical_url}. " + " ".join(errors)
    )


def resolve_athletic_team_name(canonical_url: str) -> str | None:
    """Fetch the Athletic.net team page title so standings show real school names."""
    base_url = re.sub(r"/event-records/?$", "", canonical_url)
    candidate_urls = [canonical_url]
    if base_url != canonical_url:
        candidate_urls.append(base_url)
    for url in candidate_urls:
        try:
            name = extract_team_name(fetch_text_url(url))
        except Exception:
            name = None
        if name:
            return name
    for url in candidate_urls:
        for reader_url in athletic_reader_urls(url):
            try:
                name = extract_team_name(fetch_text_url(reader_url))
            except Exception:
                name = None
            if name:
                return name
    return None


def parse_athletic_api_data(
    payload: dict[str, Any], source: str, team_role: str, gender: str = "mens"
) -> ScrapeResult:
    """Parse Athletic.net's first-party event-records JSON response."""
    source_name = extract_athletic_api_team_name(payload) or source
    records = payload.get("eventRecords")
    relay_members = payload.get("relayMembers")
    if not isinstance(records, list):
        raise RuntimeError("Athletic.net API response did not contain an eventRecords list.")
    if not isinstance(relay_members, list):
        relay_members = []

    wanted_gender = {"mens": "M", "womens": "F"}.get((gender or "").lower())
    performances: list[Performance] = []
    relays: list[RelayPerformance] = []
    relay_splits: list[Performance] = []
    members_by_result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for member in relay_members:
        if isinstance(member, dict) and member.get("IDResult") is not None:
            members_by_result[int(member["IDResult"])].append(member)

    for record in records:
        if not isinstance(record, dict):
            continue
        record_gender = clean_text(str(record.get("Gender") or "")).upper()
        if wanted_gender and record_gender != wanted_gender:
            continue
        raw_event = clean_text(str(record.get("Event") or ""))
        event_description = clean_text(str(record.get("Description") or ""))
        split_event = relay_split_base_event(f"{raw_event} {event_description}")
        event = split_event or detect_event(raw_event)
        if not event:
            continue
        mark = clean_text(str(record.get("Result") or ""))
        parsed = parse_mark(mark, event)
        if not parsed:
            continue

        if split_event:
            athlete = api_athlete_name(record)
            if not athlete:
                continue
            relay_splits.append(
                Performance(
                    athlete=athlete,
                    event=split_event,
                    mark=mark,
                    value=parsed[0],
                    is_time=True,
                    source=source_name,
                    team_role=team_role,
                )
            )
            continue

        if bool(record.get("PersonalEvent")) and event not in RELAY_EVENTS:
            athlete = api_athlete_name(record)
            if not athlete:
                continue
            performances.append(
                Performance(
                    athlete=athlete,
                    event=event,
                    mark=mark,
                    value=parsed[0],
                    is_time=parsed[1],
                    source=source_name,
                    team_role=team_role,
                )
            )
            continue

        if event not in RELAY_EVENTS:
            continue
        result_id = record.get("IDResult")
        members = members_by_result.get(int(result_id), []) if result_id is not None else []
        members = sorted(members, key=lambda member: int(member.get("SortID") or 99))
        athlete_names = [
            clean_text(str(member.get("Name") or ""))
            for member in members
            if clean_text(str(member.get("Name") or ""))
        ]
        if len(athlete_names) < 4:
            athlete_names = [
                clean_text(name)
                for name in re.split(r"<br\s*/?>", str(record.get("FirstName") or ""), flags=re.I)
                if clean_text(name)
            ]
        if len(athlete_names) < 4:
            continue
        relays.append(
            RelayPerformance(
                event=event,
                athletes=tuple(athlete_names[:4]),  # type: ignore[arg-type]
                mark=mark,
                value=parsed[0],
                source=source_name,
                team_role=team_role,
            )
        )

    return ScrapeResult(
        dedupe_performances(performances),
        dedupe_relay_performances(relays),
        dedupe_performances(relay_splits),
    )


def api_athlete_name(record: dict[str, Any]) -> str | None:
    """Build one athlete name from an Athletic.net API event record."""
    first = clean_text(str(record.get("FirstName") or ""))
    last = clean_text(str(record.get("LastName") or ""))
    candidate = clean_text(f"{first} {last}")
    return candidate if is_name_like(candidate) else None


def extract_athletic_api_team_name(payload: dict[str, Any]) -> str | None:
    """Find a readable team/school name in Athletic.net's event-records JSON."""
    preferred_containers = [
        "team",
        "Team",
        "school",
        "School",
        "teamInfo",
        "TeamInfo",
        "teamProfile",
        "TeamProfile",
        "preferences",
        "Preferences",
    ]
    for key in preferred_containers:
        value = payload.get(key)
        if isinstance(value, dict):
            name = team_name_from_mapping(value)
            if name:
                return name
    return team_name_from_mapping(payload, allow_generic_name=False) or nested_team_name(payload)


def nested_team_name(value: Any, depth: int = 0) -> str | None:
    """Search shallow nested metadata dictionaries while skipping record lists."""
    if depth > 3 or not isinstance(value, dict):
        return None
    for key, child in value.items():
        if key in {"eventRecords", "relayMembers"} or isinstance(child, list):
            continue
        if not isinstance(child, dict):
            continue
        name = team_name_from_mapping(child)
        if name:
            return name
        nested = nested_team_name(child, depth + 1)
        if nested:
            return nested
    return None


def team_name_from_mapping(data: dict[str, Any], allow_generic_name: bool = True) -> str | None:
    """Read likely team-name fields from one Athletic.net metadata object."""
    for key, value in data.items():
        key_text = str(key).lower()
        if ("team" in key_text or "school" in key_text) and any(
            token in key_text for token in ("name", "title", "display")
        ):
            name = clean_team_name(value)
            if name:
                return name
    if allow_generic_name:
        for key in ("DisplayName", "displayName", "FullName", "fullName", "Name", "name", "Title", "title"):
            if key in data:
                name = clean_team_name(data.get(key))
                if name:
                    return name
    return None


def clean_team_name(value: Any) -> str | None:
    """Normalize a raw Athletic.net team label for standings display."""
    name = clean_text(str(value or ""))
    if not name or name.lower() in {"none", "null", "true", "false"}:
        return None
    if re.match(r"^https?://", name, flags=re.I) or re.fullmatch(r"\d+", name):
        return None
    name = re.split(r"\s*\|\s*", name, maxsplit=1)[0]
    name = re.sub(r"\s+-\s+Athletic\.net.*$", "", name, flags=re.I)
    name = re.sub(r"\s+-\s+(?:High School\s+)?Track\s*(?:&|and)\s*Field.*$", "", name, flags=re.I)
    name = re.sub(r"\s+Track\s*(?:&|and)\s*Field.*$", "", name, flags=re.I)
    name = name.strip(" -")
    if len(name) < 2 or len(name) > 90:
        return None
    return name


def relay_split_base_event(event_name: str) -> str | None:
    """Map a named relay-split event to its comparable individual distance."""
    value = clean_text(event_name).lower()
    if not re.search(r"\brelay\s*split\b", value):
        return None
    aliases = [
        (r"\b100\s*(m|meter|meters)?\b", "100m"),
        (r"\b200\s*(m|meter|meters)?\b", "200m"),
        (r"\b400\s*(m|meter|meters)?\b", "400m"),
        (r"\b800\s*(m|meter|meters)?\b", "800m"),
    ]
    for pattern, event in aliases:
        if re.search(pattern, value):
            return event
    return None


def extract_team_name(page: str) -> str | None:
    """Pull a readable team name from the HTML title when it is available."""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page, flags=re.I | re.S)
    if title_match:
        name = clean_team_name(title_match.group(1))
        if name:
            return name
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r"^\s*Title:\s*([^\r\n]+)",
    ):
        match = re.search(pattern, page, flags=re.I | re.M | re.S)
        if match:
            name = clean_team_name(match.group(1))
            if name:
                return name
    return None


def parse_athletic_records_html(
    page: str, source: str, team_role: str, gender: str = "all"
) -> tuple[list[Performance], list[RelayPerformance]]:
    """Parse regular tables first, then fall back to scanning visible and embedded text."""
    performances: list[Performance] = []
    text_for_scanning = filter_gender_text(page, gender)
    soup = get_soup(page)
    if soup:
        performances.extend(parse_tables_with_soup(soup, source, team_role))
        performances.extend(parse_text_blocks_with_soup(soup, source, team_role))
    else:
        performances.extend(parse_tables_without_soup(page, source, team_role))
    if not performances:
        text = html.unescape(re.sub(r"<[^>]+>", "\n", text_for_scanning))
        performances.extend(parse_record_text(text, source, team_role))
    relay_history = parse_relay_records_text(html.unescape(re.sub(r"<[^>]+>", "\n", text_for_scanning)), source, team_role)
    return dedupe_performances(performances), dedupe_relay_performances(relay_history)


def filter_gender_text(text: str, gender: str) -> str:
    """Keep only the requested Athletic.net Mens or Womens section when present."""
    wanted = (gender or "all").strip().lower()
    if wanted not in {"mens", "womens"}:
        return text
    lines = text.splitlines()
    active: str | None = None
    kept: list[str] = []
    saw_gender = False
    for line in lines:
        label = clean_text(line).lower()
        if label in {"mens", "men", "boys"}:
            active = "mens"
            saw_gender = True
            kept.append(line)
            continue
        if label in {"womens", "women", "girls"}:
            active = "womens"
            saw_gender = True
            kept.append(line)
            continue
        if active == wanted:
            kept.append(line)
    return "\n".join(kept) if saw_gender and kept else text


def get_soup(page: str) -> Any | None:
    """Return BeautifulSoup when installed; the rest of the app still works without it."""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        return BeautifulSoup(page, "html.parser")
    except Exception:
        return None


class TableExtractor(HTMLParser):
    """Collect table rows and nearby headings using only the standard library."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[tuple[str | None, list[list[str]]]] = []
        self.last_heading: str | None = None
        self.active_heading: str | None = None
        self.active_table_event: str | None = None
        self.active_rows: list[list[str]] = []
        self.active_row: list[str] = []
        self.active_cell: list[str] = []
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.heading_tag: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5"}:
            self.heading_tag = tag
            self.active_heading = ""
        elif tag == "table":
            self.in_table = True
            self.active_rows = []
            self.active_table_event = detect_event(self.last_heading or "")
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.active_row = []
        elif tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.active_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == self.heading_tag:
            self.last_heading = clean_text(self.active_heading or "")
            self.heading_tag = None
            self.active_heading = None
        elif tag in {"td", "th"} and self.in_cell:
            self.active_row.append(clean_text(" ".join(self.active_cell)))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if any(self.active_row):
                self.active_rows.append(self.active_row)
            self.in_row = False
        elif tag == "table" and self.in_table:
            self.tables.append((self.active_table_event, self.active_rows))
            self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.heading_tag and self.active_heading is not None:
            self.active_heading += " " + data
        if self.in_cell:
            self.active_cell.append(data)


def parse_tables_without_soup(page: str, source: str, team_role: str) -> list[Performance]:
    """Extract records from HTML tables when BeautifulSoup is not available."""
    parser = TableExtractor()
    parser.feed(page)
    performances: list[Performance] = []
    for heading_event, rows in parser.tables:
        for cells in rows:
            if len(cells) < 2:
                continue
            row_text = " ".join(cells)
            if relay_split_base_event(row_text):
                continue
            row_event = detect_event(row_text) or heading_event
            if not row_event or row_event in RELAY_EVENTS:
                continue
            mark = detect_mark(cells, row_event)
            athlete = detect_athlete(cells, row_event, mark)
            if athlete and mark:
                parsed = parse_mark(mark, row_event)
                if parsed:
                    performances.append(
                        Performance(
                            athlete=athlete,
                            event=row_event,
                            mark=mark,
                            value=parsed[0],
                            is_time=parsed[1],
                            source=source,
                            team_role=team_role,
                        )
                    )
    return performances


def parse_tables_with_soup(soup: Any, source: str, team_role: str) -> list[Performance]:
    """Extract event records from HTML tables using nearby headings as event context."""
    performances: list[Performance] = []
    for table in soup.find_all("table"):
        event = find_nearby_event(table)
        for row in table.find_all("tr"):
            cells = [clean_text(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            row_text = " ".join(cells)
            if relay_split_base_event(row_text):
                continue
            row_event = detect_event(row_text) or event
            if not row_event or row_event in RELAY_EVENTS:
                continue
            mark = detect_mark(cells, row_event)
            athlete = detect_athlete(cells, row_event, mark)
            if athlete and mark:
                parsed = parse_mark(mark, row_event)
                if parsed:
                    performances.append(
                        Performance(
                            athlete=athlete,
                            event=row_event,
                            mark=mark,
                            value=parsed[0],
                            is_time=parsed[1],
                            source=source,
                            team_role=team_role,
                        )
                    )
    return performances


def parse_text_blocks_with_soup(soup: Any, source: str, team_role: str) -> list[Performance]:
    """Scan compact text blocks for event, athlete, and mark when tables are not clean."""
    chunks: list[str] = []
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "h5", "li", "p", "div", "script"]):
        text = clean_text(tag.get_text(" ", strip=True) if tag.name != "script" else tag.string or "")
        if len(text) > 8:
            chunks.append(text)
    return parse_record_text("\n".join(chunks), source, team_role)


def parse_record_text(text: str, source: str, team_role: str) -> list[Performance]:
    """Find records in loose text by keeping the current event and looking for mark/name pairs."""
    performances: list[Performance] = []
    current_event: str | None = None
    current_event_is_split = False
    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line or len(line) < 3:
            continue
        split_event = relay_split_base_event(line)
        event = detect_event(line)
        if event and len(line) < 80:
            current_event = event
            current_event_is_split = split_event is not None
        event = event or current_event
        if not event or event in RELAY_EVENTS or current_event_is_split:
            continue
        mark = detect_mark([line], event)
        if not mark:
            continue
        athlete = extract_name_from_line(raw_line, event, mark)
        parsed = parse_mark(mark, event)
        if athlete and parsed:
            performances.append(
                Performance(
                    athlete=athlete,
                    event=event,
                    mark=mark,
                    value=parsed[0],
                    is_time=parsed[1],
                    source=source,
                    team_role=team_role,
                )
            )
    return performances


def parse_relay_records_text(text: str, source: str, team_role: str) -> list[RelayPerformance]:
    """Parse historic relay teams from Athletic.net reader-style records text."""
    relays: list[RelayPerformance] = []
    current_event: str | None = None
    pending_members: list[tuple[str, float | None]] = []
    for raw_line in text.splitlines():
        line = clean_text(raw_line)
        if not line:
            continue
        detected = detect_event(line)
        if detected in RELAY_EVENTS and len(line) < 80:
            current_event = detected
            pending_members = []
            continue
        if detected and detected not in RELAY_EVENTS and len(line) < 80:
            current_event = None
            pending_members = []
            continue
        if not current_event:
            continue
        mark = detect_mark([line], current_event)
        if mark and len(pending_members) >= 4:
            parsed = parse_mark(mark, current_event)
            if parsed:
                members = pending_members[-4:]
                relays.append(
                    RelayPerformance(
                        event=current_event,
                        athletes=tuple(member[0] for member in members),  # type: ignore[arg-type]
                        mark=mark,
                        value=parsed[0],
                        source=source,
                        team_role=team_role,
                        splits=tuple(member[1] for member in members),  # type: ignore[arg-type]
                    )
                )
            pending_members = []
            continue
        name = extract_relay_member_name(raw_line)
        if name:
            split = parse_mark(mark, current_event)[0] if mark and parse_mark(mark, current_event) else None
            pending_members.append((name, split))
            continue
        if mark:
            parsed = parse_mark(mark, current_event)
            if parsed and len(pending_members) >= 4:
                members = pending_members[-4:]
                relays.append(
                    RelayPerformance(
                        event=current_event,
                        athletes=tuple(member[0] for member in members),  # type: ignore[arg-type]
                        mark=mark,
                        value=parsed[0],
                        source=source,
                        team_role=team_role,
                        splits=tuple(member[1] for member in members),  # type: ignore[arg-type]
                    )
                )
            pending_members = []
            continue
    return relays


def extract_relay_member_name(line: str) -> str | None:
    """Extract one athlete name from an Athletic.net relay-member row."""
    cells = [clean_text(cell) for cell in re.split(r"\t+|\s{2,}", line) if clean_text(cell)]
    for cell in reversed(cells):
        cell = re.sub(r"^\d+\.\s*", "", cell)
        if cell.lower() == "relay team":
            return None
        candidate = cleanup_name(cell)
        if is_name_like(candidate):
            return candidate
    return None


def find_nearby_event(table: Any) -> str | None:
    """Look backward from a table for the heading that names the event."""
    for prev in table.find_all_previous(["h1", "h2", "h3", "h4", "h5"], limit=4):
        heading = clean_text(prev.get_text(" ", strip=True))
        if relay_split_base_event(heading):
            return None
        event = detect_event(heading)
        if event:
            return event
    return None


def clean_text(value: str) -> str:
    """Normalize whitespace and HTML entities."""
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def detect_event(text: str) -> str | None:
    """Map common Athletic.net event labels to one canonical event name."""
    value = text.lower()
    aliases = [
        (r"\b4\s*x\s*800\b|\b3200\s*(m|meter)?\s*relay\b", "4x800 relay"),
        (r"\b4\s*x\s*100\b|\b400\s*(m|meter)?\s*relay\b", "4x100 relay"),
        (r"\b4\s*x\s*200\b|\b800\s*(m|meter)?\s*relay\b", "4x200 relay"),
        (r"\b4\s*x\s*400\b|\b1600\s*(m|meter)?\s*relay\b", "4x400 relay"),
        (r"\b55\s*(m|meter|meters)?\s*(hurdles|h)\b", "55h"),
        (r"\b60\s*(m|meter|meters)?\s*(hurdles|h)\b", "60h"),
        (r"\b(110|100)\s*(m|meter)?\s*(hurdles|h)\b|\bhigh hurdles\b", "110h"),
        (r"\b300\s*(m|meter)?\s*(hurdles|h)\b|\bintermediate hurdles\b", "300h"),
        (r"\b3200\s*(m|meter|meters)?\b|\btwo mile\b", "3200m"),
        (r"\b1600\s*(m|meter|meters)?\b|\bone mile\b", "1600m"),
        (r"\b800\s*(m|meter|meters)?\b", "800m"),
        (r"\b400\s*(m|meter|meters)?\b", "400m"),
        (r"\b200\s*(m|meter|meters)?\b", "200m"),
        (r"\b100\s*(m|meter|meters)?\b", "100m"),
        (r"\b55\s*(m|meter|meters)?\b", "55m"),
        (r"\b60\s*(m|meter|meters)?\b", "60m"),
        (r"\blong jump\b", "long jump"),
        (r"\btriple jump\b", "triple jump"),
        (r"\bhigh jump\b", "high jump"),
        (r"\bpole vault\b", "pole vault"),
        (r"\bshot put\b", "shot put"),
        (r"\bdiscus\b", "discus"),
    ]
    for pattern, event in aliases:
        if re.search(pattern, value):
            return event
    return None


def detect_mark(cells: list[str], event: str) -> str | None:
    """Choose the first cell or token that can be parsed as a mark for the event."""
    for cell in cells:
        for token in mark_candidates(cell, event):
            if parse_mark(token, event):
                return token
    return None


def mark_candidates(text: str, event: str) -> list[str]:
    """Find likely time, distance, and height strings without treating years as marks."""
    candidates: list[str] = []
    if event in TRACK_EVENTS:
        patterns = [
            r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?[a-zA-Z]*\b",
            r"\b\d{1,2}\.\d{1,3}[a-zA-Z]*\b",
        ]
    else:
        patterns = [
            r"\b\d{1,2}\s*'\s*\d{1,2}(?:\.\d+)?(?:\s*\"|in)?\b",
            r"\b\d{1,2}\s*-\s*\d{1,2}(?:\.\d+)?\b",
            r"\b\d{1,2}\.\d{1,3}\s*m\b",
            r"\b\d{2,3}\s*'\s*\d{0,2}(?:\.\d+)?(?:\s*\"|in)?\b",
        ]
    for pattern in patterns:
        candidates.extend(match.group(0).strip() for match in re.finditer(pattern, text, flags=re.I))
    return candidates


def parse_mark(mark: str, event: str | None = None) -> tuple[float, bool] | None:
    """Convert a performance string to seconds for races or inches for field events."""
    value = clean_text(mark).lower().replace(",", "")
    value = re.sub(r"\b(ht|h|a|c|fat)\b", "", value).strip()
    value = re.sub(r"(?<=\d)(ht|h|a|c|fat)\b", "", value).strip()
    is_time = bool(event in TRACK_EVENTS) if event else bool(re.search(r":|\d+\.\d+", value))
    if event in FIELD_EVENTS:
        inches = parse_distance_to_inches(value)
        return (inches, False) if inches is not None else None
    if ":" in value:
        parts = [float(part) for part in value.split(":") if part]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds, True
    number_match = re.search(r"\d+(?:\.\d+)?", value)
    if number_match and is_time:
        return float(number_match.group(0)), True
    return None


def parse_distance_to_inches(value: str) -> float | None:
    """Convert common feet/inches or metric field marks to inches."""
    metric = re.search(r"(\d+(?:\.\d+)?)\s*m\b", value)
    if metric:
        return float(metric.group(1)) * 39.3701
    feet_inches = re.search(r"(\d{1,3})\s*'\s*(\d{0,2}(?:\.\d+)?)?", value)
    if feet_inches:
        feet = float(feet_inches.group(1))
        inches = float(feet_inches.group(2) or 0)
        return feet * 12 + inches
    dash = re.search(r"\b(\d{1,3})\s*-\s*(\d{1,2}(?:\.\d+)?)\b", value)
    if dash:
        return float(dash.group(1)) * 12 + float(dash.group(2))
    return None


def detect_athlete(cells: list[str], event: str, mark: str | None) -> str | None:
    """Pick the most name-like cell after removing event labels and marks."""
    for cell in cells:
        if not cell or cell == mark or detect_event(cell) == event:
            continue
        candidate = cleanup_name(cell)
        if is_name_like(candidate):
            return candidate
    return None


def extract_name_from_line(line: str, event: str, mark: str) -> str | None:
    """Remove event and mark tokens from a text line and keep the most likely athlete name."""
    cells = [clean_text(cell) for cell in re.split(r"\t+|\s{2,}", line) if clean_text(cell)]
    for index, cell in enumerate(cells):
        if mark in cell:
            for prior in reversed(cells[:index]):
                prior = re.sub(r"^\d+\.\s*", "", prior)
                candidate = cleanup_name(prior)
                if is_name_like(candidate):
                    return candidate
    value = re.sub(re.escape(mark), " ", line, count=1, flags=re.I)
    value = re.sub(r"\b\d{4}\b", " ", value)
    event_words = event.replace("m", " meters").replace("h", " hurdles")
    for token in [event, event_words, "relay", "record", "rank", "grade", "season"]:
        value = re.sub(re.escape(token), " ", value, flags=re.I)
    pieces = re.findall(r"[A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3}", value)
    for piece in pieces:
        candidate = cleanup_name(piece)
        if is_name_like(candidate):
            return candidate
    return None


def cleanup_name(value: str) -> str:
    """Strip surrounding noise from a possible athlete name."""
    value = re.sub(r"^\W+|\W+$", "", clean_text(value))
    value = re.sub(r"\b(fr|so|jr|sr|freshman|sophomore|junior|senior)\b", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value


def is_name_like(value: str) -> bool:
    """Return true for short human-name-like strings."""
    if not value or len(value) > 60:
        return False
    if any(char.isdigit() for char in value):
        return False
    words = value.split()
    return 2 <= len(words) <= 4 and all(re.search(r"[A-Za-z]", word) for word in words)


def dedupe_performances(performances: list[Performance]) -> list[Performance]:
    """Keep each athlete's best mark in each event for each source."""
    best: dict[tuple[str, str, str, str], Performance] = {}
    for perf in performances:
        key = (perf.team_role, perf.source, perf.event, perf.athlete.lower())
        current = best.get(key)
        if not current or is_better(perf.value, current.value, perf.is_time):
            best[key] = perf
    return sorted(best.values(), key=lambda perf: (perf.event, perf.athlete))


def dedupe_relay_performances(relays: list[RelayPerformance]) -> list[RelayPerformance]:
    """Keep the fastest version of each recorded relay team."""
    best: dict[tuple[str, str, tuple[str, str, str, str]], RelayPerformance] = {}
    for relay in relays:
        key = (relay.team_role, relay.event, tuple(name.lower() for name in relay.athletes))  # type: ignore[arg-type]
        current = best.get(key)
        if not current or relay.value < current.value:
            best[key] = relay
    return sorted(best.values(), key=lambda relay: (relay.event, relay.value))


def prepare_scrape_result_for_meet(
    data: ScrapeResult, meet_config: MeetConfig
) -> ScrapeResult:
    """Filter records to the selected meet and add indoor 55m/60m fallback conversions."""
    performances = list(data.performances)
    if meet_config.season_type == "indoor":
        distance = meet_config.indoor_sprint_distance
        other_distance = "60" if distance == "55" else "55"
        performances.extend(
            converted_short_event_fallbacks(
                performances,
                f"{distance}m",
                f"{other_distance}m",
                INDOOR_DASH_CONVERSION_FACTOR,
            )
        )
        performances.extend(
            converted_short_event_fallbacks(
                performances,
                f"{distance}h",
                f"{other_distance}h",
                INDOOR_HURDLE_CONVERSION_FACTOR,
            )
        )
    active_events = set(meet_config.events)
    active_relays = set(meet_config.relay_events)
    active_relay_bases = {RELAY_BASE_EVENT[event] for event in active_relays}
    return ScrapeResult(
        performances=dedupe_performances(
            [perf for perf in performances if perf.event in active_events]
        ),
        relay_history=dedupe_relay_performances(
            [relay for relay in data.relay_history if relay.event in active_relays]
        ),
        relay_splits=[
            split for split in data.relay_splits if split.event in active_relay_bases
        ],
    )


def converted_short_event_fallbacks(
    performances: list[Performance],
    target_event: str,
    source_event: str,
    conversion_factor: float,
) -> list[Performance]:
    """Convert an athlete's alternate 55m/60m PR only when no target-event PR exists."""
    athletes_with_target = {
        (perf.team_role, perf.source, perf.athlete.lower())
        for perf in performances
        if perf.event == target_event
    }
    source_best: dict[tuple[str, str, str], Performance] = {}
    for perf in performances:
        if perf.event != source_event:
            continue
        key = (perf.team_role, perf.source, perf.athlete.lower())
        current = source_best.get(key)
        if not current or perf.value < current.value:
            source_best[key] = perf
    converted: list[Performance] = []
    convert_up = target_event.startswith("60")
    for key, perf in source_best.items():
        if key in athletes_with_target:
            continue
        value = perf.value * conversion_factor if convert_up else perf.value / conversion_factor
        converted.append(
            Performance(
                athlete=perf.athlete,
                event=target_event,
                mark=f"{format_time(value)}c",
                value=value,
                is_time=True,
                source=perf.source,
                team_role=perf.team_role,
            )
        )
    return converted


def is_better(candidate: float, incumbent: float, is_time: bool) -> bool:
    """Compare times low-to-high and field marks high-to-low."""
    return candidate < incumbent if is_time else candidate > incumbent


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


def normalize_athlete_name(name: str) -> str:
    """Normalize an athlete name for case-insensitive injury matching."""
    return re.sub(r"[^a-z0-9]+", " ", clean_text(name).lower()).strip()


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


def athlete_max_events(
    athlete: str, athlete_event_limits: dict[str, int] | None = None
) -> int:
    """Return one athlete's coach-set maximum, defaulting to the meet maximum."""
    return (athlete_event_limits or {}).get(
        normalize_athlete_name(athlete), MAX_EVENTS_PER_ATHLETE
    )


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
) -> LineupResult:
    """Scrape inputs, build a lineup, and evaluate the final projection."""
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
        school_result = scrape_team_data(school_url, "school", "Your Team", gender)
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
            opponent_result = scrape_team_data(url, "opponent", f"Opponent {index}", gender)
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
) -> dict[str, Any]:
    """Generate independent men's and women's lineups without combining their athlete pools."""
    return {
        "mode": "both",
        "division_results": {
            "mens": asdict(
                run_optimizer(
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
                run_optimizer(
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


HTML_PAGE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Track Lineup Optimizer</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #17202a;
      --muted: #64707d;
      --line: #d8e0e8;
      --surface: #ffffff;
      --band: #f6f8fb;
      --accent: #7a1208;
      --accent-2: #b41610;
      --gold: #f0ac1b;
      --highlight: #dff2ff;
      --highlight-line: #65bdf2;
      --ok: #247a4f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--band);
      color: var(--ink);
    }
    header {
      padding: 28px clamp(18px, 4vw, 54px) 18px;
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }
    h1 { margin: 0; font-size: clamp(1.6rem, 3vw, 2.4rem); letter-spacing: 0; }
    .version { color: var(--muted); font-size: .78rem; margin-top: 5px; }
    main {
      display: grid;
      grid-template-columns: minmax(280px, 430px) minmax(0, 1fr);
      min-height: calc(100vh - 94px);
    }
    aside {
      background: var(--surface);
      border-right: 1px solid var(--line);
      padding: 22px clamp(16px, 3vw, 28px);
    }
    section { padding: 22px clamp(16px, 3vw, 34px); }
    label { display: block; font-weight: 700; margin: 14px 0 7px; }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    textarea { min-height: 118px; resize: vertical; }
    .athlete-limit-list {
      display: grid;
      gap: 8px;
    }
    .athlete-limit-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 112px 38px;
      gap: 8px;
      align-items: center;
    }
    .athlete-limit-row input,
    .athlete-limit-row select {
      min-width: 0;
    }
    .athlete-limit-remove {
      width: 38px;
      height: 38px;
      padding: 0;
      background: #ffe3df;
      color: #9b2115;
      font-size: 1.05rem;
    }
    .athlete-limit-remove:hover { background: #ffcfc8; }
    .add-limit-button {
      margin-top: 8px;
      padding: 8px 11px;
      font-size: .84rem;
    }
    .season-specific[hidden] { display: none; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }
    button {
      appearance: none;
      border: 0;
      border-radius: 6px;
      padding: 11px 14px;
      background: var(--accent);
      color: #fff;
      font-weight: 800;
      cursor: pointer;
    }
    button.secondary { background: #e8edf2; color: var(--ink); }
    button:disabled { opacity: .65; cursor: wait; }
    .file-input-hidden { display: none; }
    .athlete-chip {
      display: inline-flex;
      align-items: center;
      max-width: 100%;
      margin: -2px 2px -2px 0;
      padding: 2px 6px;
      border: 1px solid transparent;
      border-radius: 5px;
      background: transparent;
      color: var(--ink);
      font: inherit;
      font-weight: 800;
      line-height: 1.25;
      cursor: pointer;
      transform-origin: center;
      transition: transform .14s ease, background .14s ease, border-color .14s ease, box-shadow .14s ease, color .14s ease;
    }
    .athlete-chip:hover {
      transform: scale(1.045);
      background: #fff7e3;
      border-color: rgba(240, 172, 27, .55);
      color: var(--accent);
      box-shadow: 0 2px 8px rgba(23, 32, 42, .12);
    }
    .athlete-chip.selected {
      background: var(--highlight);
      border-color: var(--highlight-line);
      box-shadow: 0 0 0 3px rgba(101, 189, 242, .28);
      color: #0b527d;
      transform: scale(1.035);
    }
    .division-tabs {
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #e8edf2;
    }
    .lineup-controls {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }
    .division-tabs[hidden] { display: none; }
    .event-sort {
      display: inline-flex;
      gap: 2px;
      padding: 3px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #e8edf2;
    }
    .division-tab,
    .sort-option {
      min-width: 92px;
      padding: 8px 12px;
      background: transparent;
      color: var(--ink);
    }
    .division-tab.active,
    .sort-option.active {
      background: var(--surface);
      color: var(--accent);
      box-shadow: 0 1px 2px rgba(23, 32, 42, .12);
    }
    .summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, .panel {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric strong { display: block; font-size: 1.55rem; }
    .metric span, .muted { color: var(--muted); font-size: .92rem; }
    .score-change {
      position: sticky;
      top: 70px;
      z-index: 9;
      display: flex;
      align-items: center;
      gap: 12px;
      margin: -6px 0 18px;
      padding: 11px 14px;
      border: 1px solid var(--line);
      border-left: 4px solid #6c7884;
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 8px 22px rgba(23, 32, 42, .12);
    }
    .score-change[hidden] { display: none; }
    .score-change-label {
      color: var(--muted);
      font-size: .78rem;
      font-weight: 900;
      text-transform: uppercase;
    }
    .score-change strong { font-size: 1.1rem; white-space: nowrap; }
    .score-change-detail { color: var(--muted); font-size: .88rem; }
    .score-change.positive { border-left-color: var(--ok); background: #f1faf5; }
    .score-change.positive strong { color: var(--ok); }
    .score-change.negative { border-left-color: #a3281a; background: #fff4f2; }
    .score-change.negative strong { color: #8a2116; }
    .score-change.neutral { border-left-color: #788692; background: #f7f9fb; }
    .mark-origin {
      display: inline-flex;
      align-items: center;
      margin-left: 6px;
      border: 1px solid transparent;
      border-radius: 999px;
      padding: 2px 6px;
      font-size: .65rem;
      font-weight: 900;
      line-height: 1.2;
      vertical-align: middle;
    }
    .mark-origin.historical {
      border-color: #bdd5e9;
      background: #e7f2fb;
      color: #185b86;
    }
    .mark-origin.predicted {
      border-color: #efd28b;
      background: #fff4d5;
      color: #7a4d00;
    }
    .team-score-metric strong { font-size: 1rem; margin-bottom: 8px; }
    .team-scores {
      display: grid;
      gap: 6px;
    }
    .team-score {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 5px 7px;
      border-radius: 6px;
      background: #f3f6f9;
      color: var(--ink);
      font-size: .82rem;
      font-weight: 800;
    }
    .team-score.school {
      background: var(--highlight);
      box-shadow: inset 0 0 0 1px var(--highlight-line);
      color: #0b527d;
    }
    .team-score-name {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .team-score-points {
      white-space: nowrap;
      color: var(--ok);
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 12px;
    }
    .event-card {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 13px;
      min-height: 128px;
    }
    .event-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .event-title {
      display: flex;
      align-items: center;
      gap: 7px;
      min-width: 0;
    }
    .event-head h3 { margin: 0 0 8px; font-size: 1rem; }
    .event-title h3 { margin-bottom: 0; }
    .points { color: var(--ok); font-weight: 800; white-space: nowrap; }
    .event-info {
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      width: 21px;
      height: 21px;
      padding: 0;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #eef3f7;
      color: #115f95;
      font-size: .78rem;
      font-weight: 900;
      line-height: 1;
    }
    .event-info:hover,
    .event-info:focus {
      background: #e0f0ff;
      color: #0b527d;
      outline: none;
    }
    .event-info-popover {
      position: absolute;
      left: 0;
      top: calc(100% + 8px);
      z-index: 18;
      display: none;
      width: min(390px, calc(100vw - 52px));
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      box-shadow: 0 16px 36px rgba(23, 32, 42, .22);
      text-align: left;
      cursor: default;
    }
    .event-info.open .event-info-popover {
      display: block;
    }
    .standings-title {
      display: block;
      margin-bottom: 8px;
      color: var(--ink);
      font-size: .84rem;
      font-weight: 900;
    }
    .standings-list {
      display: grid;
      gap: 6px;
      min-width: 0;
    }
    .standing-row {
      display: grid;
      grid-template-columns: 40px minmax(112px, 1fr) auto auto;
      gap: 8px;
      align-items: center;
      padding: 6px 7px;
      border-radius: 6px;
      background: #f7f9fb;
      color: var(--ink);
      font-size: .76rem;
      line-height: 1.25;
    }
    .standing-row.school {
      background: var(--highlight);
      box-shadow: inset 0 0 0 1px var(--highlight-line);
    }
    .standing-place,
    .standing-points {
      font-weight: 900;
      white-space: nowrap;
    }
    .standing-name {
      min-width: 0;
    }
    .standing-name strong,
    .standing-name span {
      display: block;
      overflow-wrap: anywhere;
    }
    .standing-name span {
      margin-top: 1px;
      color: var(--muted);
      font-size: .68rem;
      font-weight: 700;
    }
    .standing-mark {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: var(--accent);
      font-weight: 900;
      white-space: nowrap;
    }
    .standing-mark .mark-origin { margin-left: 0; padding: 1px 4px; font-size: .55rem; }
    ol { margin: 0; padding-left: 20px; }
    li { margin: 5px 0; }
    .relay { border-left: 4px solid var(--accent-2); }
    .athlete-panel {
      position: fixed;
      top: 112px;
      left: 18px;
      z-index: 20;
      width: min(360px, calc(100vw - 32px));
      max-height: calc(100vh - 136px);
      overflow: hidden;
      border: 1px solid rgba(122, 18, 8, .28);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: 0 18px 44px rgba(23, 32, 42, .22);
    }
    .athlete-panel[hidden] { display: none; }
    .athlete-panel-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 12px 10px 14px;
      background: var(--accent);
      color: #fff;
      cursor: move;
      user-select: none;
    }
    .athlete-panel-title { min-width: 0; }
    .athlete-panel-title strong {
      display: block;
      font-size: 1rem;
      line-height: 1.2;
    }
    .athlete-panel-title span {
      display: block;
      margin-top: 2px;
      color: rgba(255, 255, 255, .78);
      font-size: .78rem;
      font-weight: 700;
    }
    .athlete-panel-close {
      flex: 0 0 auto;
      width: 28px;
      height: 28px;
      padding: 0;
      border-radius: 5px;
      background: rgba(255, 255, 255, .12);
      color: #fff;
      font-size: 1.1rem;
      line-height: 1;
    }
    .athlete-panel-close:hover { background: rgba(255, 255, 255, .22); }
    .athlete-panel-body {
      max-height: calc(100vh - 200px);
      overflow: auto;
      padding: 12px 14px 14px;
    }
    .athlete-event-list {
      display: grid;
      gap: 9px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .athlete-event-item {
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px;
      background: #fbfcfe;
    }
    .athlete-event-top {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-weight: 800;
    }
    .athlete-event-mark {
      display: inline-flex;
      align-items: center;
      color: var(--accent);
      white-space: nowrap;
    }
    .athlete-event-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .athlete-pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 7px;
      background: #eef3f7;
      color: var(--muted);
      font-size: .78rem;
      font-weight: 800;
    }
    .athlete-pill.points-pill {
      background: #e7f6ee;
      color: var(--ok);
    }
    .athlete-event-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }
    .mini-button {
      padding: 7px 11px;
      border-radius: 5px;
      background: #eef3f7;
      color: var(--ink);
      font-size: .78rem;
    }
    .mini-button:hover { background: #fff7e3; color: var(--accent); }
    .mini-button.move-action,
    .mini-button.add-action {
      background: #e0f0ff;
      color: #115f95;
    }
    .mini-button.move-action:hover,
    .mini-button.add-action:hover {
      background: #cbe7ff;
      color: #0b527d;
    }
    .mini-button.remove-action {
      background: #ffe3df;
      color: #9b2115;
    }
    .mini-button.remove-action:hover {
      background: #ffd0c9;
      color: #7a1208;
    }
    .athlete-panel-actions {
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--line);
    }
    .coach-edit-block {
      display: grid;
      gap: 16px;
    }
    .coach-edit-title {
      margin: 0;
      font-weight: 800;
    }
    .coach-edit-question {
      margin: 0 0 7px;
      font-weight: 800;
    }
    .event-choice-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .event-choice {
      min-height: 42px;
      padding: 8px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      text-align: left;
      font-size: .82rem;
      line-height: 1.2;
    }
    .event-choice.current {
      background: var(--highlight);
      border-color: var(--highlight-line);
    }
    .event-choice:disabled {
      opacity: .72;
      cursor: not-allowed;
    }
    .event-choice.warning {
      background: #fff4cf;
      border-color: rgba(240, 172, 27, .85);
    }
    .event-choice.selected {
      box-shadow: 0 0 0 3px rgba(122, 18, 8, .16);
      border-color: var(--accent);
    }
    .edit-warning {
      border: 1px solid rgba(240, 172, 27, .75);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff8df;
      color: #6b4a00;
      font-size: .86rem;
    }
    .edit-alert {
      border: 2px solid rgba(240, 172, 27, .95);
      border-radius: 7px;
      padding: 10px 11px;
      background: #fff4cf;
      color: #6b3b00;
      font-size: .9rem;
      font-weight: 750;
      box-shadow: 0 4px 14px rgba(23, 32, 42, .12);
    }
    .edit-alert.ok {
      border-color: rgba(36, 122, 79, .6);
      background: #e8f7ef;
      color: var(--ok);
    }
    .choice-list {
      display: grid;
      gap: 10px;
    }
    .choice-card {
      width: 100%;
      padding: 11px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      text-align: left;
      font-size: .86rem;
    }
    .choice-card.selected {
      border-color: var(--accent);
      background: #fff7e3;
      box-shadow: 0 0 0 3px rgba(240, 172, 27, .22);
    }
    .choice-card > strong,
    .choice-card > span {
      display: block;
    }
    .choice-card > span {
      margin-top: 2px;
      color: var(--muted);
      font-size: .78rem;
    }
    .choice-name-line {
      display: flex;
      align-items: center;
      gap: 5px;
    }
    .event-asterisk {
      position: relative;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 17px;
      height: 17px;
      border-radius: 999px;
      background: #eef3f7;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 900;
    }
    .event-asterisk-tip {
      position: absolute;
      left: 50%;
      bottom: calc(100% + 8px);
      z-index: 30;
      display: none;
      width: max-content;
      max-width: 230px;
      padding: 8px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--ink);
      box-shadow: 0 10px 24px rgba(23, 32, 42, .18);
      transform: translateX(-50%);
      font-size: .76rem;
      line-height: 1.35;
      white-space: normal;
    }
    .event-asterisk:hover .event-asterisk-tip,
    .event-asterisk:focus .event-asterisk-tip {
      display: block;
    }
    .choice-warning-text {
      display: block;
      margin-top: 5px;
      color: #8a130a;
      font-weight: 850;
      font-size: .78rem;
    }
    .edit-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .error {
      border: 1px solid #f0b7ad;
      color: #84291d;
      background: #fff3f0;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 0 0 14px;
    }
    .notice {
      border: 1px solid rgba(101, 189, 242, .7);
      color: #0b527d;
      background: #eaf6ff;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 0 0 14px;
      font-weight: 700;
    }
    @media (max-width: 880px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .summary { grid-template-columns: 1fr; }
      .score-change {
        top: 62px;
        align-items: flex-start;
        flex-direction: column;
        gap: 4px;
      }
      .athlete-panel {
        top: auto;
        left: 12px;
        bottom: 12px;
        max-height: min(68vh, 520px);
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Track Lineup Optimizer</h1>
    <div class="version">Build 2026.09.13-v31</div>
  </header>
  <main>
    <aside>
      <form id="optimizer-form">
        <label for="season-type">Season</label>
        <select id="season-type" name="seasonType">
          <option value="outdoor" selected>Outdoor</option>
          <option value="indoor">Indoor</option>
        </select>
        <div id="indoor-sprint-settings" class="season-specific" hidden>
          <label for="indoor-sprint-distance">Indoor sprint events</label>
          <select id="indoor-sprint-distance" name="indoorSprintDistance">
            <option value="55" selected>55m and 55m Hurdles</option>
            <option value="60">60m and 60m Hurdles</option>
          </select>
        </div>
        <label for="school-url">School Athletic.net event records URL</label>
        <input id="school-url" name="schoolUrl" placeholder="Paste Athletic.net event records URL">
        <label for="gender">Division</label>
        <select id="gender" name="gender">
          <option value="mens" selected>Mens</option>
          <option value="womens">Womens</option>
          <option value="both">Both</option>
        </select>
        <label for="opponents">Opponent event records URLs</label>
        <textarea id="opponents" name="opponents" placeholder="One URL per line"></textarea>
        <label for="injured-athletes">Injured / unavailable athletes</label>
        <textarea id="injured-athletes" name="injuredAthletes" placeholder="One unavailable athlete per line, including opponents"></textarea>
        <label for="team-event-limit">Limit every school athlete to</label>
        <select id="team-event-limit" name="teamEventLimit">
          <option value="4" selected>4 events (standard)</option>
          <option value="3">3 events</option>
          <option value="2">2 events</option>
        </select>
        <label>School athlete event limits</label>
        <div id="athlete-limit-list" class="athlete-limit-list"></div>
        <button id="add-athlete-limit" class="secondary add-limit-button" type="button">Add athlete limit</button>
        <div class="actions">
          <button id="run-button" type="submit">Generate Lineup</button>
          <button class="secondary" id="save-button" type="button">Save Lineup</button>
          <button class="secondary" id="load-button" type="button">Load Saved Lineup</button>
          <input class="file-input-hidden" id="load-file" type="file" accept="application/json,.json">
        </div>
      </form>
    </aside>
    <section>
      <div class="lineup-controls">
        <div id="division-tabs" class="division-tabs" hidden>
          <button class="division-tab active" data-division="mens" type="button">Mens</button>
          <button class="division-tab" data-division="womens" type="button">Womens</button>
        </div>
        <div id="event-sort" class="event-sort" aria-label="Event order">
          <button class="sort-option active" data-sort="distance" type="button">Distance</button>
          <button class="sort-option" data-sort="schedule" type="button">Schedule</button>
        </div>
      </div>
      <div id="errors"></div>
      <div class="summary">
        <div class="metric"><strong id="total-points">0</strong><span>projected points</span></div>
        <div class="metric"><strong id="school-count">0</strong><span>school records parsed</span></div>
        <div class="metric"><strong id="opponent-count">0</strong><span>opponent records parsed</span></div>
        <div class="metric team-score-metric"><strong>Projected Team Scores</strong><div id="team-points" class="team-scores"></div></div>
      </div>
      <div id="score-change" class="score-change" aria-live="polite" hidden>
        <span class="score-change-label">Last manual change</span>
        <strong id="score-change-delta">0.0 points</strong>
        <span id="score-change-detail" class="score-change-detail"></span>
      </div>
      <div id="results" class="grid"></div>
    </section>
  </main>
  <div id="athlete-panel" class="athlete-panel" role="dialog" aria-modal="false" aria-labelledby="athlete-panel-name" hidden>
    <div id="athlete-panel-head" class="athlete-panel-head">
      <div class="athlete-panel-title">
        <strong id="athlete-panel-name">Athlete</strong>
        <span id="athlete-panel-count">0 events</span>
      </div>
      <button id="athlete-panel-close" class="athlete-panel-close" type="button" aria-label="Close athlete overview">x</button>
    </div>
    <div class="athlete-panel-body">
      <ul id="athlete-panel-events" class="athlete-event-list"></ul>
    </div>
  </div>
  <script>
    const form = document.querySelector("#optimizer-form");
    const runButton = document.querySelector("#run-button");
    const saveButton = document.querySelector("#save-button");
    const loadButton = document.querySelector("#load-button");
    const loadFileInput = document.querySelector("#load-file");
    const seasonTypeInput = document.querySelector("#season-type");
    const indoorSprintSettings = document.querySelector("#indoor-sprint-settings");
    const indoorSprintDistanceInput = document.querySelector("#indoor-sprint-distance");
    const schoolUrlInput = document.querySelector("#school-url");
    const genderInput = document.querySelector("#gender");
    const opponentsInput = document.querySelector("#opponents");
    const injuredAthletesInput = document.querySelector("#injured-athletes");
    const teamEventLimitInput = document.querySelector("#team-event-limit");
    const athleteLimitList = document.querySelector("#athlete-limit-list");
    const addAthleteLimitButton = document.querySelector("#add-athlete-limit");
    const results = document.querySelector("#results");
    const errors = document.querySelector("#errors");
    const divisionTabs = document.querySelector("#division-tabs");
    const eventSortControls = document.querySelector("#event-sort");
    const athletePanel = document.querySelector("#athlete-panel");
    const athletePanelHead = document.querySelector("#athlete-panel-head");
    const athletePanelName = document.querySelector("#athlete-panel-name");
    const athletePanelCount = document.querySelector("#athlete-panel-count");
    const athletePanelEvents = document.querySelector("#athlete-panel-events");
    const athletePanelClose = document.querySelector("#athlete-panel-close");
    const teamPointsList = document.querySelector("#team-points");
    const scoreChange = document.querySelector("#score-change");
    const scoreChangeDelta = document.querySelector("#score-change-delta");
    const scoreChangeDetail = document.querySelector("#score-change-detail");
    const APP_BUILD_VERSION = "2026.09.13-team-limits-score-delta-v31";
    const PROJECT_SCHEMA_VERSION = 1;
    const OUTDOOR_EVENT_SORT_ORDERS = {
      schedule: [
        "4x800 relay", "4x100 relay", "3200m", "110h", "100m", "800m",
        "4x200 relay", "400m", "300h", "1600m", "200m", "4x400 relay",
        "shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"
      ],
      distance: [
        "100m", "200m", "400m", "800m", "1600m", "3200m", "110h", "300h",
        "4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay",
        "shot put", "discus", "high jump", "pole vault", "long jump", "triple jump"
      ]
    };
    const OUTDOOR_ALL_EVENTS = OUTDOOR_EVENT_SORT_ORDERS.schedule;
    const OUTDOOR_RELAY_EVENTS = ["4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"];
    const FIELD_EVENTS = new Set(["high jump", "pole vault", "discus", "shot put", "long jump", "triple jump"]);
    const DISTANCE_EVENTS = new Set(["4x800 relay", "800m", "1600m", "3200m"]);
    const LONG_DISTANCE_EVENTS = new Set(["1600m", "3200m"]);
    const DISTANCE_THREE_EVENT_ALLOWED_EVENTS = new Set(["4x800 relay", "800m", "400m", "4x400 relay"]);
    const OUTDOOR_RUNNING_ORDER = {
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
      "4x400 relay": 12
    };
    let divisionResults = null;
    let activeDivision = "mens";
    let currentResult = null;
    let activeEventSort = "distance";
    let athleteIndex = new Map();
    let selectedAthleteKey = "";
    let selectedEventInfo = "";
    let panelDrag = null;
    let editState = null;
    let manualScoreChanges = {};

    seasonTypeInput.addEventListener("change", updateSeasonControls);
    updateSeasonControls();

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await optimize("/api/optimize", {
        schoolUrl: schoolUrlInput.value.trim(),
        opponentUrls: opponentsInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        injuredAthletes: injuredAthletesInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        athleteEventLimits: collectAthleteEventLimits(),
        teamEventLimit: Number(teamEventLimitInput.value),
        gender: genderInput.value,
        seasonType: seasonTypeInput.value,
        indoorSprintDistance: indoorSprintDistanceInput.value
      });
    });

    addAthleteLimitButton.addEventListener("click", () => addAthleteLimitRow());
    athleteLimitList.addEventListener("click", (event) => {
      const removeButton = event.target.closest("[data-remove-athlete-limit]");
      if (removeButton) removeButton.closest(".athlete-limit-row")?.remove();
    });
    saveButton.addEventListener("click", saveLineupProject);
    loadButton.addEventListener("click", () => loadFileInput.click());
    loadFileInput.addEventListener("change", loadLineupProject);
    results.addEventListener("click", (event) => {
      const infoButton = event.target.closest(".event-info");
      if (infoButton) {
        event.stopPropagation();
        if (event.target.closest(".event-info-popover")) return;
        toggleEventInfo(infoButton.dataset.eventInfo || "");
        return;
      }
      const button = event.target.closest(".athlete-chip");
      if (!button) return;
      closeEventInfo();
      openAthletePanel(button.dataset.athleteName || button.textContent.trim());
    });
    athletePanelEvents.addEventListener("click", (event) => {
      const actionButton = event.target.closest("[data-edit-action]");
      if (actionButton) {
        startCoachEdit(actionButton.dataset.editAction, actionButton.dataset.athlete, actionButton.dataset.event);
        return;
      }
      const eventButton = event.target.closest("[data-target-event]");
      if (eventButton && editState) {
        editState.targetEvent = eventButton.dataset.targetEvent;
        renderCoachEditPanel();
        return;
      }
      const choiceButton = event.target.closest("[data-choice-role]");
      if (choiceButton && editState) {
        editState[choiceButton.dataset.choiceRole] = choiceButton.dataset.athlete;
        renderCoachEditPanel();
        return;
      }
      const commandButton = event.target.closest("[data-edit-command]");
      if (commandButton) handleEditCommand(commandButton.dataset.editCommand);
    });
    athletePanelClose.addEventListener("click", closeAthletePanel);
    athletePanelHead.addEventListener("pointerdown", startPanelDrag);
    document.addEventListener("pointermove", dragAthletePanel);
    document.addEventListener("pointerup", stopPanelDrag);
    document.addEventListener("click", (event) => {
      if (!event.target.closest(".event-info")) closeEventInfo();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeEventInfo();
    });
    window.addEventListener("resize", () => {
      if (!athletePanel.hidden && !panelDrag) placePanelOnSide();
    });
    divisionTabs.addEventListener("click", (event) => {
      const button = event.target.closest("[data-division]");
      if (!button || !divisionResults) return;
      activeDivision = button.dataset.division;
      updateDivisionTabs();
      renderSingle(divisionResults[activeDivision] || {});
    });
    eventSortControls.addEventListener("click", (event) => {
      const button = event.target.closest("[data-sort]");
      if (!button || button.dataset.sort === activeEventSort) return;
      activeEventSort = button.dataset.sort;
      updateEventSortControls();
      if (currentResult) renderSingle(currentResult);
    });

    async function optimize(url, payload) {
      runButton.disabled = true;
      manualScoreChanges = {};
      results.innerHTML = "";
      errors.innerHTML = "";
      closeAthletePanel();
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        render(data);
      } catch (error) {
        errors.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      } finally {
        runButton.disabled = false;
      }
    }

    function saveLineupProject() {
      if (!currentResult && !divisionResults) {
        errors.innerHTML = `<div class="error">Generate or load a lineup before saving.</div>`;
        return;
      }
      const project = {
        type: "track-lineup-project",
        schemaVersion: PROJECT_SCHEMA_VERSION,
        appVersion: APP_BUILD_VERSION,
        savedAt: new Date().toISOString(),
        form: currentFormState(),
        activeEventSort,
        activeDivision,
        manualScoreChanges: cloneResult(manualScoreChanges),
        result: divisionResults
          ? {mode: "both", division_results: cloneResult(divisionResults)}
          : cloneResult(currentResult)
      };
      downloadJson(project, projectFileName(project));
      errors.innerHTML = `<div class="notice">Lineup project saved. It includes the current lineup, relays, points, and parsed school/opponent data.</div>`;
    }

    async function loadLineupProject(event) {
      const file = event.target.files?.[0];
      if (!file) return;
      try {
        const project = JSON.parse(await file.text());
        restoreLineupProject(project);
      } catch (error) {
        errors.innerHTML = `<div class="error">Could not load saved lineup: ${escapeHtml(error.message)}</div>`;
      } finally {
        event.target.value = "";
      }
    }

    function restoreLineupProject(project) {
      if (!project || project.type !== "track-lineup-project") {
        throw new Error("This does not look like a Track Lineup Optimizer project file.");
      }
      if (Number(project.schemaVersion || 0) > PROJECT_SCHEMA_VERSION) {
        throw new Error("This project file was saved by a newer app version.");
      }
      if (!project.result || typeof project.result !== "object") {
        throw new Error("The saved project does not include lineup results.");
      }
      restoreFormState(project.form || {});
      manualScoreChanges = project.manualScoreChanges && typeof project.manualScoreChanges === "object"
        ? cloneResult(project.manualScoreChanges)
        : {};
      activeEventSort = ["distance", "schedule"].includes(project.activeEventSort) ? project.activeEventSort : "distance";
      updateEventSortControls();
      closeAthletePanel();
      if (project.result.mode === "both" && project.result.division_results) {
        divisionResults = project.result.division_results;
        activeDivision = project.activeDivision in divisionResults ? project.activeDivision : "mens";
        divisionTabs.hidden = false;
        updateDivisionTabs();
        renderSingle(divisionResults[activeDivision] || {});
      } else {
        divisionResults = null;
        divisionTabs.hidden = true;
        renderSingle(project.result);
      }
      errors.innerHTML = `<div class="notice">Loaded saved lineup from ${escapeHtml(project.savedAt ? new Date(project.savedAt).toLocaleString() : fileDateFallback())}.</div>`;
    }

    function currentFormState() {
      return {
        schoolUrl: schoolUrlInput.value.trim(),
        opponentUrls: opponentsInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        injuredAthletes: injuredAthletesInput.value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        athleteEventLimits: collectAthleteEventLimits(),
        teamEventLimit: Number(teamEventLimitInput.value),
        gender: genderInput.value,
        seasonType: seasonTypeInput.value,
        indoorSprintDistance: indoorSprintDistanceInput.value
      };
    }

    function restoreFormState(state) {
      seasonTypeInput.value = ["outdoor", "indoor"].includes(state.seasonType)
        ? state.seasonType
        : "outdoor";
      indoorSprintDistanceInput.value = ["55", "60"].includes(String(state.indoorSprintDistance))
        ? String(state.indoorSprintDistance)
        : "55";
      updateSeasonControls();
      schoolUrlInput.value = state.schoolUrl || "";
      opponentsInput.value = Array.isArray(state.opponentUrls) ? state.opponentUrls.join("\n") : "";
      injuredAthletesInput.value = Array.isArray(state.injuredAthletes) ? state.injuredAthletes.join("\n") : "";
      teamEventLimitInput.value = ["2", "3", "4"].includes(String(state.teamEventLimit))
        ? String(state.teamEventLimit)
        : "4";
      restoreAthleteEventLimits(state.athleteEventLimits || []);
      if (["mens", "womens", "both"].includes(state.gender)) genderInput.value = state.gender;
    }

    function updateSeasonControls() {
      indoorSprintSettings.hidden = seasonTypeInput.value !== "indoor";
    }

    function addAthleteLimitRow(athlete = "", maxEvents = 2) {
      const row = document.createElement("div");
      row.className = "athlete-limit-row";
      row.innerHTML = `
        <input class="athlete-limit-name" type="text" value="${escapeHtml(athlete)}" placeholder="Athlete name" aria-label="Athlete name">
        <select class="athlete-limit-max" aria-label="Maximum events">
          ${[1, 2, 3, 4].map(limit => `<option value="${limit}" ${Number(maxEvents) === limit ? "selected" : ""}>Max ${limit}</option>`).join("")}
        </select>
        <button class="athlete-limit-remove" type="button" data-remove-athlete-limit aria-label="Remove athlete limit">x</button>
      `;
      athleteLimitList.appendChild(row);
    }

    function collectAthleteEventLimits() {
      const limits = new Map();
      athleteLimitList.querySelectorAll(".athlete-limit-row").forEach(row => {
        const athlete = row.querySelector(".athlete-limit-name")?.value.trim() || "";
        const maxEvents = Number(row.querySelector(".athlete-limit-max")?.value || 4);
        if (!athlete) return;
        const key = athleteLimitKey(athlete);
        const current = limits.get(key);
        if (!current || maxEvents < current.maxEvents) limits.set(key, {athlete, maxEvents});
      });
      return [...limits.values()];
    }

    function restoreAthleteEventLimits(limits) {
      athleteLimitList.innerHTML = "";
      if (!Array.isArray(limits)) return;
      limits.forEach(limit => addAthleteLimitRow(limit.athlete || limit.name || "", limit.maxEvents || limit.max_events || 2));
    }

    function downloadJson(data, filename) {
      const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    function projectFileName(project) {
      const formState = project.form || {};
      const teamId = String(formState.schoolUrl || "").match(/team\/(\d+)/)?.[1] || "lineup";
      const division = formState.gender === "both" ? "both" : (formState.gender || activeDivision || "lineup");
      const date = new Date().toISOString().slice(0, 10);
      return `track-lineup-${safeFilePart(teamId)}-${safeFilePart(division)}-${date}.json`;
    }

    function safeFilePart(value) {
      return String(value || "lineup").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "lineup";
    }

    function fileDateFallback() {
      return "saved file";
    }

    function render(data) {
      if (data.mode === "both" && data.division_results) {
        divisionResults = data.division_results;
        activeDivision = "mens";
        divisionTabs.hidden = false;
        updateDivisionTabs();
        renderSingle(divisionResults[activeDivision] || {});
        return;
      }
      divisionResults = null;
      divisionTabs.hidden = true;
      renderSingle(data);
    }

    function updateDivisionTabs() {
      divisionTabs.querySelectorAll("[data-division]").forEach(button => {
        button.classList.toggle("active", button.dataset.division === activeDivision);
      });
    }

    function updateEventSortControls() {
      eventSortControls.querySelectorAll("[data-sort]").forEach(button => {
        button.classList.toggle("active", button.dataset.sort === activeEventSort);
      });
    }

    function currentEventSortOrders() {
      const context = currentResult?.edit_context || {};
      return {
        schedule: Array.isArray(context.schedule_order) && context.schedule_order.length
          ? context.schedule_order
          : OUTDOOR_EVENT_SORT_ORDERS.schedule,
        distance: Array.isArray(context.distance_order) && context.distance_order.length
          ? context.distance_order
          : OUTDOOR_EVENT_SORT_ORDERS.distance
      };
    }

    function currentAllEvents() {
      const events = currentResult?.edit_context?.events;
      return Array.isArray(events) && events.length ? events : OUTDOOR_ALL_EVENTS;
    }

    function currentRelayEvents() {
      const events = currentResult?.edit_context?.relay_events;
      return new Set(Array.isArray(events) && events.length ? events : OUTDOOR_RELAY_EVENTS);
    }

    function currentRunningOrder() {
      const order = currentResult?.edit_context?.running_order;
      return order && typeof order === "object" ? order : OUTDOOR_RUNNING_ORDER;
    }

    function renderSingleLegacy(data) {
      closeAthletePanel();
      athleteIndex = buildAthleteIndex(data);
      document.querySelector("#total-points").textContent = Number(data.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = data.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = data.scraped?.opponent_records || 0;
      renderTeamScores(data);
      errors.innerHTML = (data.errors || []).map(error => `<div class="error">${escapeHtml(error)}</div>`).join("");
      const eventPoints = data.event_points || {};
      const cards = [];
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        if (!entries.length) continue;
        cards.push(`
          <article class="event-card">
            <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(eventPoints[event] || 0).toFixed(1)} pts</span></div>
            <ol>${entries.map(entry => `<li><strong>${escapeHtml(entry.athlete)}</strong> ${escapeHtml(entry.adjusted_mark)} <span class="muted">(${formatPlace(entry.projected_place_label)} - ${formatPoints(entry.projected_points)} points)${entry.mark !== entry.adjusted_mark ? ` · PR ${escapeHtml(entry.mark)}` : ""}</span></li>`).join("")}</ol>
          </article>
        `);
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        cards.push(`
          <article class="event-card relay">
            <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(relay.projected_points || 0).toFixed(1)} pts</span></div>
            <ol>${relay.athletes.map(name => `<li><strong>${escapeHtml(name)}</strong></li>`).join("")}</ol>
            <div class="muted">Projected ${escapeHtml(relay.projected_mark || "n/a")} · ${escapeHtml(relay.method || "relay")} ${relay.source_mark ? `from ${escapeHtml(relay.source_mark)}` : ""}</div>
          </article>
        `);
      }
      results.innerHTML = cards.join("") || `<div class="panel">No lineup could be generated from the parsed records.</div>`;
    }

    function renderSingle(data) {
      closeAthletePanel();
      closeEventInfo();
      currentResult = data || {};
      athleteIndex = buildAthleteIndex(currentResult);
      document.querySelector("#total-points").textContent = Number(currentResult.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = currentResult.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = currentResult.scraped?.opponent_records || 0;
      renderTeamScores(currentResult);
      renderScoreChange();
      errors.innerHTML = (currentResult.errors || []).map(error => `<div class="error">${escapeHtml(error)}</div>`).join("");
      const eventPoints = currentResult.event_points || {};
      const cards = [];
      for (const event of sortedEventNames(currentResult)) {
        const entries = currentResult.lineup?.[event] || [];
        const relay = currentResult.relays?.[event];
        if (entries.length) cards.push(renderIndividualEventCard(event, entries, eventPoints));
        if (relay) cards.push(renderRelayEventCard(event, relay));
      }
      results.innerHTML = cards.join("") || `<div class="panel">No lineup could be generated from the parsed records.</div>`;
    }

    function scoreChangeKey() {
      return divisionResults ? `division:${activeDivision}` : "single";
    }

    function renderScoreChange() {
      const change = manualScoreChanges[scoreChangeKey()];
      if (!change) {
        scoreChange.hidden = true;
        scoreChange.classList.remove("positive", "negative", "neutral");
        return;
      }
      const before = Number(change.before || 0);
      const after = Number(change.after || 0);
      const delta = after - before;
      const isNeutral = Math.abs(delta) < 0.005;
      scoreChange.hidden = false;
      scoreChange.classList.toggle("positive", delta > 0.005);
      scoreChange.classList.toggle("negative", delta < -0.005);
      scoreChange.classList.toggle("neutral", isNeutral);
      scoreChangeDelta.textContent = isNeutral
        ? "0.0 points"
        : `${delta > 0 ? "+" : ""}${delta.toFixed(1)} points`;
      scoreChangeDetail.textContent = isNeutral
        ? `No estimated net change (${before.toFixed(1)} to ${after.toFixed(1)} projected points).`
        : `Projected team score changed from ${before.toFixed(1)} to ${after.toFixed(1)} points.`;
    }

    function renderIndividualEventCard(event, entries, eventPoints) {
      return `
        <article class="event-card">
          <div class="event-head"><div class="event-title"><h3>${escapeHtml(titleCase(event))}</h3>${eventInfoIcon(event)}</div><span class="points">${Number(eventPoints[event] || 0).toFixed(1)} pts</span></div>
          <ol>${entries.map(entry => `<li>${athleteButton(entry.athlete)} ${escapeHtml(entry.adjusted_mark)}${markOriginBadge(entry.mark_origin)} <span class="muted">(${formatPlace(entry.projected_place_label)} - ${formatPoints(entry.projected_points)} points)${entry.mark !== entry.adjusted_mark ? ` - PR ${escapeHtml(entry.mark)}` : ""}</span></li>`).join("")}</ol>
        </article>
      `;
    }

    function markOriginBadge(origin) {
      const normalized = String(origin || "").toLowerCase();
      if (!['historical', 'predicted'].includes(normalized)) return "";
      const label = normalized === 'predicted' ? 'Predicted' : 'Historical';
      return `<span class="mark-origin ${normalized}">${label}</span>`;
    }

    function eventInfoIcon(event) {
      const rows = (currentResult.event_standings?.[event] || []).slice(0, 8);
      if (!rows.length) return "";
      const isOpen = selectedEventInfo === event;
      return `
        <button class="event-info ${isOpen ? "open" : ""}" type="button" data-event-info="${escapeHtml(event)}" aria-expanded="${isOpen ? "true" : "false"}" aria-label="Projected top eight places for ${escapeHtml(titleCase(event))}">i
          <span class="event-info-popover" role="tooltip">
            <span class="standings-title">${escapeHtml(titleCase(event))} projected places</span>
            <span class="standings-list">
              ${rows.map(renderStandingRow).join("")}
            </span>
          </span>
        </button>
      `;
    }

    function toggleEventInfo(event) {
      selectedEventInfo = selectedEventInfo === event ? "" : event;
      updateEventInfoPopovers();
    }

    function closeEventInfo() {
      if (!selectedEventInfo) return;
      selectedEventInfo = "";
      updateEventInfoPopovers();
    }

    function updateEventInfoPopovers() {
      results.querySelectorAll(".event-info").forEach(button => {
        const isOpen = Boolean(selectedEventInfo) && button.dataset.eventInfo === selectedEventInfo;
        button.classList.toggle("open", isOpen);
        button.setAttribute("aria-expanded", isOpen ? "true" : "false");
      });
    }

    function renderStandingRow(row) {
      return `
        <span class="standing-row ${row.team_role === "school" ? "school" : ""}">
          <span class="standing-place">${escapeHtml(row.place_label || ordinalLabel(row.place))}</span>
          <span class="standing-name"><strong>${escapeHtml(row.athlete || "Unknown athlete")}</strong><span>${escapeHtml(row.school || "Unknown school")}</span></span>
          <span class="standing-mark">${escapeHtml(row.projected_mark || "n/a")}${markOriginBadge(row.mark_origin)}</span>
          <span class="standing-points">${formatPoints(row.projected_points)} pts</span>
        </span>
      `;
    }

    function renderRelayEventCard(event, relay) {
      return `
        <article class="event-card relay">
          <div class="event-head"><div class="event-title"><h3>${escapeHtml(titleCase(event))}</h3>${eventInfoIcon(event)}</div><span class="points">${Number(relay.projected_points || 0).toFixed(1)} pts</span></div>
          <ol>${relay.athletes.map(name => `<li>${athleteButton(name)}</li>`).join("")}</ol>
          <div class="muted">Projected ${escapeHtml(relay.projected_mark || "n/a")} - ${escapeHtml(relay.method || "relay")} ${relay.source_mark ? `from ${escapeHtml(relay.source_mark)}` : ""}</div>
        </article>
      `;
    }

    function renderTeamScores(data) {
      const scores = data?.team_points || {};
      let entries = Object.entries(scores);
      const schoolName = data?.scraped?.school_name || "Your Team";
      if (!entries.length && Number(data?.total_points || 0) > 0) {
        entries = [[schoolName, Number(data.total_points || 0)]];
      }
      let highlightedMain = false;
      teamPointsList.innerHTML = entries.length
        ? entries.map(([team, points]) => {
          const number = Number(points || 0);
          const isMain = team === schoolName || (!highlightedMain && Math.abs(number - Number(data?.total_points || 0)) < 0.01);
          if (isMain) highlightedMain = true;
          return `
            <span class="team-score ${isMain ? "school" : ""}">
              <span class="team-score-name">${escapeHtml(team)}</span>
              <span class="team-score-points">${number.toFixed(1)}</span>
            </span>
          `;
        }).join("")
        : `<span class="muted">No projected team scores yet</span>`;
    }

    function sortedEventNames(data) {
      const names = new Set();
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        if ((entries || []).length) names.add(event);
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        if (relay) names.add(event);
      }
      return [...names].sort((a, b) => eventSortRank(a) - eventSortRank(b) || titleCase(a).localeCompare(titleCase(b)));
    }

    function eventSortRank(event) {
      const orders = currentEventSortOrders();
      const order = orders[activeEventSort] || orders.schedule;
      const rank = order.indexOf(event);
      return rank >= 0 ? rank : 1000;
    }

    function athleteButton(name) {
      return `<button class="athlete-chip" type="button" data-athlete-key="${escapeHtml(athleteKey(name))}" data-athlete-name="${escapeHtml(name)}">${escapeHtml(name)}</button>`;
    }

    function buildAthleteIndex(data) {
      const index = new Map();
      for (const [event, entries] of Object.entries(data.lineup || {})) {
        for (const entry of entries || []) {
          addAthleteEvent(index, entry.athlete, {
            event,
            mark: entry.adjusted_mark || entry.mark || "n/a",
            markOrigin: entry.mark_origin || "",
            place: entry.projected_place_label || "unplaced",
            points: Number(entry.projected_points || 0),
            type: "Individual"
          });
        }
      }
      for (const [event, relay] of Object.entries(data.relays || {})) {
        for (const name of relay.athletes || []) {
          addAthleteEvent(index, name, {
            event,
            mark: relay.projected_mark || "n/a",
            place: relayPlaceFromPoints(Number(relay.projected_points || 0)),
            points: Number(relay.projected_points || 0),
            type: "Relay"
          });
        }
      }
      return index;
    }

    function addAthleteEvent(index, athlete, detail) {
      const key = athleteKey(athlete);
      if (!index.has(key)) index.set(key, {name: athlete, events: []});
      index.get(key).events.push(detail);
    }

    function openAthletePanel(name) {
      const key = athleteKey(name);
      const athlete = athleteIndex.get(key);
      if (!athlete) return;
      selectedAthleteKey = key;
      highlightAthlete();
      athletePanelName.textContent = athlete.name;
      const maxEvents = athleteMaxEvents(athlete.name);
      athletePanelCount.textContent = `${athlete.events.length} ${athlete.events.length === 1 ? "event" : "events"}${maxEvents < 4 ? ` (max ${maxEvents})` : ""}`;
      const athleteEvents = [...athlete.events].sort((a, b) => eventSortRank(a.event) - eventSortRank(b.event) || titleCase(a.event).localeCompare(titleCase(b.event)));
      const addAction = athlete.events.length < maxEvents ? `
        <li class="athlete-panel-actions">
          <button class="mini-button add-action" type="button" data-edit-action="add" data-athlete="${escapeHtml(athlete.name)}" data-event="">Add Event</button>
        </li>
      ` : "";
      athletePanelEvents.innerHTML = addAction + athleteEvents.map(detail => `
        <li class="athlete-event-item">
          <div class="athlete-event-top">
            <span>${escapeHtml(titleCase(detail.event))}</span>
            <span class="athlete-event-mark">${escapeHtml(detail.mark)}${markOriginBadge(detail.markOrigin)}</span>
          </div>
          <div class="athlete-event-meta">
            <span class="athlete-pill">${escapeHtml(detail.type)}</span>
            <span class="athlete-pill">${formatPlace(detail.place)}</span>
            <span class="athlete-pill points-pill">${formatPoints(detail.points)} pts</span>
          </div>
          <div class="athlete-event-actions">
            <button class="mini-button move-action" type="button" data-edit-action="move" data-athlete="${escapeHtml(athlete.name)}" data-event="${escapeHtml(detail.event)}">Move</button>
            <button class="mini-button remove-action" type="button" data-edit-action="remove" data-athlete="${escapeHtml(athlete.name)}" data-event="${escapeHtml(detail.event)}">Remove</button>
          </div>
        </li>
      `).join("");
      athletePanel.hidden = false;
      document.body.classList.add("athlete-panel-open");
      placePanelOnSide();
    }

    function closeAthletePanel() {
      editState = null;
      selectedAthleteKey = "";
      highlightAthlete();
      athletePanel.hidden = true;
      document.body.classList.remove("athlete-panel-open");
      panelDrag = null;
    }

    function highlightAthlete() {
      results.querySelectorAll(".athlete-chip").forEach(button => {
        button.classList.toggle("selected", Boolean(selectedAthleteKey) && button.dataset.athleteKey === selectedAthleteKey);
      });
    }

    function placePanelOnSide() {
      if (athletePanel.hidden) return;
      const height = athletePanel.offsetHeight || 420;
      athletePanel.style.left = `${window.innerWidth <= 880 ? 12 : 18}px`;
      athletePanel.style.top = `${window.innerWidth <= 880 ? Math.max(12, window.innerHeight - height - 12) : 112}px`;
      athletePanel.style.right = "auto";
      athletePanel.style.bottom = "auto";
    }

    function startPanelDrag(event) {
      if (event.target.closest(".athlete-panel-close")) return;
      const rect = athletePanel.getBoundingClientRect();
      panelDrag = {
        pointerId: event.pointerId,
        offsetX: event.clientX - rect.left,
        offsetY: event.clientY - rect.top
      };
      athletePanelHead.setPointerCapture(event.pointerId);
    }

    function dragAthletePanel(event) {
      if (!panelDrag || event.pointerId !== panelDrag.pointerId) return;
      const width = athletePanel.offsetWidth;
      const height = athletePanel.offsetHeight;
      const left = Math.min(Math.max(8, event.clientX - panelDrag.offsetX), Math.max(8, window.innerWidth - width - 8));
      const top = Math.min(Math.max(8, event.clientY - panelDrag.offsetY), Math.max(8, window.innerHeight - height - 8));
      athletePanel.style.left = `${left}px`;
      athletePanel.style.top = `${top}px`;
      athletePanel.style.right = "auto";
      athletePanel.style.bottom = "auto";
    }

    function stopPanelDrag(event) {
      if (!panelDrag || event.pointerId !== panelDrag.pointerId) return;
      panelDrag = null;
    }

    function startCoachEdit(mode, athlete, sourceEvent) {
      if (!currentResult?.edit_context) {
        errors.innerHTML = `<div class="error">Generate a lineup before editing assignments.</div>`;
        return;
      }
      const maxEvents = athleteMaxEvents(athlete);
      if (mode === "add" && athleteEventList(athlete).length >= maxEvents) {
        errors.innerHTML = `<div class="error">${escapeHtml(athlete)} is already at the maximum of ${maxEvents} ${maxEvents === 1 ? "event" : "events"}.</div>`;
        return;
      }
      editState = {
        mode,
        athlete,
        sourceEvent: sourceEvent || "",
        targetEvent: "",
        sourceReplacement: "",
        targetRemoval: ""
      };
      selectedAthleteKey = athleteKey(athlete);
      highlightAthlete();
      renderCoachEditPanel();
      athletePanel.hidden = false;
      document.body.classList.add("athlete-panel-open");
      placePanelOnSide();
    }

    function renderCoachEditPanel() {
      if (!editState) return;
      athletePanelName.textContent = editPanelTitle();
      athletePanelCount.textContent = editState.sourceEvent ? titleCase(editState.sourceEvent) : "New event";
      const needsSourceReplacement = editState.mode !== "add";
      const needsTargetEvent = editState.mode !== "remove";
      const sourceSuggestions = needsSourceReplacement ? replacementSuggestions(editState.sourceEvent, editState.athlete) : [];
      if (needsSourceReplacement && editState.sourceReplacement && !sourceSuggestions.some(item => item.athlete === editState.sourceReplacement)) {
        editState.sourceReplacement = "";
      }
      const targetStatus = needsTargetEvent && editState.targetEvent
        ? targetEventStatus(editState.athlete, editState.sourceEvent, editState.targetEvent)
        : {reasons: []};
      const targetChoices = needsTargetEvent && editState.targetEvent
        ? eventAthletes(editState.targetEvent).filter(name => athleteKey(name) !== athleteKey(editState.athlete))
        : [];
      if (editState.targetRemoval && !targetChoices.includes(editState.targetRemoval)) {
        editState.targetRemoval = "";
      }
      const canApply = Boolean(
        editState.mode === "remove"
          ? editState.sourceReplacement
          : (
            editState.targetEvent
            && !targetStatus.reasons.length
            && (!targetChoices.length || editState.targetRemoval)
            && (editState.mode === "add" || editState.sourceReplacement)
          )
      );
      athletePanelEvents.innerHTML = `
        <li class="coach-edit-block">
          <p class="coach-edit-title">${escapeHtml(editPanelLead())}</p>
          ${needsTargetEvent ? renderEventScheduleChoices(targetStatus) : ""}
          ${needsSourceReplacement ? renderReplacementQuestion(
            "sourceReplacement",
            `Who will replace ${editState.athlete} in ${titleCase(editState.sourceEvent)}?`,
            sourceSuggestions,
            editState.sourceReplacement
          ) : ""}
          ${needsTargetEvent && editState.targetEvent ? renderTargetRemovalQuestion(targetChoices) : ""}
          <div class="edit-actions">
            <button class="mini-button" type="button" data-edit-command="back">Back</button>
            <button type="button" data-edit-command="apply" ${canApply ? "" : "disabled"}>Apply Change</button>
          </div>
        </li>
      `;
    }

    function editPanelTitle() {
      if (editState.mode === "add") return `Add event for ${editState.athlete}`;
      if (editState.mode === "move") return `Move ${editState.athlete}`;
      return `Remove ${editState.athlete}`;
    }

    function editPanelLead() {
      if (editState.mode === "add") return `Add an event for ${editState.athlete}`;
      if (editState.mode === "move") return `Move ${editState.athlete} from ${titleCase(editState.sourceEvent)}`;
      return `Remove ${editState.athlete} from ${titleCase(editState.sourceEvent)}`;
    }

    function renderEventScheduleChoices(selectedStatus) {
      const currentEvents = new Set(athleteEventList(editState.athlete));
      const selectedAlert = editState.targetEvent
        ? (
          selectedStatus.reasons.length
            ? `<div class="edit-alert">${selectedStatus.reasons.map(escapeHtml).join(" ")}</div>`
            : `<div class="edit-alert ok">Valid target selected: ${escapeHtml(titleCase(editState.targetEvent))}.</div>`
        )
        : `<div class="edit-warning">Current events are light blue and disabled. Yellow events need review; click one to see why.</div>`;
      return `
        <div>
          <p class="coach-edit-question">Which event should ${escapeHtml(editState.athlete)} move into?</p>
          ${selectedAlert}
          <div class="event-choice-grid">
            ${currentAllEvents().map(event => {
              const status = targetEventStatus(editState.athlete, editState.sourceEvent, event);
              const isCurrent = currentEvents.has(event);
              const classes = [
                "event-choice",
                isCurrent ? "current" : "",
                !isCurrent && status.reasons.length ? "warning" : "",
                editState.targetEvent === event ? "selected" : ""
              ].filter(Boolean).join(" ");
              return `<button class="${classes}" type="button" data-target-event="${escapeHtml(event)}" ${isCurrent ? "disabled" : ""}>${escapeHtml(titleCase(event))}</button>`;
            }).join("")}
          </div>
        </div>
      `;
    }

    function renderReplacementQuestion(role, question, choices, selected) {
      return `
        <div>
          <p class="coach-edit-question">${escapeHtml(question)}</p>
          <div class="choice-list">
            ${choices.length ? choices.map(choice => renderChoiceCard(role, choice, selected)).join("") : `<div class="edit-warning">No available recorded replacement was found for ${escapeHtml(titleCase(editState.sourceEvent))}.</div>`}
          </div>
        </div>
      `;
    }

    function renderTargetRemovalQuestion(choices) {
      if (!choices.length) {
        return `<div class="edit-warning">No athlete needs to be removed from ${escapeHtml(titleCase(editState.targetEvent))} because there is an open spot.</div>`;
      }
      return `
        <div>
          <p class="coach-edit-question">${escapeHtml(`Who will ${editState.athlete} replace in ${titleCase(editState.targetEvent)}?`)}</p>
          <div class="choice-list">
            ${choices.map(name => renderChoiceCard("targetRemoval", {
              athlete: name,
              mark: athleteEventMark(name, editState.targetEvent),
              warnings: []
            }, editState.targetRemoval)).join("")}
          </div>
        </div>
      `;
    }

    function renderChoiceCard(role, choice, selected) {
      const warnings = choice.warnings?.length ? `<span class="choice-warning-text">${escapeHtml(choice.warnings.join(" "))}</span>` : "";
      const eventSummary = athleteEventList(choice.athlete).map(titleCase).join(", ") || "No current events";
      return `
        <button class="choice-card ${selected === choice.athlete ? "selected" : ""}" type="button" data-choice-role="${escapeHtml(role)}" data-athlete="${escapeHtml(choice.athlete)}">
          <strong class="choice-name-line">
            ${escapeHtml(choice.athlete)}
            <span class="event-asterisk" tabindex="0">*
              <span class="event-asterisk-tip">Current events: ${escapeHtml(eventSummary)}</span>
            </span>
          </strong>
          <span>${escapeHtml(choice.mark || "recorded mark")}</span>
          ${warnings}
        </button>
      `;
    }

    function handleEditCommand(command) {
      if (!editState) return;
      if (command === "back") {
        openAthletePanel(editState.athlete);
        return;
      }
      if (command === "apply") applyCoachEdit();
    }

    async function applyCoachEdit() {
      if (!editState || !currentResult) return;
      const next = cloneResult(currentResult);
      try {
        if (editState.mode === "remove") {
          replaceEventAthlete(next, editState.sourceEvent, editState.athlete, editState.sourceReplacement);
        } else if (editState.mode === "add") {
          addAthleteToEvent(next, editState.targetEvent, editState.athlete, editState.targetRemoval);
        } else {
          replaceEventAthlete(next, editState.sourceEvent, editState.athlete, editState.sourceReplacement);
          addAthleteToEvent(next, editState.targetEvent, editState.athlete, editState.targetRemoval);
        }
        await rescoreEditedLineup(next);
      } catch (error) {
        errors.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
      }
    }

    async function rescoreEditedLineup(next) {
      athletePanelCount.textContent = "rescoring...";
      const previousPoints = Number(currentResult?.total_points || 0);
      const changeKey = scoreChangeKey();
      const response = await fetch("/api/rescore", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          lineup: next.lineup || {},
          relays: next.relays || {},
          edit_context: currentResult.edit_context || {}
        })
      });
      const data = await response.json();
      if (!response.ok) throw new Error((data.errors || [data.error || "Could not rescore edited lineup."])[0]);
      manualScoreChanges[changeKey] = {
        before: previousPoints,
        after: Number(data.total_points || 0)
      };
      if (divisionResults) divisionResults[activeDivision] = data;
      renderSingle(data);
    }

    function replaceEventAthlete(result, event, outgoingAthlete, incomingAthlete) {
      if (!incomingAthlete) throw new Error(`Choose a replacement for ${titleCase(event)}.`);
      if (currentRelayEvents().has(event)) {
        replaceRelayAthlete(result, event, outgoingAthlete, incomingAthlete);
        return;
      }
      const entries = result.lineup?.[event] || [];
      const nextEntries = entries.filter(entry => athleteKey(entry.athlete) !== athleteKey(outgoingAthlete));
      if (!nextEntries.some(entry => athleteKey(entry.athlete) === athleteKey(incomingAthlete))) {
        nextEntries.push(entryForAthlete(incomingAthlete, event));
      }
      result.lineup[event] = nextEntries.slice(0, 3);
    }

    function addAthleteToEvent(result, event, athlete, removedAthlete) {
      if (currentRelayEvents().has(event)) {
        if (!removedAthlete) throw new Error(`Choose who ${athlete} will replace in ${titleCase(event)}.`);
        replaceRelayAthlete(result, event, removedAthlete, athlete);
        return;
      }
      const entries = [...(result.lineup?.[event] || [])];
      const nextEntries = removedAthlete
        ? entries.filter(entry => athleteKey(entry.athlete) !== athleteKey(removedAthlete))
        : entries;
      if (!nextEntries.some(entry => athleteKey(entry.athlete) === athleteKey(athlete))) {
        nextEntries.push(entryForAthlete(athlete, event));
      }
      result.lineup[event] = nextEntries.slice(0, 3);
    }

    function replaceRelayAthlete(result, event, outgoingAthlete, incomingAthlete) {
      const relay = result.relays?.[event];
      if (!relay) throw new Error(`${titleCase(event)} is not currently in the lineup.`);
      const index = (relay.athletes || []).findIndex(name => athleteKey(name) === athleteKey(outgoingAthlete));
      if (index < 0) throw new Error(`${outgoingAthlete} is not in ${titleCase(event)}.`);
      const oldLeg = relayLegValue(event, outgoingAthlete) ?? relayLegTimeAt(relay, index);
      const newLeg = relayLegValue(event, incomingAthlete);
      if (!Number.isFinite(newLeg)) throw new Error(`${incomingAthlete} does not have a relay-compatible mark for ${titleCase(event)}.`);
      relay.athletes[index] = incomingAthlete;
      const oldSeconds = Number(relay.projected_seconds || 0);
      relay.projected_seconds = Math.max(0, oldSeconds + newLeg - oldLeg);
      relay.projected_mark = formatSeconds(relay.projected_seconds);
      relay.method = "coach edited";
      relay.source_mark = "old relay time plus leg-time difference";
      relay.leg_times = Array.isArray(relay.leg_times) && relay.leg_times.length === 4
        ? relay.leg_times.map((value, legIndex) => legIndex === index ? newLeg : value)
        : [];
    }

    function replacementSuggestions(event, removedAthlete) {
      const existing = new Set(eventAthletes(event).map(athleteKey));
      const candidates = eventCandidates(event)
        .filter(choice => athleteKey(choice.athlete) !== athleteKey(removedAthlete))
        .filter(choice => !existing.has(athleteKey(choice.athlete)))
        .filter(choice => athleteEventList(choice.athlete).length < athleteMaxEvents(choice.athlete))
        .filter(choice => !hasAdjacentRunningEvent(choice.athlete, event))
        .map(choice => ({...choice, warnings: replacementWarnings(choice.athlete, event)}));
      candidates.sort((a, b) => eventBetterValue(event, a.value, b.value));
      return candidates.slice(0, 6);
    }

    function eventCandidates(event) {
      if (currentRelayEvents().has(event)) {
        const legs = currentResult.edit_context?.relay_leg_values?.[event] || {};
        return Object.entries(legs).map(([athlete, value]) => ({
          athlete,
          value: Number(value),
          mark: formatSeconds(Number(value))
        }));
      }
      const best = new Map();
      for (const perf of currentResult.edit_context?.school_performances || []) {
        if (perf.event !== event) continue;
        const key = athleteKey(perf.athlete);
        const current = best.get(key);
        if (!current || eventBetterValue(event, perf.value, current.value) < 0) {
          best.set(key, {athlete: perf.athlete, value: Number(perf.value), mark: perf.mark});
        }
      }
      return [...best.values()];
    }

    function replacementWarnings(athlete, event) {
      const events = [...athleteEventList(athlete), event];
      const warnings = [];
      if (hasForbidden400Double(events)) warnings.push("400/4x400 warning.");
      if (distanceLimitExceeded(events)) warnings.push("distance load warning.");
      return warnings;
    }

    function targetEventStatus(athlete, sourceEvent, targetEvent) {
      const currentEvents = athleteEventList(athlete);
      const afterMove = currentEvents.filter(event => event !== sourceEvent);
      const reasons = [];
      if (targetEvent === sourceEvent) reasons.push(`${athlete} is already in ${titleCase(sourceEvent)}.`);
      if (afterMove.includes(targetEvent)) reasons.push(`${athlete} is already entered in ${titleCase(targetEvent)}.`);
      if (!hasRecordedEventMark(athlete, targetEvent)) reasons.push(`${athlete} does not have a recorded mark for ${titleCase(targetEvent)}.`);
      const proposedEvents = [...afterMove, targetEvent];
      const maxEvents = athleteMaxEvents(athlete);
      if (proposedEvents.length > maxEvents) reasons.push(`${athlete} would exceed the maximum of ${maxEvents} ${maxEvents === 1 ? "event" : "events"}.`);
      if (hasForbidden400Double(proposedEvents)) reasons.push(`${athlete} would be in both the 400m and 4x400 relay.`);
      if (distanceLimitExceeded(proposedEvents)) reasons.push(`${athlete} would exceed the distance-event limit.`);
      if (hasAdjacentRunningPair(proposedEvents)) reasons.push(`${athlete} would have back to back running events.`);
      return {reasons};
    }

    function eventAthletes(event) {
      if (currentRelayEvents().has(event)) return [...(currentResult.relays?.[event]?.athletes || [])];
      return (currentResult.lineup?.[event] || []).map(entry => entry.athlete);
    }

    function athleteEventList(athlete) {
      const key = athleteKey(athlete);
      const events = [];
      for (const [event, entries] of Object.entries(currentResult?.lineup || {})) {
        if ((entries || []).some(entry => athleteKey(entry.athlete) === key)) events.push(event);
      }
      for (const [event, relay] of Object.entries(currentResult?.relays || {})) {
        if ((relay.athletes || []).some(name => athleteKey(name) === key)) events.push(event);
      }
      return events;
    }

    function athleteEventMark(athlete, event) {
      if (currentRelayEvents().has(event)) {
        const value = relayLegValue(event, athlete);
        return Number.isFinite(value) ? formatSeconds(value) : "relay leg";
      }
      const entry = (currentResult.lineup?.[event] || []).find(item => athleteKey(item.athlete) === athleteKey(athlete));
      if (entry) return entry.adjusted_mark || entry.mark || "recorded mark";
      const perf = bestPerformance(athlete, event);
      return perf?.mark || "recorded mark";
    }

    function hasRecordedEventMark(athlete, event) {
      if (currentRelayEvents().has(event)) return Number.isFinite(relayLegValue(event, athlete));
      return Boolean(bestPerformance(athlete, event));
    }

    function bestPerformance(athlete, event) {
      const key = athleteKey(athlete);
      let best = null;
      for (const perf of currentResult.edit_context?.school_performances || []) {
        if (perf.event !== event || athleteKey(perf.athlete) !== key) continue;
        if (!best || eventBetterValue(event, perf.value, best.value) < 0) best = perf;
      }
      return best;
    }

    function entryForAthlete(athlete, event) {
      const perf = bestPerformance(athlete, event);
      return {
        athlete,
        mark: perf?.mark || "n/a",
        adjusted_mark: perf?.mark || "n/a",
        projected_place_label: "unplaced",
        projected_points: 0
      };
    }

    function relayLegValue(event, athlete) {
      const value = currentResult.edit_context?.relay_leg_values?.[event]?.[athlete];
      return value === undefined ? NaN : Number(value);
    }

    function relayLegTimeAt(relay, index) {
      const explicit = Number(relay.leg_times?.[index]);
      if (Number.isFinite(explicit)) return explicit;
      const projected = Number(relay.projected_seconds || 0);
      return projected > 0 ? projected / 4 : 0;
    }

    function hasAdjacentRunningEvent(athlete, event) {
      const runningOrder = currentRunningOrder();
      if (!(event in runningOrder)) return false;
      const order = runningOrder[event];
      return athleteEventList(athlete).some(existing => existing in runningOrder && Math.abs(runningOrder[existing] - order) === 1);
    }

    function hasAdjacentRunningPair(events) {
      const runningOrder = currentRunningOrder();
      const ordered = events.filter(event => event in runningOrder).map(event => runningOrder[event]).sort((a, b) => a - b);
      return ordered.some((value, index) => index > 0 && value - ordered[index - 1] <= 1);
    }

    function distanceLimitExceeded(events) {
      if (!events.some(event => DISTANCE_EVENTS.has(event))) return false;
      if (events.some(event => LONG_DISTANCE_EVENTS.has(event))) return events.length > 2;
      if (events.length <= 2) return false;
      return events.length > 3 || !isUnder1600DistanceLoad(events);
    }

    function isUnder1600DistanceLoad(events) {
      return events.some(event => DISTANCE_EVENTS.has(event))
        && !events.some(event => LONG_DISTANCE_EVENTS.has(event))
        && events.every(event => DISTANCE_THREE_EVENT_ALLOWED_EVENTS.has(event));
    }

    function hasForbidden400Double(events) {
      return events.includes("400m")
        && events.includes("4x400 relay")
        && !isUnder1600DistanceLoad(events);
    }

    function eventBetterValue(event, a, b) {
      return FIELD_EVENTS.has(event) ? Number(b) - Number(a) : Number(a) - Number(b);
    }

    function cloneResult(value) {
      return JSON.parse(JSON.stringify(value));
    }

    function formatSeconds(value) {
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) return "n/a";
      if (seconds >= 60) {
        const minutes = Math.floor(seconds / 60);
        const rest = (seconds - minutes * 60).toFixed(2).padStart(5, "0");
        return `${minutes}:${rest}`;
      }
      return seconds.toFixed(2);
    }

    function titleCase(value) {
      if (value === "55h" || value === "60h") return `${value.slice(0, -1)}m Hurdles`;
      return value.replace(/\b\w/g, letter => letter.toUpperCase()).replace("Relay", "Relay");
    }

    function formatPoints(value) {
      const number = Number(value || 0);
      return Number.isInteger(number) ? String(number) : number.toFixed(1);
    }

    function formatPlace(label) {
      return label && label !== "unplaced" ? `${escapeHtml(label)} place` : "unplaced";
    }

    function relayPlaceFromPoints(points) {
      const places = {10: "1st", 8: "2nd", 6: "3rd", 4: "4th", 2: "5th"};
      return places[points] || "unplaced";
    }

    function ordinalLabel(value) {
      const number = Number(value || 0);
      if (!number) return "n/a";
      const mod100 = number % 100;
      if (mod100 >= 10 && mod100 <= 20) return `${number}th`;
      const suffix = {1: "st", 2: "nd", 3: "rd"}[number % 10] || "th";
      return `${number}${suffix}`;
    }

    function athleteKey(value) {
      return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
    }

    function athleteLimitKey(value) {
      return String(value || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    }

    function athleteMaxEvents(athlete) {
      const context = currentResult?.edit_context || {};
      const limits = context.athlete_event_limits || {};
      const teamLimit = Number(context.team_event_limit || context.max_events_per_athlete || 4);
      const athleteLimit = Number(limits[athleteLimitKey(athlete)] || 4);
      const limit = Math.min(teamLimit, athleteLimit);
      return Number.isInteger(limit) && limit >= 1 && limit <= 4 ? limit : 4;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#039;"
      }[char]));
    }
  </script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    """Tiny HTTP app with a page route and JSON API routes."""

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self.send_text(HTML_PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self.send_json(
                {
                    "status": "ok",
                    "version": APP_VERSION,
                    "fetch_strategy": "athletic-net-first-party-api",
                }
            )
        else:
            self.send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/rescore":
                self.send_json(asdict(rescore_edited_result(payload)))
                return
            if self.path not in {"/api/demo", "/api/optimize"}:
                self.send_json({"error": "Not found"}, status=404)
                return
            season_type = clean_text(payload.get("seasonType", "outdoor")).lower() or "outdoor"
            indoor_sprint_distance = clean_text(
                payload.get("indoorSprintDistance", "55")
            ).lower().replace("m", "") or "55"
            try:
                meet_config_for(season_type, indoor_sprint_distance)
            except ValueError as exc:
                self.send_json({"errors": [str(exc)], "total_points": 0}, status=400)
                return
            if self.path == "/api/demo":
                self.send_json(
                    asdict(
                        demo_result(
                            payload.get("athleteEventLimits", []),
                            season_type,
                            indoor_sprint_distance,
                        )
                    )
                )
                return
            school_url = clean_text(payload.get("schoolUrl", ""))
            opponent_urls = payload.get("opponentUrls", [])
            injured_athletes = payload.get("injuredAthletes", [])
            athlete_event_limits = payload.get("athleteEventLimits", [])
            team_event_limit = payload.get("teamEventLimit", MAX_EVENTS_PER_ATHLETE)
            gender = clean_text(payload.get("gender", "mens")).lower() or "mens"
            if not school_url:
                self.send_json({"errors": ["Enter a school Athletic.net event records URL."], "total_points": 0}, status=400)
                return
            cleaned_opponent_urls = [clean_text(url) for url in opponent_urls]
            cleaned_injured_athletes = [clean_text(name) for name in injured_athletes]
            try:
                cleaned_event_limits = normalize_athlete_event_limits(athlete_event_limits)
                cleaned_team_event_limit = normalize_team_event_limit(team_event_limit)
            except ValueError as exc:
                self.send_json({"errors": [str(exc)], "total_points": 0}, status=400)
                return
            if gender in {"both", "all"}:
                self.send_json(
                    run_optimizer_both(
                        school_url,
                        cleaned_opponent_urls,
                        cleaned_injured_athletes,
                        cleaned_event_limits,
                        season_type,
                        indoor_sprint_distance,
                        cleaned_team_event_limit,
                    )
                )
                return
            if gender not in {"mens", "womens"}:
                self.send_json({"errors": [f"Unknown division: {gender}"], "total_points": 0}, status=400)
                return
            result = run_optimizer(
                school_url,
                cleaned_opponent_urls,
                gender,
                cleaned_injured_athletes,
                cleaned_event_limits,
                season_type,
                indoor_sprint_distance,
                cleaned_team_event_limit,
            )
            self.send_json(asdict(result))
        except Exception as exc:
            self.send_json(
                {"errors": [str(exc), traceback.format_exc(limit=1)], "total_points": 0, "lineup": {}, "relays": {}},
                status=500,
            )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, body: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(body, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main() -> None:
    """Start the local web server."""
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    requested_port = int(os.environ.get("PORT") or (sys.argv[1] if len(sys.argv) > 1 else 8000))
    server = None
    port = requested_port
    port_candidates = [requested_port] if os.environ.get("PORT") else range(requested_port, requested_port + 20)
    for candidate in port_candidates:
        try:
            server = ThreadingHTTPServer((host, candidate), AppHandler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        if os.environ.get("PORT"):
            raise RuntimeError(f"Could not bind to required PORT {requested_port}")
        raise RuntimeError(f"No open port found from {requested_port} to {requested_port + 19}")
    try:
        if sys.stdout:
            print(f"Track Lineup Optimizer (Made by Jayden Yang) {APP_VERSION} running at http://{host}:{port}")
    except OSError:
        pass
    server.serve_forever()


if __name__ == "__main__":
    main()
