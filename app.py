from __future__ import annotations

import html
import json
import math
import re
import sys
import traceback
from collections import defaultdict
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
APP_VERSION = "2026.06.25-event-sort-v15"
MAX_EVENTS_PER_ATHLETE = 4
MAX_INDIVIDUAL_ENTRIES = 3
ELITE_ATHLETE_COUNT = 5
MAX_ELITE_REPLACEMENTS = 15

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

FIELD_EVENTS = {"high jump", "pole vault", "discus", "shot put", "long jump", "triple jump"}
TRACK_EVENTS = set(RUNNING_ORDER)
RELAY_EVENTS = {"4x100 relay", "4x200 relay", "4x400 relay", "4x800 relay"}
DISTANCE_EVENTS = {"4x800 relay", "800m", "1600m", "3200m"}
ELITE_INDIVIDUAL_EVENTS = {"100m", "200m", "400m"}
SPRINT_RELAY_EVENTS = {"4x100 relay", "4x200 relay", "4x400 relay"}
EVENTS = list(RUNNING_ORDER) + ["long jump", "triple jump", "high jump", "pole vault", "shot put", "discus"]

RELAY_BASE_EVENT = {
    "4x100 relay": "100m",
    "4x200 relay": "200m",
    "4x400 relay": "400m",
    "4x800 relay": "800m",
}

RELAY_EVENT_FOR_BASE = {base_event: relay_event for relay_event, base_event in RELAY_BASE_EVENT.items()}

RELAY_SYNTHETIC_CREDIT = {
    "4x100 relay": 2.7,
    "4x200 relay": 3.0,
    "4x400 relay": 3.0,
    "4x800 relay": 2.0,
}

HISTORIC_RELAY_IMPROVEMENT = {
    "4x100 relay": 0.2,
    "4x200 relay": 0.2,
    "4x400 relay": 0.2,
    "4x800 relay": 0.0,
}


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
    scraped: dict[str, int]
    errors: list[str]


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
        r"/team/(\d+)/track-and-field-(outdoor|indoor)/(\d{4})/event-records(?:/|$)",
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
    return team_id, season_id, canonical


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
    source_name = source or canonical_url
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
        page_source = source or extract_team_name(page) or canonical_url
        performances, relay_history = parse_athletic_records_html(page, page_source, team_role, gender)
        if performances:
            return ScrapeResult(performances, relay_history)

    raise RuntimeError(
        f"No event records could be loaded for {canonical_url}. " + " ".join(errors)
    )


def parse_athletic_api_data(
    payload: dict[str, Any], source: str, team_role: str, gender: str = "mens"
) -> ScrapeResult:
    """Parse Athletic.net's first-party event-records JSON response."""
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
                    source=source,
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
                    source=source,
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
                source=source,
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
    if not title_match:
        return None
    title = clean_text(title_match.group(1))
    return title.split("|")[0].strip() if title else None


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
        (r"\b(110|100)\s*(m|meter)?\s*(hurdles|h)\b|\bhigh hurdles\b", "110h"),
        (r"\b300\s*(m|meter)?\s*(hurdles|h)\b|\bintermediate hurdles\b", "300h"),
        (r"\b3200\s*(m|meter|meters)?\b|\btwo mile\b", "3200m"),
        (r"\b1600\s*(m|meter|meters)?\b|\bone mile\b", "1600m"),
        (r"\b800\s*(m|meter|meters)?\b", "800m"),
        (r"\b400\s*(m|meter|meters)?\b", "400m"),
        (r"\b200\s*(m|meter|meters)?\b", "200m"),
        (r"\b100\s*(m|meter|meters)?\b", "100m"),
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


def is_better(candidate: float, incumbent: float, is_time: bool) -> bool:
    """Compare times low-to-high and field marks high-to-low."""
    return candidate < incumbent if is_time else candidate > incumbent


def compute_scores(school: list[Performance], opponents: list[Performance]) -> dict[tuple[str, str], float]:
    """Estimate each school athlete's points by simulating their PR against all opponents."""
    potentials: dict[tuple[str, str], float] = {}
    for event in EVENTS:
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
) -> dict[str, Any]:
    """Build and locally improve a lineup while respecting event limits and race spacing."""
    school_relay_history = school_relay_history or []
    opponent_relay_history = opponent_relay_history or []
    school_relay_splits = school_relay_splits or []
    opponent_relay_splits = opponent_relay_splits or []
    potentials = compute_scores(school, opponents)
    by_athlete = group_school_events(school, potentials)
    athlete_order = rank_athletes_by_value(by_athlete)
    lineup: dict[str, list[str]] = {event: [] for event in EVENTS if event not in RELAY_EVENTS}
    relays: dict[str, RelaySelection] = {}
    athlete_events: dict[str, list[str]] = defaultdict(list)

    for athlete in athlete_order:
        for event, _mark, score in by_athlete[athlete][:3]:
            if event in RELAY_EVENTS or score <= 0:
                continue
            try_add_entry(lineup, athlete_events, athlete, event)

    for relay_event in RELAY_EVENTS:
        team = choose_relay_team(
            relay_event,
            school,
            opponents,
            athlete_events,
            school_relay_history,
            opponent_relay_history,
            school_relay_splits,
            opponent_relay_splits,
        )
        if team:
            relays[relay_event] = team
            for athlete in team.athletes:
                athlete_events[athlete].append(relay_event)

    fill_remaining_spots(lineup, school, athlete_events)
    lineup, relays = optimize_lineup(
        lineup,
        relays,
        school,
        opponents,
        athlete_events,
        opponent_relay_history,
        opponent_relay_splits,
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
    )
    missing_events = [
        event
        for event in EVENTS
        if (event in RELAY_EVENTS and event not in relays)
        or (event not in RELAY_EVENTS and not lineup.get(event))
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
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Fill every supported event, using depth athletes for non-scoring relays."""
    athlete_events = collect_athlete_events(lineup, relays)
    potentials = compute_scores(school, opponents)
    fill_remaining_spots(lineup, school, athlete_events)
    force_empty_individual_events(lineup, school, athlete_events, potentials)

    for relay_event in sorted(RELAY_EVENTS, key=event_sort_value):
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
            )
        if selection:
            relays[relay_event] = selection
            for athlete in selection.athletes:
                athlete_events[athlete].append(relay_event)

    fill_remaining_spots(lineup, school, athlete_events)
    force_empty_individual_events(lineup, school, athlete_events, potentials)
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


def minimum_event_removals(existing_events: list[str], new_event: str) -> list[str] | None:
    """Find the fewest removable individual events needed to make a new event valid."""
    removable = [event for event in existing_events if event not in RELAY_EVENTS]
    for count in range(len(removable) + 1):
        for removed in combinations(removable, count):
            remaining = list(existing_events)
            for event in removed:
                remaining.remove(event)
            if can_take_event(remaining, new_event):
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
) -> RelaySelection | None:
    """Free low-cost individual assignments to guarantee a four-person relay."""
    candidates = best_relay_leg_candidates(relay_event, school, relay_history, relay_splits)
    options: list[tuple[float, int, float, str, list[str]]] = []
    for athlete, leg_time in candidates:
        removals = minimum_event_removals(athlete_events[athlete], relay_event)
        if removals is None:
            continue
        cost = sum(potentials.get((athlete, event), 0.0) for event in removals)
        options.append((cost, len(removals), leg_time, athlete, removals))
    if len(options) < 4:
        return None
    chosen = sorted(options, key=lambda item: (item[0], item[1], item[2]))[:4]
    team: list[tuple[str, float]] = []
    for _cost, _count, leg_time, athlete, removals in chosen:
        remove_athlete_events(lineup, athlete_events, athlete, removals)
        team.append((athlete, leg_time))
    ordered_team = order_synthetic_relay_legs(team)
    leg_times = tuple(item[1] for item in ordered_team)
    return RelaySelection(
        event=relay_event,
        athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
        projected_time=sum(leg_times) - RELAY_SYNTHETIC_CREDIT[relay_event],
        method="synthetic",
        source_mark="completion depth relay",
        leg_times=leg_times,  # type: ignore[arg-type]
    )


def force_empty_individual_events(
    lineup: dict[str, list[str]],
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    potentials: dict[tuple[str, str], float],
) -> None:
    """Guarantee at least one athlete in each individual event when a recorded athlete exists."""
    for event in (event for event in EVENTS if event not in RELAY_EVENTS and not lineup.get(event)):
        options: list[tuple[float, float, Performance, list[str]]] = []
        for perf in sort_event_pool([item for item in school if item.event == event], event):
            removals = minimum_event_removals(athlete_events[perf.athlete], event)
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
        try_add_entry(lineup, athlete_events, perf.athlete, event)


def optimize_elite_sprint_utilization(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    school_relay_history: list[RelayPerformance],
    opponent_relay_history: list[RelayPerformance],
    school_relay_splits: list[Performance],
    opponent_relay_splits: list[Performance],
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Push top flat sprinters toward four legal running events when it does not cost points."""
    potentials = compute_scores(school, opponents)
    elite_order = rank_elite_sprint_jump_athletes(
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
            if len(athlete_events[athlete]) >= MAX_EVENTS_PER_ATHLETE:
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
            )
            if not replacement:
                break
            lineup = replacement.lineup
            relays = replacement.relays
            replacements += 1
        protected.add(athlete)
    return lineup, relays


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
        if perf.event in ELITE_INDIVIDUAL_EVENTS:
            priority_grouped[perf.athlete].append(potentials.get((perf.athlete, perf.event), 0.0))
        if perf.event in ELITE_INDIVIDUAL_EVENTS:
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
        if perf.event in ELITE_INDIVIDUAL_EVENTS:
            possible[perf.athlete].add(perf.event)
    for relay_event in SPRINT_RELAY_EVENTS:
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
        if event not in SPRINT_RELAY_EVENTS:
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
    for event in sorted(ELITE_INDIVIDUAL_EVENTS, key=event_sort_value):
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
        )
        if replacement:
            replacements.append(replacement)
    for event in sorted(SPRINT_RELAY_EVENTS, key=event_sort_value):
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
) -> EliteReplacement | None:
    """Try adding an elite athlete to an individual event by replacing the weakest entry."""
    if event not in lineup or athlete in lineup[event]:
        return None
    perf = best_perf.get((athlete, event))
    if not perf:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(athlete_events[athlete], event):
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
) -> list[EliteReplacement]:
    """Try moving a displaced protected athlete into another individual flat sprint event."""
    options: list[EliteReplacement] = []
    athlete_events = collect_athlete_events(trial_lineup, relays)
    for new_event in sorted(ELITE_INDIVIDUAL_EVENTS, key=event_sort_value):
        if new_event == replaced_event or protected_athlete in trial_lineup.get(new_event, []):
            continue
        protected_perf = best_perf.get((protected_athlete, new_event))
        if not protected_perf or not can_take_event(athlete_events[protected_athlete], new_event):
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
) -> list[EliteReplacement]:
    """Try moving a displaced protected athlete into a synthetic sprint relay."""
    options: list[EliteReplacement] = []
    athlete_events = collect_athlete_events(trial_lineup, relays)
    for relay_event in sorted(SPRINT_RELAY_EVENTS, key=event_sort_value):
        relay = relays.get(relay_event)
        if not relay or relay.method != "synthetic" or protected_athlete in relay.athletes:
            continue
        if not can_take_event(athlete_events[protected_athlete], relay_event):
            continue
        leg_times = relay_leg_time_map(relay_event, school, school_relay_history, school_relay_splits)
        protected_time = leg_times.get(protected_athlete)
        if protected_time is None:
            continue
        current_team = []
        for current_athlete in relay.athletes:
            current_time = leg_times.get(current_athlete)
            if current_time is None:
                break
            current_team.append((current_athlete, current_time))
        if len(current_team) != 4:
            continue
        replaceable_team = [item for item in current_team if item[0] not in protected]
        if not replaceable_team:
            continue
        target_athlete, target_time = max(replaceable_team, key=lambda item: item[1])
        if protected_time >= target_time:
            continue
        new_team = [(name, value) for name, value in current_team if name != target_athlete]
        new_team.append((protected_athlete, protected_time))
        ordered_team = order_synthetic_relay_legs(new_team)
        ordered_times = tuple(item[1] for item in ordered_team)
        option_relays = dict(relays)
        option_relays[relay_event] = RelaySelection(
            event=relay_event,
            athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
            projected_time=sum(ordered_times) - RELAY_SYNTHETIC_CREDIT[relay_event],
            method="synthetic",
            source_mark="protected elite compensation using best individual PR/relay split",
            leg_times=ordered_times,  # type: ignore[arg-type]
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
) -> EliteReplacement | None:
    """Try replacing the slowest leg in a synthetic sprint relay with an elite athlete."""
    relay = relays.get(relay_event)
    if not relay or relay.method != "synthetic" or athlete in relay.athletes:
        return None
    athlete_events = collect_athlete_events(lineup, relays)
    if not can_take_event(athlete_events[athlete], relay_event):
        return None
    leg_times = relay_leg_time_map(relay_event, school, school_relay_history, school_relay_splits)
    candidate_time = leg_times.get(athlete)
    if candidate_time is None:
        return None
    current_team = []
    for current_athlete in relay.athletes:
        current_time = leg_times.get(current_athlete)
        if current_time is None:
            return None
        current_team.append((current_athlete, current_time))
    replaceable_team = [item for item in current_team if item[0] not in protected]
    if not replaceable_team:
        return None
    target_athlete, target_time = max(replaceable_team, key=lambda item: item[1])
    if candidate_time >= target_time:
        return None
    new_team = [(name, value) for name, value in current_team if name != target_athlete]
    new_team.append((athlete, candidate_time))
    ordered_team = order_synthetic_relay_legs(new_team)
    ordered_times = tuple(item[1] for item in ordered_team)
    trial_relays = dict(relays)
    trial_relays[relay_event] = RelaySelection(
        event=relay_event,
        athletes=tuple(item[0] for item in ordered_team),  # type: ignore[arg-type]
        projected_time=sum(ordered_times) - RELAY_SYNTHETIC_CREDIT[relay_event],
        method="synthetic",
        source_mark="elite replacement using best individual PR/relay split",
        leg_times=ordered_times,  # type: ignore[arg-type]
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
) -> EliteReplacement | None:
    """Score a trial elite replacement and keep it only when team points do not drop."""
    if not lineup_is_valid(trial_lineup, trial_relays):
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
    if total_delta < -0.01:
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
) -> bool:
    """Add an athlete to an individual event if all constraints allow it."""
    if event not in lineup:
        return False
    if athlete in lineup[event] or len(lineup[event]) >= max_entries:
        return False
    if not can_take_event(athlete_events[athlete], event):
        return False
    lineup[event].append(athlete)
    athlete_events[athlete].append(event)
    return True


def can_take_event(existing_events: list[str], event: str) -> bool:
    """Limit athletes to four events and avoid consecutive running races."""
    if event in existing_events:
        return False
    if not can_event_set_stand(existing_events + [event]):
        return False
    if event in RUNNING_ORDER:
        event_order = RUNNING_ORDER[event]
        for existing in existing_events:
            if existing in RUNNING_ORDER and abs(RUNNING_ORDER[existing] - event_order) == 1:
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
) -> RelaySelection | None:
    """Prefer a scoring relay, but always return the best valid relay candidate."""
    candidates: list[RelaySelection] = []
    for relay in sorted([item for item in school_relay_history if item.event == relay_event], key=lambda item: item.value):
        if all(can_take_event(athlete_events[athlete], relay_event) for athlete in relay.athletes):
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
    )
    return depth_relay or scored[0][2]


def relay_selection_time_for_build(
    selection: RelaySelection, athlete_events: dict[str, list[str]]
) -> float:
    """Apply current known fatigue to a relay candidate during lineup construction."""
    if selection.method == "historic":
        avg_fatigue = sum(fatigue_factor(len(athlete_events[athlete])) for athlete in selection.athletes) / 4
        return selection.projected_time * avg_fatigue
    if selection.leg_times:
        return sum(
            leg_time * fatigue_factor(len(athlete_events[athlete]))
            for athlete, leg_time in zip(selection.athletes, selection.leg_times)
        ) - RELAY_SYNTHETIC_CREDIT[selection.event]
    return selection.projected_time


def synthesize_relay(
    relay_event: str,
    school: list[Performance],
    athlete_events: dict[str, list[str]],
    school_relay_history: list[RelayPerformance] | None = None,
    school_relay_splits: list[Performance] | None = None,
    prefer_depth: bool = False,
) -> RelaySelection | None:
    """Create a relay using each athlete's faster individual PR or recorded relay split."""
    candidates = best_relay_leg_candidates(
        relay_event,
        school,
        school_relay_history or [],
        school_relay_splits or [],
    )
    available = [
        (athlete, leg_time)
        for athlete, leg_time in candidates
        if can_take_event(athlete_events[athlete], relay_event)
    ]
    if prefer_depth and len(available) >= 4:
        candidate_pool = available[:8]
        team = candidate_pool[-4:]
    else:
        team = available[:4]
    if len(team) < 4:
        return None
    for athlete, _leg_time in team:
        if can_take_event(athlete_events[athlete], relay_event):
            continue
    ordered_team = order_synthetic_relay_legs(team)
    athletes = tuple(item[0] for item in ordered_team)
    leg_times = tuple(item[1] for item in ordered_team)
    time_value = sum(leg_times) - RELAY_SYNTHETIC_CREDIT[relay_event]
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
    )


def best_relay_leg_candidates(
    relay_event: str,
    school: list[Performance],
    relay_history: list[RelayPerformance],
    relay_splits: list[Performance] | None = None,
) -> list[tuple[str, float]]:
    """Return athletes ranked by their fastest comparable individual time or relay split."""
    base_event = RELAY_BASE_EVENT[relay_event]
    best: dict[str, tuple[str, float]] = {}
    for perf in school:
        if perf.event != base_event:
            continue
        key = perf.athlete.lower()
        current = best.get(key)
        if not current or perf.value < current[1]:
            best[key] = (perf.athlete, perf.value)
    for relay in relay_history:
        if relay.event != relay_event:
            continue
        for athlete, split in zip(relay.athletes, relay.splits):
            if split is None:
                continue
            key = athlete.lower()
            current = best.get(key)
            if not current or split < current[1]:
                best[key] = (athlete, split)
    for split in relay_splits or []:
        if split.event != base_event:
            continue
        key = split.athlete.lower()
        current = best.get(key)
        if not current or split.value < current[1]:
            best[key] = (split.athlete, split.value)
    return sorted(best.values(), key=lambda item: item[1])


def order_synthetic_relay_legs(team: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Order four synthetic legs as second, third, slowest, fastest."""
    ranked = sorted(team, key=lambda item: item[1])
    if len(ranked) != 4:
        return ranked
    return [ranked[1], ranked[2], ranked[3], ranked[0]]


def historic_relay_time(relay: RelayPerformance) -> float:
    """Use a recorded relay time with a small improvement assumption for sprint relays."""
    return relay.value - HISTORIC_RELAY_IMPROVEMENT.get(relay.event, 0.0)


def relay_time(relay_event: str, legs: list[Performance], adjustments: dict[str, float] | None = None) -> float:
    """Estimate relay time as four PRs minus exchange credit."""
    total = 0.0
    for perf in legs[:4]:
        factor = adjustments.get(perf.athlete, 1.0) if adjustments else 1.0
        total += perf.value * factor
    return total - RELAY_SYNTHETIC_CREDIT[relay_event]


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
    recorded_by_source: dict[str, float] = {}
    for relay in opponent_relay_history or []:
        if relay.event != relay_event:
            continue
        time_value = historic_relay_time(relay)
        recorded_by_source[relay.source] = min(
            recorded_by_source.get(relay.source, math.inf),
            time_value,
        )
    for perf in opponents:
        if perf.event != relay_event:
            continue
        time_value = perf.value - HISTORIC_RELAY_IMPROVEMENT.get(relay_event, 0.0)
        recorded_by_source[perf.source] = min(
            recorded_by_source.get(perf.source, math.inf),
            time_value,
        )

    by_source: dict[str, list[Performance]] = defaultdict(list)
    for perf in opponents:
        by_source[perf.source].append(perf)
    split_by_source: dict[str, list[Performance]] = defaultdict(list)
    for split in opponent_relay_splits or []:
        split_by_source[split.source].append(split)
    estimates: list[float] = []
    all_sources = set(recorded_by_source) | set(by_source) | set(split_by_source)
    for source in all_sources:
        if source in recorded_by_source:
            estimates.append(recorded_by_source[source])
            continue
        candidates = best_relay_leg_candidates(
            relay_event,
            by_source.get(source, []),
            [],
            split_by_source.get(source, []),
        )[:4]
        if len(candidates) == 4:
            estimates.append(sum(value for _athlete, value in candidates) - RELAY_SYNTHETIC_CREDIT[relay_event])
    return estimates


def fill_remaining_spots(
    lineup: dict[str, list[str]], school: list[Performance], athlete_events: dict[str, list[str]]
) -> None:
    """Fill open individual entries by PR after the main scoring assignments."""
    for event in lineup:
        event_perfs = sort_event_pool([perf for perf in school if perf.event == event], event)
        for perf in event_perfs:
            if len(lineup[event]) >= MAX_INDIVIDUAL_ENTRIES:
                break
            try_add_entry(lineup, athlete_events, perf.athlete, event)


def optimize_lineup(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    athlete_events: dict[str, list[str]],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
) -> tuple[dict[str, list[str]], dict[str, RelaySelection]]:
    """Try simple one-athlete replacements and keep changes that raise projected points."""
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
    school_by_event = {event: sort_event_pool([perf for perf in school if perf.event == event], event) for event in EVENTS}

    improved = True
    passes = 0
    while improved and passes < 4:
        improved = False
        passes += 1
        for event, athletes in list(best_lineup.items()):
            for slot, old_athlete in enumerate(list(athletes)):
                for candidate in school_by_event.get(event, [])[:10]:
                    if candidate.athlete in athletes:
                        continue
                    trial_lineup = clone_lineup(best_lineup)
                    trial_lineup[event][slot] = candidate.athlete
                    if not lineup_is_valid(trial_lineup, best_relays):
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


def lineup_is_valid(lineup: dict[str, list[str]], relays: dict[str, RelaySelection]) -> bool:
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
    return all(can_event_set_stand(events) for events in athlete_events.values())


def can_event_set_stand(events: list[str]) -> bool:
    """Check the full set of events for one athlete."""
    if len(events) > MAX_EVENTS_PER_ATHLETE:
        return False
    if {"400m", "4x400 relay"}.issubset(set(events)):
        return False
    if any(event in DISTANCE_EVENTS for event in events):
        distance_limit = 3 if {"4x800 relay", "800m"}.issubset(set(events)) else 2
        if len(events) > distance_limit:
            return False
    ordered = sorted(RUNNING_ORDER[event] for event in events if event in RUNNING_ORDER)
    return all(b - a > 1 for a, b in zip(ordered, ordered[1:]))


def evaluate_lineup(
    lineup: dict[str, list[str]],
    relays: dict[str, RelaySelection],
    school: list[Performance],
    opponents: list[Performance],
    opponent_relay_history: list[RelayPerformance] | None = None,
    opponent_relay_splits: list[Performance] | None = None,
) -> LineupResult:
    """Apply fatigue, simulate every event, and return projected team points."""
    best_perf = {(perf.athlete, perf.event): perf for perf in school}
    event_points: dict[str, float] = {}
    output_lineup: dict[str, list[dict[str, Any]]] = {}
    athlete_history: dict[str, list[str]] = defaultdict(list)

    for event in sorted(lineup, key=event_sort_value):
        entrants = []
        for athlete in lineup[event]:
            perf = best_perf.get((athlete, event))
            if not perf:
                continue
            adjusted_value = apply_fatigue(perf.value, perf.is_time, len(athlete_history[athlete]))
            entrants.append(make_adjusted_perf(perf, adjusted_value))
        event_points[event], athlete_details = score_event_details(event, entrants, opponents)
        output_lineup[event] = [
            entry_to_dict(perf, athlete_details.get(perf.athlete))
            for perf in sort_event_pool(entrants, event)
        ]
        for perf in entrants:
            athlete_history[perf.athlete].append(event)

    relay_output: dict[str, dict[str, Any]] = {}
    for event, relay in sorted(relays.items(), key=lambda item: event_sort_value(item[0])):
        time_value = relay_selection_time_for_build(relay, athlete_history)
        if math.isfinite(time_value):
            points = projected_relay_points(
                event,
                time_value,
                opponents,
                opponent_relay_history,
                opponent_relay_splits,
            )
        else:
            points = 0.0
        event_points[event] = points
        relay_output[event] = {
            "athletes": list(relay.athletes),
            "projected_mark": format_time(time_value) if math.isfinite(time_value) else "n/a",
            "projected_points": points,
            "method": relay.method,
            "source_mark": relay.source_mark,
        }
        for athlete in relay.athletes:
            athlete_history[athlete].append(event)

    total = round(sum(event_points.values()), 2)
    return LineupResult(
        lineup=output_lineup,
        relays=relay_output,
        event_points={event: round(points, 2) for event, points in sorted(event_points.items(), key=lambda item: event_sort_value(item[0]))},
        total_points=total,
        scraped={"school_records": len(school), "opponent_records": len(opponents)},
        errors=[],
    )


def apply_fatigue(value: float, is_time: bool, prior_events: int) -> float:
    """Add 1-3% to times after an athlete has already done multiple events."""
    return value * fatigue_factor(prior_events) if is_time else value


def fatigue_factor(prior_events: int) -> float:
    """Return the fatigue multiplier based on already-completed events."""
    if prior_events >= 3:
        return 1.03
    if prior_events == 2:
        return 1.01
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


def entry_to_dict(perf: Performance, projection: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a lineup entry."""
    display_mark = format_display_mark(perf.mark)
    data = {
        "athlete": perf.athlete,
        "mark": display_mark,
        "adjusted_mark": display_mark,
    }
    if projection is not None:
        data["projected_place"] = projection.get("place")
        data["projected_place_label"] = projection.get("place_label")
        data["projected_points"] = round(float(projection.get("points", 0.0)), 2)
    return data


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
    if event in RUNNING_ORDER:
        return float(RUNNING_ORDER[event])
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
    return event.replace("m", "m").replace(" relay", " Relay").title()


def format_inches(value: float) -> str:
    """Format inches as feet-inches."""
    feet = int(value // 12)
    inches = value - feet * 12
    return f"{feet}' {inches:.2f}\""


def normalize_athlete_name(name: str) -> str:
    """Normalize an athlete name for case-insensitive injury matching."""
    return re.sub(r"[^a-z0-9]+", " ", clean_text(name).lower()).strip()


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
) -> LineupResult:
    """Scrape inputs, build a lineup, and evaluate the final projection."""
    errors: list[str] = []
    school: list[Performance] = []
    opponents: list[Performance] = []
    school_relay_history: list[RelayPerformance] = []
    opponent_relay_history: list[RelayPerformance] = []
    school_relay_splits: list[Performance] = []
    opponent_relay_splits: list[Performance] = []
    try:
        school_result = scrape_team_data(school_url, "school", "Your Team", gender)
        school_result = filter_injured_athletes(school_result, injured_athletes or [])
        school = school_result.performances
        school_relay_history = school_result.relay_history
        school_relay_splits = school_result.relay_splits
    except Exception as exc:
        errors.append(str(exc))
    for index, url in enumerate(opponent_urls, start=1):
        if not url.strip():
            continue
        try:
            opponent_result = scrape_team_data(url, "opponent", f"Opponent {index}", gender)
            opponents.extend(opponent_result.performances)
            opponent_relay_history.extend(opponent_result.relay_history)
            opponent_relay_splits.extend(opponent_result.relay_splits)
        except Exception as exc:
            errors.append(str(exc))
    if not school:
        return LineupResult({}, {}, {}, 0.0, {"school_records": 0, "opponent_records": len(opponents)}, errors)
    raw_lineup = build_lineup(
        school,
        opponents,
        school_relay_history,
        opponent_relay_history,
        school_relay_splits,
        opponent_relay_splits,
    )
    result = evaluate_lineup(
        raw_lineup["lineup"],
        raw_lineup["relays"],
        school,
        opponents,
        opponent_relay_history,
        opponent_relay_splits,
    )
    result.scraped = {"school_records": len(school), "opponent_records": len(opponents)}
    missing_events = raw_lineup.get("missing_events", [])
    if missing_events:
        errors.append(
            "No eligible recorded athletes were available for: "
            + ", ".join(title_event(event) for event in missing_events)
        )
    result.errors = errors
    return result


def run_optimizer_both(
    school_url: str,
    opponent_urls: list[str],
    injured_athletes: list[str] | None = None,
) -> dict[str, Any]:
    """Generate independent men's and women's lineups without combining their athlete pools."""
    return {
        "mode": "both",
        "division_results": {
            "mens": asdict(run_optimizer(school_url, opponent_urls, "mens", injured_athletes)),
            "womens": asdict(run_optimizer(school_url, opponent_urls, "womens", injured_athletes)),
        },
    }


def demo_result() -> LineupResult:
    """Run the optimizer on built-in sample marks for offline testing."""
    school, opponents, school_relays, opponent_relays = sample_data()
    raw_lineup = build_lineup(school, opponents, school_relays, opponent_relays)
    result = evaluate_lineup(raw_lineup["lineup"], raw_lineup["relays"], school, opponents, opponent_relays)
    result.scraped = {"school_records": len(school), "opponent_records": len(opponents)}
    return result


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
      grid-template-columns: repeat(3, minmax(0, 1fr));
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
    .event-head { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; }
    .event-head h3 { margin: 0 0 8px; font-size: 1rem; }
    .points { color: var(--ok); font-weight: 800; white-space: nowrap; }
    ol { margin: 0; padding-left: 20px; }
    li { margin: 5px 0; }
    .relay { border-left: 4px solid var(--accent-2); }
    .athlete-panel {
      position: fixed;
      top: 112px;
      right: 18px;
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
    .error {
      border: 1px solid #f0b7ad;
      color: #84291d;
      background: #fff3f0;
      border-radius: 8px;
      padding: 10px 12px;
      margin: 0 0 14px;
    }
    @media (max-width: 880px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .summary { grid-template-columns: 1fr; }
      .athlete-panel {
        top: auto;
        right: 12px;
        bottom: 12px;
        max-height: min(68vh, 520px);
      }
    }
  </style>
</head>
<body>
  <header>
    <h1>Track Lineup Optimizer</h1>
    <div class="version">Build 2026.06.25-v15</div>
  </header>
  <main>
    <aside>
      <form id="optimizer-form">
        <label for="school-url">School Athletic.net event records URL</label>
        <input id="school-url" name="schoolUrl" value="https://www.athletic.net/team/16546/track-and-field-outdoor/2025/event-records">
        <label for="gender">Division</label>
        <select id="gender" name="gender">
          <option value="mens" selected>Mens</option>
          <option value="womens">Womens</option>
          <option value="both">Both</option>
        </select>
        <label for="opponents">Opponent event records URLs</label>
        <textarea id="opponents" name="opponents" placeholder="One URL per line"></textarea>
        <label for="injured-athletes">Injured athletes</label>
        <textarea id="injured-athletes" name="injuredAthletes" placeholder="One athlete name per line"></textarea>
        <div class="actions">
          <button id="run-button" type="submit">Generate Lineup</button>
          <button class="secondary" id="demo-button" type="button">Use Demo Data</button>
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
          <button class="sort-option active" data-sort="schedule" type="button">Schedule</button>
          <button class="sort-option" data-sort="distance" type="button">Distance</button>
        </div>
      </div>
      <div id="errors"></div>
      <div class="summary">
        <div class="metric"><strong id="total-points">0</strong><span>projected points</span></div>
        <div class="metric"><strong id="school-count">0</strong><span>school records parsed</span></div>
        <div class="metric"><strong id="opponent-count">0</strong><span>opponent records parsed</span></div>
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
    const demoButton = document.querySelector("#demo-button");
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
    const EVENT_SORT_ORDERS = {
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
    let divisionResults = null;
    let activeDivision = "mens";
    let currentResult = null;
    let activeEventSort = "schedule";
    let athleteIndex = new Map();
    let selectedAthleteKey = "";
    let panelDrag = null;

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await optimize("/api/optimize", {
        schoolUrl: document.querySelector("#school-url").value.trim(),
        opponentUrls: document.querySelector("#opponents").value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        injuredAthletes: document.querySelector("#injured-athletes").value.split(/\n+/).map(x => x.trim()).filter(Boolean),
        gender: document.querySelector("#gender").value
      });
    });

    demoButton.addEventListener("click", async () => optimize("/api/demo", {}));
    results.addEventListener("click", (event) => {
      const button = event.target.closest(".athlete-chip");
      if (!button) return;
      openAthletePanel(button.dataset.athleteName || button.textContent.trim());
    });
    athletePanelClose.addEventListener("click", closeAthletePanel);
    athletePanelHead.addEventListener("pointerdown", startPanelDrag);
    document.addEventListener("pointermove", dragAthletePanel);
    document.addEventListener("pointerup", stopPanelDrag);
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

    function renderSingleLegacy(data) {
      closeAthletePanel();
      athleteIndex = buildAthleteIndex(data);
      document.querySelector("#total-points").textContent = Number(data.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = data.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = data.scraped?.opponent_records || 0;
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
      currentResult = data || {};
      athleteIndex = buildAthleteIndex(currentResult);
      document.querySelector("#total-points").textContent = Number(currentResult.total_points || 0).toFixed(1);
      document.querySelector("#school-count").textContent = currentResult.scraped?.school_records || 0;
      document.querySelector("#opponent-count").textContent = currentResult.scraped?.opponent_records || 0;
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

    function renderIndividualEventCard(event, entries, eventPoints) {
      return `
        <article class="event-card">
          <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(eventPoints[event] || 0).toFixed(1)} pts</span></div>
          <ol>${entries.map(entry => `<li>${athleteButton(entry.athlete)} ${escapeHtml(entry.adjusted_mark)} <span class="muted">(${formatPlace(entry.projected_place_label)} - ${formatPoints(entry.projected_points)} points)${entry.mark !== entry.adjusted_mark ? ` - PR ${escapeHtml(entry.mark)}` : ""}</span></li>`).join("")}</ol>
        </article>
      `;
    }

    function renderRelayEventCard(event, relay) {
      return `
        <article class="event-card relay">
          <div class="event-head"><h3>${escapeHtml(titleCase(event))}</h3><span class="points">${Number(relay.projected_points || 0).toFixed(1)} pts</span></div>
          <ol>${relay.athletes.map(name => `<li>${athleteButton(name)}</li>`).join("")}</ol>
          <div class="muted">Projected ${escapeHtml(relay.projected_mark || "n/a")} - ${escapeHtml(relay.method || "relay")} ${relay.source_mark ? `from ${escapeHtml(relay.source_mark)}` : ""}</div>
        </article>
      `;
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
      const order = EVENT_SORT_ORDERS[activeEventSort] || EVENT_SORT_ORDERS.schedule;
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
      athletePanelCount.textContent = `${athlete.events.length} ${athlete.events.length === 1 ? "event" : "events"}`;
      const athleteEvents = [...athlete.events].sort((a, b) => eventSortRank(a.event) - eventSortRank(b.event) || titleCase(a.event).localeCompare(titleCase(b.event)));
      athletePanelEvents.innerHTML = athleteEvents.map(detail => `
        <li class="athlete-event-item">
          <div class="athlete-event-top">
            <span>${escapeHtml(titleCase(detail.event))}</span>
            <span class="athlete-event-mark">${escapeHtml(detail.mark)}</span>
          </div>
          <div class="athlete-event-meta">
            <span class="athlete-pill">${escapeHtml(detail.type)}</span>
            <span class="athlete-pill">${formatPlace(detail.place)}</span>
            <span class="athlete-pill points-pill">${formatPoints(detail.points)} pts</span>
          </div>
        </li>
      `).join("");
      athletePanel.hidden = false;
      document.body.classList.add("athlete-panel-open");
      placePanelOnSide();
    }

    function closeAthletePanel() {
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
      const width = athletePanel.offsetWidth || 360;
      const height = athletePanel.offsetHeight || 420;
      athletePanel.style.left = `${Math.max(12, window.innerWidth - width - 18)}px`;
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

    function titleCase(value) {
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

    function athleteKey(value) {
      return String(value || "").trim().toLowerCase().replace(/\s+/g, " ");
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
            if self.path == "/api/demo":
                self.send_json(asdict(demo_result()))
                return
            if self.path != "/api/optimize":
                self.send_json({"error": "Not found"}, status=404)
                return
            school_url = clean_text(payload.get("schoolUrl", ""))
            opponent_urls = payload.get("opponentUrls", [])
            injured_athletes = payload.get("injuredAthletes", [])
            gender = clean_text(payload.get("gender", "mens")).lower() or "mens"
            if not school_url:
                self.send_json({"errors": ["Enter a school Athletic.net event records URL."], "total_points": 0}, status=400)
                return
            cleaned_opponent_urls = [clean_text(url) for url in opponent_urls]
            cleaned_injured_athletes = [clean_text(name) for name in injured_athletes]
            if gender in {"both", "all"}:
                self.send_json(
                    run_optimizer_both(
                        school_url,
                        cleaned_opponent_urls,
                        cleaned_injured_athletes,
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
    host = "127.0.0.1"
    requested_port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = None
    port = requested_port
    for candidate in range(requested_port, requested_port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), AppHandler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"No open port found from {requested_port} to {requested_port + 19}")
    if sys.stdout:
        print(f"Track Lineup Optimizer (Made by Jayden Yang) {APP_VERSION} running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()


















