#!/usr/bin/env python3
"""
Extracts structured darts match data from scoreboard screenshots using
Claude's vision API, and appends the results to darts/matches.json.

Run by the "Process Darts Screenshots" GitHub Action. Not intended to be
run manually, but it works fine locally too if ANTHROPIC_API_KEY is set
in your environment.
"""

import os
import sys
import json
import base64
import hashlib
import re
from pathlib import Path
from datetime import datetime, timezone

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
INCOMING_DIR = REPO_ROOT / "darts" / "incoming"
PROCESSED_DIR = REPO_ROOT / "darts" / "processed"
DATA_FILE = REPO_ROOT / "darts" / "matches.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

MODEL = "claude-sonnet-5"

SCHEMA_PROMPT = """You are extracting structured data from a screenshot of a darts scoreboard app (a completed 501-style leg).

Return ONLY valid JSON (no markdown code fences, no commentary, no explanation) matching exactly this structure:

{
  "game_type": "<the game name shown, e.g. '501'>",
  "checkout_rule": "<the checkout rule text shown, e.g. 'Straight In / Straight Out'>",
  "raw_date": "<the date exactly as shown in the header, e.g. '03.07.26'>",
  "date": "<ISO date YYYY-MM-DD -- the date shown is in DD.MM.YY format, convert it>",
  "time": "<time as shown, e.g. '22:39'>",
  "duration_minutes": <integer minutes from the duration field>,
  "duration_seconds": <integer seconds from the duration field, 0 if none shown>,
  "players": ["<player name 1>", "<player name 2>", "..."],
  "leg_stats": {
    "<player name>": {
      "ppr": <float>,
      "first9_ppr": <float>,
      "darts_thrown": <integer>,
      "checkout_pct": <float, e.g. 14.29>,
      "checkout_attempts": <integer, the denominator of the fraction shown next to checkout %>,
      "checkout_makes": <integer, the numerator of that fraction>,
      "checkout_points": <integer, or null if the cell shows '-'>,
      "60_plus": <integer>,
      "100_plus": <integer>,
      "140_plus": <integer>,
      "180": <integer>
    }
    /* one entry per player, using the exact player names from "players" */
  },
  "rounds": [
    {
      "round": <integer round number>,
      "scores": {
        "<player name>": {
          "score": <integer round score, or null if the cell is empty or shows BUST>,
          "darts": "<the small dart-by-dart text shown in the cell, exactly as written, e.g. '2 16 6' or 'T20 T14 D4' or '18 - 8'>",
          "remaining": <integer, the running total shown at the bottom of the cell (the score-remaining number), or null if not shown>,
          "bust": <true if the cell shows the word BUST, otherwise false>
        }
        /* only include players who have a cell for this round; omit players with no cell at all */
      }
    }
    /* one entry per round shown in the History section, in order, top to bottom */
  ]
}

Rules:
- Use player names exactly as shown in the header row of the stats table (same capitalization).
- Include every round visible in the History section.
- Use JSON null (not the string "-" or an empty string) for any value that is unavailable, dashed out, or shown as "-".
- Parse combined fields like "14.29% (1/7)" into checkout_pct=14.29, checkout_makes=1, checkout_attempts=7.
- Do not guess or invent numbers. If a value is genuinely unreadable, use null.
- Output raw JSON only. The response must start with { and end with }.
"""


def image_to_base64(path: Path):
    data = path.read_bytes()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }[path.suffix.lower()]
    return media_type, base64.standard_b64encode(data).decode("utf-8")


def extract_match_data(client: "anthropic.Anthropic", image_path: Path) -> dict:
    media_type, b64 = image_to_base64(image_path)
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": SCHEMA_PROMPT},
                ],
            }
        ],
    )
    text = "".join(block.text for block in message.content if block.type == "text").strip()
    # Defensively strip markdown fences in case the model adds them anyway.
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def load_existing_matches() -> list:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: {DATA_FILE} was not valid JSON. Starting a fresh list "
                  f"instead of overwriting -- check the file manually.")
            sys.exit(1)
    return []


def save_matches(matches: list):
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(matches, indent=2, ensure_ascii=False) + "\n")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    images = sorted(
        p for p in INCOMING_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )

    if not images:
        print("No new screenshots found in darts/incoming/. Nothing to do.")
        return

    matches = load_existing_matches()
    known_hashes = {m.get("source_hash") for m in matches if m.get("source_hash")}

    any_errors = False

    for image_path in images:
        print(f"Processing {image_path.name} ...")
        h = file_hash(image_path)

        if h in known_hashes:
            print("  Identical image already processed before -- moving without re-extracting.")
            image_path.rename(PROCESSED_DIR / image_path.name)
            continue

        try:
            parsed = extract_match_data(client, image_path)
        except Exception as e:
            print(f"  ERROR extracting data from {image_path.name}: {e}")
            print("  Leaving this file in darts/incoming/ so it can be retried.")
            any_errors = True
            continue

        match_id_source = f"{parsed.get('date', 'unknown')}_{parsed.get('time', 'unknown')}_{image_path.stem}"
        match_id = re.sub(r"[^a-zA-Z0-9_-]", "-", match_id_source).strip("-").lower()

        parsed["match_id"] = match_id
        parsed["source_image"] = image_path.name
        parsed["source_hash"] = h
        parsed["processed_at"] = datetime.now(timezone.utc).isoformat()

        matches.append(parsed)
        known_hashes.add(h)

        image_path.rename(PROCESSED_DIR / image_path.name)
        print(f"  Extracted OK -> match_id={match_id}")

    save_matches(matches)
    print(f"Done. Total matches in database: {len(matches)}")

    if any_errors:
        # Non-zero exit still lets git-auto-commit-action save any successful
        # extractions from this run, but marks the Action run as failed so
        # it's visible in the Actions tab.
        sys.exit(1)


if __name__ == "__main__":
    main()
