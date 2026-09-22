#!/usr/bin/env python3
"""
Fetches published Google Sheets tabs for Shuffleboard and Petanque, converts
each to JSON, and writes the results into the repo. The sites then read this
local, same-origin JSON instead of hitting Google Sheets directly through a
CORS proxy on every page load.

Completed tournaments (Deathmatch, March Madness) don't need repeated live
syncing, so their rows -- and the team-name mappings only they use -- get
captured ONCE into permanent "frozen" files on the first run, and are never
re-fetched after that. shuffleboard/all_time.json is then derived by
combining those two frozen files with League 2026's live rows (League 2026
keeps syncing live because its per-match badge columns, e.g. PD/PL/PB/KS/
TK/TG/B6/WM, are only correctly populated in the live "all_time" tab -- we
deliberately don't try to reimplement that logic here).

Also derives shuffleboard/ratings.json -- a per-player Elo-style skill
rating computed from every match in all_time.json, in chronological order
(Deathmatch -> March Madness -> League 2026, by month/round within each).
Team-tournament results (Deathmatch, March Madness) apply the same rating
change to both members of a team. This feeds the handicap suggestion on
the All-Time page.

Uses only the Python standard library -- no dependencies to install.

Run manually via the "Sync Sheets to JSON" GitHub Action (Actions tab ->
Sync Sheets to JSON -> Run workflow) any time after updating scores in
either spreadsheet.
"""

import csv
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SHUFFLEBOARD_BASE = 'https://docs.google.com/spreadsheets/d/e/2PACX-1vTbTUPmYdu5y5fSoXrduhbSs9X9tRuTtDoSS_1rsXDSedFCyLpm_xY0FTEOrR-jvWvzBTPFqEwJ3f4S/pub'
PETANQUE_BASE = 'https://docs.google.com/spreadsheets/d/e/2PACX-1vRrlm9n0oNlUxj19V9cD6W2YiV6o-NmVrmgt7MmfnR327dwrisPBEp0r1Ptn5Ua_CbqpgSYVsaEcf40/pub'

FIXTURES_URL = f'{SHUFFLEBOARD_BASE}?gid=273978565&single=true&output=csv'
BADGES_URL = f'{SHUFFLEBOARD_BASE}?gid=1226801861&single=true&output=csv'
REPLACEMENTS_URL = f'{SHUFFLEBOARD_BASE}?gid=762367159&single=true&output=csv'
ALLTIME_URL = f'{SHUFFLEBOARD_BASE}?gid=1865190557&single=true&output=csv'
TEAMS_URL = f'{SHUFFLEBOARD_BASE}?gid=804843551&single=true&output=csv'
PETANQUE_FIXTURES_URL = f'{PETANQUE_BASE}?gid=1612705044&single=true&output=csv'

# Tabs that keep syncing live on every run.
LIVE_SOURCES = {
    'shuffleboard/fixtures.json': FIXTURES_URL,
    'shuffleboard/badges.json': BADGES_URL,
    'shuffleboard/replacements.json': REPLACEMENTS_URL,
    'petanque/fixtures.json': PETANQUE_FIXTURES_URL,
}

# Completed tournaments -- captured once, never touched again after that.
FROZEN_DEATHMATCH = 'shuffleboard/deathmatch_frozen.json'
FROZEN_MARCHMADNESS = 'shuffleboard/marchmadness_frozen.json'
FROZEN_TEAMS = 'shuffleboard/teams.json'  # only Deathmatch/March Madness ever need team lookups


def fetch_csv_as_rows(url: str) -> list:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode('utf-8-sig')  # strip BOM if present
    reader = csv.DictReader(io.StringIO(raw))
    # Keep every value as a string, exactly like a browser-side CSV fetch
    # would -- this means every site's existing parseInt/parseFloat calls
    # keep working unchanged.
    return [dict(row) for row in reader]


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


def norm_tournament(row: dict) -> str:
    return (row.get('tournament') or '').strip().upper()


# --- Elo-style rating engine -------------------------------------------

RATING_K = 24
RATING_INITIAL = 1500
PROVISIONAL_THRESHOLD = 15  # matches; below this, rating is flagged provisional

TOURNAMENT_ORDER = {'DEATHMATCH': 0, 'MARCH MADNESS': 1, 'LEAGUE 2026': 2}


def _first_int(text: str) -> int:
    m = re.search(r'(\d+)', text or '')
    return int(m.group(1)) if m else 0


def compute_ratings(all_time_rows: list, team_lookup: dict, repl_lookup: dict) -> list:
    """team_lookup maps 'TOURNAMENT|TEAM_CODE' -> [member1, member2].
    repl_lookup maps 'TOURNAMENT|MONTH|original player (lowercase)' -> substitute
    name, same convention the site's own rankings table uses: a match played
    by a substitute counts toward the substitute, not the absent roster
    player. Deathmatch/March Madness are fixed teams with no substitutes, so
    this only ever matches League 2026 rows.
    """

    def resolve_side(tournament: str, month: str, name: str) -> list:
        name = (name or '').strip()
        team_key = f'{tournament}|{name.upper()}'
        if team_key in team_lookup:
            return team_lookup[team_key]
        repl_key = f'{tournament}|{month}|{name.lower()}'
        return [repl_lookup.get(repl_key, name)]

    rows = [r for r in all_time_rows
            if str(r.get('score A', '')).strip() != '' and str(r.get('score B', '')).strip() != ''
            and (r.get('player A') or '').strip() and (r.get('player B') or '').strip()]

    rows.sort(key=lambda r: (
        TOURNAMENT_ORDER.get(norm_tournament(r), 99),
        r.get('month', ''),
        _first_int(r.get('round', '')),
        _first_int(r.get('table', '')),
    ))

    ratings, matches_played = {}, {}

    def get_rating(p):
        return ratings.setdefault(p, RATING_INITIAL)

    for r in rows:
        tourn = norm_tournament(r)
        try:
            sA, sB = float(r['score A']), float(r['score B'])
        except (KeyError, ValueError):
            continue
        month = (r.get('month') or '').strip()
        sideA = [n for n in resolve_side(tourn, month, r['player A']) if n]
        sideB = [n for n in resolve_side(tourn, month, r['player B']) if n]
        if not sideA or not sideB:
            continue

        outcomeA = 1.0 if sA > sB else (0.0 if sA < sB else 0.5)
        ratingA = sum(get_rating(p) for p in sideA) / len(sideA)
        ratingB = sum(get_rating(p) for p in sideB) / len(sideB)
        expA = 1 / (1 + 10 ** ((ratingB - ratingA) / 400))
        deltaA = RATING_K * (outcomeA - expA)

        for p in sideA:
            ratings[p] = get_rating(p) + deltaA
            matches_played[p] = matches_played.get(p, 0) + 1
        for p in sideB:
            ratings[p] = get_rating(p) + (-deltaA)
            matches_played[p] = matches_played.get(p, 0) + 1

    ranked = sorted(ratings.items(), key=lambda kv: -kv[1])
    return [
        {
            'rank': i + 1,
            'player': player,
            'rating': round(rating),
            'matches_played': matches_played.get(player, 0),
            'provisional': matches_played.get(player, 0) < PROVISIONAL_THRESHOLD,
        }
        for i, (player, rating) in enumerate(ranked)
    ]


def main():
    any_errors = False

    # 1. Always-live tabs.
    replacements_rows = []
    for rel_path, url in LIVE_SOURCES.items():
        print(f'Fetching {rel_path} ...')
        try:
            rows = fetch_csv_as_rows(url)
        except Exception as e:
            print(f'  ERROR fetching {rel_path}: {e}')
            any_errors = True
            continue
        write_json(REPO_ROOT / rel_path, rows)
        print(f'  Wrote {len(rows)} rows -> {rel_path}')
        if rel_path == 'shuffleboard/replacements.json':
            replacements_rows = rows

    # Substitution lookup for the rating engine, same convention as the
    # site's own rankings table: 'TOURNAMENT|MONTH|original player' -> sub.
    repl_lookup = {}
    for r in replacements_rows:
        player = (r.get('player') or '').strip()
        replacement = (r.get('replacement') or '').strip()
        if not player or not replacement:
            continue
        key = f"{norm_tournament(r)}|{(r.get('month') or '').strip()}|{player.lower()}"
        repl_lookup[key] = replacement

    # 2. Fetch the all_time tab live -- needed every run for League 2026's
    #    badge-annotated rows, and (only on the very first run) to seed the
    #    Deathmatch/March Madness freeze below.
    print('Fetching shuffleboard all_time tab ...')
    try:
        alltime_rows_live = fetch_csv_as_rows(ALLTIME_URL)
    except Exception as e:
        print(f'  ERROR fetching all_time tab: {e}')
        sys.exit(1)

    dm_path = REPO_ROOT / FROZEN_DEATHMATCH
    mm_path = REPO_ROOT / FROZEN_MARCHMADNESS
    teams_path = REPO_ROOT / FROZEN_TEAMS
    already_frozen = dm_path.exists() and mm_path.exists() and teams_path.exists()

    if not already_frozen:
        print('Completed tournaments not yet frozen -- freezing now (one-time only)...')
        try:
            teams_rows = fetch_csv_as_rows(TEAMS_URL)
        except Exception as e:
            print(f'  ERROR fetching teams tab: {e}')
            sys.exit(1)

        dm_rows = [r for r in alltime_rows_live if norm_tournament(r) == 'DEATHMATCH']
        mm_rows = [r for r in alltime_rows_live if norm_tournament(r) == 'MARCH MADNESS']
        completed_teams = [r for r in teams_rows if norm_tournament(r) in ('DEATHMATCH', 'MARCH MADNESS')]

        write_json(dm_path, dm_rows)
        write_json(mm_path, mm_rows)
        write_json(teams_path, completed_teams)
        print(f'  Froze {len(dm_rows)} Deathmatch rows, {len(mm_rows)} March Madness rows, '
              f'{len(completed_teams)} team mappings.')
        print('  These 3 files will not be touched by future syncs. Edit them directly '
              'if historical data ever needs correcting.')
    else:
        print('Completed tournaments already frozen -- leaving deathmatch_frozen.json, '
              'marchmadness_frozen.json, and teams.json untouched.')

    # Team lookup is needed every run (not just on first freeze) to resolve
    # Deathmatch/March Madness TEAM_X codes to real player names for ratings.
    completed_teams = json.loads(teams_path.read_text()) if teams_path.exists() else []
    team_lookup = {}
    for t in completed_teams:
        key = f"{norm_tournament(t)}|{(t.get('team') or '').strip().upper()}"
        team_lookup[key] = [m for m in (t.get('member_1'), t.get('member_2')) if m]

    # 3. League 2026's live slice of all_time (this is the only part of
    #    all_time.json that actually changes run to run).
    league2026_rows = [r for r in alltime_rows_live if norm_tournament(r) == 'LEAGUE 2026']
    write_json(REPO_ROOT / 'shuffleboard/league2026_alltime_rows.json', league2026_rows)
    print(f'Wrote {len(league2026_rows)} League 2026 rows -> shuffleboard/league2026_alltime_rows.json')

    # 4. Derive the combined all_time.json every run. git only actually
    #    commits this if the content changed, which -- since the frozen
    #    files never change -- only happens when League 2026 has new rows.
    dm_rows = json.loads(dm_path.read_text()) if dm_path.exists() else []
    mm_rows = json.loads(mm_path.read_text()) if mm_path.exists() else []
    combined = dm_rows + mm_rows + league2026_rows
    write_json(REPO_ROOT / 'shuffleboard/all_time.json', combined)
    print(f'Derived shuffleboard/all_time.json: {len(combined)} rows total '
          f'({len(dm_rows)} Deathmatch + {len(mm_rows)} March Madness + {len(league2026_rows)} League 2026).')

    # 5. Recompute skill ratings from the full match history every run.
    ratings = compute_ratings(combined, team_lookup, repl_lookup)
    write_json(REPO_ROOT / 'shuffleboard/ratings.json', {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'k_factor': RATING_K,
        'initial_rating': RATING_INITIAL,
        'provisional_threshold': PROVISIONAL_THRESHOLD,
        'players': ratings,
    })
    print(f'Computed shuffleboard/ratings.json: {len(ratings)} rated players.')

    if any_errors:
        print('\nOne or more live tabs failed to sync -- check the errors above.')
        sys.exit(1)

    print('\nDone.')


if __name__ == '__main__':
    main()
