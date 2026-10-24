"""Fetch and parse Athletic.net individual, split, and relay records."""

from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen

from .domain import (
    FIELD_EVENTS,
    INDOOR_DASH_CONVERSION_FACTOR,
    INDOOR_HURDLE_CONVERSION_FACTOR,
    MeetConfig,
    Performance,
    RELAY_BASE_EVENT,
    RELAY_EVENTS,
    RelayPerformance,
    ScrapeResult,
    TRACK_EVENTS,
    active_events,
    clean_text,
    format_time,
    is_better,
)

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
    url: str, team_role: str = "school", source: str | None = None, gender: str = "mens",
    *, fetch_text=None,
) -> ScrapeResult:
    """Scrape individual records and historic relay teams from an event-records page."""
    fetch = fetch_text or fetch_text_url
    team_id, season_id, canonical_url = parse_athletic_team_url(url)
    source_name = resolve_athletic_team_name(canonical_url, fetch_text=fetch) or source or canonical_url
    errors: list[str] = []

    try:
        payload = json.loads(fetch(athletic_api_url(team_id, season_id)))
        result = parse_athletic_api_data(payload, source_name, team_role, gender)
        if result.performances:
            return result
        errors.append("Athletic.net API returned no matching individual event records.")
    except Exception as exc:
        errors.append(f"Athletic.net API failed: {exc}")

    page_candidates: list[str] = []
    try:
        page_candidates.append(fetch(canonical_url))
    except Exception as exc:
        errors.append(f"Direct page failed: {exc}")
    for reader_url in athletic_reader_urls(canonical_url):
        try:
            page_candidates.append(fetch(reader_url))
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


def resolve_athletic_team_name(canonical_url: str, *, fetch_text=None) -> str | None:
    """Fetch the Athletic.net team page title so standings show real school names."""
    fetch = fetch_text or fetch_text_url
    base_url = re.sub(r"/event-records/?$", "", canonical_url)
    candidate_urls = [canonical_url]
    if base_url != canonical_url:
        candidate_urls.append(base_url)
    for url in candidate_urls:
        try:
            name = extract_team_name(fetch(url))
        except Exception:
            name = None
        if name:
            return name
    for url in candidate_urls:
        for reader_url in athletic_reader_urls(url):
            try:
                name = extract_team_name(fetch(reader_url))
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


__all__ = [
    "TableExtractor",
    "api_athlete_name",
    "athletic_api_url",
    "athletic_reader_urls",
    "athletic_url_season_type",
    "clean_team_name",
    "cleanup_name",
    "converted_short_event_fallbacks",
    "dedupe_performances",
    "dedupe_relay_performances",
    "detect_athlete",
    "detect_event",
    "detect_mark",
    "extract_athletic_api_team_name",
    "extract_name_from_line",
    "extract_relay_member_name",
    "extract_team_name",
    "fetch_html",
    "fetch_text_url",
    "filter_gender_text",
    "find_nearby_event",
    "get_soup",
    "is_athletic_url",
    "is_cloudflare_challenge",
    "is_name_like",
    "mark_candidates",
    "nested_team_name",
    "normalize_athletic_url",
    "parse_athletic_api_data",
    "parse_athletic_records_html",
    "parse_athletic_team_url",
    "parse_distance_to_inches",
    "parse_mark",
    "parse_record_text",
    "parse_relay_records_text",
    "parse_tables_with_soup",
    "parse_tables_without_soup",
    "parse_text_blocks_with_soup",
    "prepare_scrape_result_for_meet",
    "relay_split_base_event",
    "resolve_athletic_team_name",
    "scrape_data",
    "scrape_team_data",
    "team_name_from_mapping",
    "validate_meet_urls",
]
