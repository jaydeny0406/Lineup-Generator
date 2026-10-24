"""HTTP entry point and compatibility exports for the lineup modules."""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from lineup.domain import *
from lineup.records import *
from lineup.optimizer import *
from lineup.scoring import *
from lineup.edits import *
from lineup.service import *
from lineup import records as _records
from lineup import service as _service


_UI = Path(__file__).resolve().parent / "ui"
HTML_PAGE = (_UI / "index.html").read_text(encoding="utf-8")
HTML_PAGE = HTML_PAGE.replace("__INLINE_CSS__", (_UI / "styles.css").read_text(encoding="utf-8"))
HTML_PAGE = HTML_PAGE.replace("__INLINE_JS__", (_UI / "app.js").read_text(encoding="utf-8"))


def scrape_team_data(
    url: str, team_role: str = "school", source: str | None = None, gender: str = "mens"
) -> ScrapeResult:
    """Scrape a team while keeping the public fetch hook available to tests."""
    return _records.scrape_team_data(url, team_role, source, gender, fetch_text=fetch_text_url)


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
    """Build one division with the public scraper hook available to tests."""
    return _service.run_optimizer(
        school_url, opponent_urls, gender, injured_athletes, athlete_event_limits,
        season_type, indoor_sprint_distance, team_event_limit,
        scrape_team_data_fn=scrape_team_data,
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
    """Generate both divisions through the public optimizer hook."""
    return _service.run_optimizer_both(
        school_url, opponent_urls, injured_athletes, athlete_event_limits,
        season_type, indoor_sprint_distance, team_event_limit,
        optimize_fn=run_optimizer,
    )


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
