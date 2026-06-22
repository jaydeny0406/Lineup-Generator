# Track Lineup Optimizer

A local Python web app that scrapes Athletic.net event-record pages, estimates scoring against optional opponent teams, and builds a high school track and field lineup.

The Division selector supports `Mens`, `Womens`, and `Both`. Both mode generates two independent lineups and displays them in separate Mens and Womens result tabs.

## Run

```powershell
cd C:\Users\jayde\Documents\Codex\2026-06-07\create-a-web-app-that-generates\outputs\track-lineup-app
.\restart.ps1
```

Then open:

```text
http://127.0.0.1:8000
```

`restart.ps1` stops stale Python app servers on port `8000` before starting the current build. You can select another port:

```powershell
.\restart.ps1 -Port 8001
```

Check the running build at `http://127.0.0.1:8000/api/health`.

## Test

```powershell
cd C:\Users\jayde\Documents\Codex\2026-06-07\create-a-web-app-that-generates\outputs\track-lineup-app
python -m unittest
```

## Notes

- The app uses only Python's standard library for the server.
- The scraper normalizes standard and Reader-prefixed Athletic.net event-records URLs, then uses Athletic.net's first-party event-records JSON endpoint.
- If the API is unavailable, the app can still fall back to HTML/text parsing and Reader URL variants.
- If `beautifulsoup4` is installed, fallback HTML parsing uses it for cleaner table extraction. If not, it uses the standard library.
- The optimizer enforces four events per athlete and avoids consecutive running races.
- Individual scoring is `10 8 6 5 4 3 2 1`; relay scoring is `10 8 6 4 2`.
