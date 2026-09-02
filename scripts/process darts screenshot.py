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
import time
from pathlib import Path
from datetime import datetime, timezone

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
INCOMING_DIR = REPO_ROOT / "darts" / "incoming"
PROCESSED_DIR = REPO_ROOT / "darts" / "processed"
MATCHES_FILE = REPO_ROOT / "darts" / "matches.json"
THROWS_FILE = REPO_ROOT / "darts" / "throws.json"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

MODEL = "claude-sonnet-5"

SCHEMA_PROMPT = """You are extracting structured data from a screenshot of a darts scoreboard app (a completed 501-style leg).

Return ONLY valid JSON (no markdown code fences, no commentary, no explanation) matching exactly this structure:

{
  "match": {
    "game_type": "<the game name shown, e.g. '501'>",
    "checkout_rule": "<the checkout rule text shown, e.g. 'Straight In / Straight Out'>",
    "raw_date": "<the date exactly as shown in the header, e.g. '03.07.26'>",
    "date": "<ISO date YYYY-MM-DD -- the date shown is in DD.MM.YY format, convert it>",
    "time": "<time as shown, e.g. '22:39'>",
    "duration_minutes": <integer minutes from the duration field>,
    "duration_seconds": <integer seconds from the duration field, 0 if none shown>,
    "players": ["<player name 1>", "<player name 2>", "..."],
    "winner": "<name of the player whose remaining score reaches exactly 0, or null if none does>",
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
    }
  },
  "throws": [
    {
      "round": <integer round number>,
      "player": "<player name, exactly as in "players">",
      "throw_number": <integer 1, 2, or 3 -- position within that player's turn that round>,
      "base": <integer 1-20, or 25 for the bull, or null if not_thrown or a genuine miss>,
      "multiplier": <integer 1 (single), 2 (double), or 3 (triple); for the bull only 1 or 2 is valid (no triple bull); null if not_thrown or a genuine miss>,
      "value": <integer, base * multiplier, or 0 if a genuine miss, or null if not_thrown>,
      "is_miss": <true if this dart was thrown and scored zero (shown as "-" while other darts in the same turn were thrown before/after it and the turn was not yet won), otherwise false>,
      "not_thrown": <true if this dart was never thrown because the player's turn had already ended earlier that round (via checkout to 0, or via a bust), otherwise false>,
      "checkout_dart": <true if this exact dart brought the player's remaining score to precisely 0, otherwise false>,
      "round_score": <integer, the total score for this player this round as shown in bold, or null if the round is a BUST>,
      "remaining": <integer, the running total shown at the bottom of the cell for this player this round>,
      "bust": <true if this player's cell for this round shows the word BUST, otherwise false>
    }
    /* One entry per dart per player per round. A normal round contributes 3 throw entries per player
       (fewer only if the player checked out mid-turn, using not_thrown for the remaining slot(s), or if
       the screenshot cell only shows fewer dart values). Include throws for BUST rounds too -- the
       individual dart values shown are real throws, only the round_score is null and bust is true.
       Skip a player entirely for a round only if that player has no cell at all for that round
       (e.g. the leg already ended for them). */
  ]
}

Rules:
- Use player names exactly as shown in the header row of the stats table (same capitalization).
- Parse dart notation exactly: "T20" = base 20, multiplier 3. "D4" = base 4, multiplier 2. "17" (plain number) = base 17, multiplier 1. "25" = single bull, base 25 multiplier 1. "D25" = double bull, base 25 multiplier 2 (value 50). There is no triple bull.
- A "-" that appears while the player's turn is still ongoing (i.e. there are more real dart values shown later in that same turn) is a genuine miss: is_miss=true, base=null, multiplier=null, value=0.
- A "-" that appears after the player's turn has effectively already ended -- either because an earlier dart that turn reached exactly 0 (checkout), or because an earlier dart that turn caused a bust (going below zero, or leaving exactly 1) -- is not_thrown=true instead, since darts are never thrown after a turn ends: base=null, multiplier=null, value=null, is_miss=false.
- Parse combined fields like "14.29% (1/7)" into checkout_pct=14.29, checkout_makes=1, checkout_attempts=7.
- Use JSON null (not the string "-") for any value that is genuinely unavailable.
- Do not guess or invent numbers. If a value is unreadable, use null.
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


def extract_match_data(client: "anthropic.Anthropic", image_path: Path, max_retries: int = 3) -> dict:
    media_type, b64 = image_to_base64(image_path)
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            message = client.messages.create(
                model=MODEL,
                max_tokens=16000,
                output_config={"effort": "medium"},
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
        except Exception as e:
            last_error = e
            print(f"  API call failed on attempt {attempt}/{max_retries}: {e}")
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
            continue

        if message.stop_reason == "max_tokens":
            last_error = RuntimeError(
                "Response was cut off at the max_tokens limit before finishing -- "
                "the match likely has more rounds/players than usual."
            )
            print(f"  Attempt {attempt}/{max_retries}: {last_error}")
            if attempt < max_retries:
                time.sleep(2 ** attempt)
            continue

        text = "".join(block.text for block in message.content if block.type == "text").strip()
        text = re.sub(r"^```(json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()

        if not text:
            last_error = RuntimeError(f"Empty text response (stop_reason={message.stop_reason}).")
            print(f"  Attempt {attempt}/{max_retries}: {last_error}")
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
            continue

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            preview = text[:1000]
            last_error = ValueError(
                f"Response was not valid JSON: {e}\n--- Raw response (first 1000 chars) ---\n{preview}"
            )
            print(f"  Attempt {attempt}/{max_retries}: {last_error}")
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)
            continue

    raise last_error


def load_json_list(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"WARNING: {path} was not valid JSON. Refusing to overwrite -- check it manually.")
            sys.exit(1)
    return []


def save_json_list(path: Path, items: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    print("SCRIPT VERSION: 2026-09-02-v3 (retry logic + max_tokens=16000 + output_config effort=medium)")
    print(f"anthropic SDK version: {getattr(anthropic, '__version__', 'unknown')}")

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

    matches = load_json_list(MATCHES_FILE)
    throws = load_json_list(THROWS_FILE)
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
            match = parsed["match"]
            match_throws = parsed["throws"]
        except Exception as e:
            print(f"  ERROR extracting data from {image_path.name}: {e}")
            print("  Leaving this file in darts/incoming/ so it can be retried.")
            any_errors = True
            continue

        match_id_source = f"{match.get('date', 'unknown')}_{match.get('time', 'unknown')}_{image_path.stem}"
        match_id = re.sub(r"[^a-zA-Z0-9_-]", "-", match_id_source).strip("-").lower()

        match["match_id"] = match_id
        match["source_image"] = image_path.name
        match["source_hash"] = h
        match["processed_at"] = datetime.now(timezone.utc).isoformat()

        for t in match_throws:
            t["match_id"] = match_id

        matches.append(match)
        throws.extend(match_throws)
        known_hashes.add(h)

        image_path.rename(PROCESSED_DIR / image_path.name)
        print(f"  Extracted OK -> match_id={match_id} ({len(match_throws)} throws)")

    save_json_list(MATCHES_FILE, matches)
    save_json_list(THROWS_FILE, throws)
    print(f"Done. Total matches: {len(matches)}, total throws: {len(throws)}")

    if any_errors:
        # Non-zero exit still lets git-auto-commit-action save any successful
        # extractions from this run, but marks the Action run as failed so
        # it's visible in the Actions tab.
        sys.exit(1)


if __name__ == "__main__":
    main()
