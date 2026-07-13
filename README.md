# Track Lineup Optimizer

A local Python web app that scrapes Athletic.net event-record pages, estimates scoring against optional opponent teams, and builds a high school track and field lineup.

The Division selector supports `Mens`, `Womens`, and `Both`. Both mode generates two independent lineups and displays them in separate Mens and Womens result tabs.

List unavailable athletes in the `Injured athletes` box, one exact athlete name per line. Matching is case-insensitive, and injured athletes are removed from individual events, relay splits, and historic relay teams.

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

## Deploy Beta

The app is ready to run as a Python web service on hosts such as Render, Railway, or Fly.io.

For Render:

1. Push this folder to a GitHub repository.
2. Create a new Render Web Service from that repository.
3. Use `outputs/track-lineup-app` as the root directory if the full Codex workspace is pushed.
4. Use the included `render.yaml`, or configure manually:
   - Build command: `pip install -r requirements.txt`
   - Start command: `python app.py`
5. Render will set `PORT`; the app automatically binds to `0.0.0.0` in production.

The generated Render URL can stay unadvertised and shared only with beta testers.

## Notes

- The app uses only Python's standard library for the server.
- The scraper normalizes standard and Reader-prefixed Athletic.net event-records URLs, then uses Athletic.net's first-party event-records JSON endpoint.
- If the API is unavailable, the app can still fall back to HTML/text parsing and Reader URL variants.
- If `beautifulsoup4` is installed, fallback HTML parsing uses it for cleaner table extraction. If not, it uses the standard library.
- The optimizer enforces four events per athlete and avoids consecutive running races.
- Projected individual-event fields include only each opponent team's best three athletes per event.
- Relay projections include exactly one entry per opponent school: its fastest recorded relay, or one synthetic relay if it has no recorded team.
- Finalization checks all 18 supported events. Relays below five projected points use depth runners instead of being omitted.
- Individual scoring is `10 8 6 5 4 3 2 1`; relay scoring is `10 8 6 4 2`.
