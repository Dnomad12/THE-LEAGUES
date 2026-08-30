# Darts Data Pipeline

This folder is the "database" for the darts league. There's no Google Sheet
involved -- instead, match screenshots get turned into structured data
automatically by a GitHub Action.

## How it works

1. You take a screenshot of a completed leg from the scoring app.
2. Upload it to `darts/incoming/` via GitHub's web UI (Add file -> Upload
   files -> Commit).
3. That commit triggers the "Process Darts Screenshots" GitHub Action.
4. The Action sends the screenshot to Claude's vision API, which reads the
   leg stats and full round-by-round history table and returns it as
   structured JSON.
5. The Action appends that match to `darts/matches.json` and moves the
   screenshot from `darts/incoming/` to `darts/processed/`, committing both
   changes back to the repo automatically.
6. The darts website (once built) reads `darts/matches.json` directly --
   no CORS proxy, no Google Sheets, since it's just a file in the same repo.

## Setup (one-time)

1. Get an Anthropic API key from https://console.anthropic.com (Settings ->
   API Keys).
2. In this repo: Settings -> Secrets and variables -> Actions -> New
   repository secret. Name it `ANTHROPIC_API_KEY` and paste the key.
3. In this repo: Settings -> Actions -> General -> Workflow permissions ->
   select "Read and write permissions" and save. (The Action needs this to
   commit the extracted data back to the repo.)

That's it -- from then on, uploading a screenshot is the only manual step.

## If something goes wrong

- Check the "Actions" tab in GitHub to see the run and its logs.
- If extraction fails for a screenshot (e.g. the image was unreadable), it's
  left in `darts/incoming/` untouched, and the run is marked failed so you'll
  notice. Re-running the workflow (via the "Re-run jobs" button on that
  Action run, or `workflow_dispatch` from the Actions tab) will retry it.
- If you accidentally upload the same screenshot twice, the Action detects
  the duplicate (by file content, not filename) and just files it away
  without adding a second entry to `matches.json`.

## Data format

Each entry in `darts/matches.json` looks like this:

```json
{
  "game_type": "501",
  "checkout_rule": "Straight In / Straight Out",
  "date": "2026-07-03",
  "time": "22:39",
  "duration_minutes": 38,
  "duration_seconds": 44,
  "players": ["SVENJA", "MORGI", "CECILE", "JULIA", "DNOMAD12"],
  "leg_stats": {
    "SVENJA": { "ppr": 30.38, "first9_ppr": 20.33, "darts_thrown": 48,
                "checkout_pct": 0, "checkout_attempts": 1, "checkout_makes": 0,
                "checkout_points": null, "60_plus": 2, "100_plus": 0,
                "140_plus": 0, "180": 0 }
  },
  "rounds": [
    {
      "round": 1,
      "scores": {
        "SVENJA": { "score": 24, "darts": "2 16 6", "remaining": 477, "bust": false }
      }
    }
  ],
  "match_id": "2026-07-03_22-39_...",
  "source_image": "...jpeg",
  "source_hash": "...",
  "processed_at": "2026-08-30T12:00:00+00:00"
}
```

Build the darts site's rankings/history views by fetching this file and
aggregating across all matches, the same way the shuffleboard and pétanque
sites aggregate their sheet data.
