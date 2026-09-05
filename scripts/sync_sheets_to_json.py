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

Uses only the Python standard library -- no dependencies to install.

Run manually via the "Sync Sheets to JSON" GitHub Action (Actions tab ->
Sync Sheets to JSON -> Run workflow) any time after updating scores in
either spreadsheet.
"""

import csv
import io
import json
import sys
import urllib.request
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


def main():
    any_errors = False

    # 1. Always-live tabs.
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

    if any_errors:
        print('\nOne or more live tabs failed to sync -- check the errors above.')
        sys.exit(1)

    print('\nDone.')


if __name__ == '__main__':
    main()
