#!/usr/bin/env python3
"""
NFL DFS Projection Pipeline
────
Sources:
  1. Sleeper API    – active rosters, positions, depth-chart ranks
  2. nfl_data_py    – historical per-game fantasy-point baselines (rolling 3 seasons)
                    with recency weighting (last RECENCY_WEEKS weeks count 2×)
                    LOAD_YEARS is computed dynamically — no manual updates needed.
                    If the current season has no data yet (e.g. pre-Week 1),
                    the pipeline gracefully falls back to the prior two seasons.
  3. The Odds API   – live game totals used to scale projections up/down

Output:
  One tab per position (QB, RB, WR, TE, K, DEF) + a "Color Key" tab, each containing:
    Name | Position | Team | DK Salary | DK Projection | FD Projection | Ownership
  Rows are color-coded by projection tier (position-relative percentile).
  POST {"tab": "...", "clear": True, "rows": rows, "rowColors": colors} → GOOGLE_WEBAPP_URL

  ── Google Apps Script requirement ──────────────────────────────────────────
  Your Apps Script doPost() must handle the "rowColors" field in the payload.
  After writing each row, apply the background color from rowColors[i] to that row.
  Example snippet to add inside your doPost() after writing rows:
    var colors = data.rowColors;
    if (colors && colors.length === rows.length) {
      for (var i = 0; i < rows.length; i++) {
        sheet.getRange(startRow + i, 1, 1, rows[i].length)
             .setBackground(colors[i]);
      }
    }
  ────────────────────────────────────────────────────────────────────────────
"""

import os
import re
import sys
import logging
from collections import defaultdict
from datetime import datetime

import requests
import nfl_data_py as nfl

# ── Logging ────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ────
SLEEPER_URL     = "https://api.sleeper.app/v1/players/nfl"
ODDS_API_URL    = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
AVG_GAME_TOTAL  = 44.5          # league-average O/U used as multiplier baseline

# Dynamic rolling 3-season window — never needs manual updating.
# E.g. run in Sept 2026 → [2024, 2025, 2026]; 2026 rows added as games are played.
_CURRENT_YEAR = datetime.now().year
LOAD_YEARS    = [_CURRENT_YEAR - 2, _CURRENT_YEAR - 1, _CURRENT_YEAR]

# At a new-season start, end-of-prior-season form matters more → wider window.
# Weeks weighted 2× for recency. 8 weeks covers the last ~2 months of the prior season.
RECENCY_WEEKS = 8

SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}

# DraftKings salary bands (min, max) per position
SALARY_BANDS = {
    "QB":  (5500, 8500),
    "RB":  (4000, 8000),
    "WR":  (3500, 8500),
    "TE":  (3000, 7000),
    "K":   (3400, 5200),
    "DEF": (2200, 4800),
}

# Realistic per-game fantasy-point ceiling for salary scaling and % projection display
PROJ_CEILING = {
    "QB": 30.0, "RB": 22.0, "WR": 18.0,
    "TE": 14.0, "K":  10.0, "DEF": 12.0,
}

# ── FanDuel vs DraftKings scoring adjustments ────────────────────────────────
# Key differences accounted for:
#   QB  – DK awards +3 bonus for 300+ passing yards; FD does not.
#   RB  – DK is full PPR (1 pt/reception); FD is half PPR (0.5 pt/rec).
#          DK awards +3 bonus for 100+ rushing yards; FD does not.
#   WR  – Same PPR and bonus difference as RB (applied to receiving).
#   TE  – Same PPR and bonus difference as WR.
#   K   – Scoring is nearly identical on both sites; factor = 1.00.
#   DEF – FD does not award as many incremental bonuses for sacks/TOs;
#          slight downward adjustment applied.
FD_ADJUSTMENT: dict[str, float] = {
    "QB":  0.95,
    "RB":  0.90,
    "WR":  0.87,
    "TE":  0.88,
    "K":   1.00,
    "DEF": 0.95,
}

# ── Tier color scheme ─────────────────────────────────────────────────────────
# Tiers are assigned per-position so a WR's percentile is relative to other WRs,
# not QBs. Percentile thresholds run from the top (rank 1 = highest projection).
#
#   Elite   top 15%     Gold   #FFD966  — must-start anchors & game-stack pieces
#   Strong  top 35%    Green   #93C47D  — core lineup builders, best edge plays
#   Solid   top 60%     Blue   #9FC5E8  — cash-game plays, reliable floors
#   Average top 80%    White   #FFFFFF  — situational / matchup-dependent
#   Fade    bottom 20%   Red   #EA9999  — below average, avoid or GPP dart only
#
TIER_COLORS: dict[str, str] = {
    "Elite":   "#FFD966",   # gold
    "Strong":  "#93C47D",   # green
    "Solid":   "#9FC5E8",   # blue
    "Average": "#FFFFFF",   # white
    "Fade":    "#EA9999",   # pink/red
}
HEADER_COLOR = "#1F4E79"    # dark navy for every header row

# Column headers for each position sheet
HEADERS = ["Name", "Position", "Team", "DK Salary", "DK Projection", "FD Projection", "Ownership"]

# ── Color Key tab content ─────────────────────────────────────────────────────
COLOR_KEY_HEADERS = ["Color", "Tier", "Percentile", "DFS Recommendation"]
_COLOR_KEY_DATA = [
    ("Elite",   "Top 15%",      "Must-start anchors & game-stack core pieces"),
    ("Strong",  "Top 35%",      "High-floor edge plays — core lineup builders"),
    ("Solid",   "Top 60%",      "Reliable cash-game plays, safe floors"),
    ("Average", "Top 80%",      "Situational — use only in strong matchups"),
    ("Fade",    "Bottom 20%",   "Below average — avoid or contrarian GPP dart"),
]
# Row data for Color Key tab (first cell left blank; row fill color IS the key)
COLOR_KEY_ROWS       = [["", tier, pct, rec] for tier, pct, rec in _COLOR_KEY_DATA]
COLOR_KEY_ROW_COLORS = [TIER_COLORS[tier]    for tier, _,   _   in _COLOR_KEY_DATA]

# ── Credentials (env vars take priority; hardcoded values are fallbacks) ────
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY",      "ab02e77ff7e1a86cb058f27c62396f38")
GOOGLE_WEBAPP_URL = os.environ.get(
    "GOOGLE_WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbzTa_GcQc3t4q_2pvYSNjtYEwkjBrIof5J6cm2Ti0mx2xQ_Sy30EI1Mkhp_LH9xGL33XQ/exec",
)


# ════
# 1. Sleeper API — active rosters & depth charts
# ════

def fetch_sleeper_players() -> dict[str, dict]:
    """
    Returns:
        { full_name: {"position": str, "team": str, "depth_order": int} }
    Only active, skill-position players with a current NFL team are kept.
    """
    log.info("Fetching Sleeper player data …")
    try:
        resp = requests.get(SLEEPER_URL, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        log.error("Sleeper API timed out.")
        return {}
    except requests.exceptions.RequestException as exc:
        log.error("Sleeper API error: %s", exc)
        return {}

    players: dict[str, dict] = {}
    for info in resp.json().values():
        pos  = (info.get("position") or "").strip()
        team = (info.get("team")     or "").strip()
        name = (info.get("full_name") or "").strip()

        if not name or pos not in SKILL_POSITIONS or not team:
            continue

        depth = int(info.get("depth_chart_order") or 99)

        # Keep the shallowest (starter) depth slot when duplicates appear
        if name not in players or depth < players[name]["depth_order"]:
            players[name] = {"position": pos, "team": team, "depth_order": depth}

    log.info("Sleeper: %d active skill-position players loaded.", len(players))
    return players


# ════
# 2. nfl_data_py — weekly stats with recency weighting
# ════

def fetch_baselines() -> dict[str, float]:
    """
    Loads player stats for LOAD_YEARS via nfl_data_py and returns:
        { player_name: weighted_avg_fantasy_points_per_game }

    LOAD_YEARS is a rolling 3-season window computed at runtime (e.g. [2024,2025,2026]).
    If the current season has no rows yet (pre-Week 1 of a new season), the pipeline
    automatically falls back to the prior two seasons so projections never break.

    The most recent RECENCY_WEEKS weeks of the latest available season are weighted 2×
    to reflect current form more accurately than a flat historical average.
    Uses half-PPR scoring when available, falls back to full-PPR or standard.
    """
    log.info("Loading nfl_data_py player stats for seasons %s …", LOAD_YEARS)

    # Try the full rolling window first; if the current year isn't available yet,
    # fall back to just the prior two seasons.
    for years_to_try in (LOAD_YEARS, LOAD_YEARS[:-1]):
        try:
            df = nfl.import_weekly_data(years_to_try)
            if df is not None and not df.empty:
                if years_to_try != LOAD_YEARS:
                    log.warning(
                        "nfl_data_py: no data for %d yet — using %s as fallback.",
                        LOAD_YEARS[-1], years_to_try,
                    )
                log.info("nfl_data_py: loaded %d rows for seasons %s.", len(df), years_to_try)
                break
            log.warning("nfl_data_py: empty result for seasons %s, trying fallback …", years_to_try)
        except Exception as exc:
            log.warning("nfl_data_py failed for %s: %s — trying fallback …", years_to_try, exc)
    else:
        log.error("nfl_data_py: all attempts failed, no baseline data available.")
        return {}

    # Detect scoring and name columns
    pts_col  = next((c for c in ("fantasy_points_half_ppr", "fantasy_points_ppr",
                                 "fantasy_points") if c in df.columns), None)
    name_col = next((c for c in ("player_display_name", "player_name",
                                 "player_id") if c in df.columns), None)

    if not pts_col or not name_col:
        log.error("nfl_data_py: required columns missing. Got: %s", list(df.columns))
        return {}

    log.info("nfl_data_py: using '%s' as points column.", pts_col)

    # Determine recency cutoff from the most recent season + week in the data
    has_season = "season" in df.columns
    has_week   = "week"   in df.columns

    if has_season and has_week:
        max_season = int(df["season"].max())
        max_week   = int(df[df["season"] == max_season]["week"].max())
        log.info(
            "nfl_data_py: latest data → season %d week %d (recent window: last %d weeks).",
            max_season, max_week, RECENCY_WEEKS,
        )

        def _weight(row) -> float:
            """Return 2.0 for recent games, 1.0 for older games."""
            return 2.0 if (
                row["season"] == max_season
                and (max_week - row["week"]) < RECENCY_WEEKS
            ) else 1.0
    else:
        def _weight(row) -> float:  # type: ignore[misc]
            return 1.0

    keep_cols = [name_col, pts_col] + (["season", "week"] if has_season and has_week else [])
    sub = df[keep_cols].dropna()
    sub = sub[sub[pts_col].astype(float) > 0].copy()

    # Weighted average: w_sum / w_tot per player
    w_sum: dict[str, float] = defaultdict(float)
    w_tot: dict[str, float] = defaultdict(float)

    for _, row in sub.iterrows():
        name   = str(row[name_col]).strip()
        pts    = float(row[pts_col])
        weight = _weight(row)
        w_sum[name] += pts * weight
        w_tot[name] += weight

    baselines = {
        name: round(w_sum[name] / w_tot[name], 3)
        for name in w_sum if w_tot[name] > 0
    }

    log.info("nfl_data_py: %d player baselines computed.", len(baselines))
    return baselines


# ════
# 3. The Odds API — live game totals → projection multipliers
# ════

def fetch_multipliers() -> dict[str, float]:
    """
    Returns:
        { team_name: multiplier }   e.g. {"Kansas City Chiefs": 1.08}
    multiplier = game_total / AVG_GAME_TOTAL
    Returns {} when key is missing or the request fails.
    """
    if not ODDS_API_KEY:
        log.warning("ODDS_API_KEY not set — odds adjustment disabled.")
        return {}

    log.info("Fetching NFL game totals from The Odds API …")
    params = {
        "apiKey":     ODDS_API_KEY,
        "regions":    "us",
        "markets":    "totals",
        "oddsFormat": "american",
    }
    try:
        resp = requests.get(ODDS_API_URL, params=params, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        log.error("Odds API timed out.")
        return {}
    except requests.exceptions.RequestException as exc:
        log.error("Odds API error: %s", exc)
        return {}

    multipliers: dict[str, float] = {}
    for game in resp.json():
        total = _game_total(game)
        if total is None:
            continue
        mult = round(total / AVG_GAME_TOTAL, 4)
        for team in (game.get("home_team", ""), game.get("away_team", "")):
            if team:
                multipliers[team.strip()] = mult

    log.info("Odds API: %d team multipliers loaded.", len(multipliers))
    return multipliers


def _game_total(game: dict) -> float | None:
    """Extract the Over/Under point total from the first bookmaker that has it."""
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") == "totals":
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") == "Over":
                        try:
                            return float(outcome["point"])
                        except (KeyError, TypeError, ValueError):
                            pass
    return None


# ════
# Helpers
# ════

def _normalize(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower()).strip()


def _match_baseline(name: str, baselines: dict[str, float],
                    _cache: dict = {}) -> float:
    """Exact → normalised → last-name-only cascade. Returns 0.0 on miss."""
    if name in _cache:
        return _cache[name]
    if name in baselines:
        _cache[name] = baselines[name]
        return baselines[name]

    norm = _normalize(name)
    for bname, val in baselines.items():
        if _normalize(bname) == norm:
            _cache[name] = val
            return val

    last = norm.split()[-1] if norm else ""
    for bname, val in baselines.items():
        parts = _normalize(bname).split()
        if parts and parts[-1] == last:
            _cache[name] = val
            return val

    _cache[name] = 0.0
    return 0.0


def _team_mult(team: str, multipliers: dict[str, float]) -> float:
    """Match Sleeper abbr (e.g. 'KC') against Odds API full names."""
    if not multipliers:
        return 1.0
    if team in multipliers:
        return multipliers[team]
    for key, val in multipliers.items():
        if team.upper() in key.upper():
            return val
    return 1.0


def _salary(pos: str, projection: float, depth: int) -> int:
    lo, hi  = SALARY_BANDS.get(pos, (3000, 6000))
    ceiling = PROJ_CEILING.get(pos, 20.0)
    score   = min(projection / ceiling, 1.0)
    penalty = max(0.0, (depth - 1) * 0.05)
    score   = max(0.0, score - penalty)
    return round((lo + score * (hi - lo)) / 100) * 100


def _ownership(rank: int, pos: str) -> float:
    base = {"QB": 25, "RB": 20, "WR": 18, "TE": 18, "K": 12, "DEF": 15}.get(pos, 15)
    return round(base * (0.75 ** (rank - 1)), 1)


def _fmt_proj(projection: float) -> str:
    """Format projection as a fixed 2-decimal string, e.g. '22.50'."""
    return f"{projection:.2f}"


def _fd_proj(dk_proj: float, pos: str) -> str:
    """
    Convert a DK projection to an FD projection using position-specific
    adjustment factors that account for:
      - Half-PPR (FD) vs full-PPR (DK) for skill positions
      - No yardage milestone bonuses on FD (DK: +3 for 100+ rush/rec yds,
        +3 for 300+ pass yds)
      - Minor defensive scoring structure differences
    Returns a formatted 2-decimal string.
    """
    return _fmt_proj(round(dk_proj * FD_ADJUSTMENT.get(pos, 1.0), 2))


def _assign_tiers(n: int) -> list[str]:
    """
    Given n players already sorted highest-projection-first, return a tier
    label for each based on their position-relative percentile rank.

    Thresholds (measured from the top):
        Elite   → top 15%   — Gold   must-start anchors & game-stack pieces
        Strong  → top 35%   — Green  core lineup builders, best edge plays
        Solid   → top 60%   — Blue   cash-game plays, reliable floors
        Average → top 80%   — White  situational / matchup-dependent
        Fade    → bottom 20% — Red   below average, avoid or GPP dart
    """
    tiers: list[str] = []
    for rank in range(1, n + 1):
        pct = rank / n          # fraction from top: 1/n for rank-1, 1.0 for last
        if pct <= 0.15:
            tiers.append("Elite")
        elif pct <= 0.35:
            tiers.append("Strong")
        elif pct <= 0.60:
            tiers.append("Solid")
        elif pct <= 0.80:
            tiers.append("Average")
        else:
            tiers.append("Fade")
    return tiers


# ════
# 4. Build projection rows — one dict entry per position
# ════

def build_rows(
    sleeper: dict,
    baselines: dict,
    multipliers: dict,
) -> dict[str, tuple[list[list], list[str]]]:
    """
    Merge all three sources and return a dict keyed by position:
        {
          "QB": (rows, row_colors),
          "RB": (rows, row_colors),
          …
        }

    rows       – [HEADERS, data_row, ...]
    row_colors – [HEADER_COLOR, tier_hex, ...]  (parallel to rows)

    Each data row: [Name, Position, Team, DK Salary, DK Projection, FD Projection, Ownership]
    Each data row is color-coded by its projection tier within the position group.
    """

    # Per-position average baseline (fallback for players without history)
    pos_avg: dict[str, float] = {}
    for pos in SKILL_POSITIONS:
        vals = [
            _match_baseline(n, baselines)
            for n, d in sleeper.items()
            if d["position"] == pos and _match_baseline(n, baselines) > 0
        ]
        pos_avg[pos] = round(sum(vals) / len(vals), 3) if vals else 10.0

    # Group players by position
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for name, info in sleeper.items():
        pos      = info["position"]
        baseline = _match_baseline(name, baselines) or pos_avg.get(pos, 10.0)
        dk_proj  = round(baseline * _team_mult(info["team"], multipliers), 2)
        by_pos[pos].append({
            "name":    name,
            "pos":     pos,
            "team":    info["team"],
            "depth":   info["depth_order"],
            "dk_proj": dk_proj,
        })

    # Build per-position tables with tier colors
    position_tables: dict[str, tuple[list[list], list[str]]] = {}
    for pos in sorted(by_pos):
        ranked = sorted(by_pos[pos], key=lambda p: (-p["dk_proj"], p["depth"]))
        tiers  = _assign_tiers(len(ranked))

        rows:       list[list] = [HEADERS]
        row_colors: list[str]  = [HEADER_COLOR]        # header gets navy

        for rank, (tier, p) in enumerate(zip(tiers, ranked), 1):
            rows.append([
                p["name"],
                p["pos"],
                p["team"],
                _salary(p["pos"], p["dk_proj"], p["depth"]),
                _fmt_proj(p["dk_proj"]),                       # DK Projection
                _fd_proj(p["dk_proj"], p["pos"]),              # FD Projection
                round(_ownership(rank, p["pos"]) / 100, 4),   # Ownership
            ])
            row_colors.append(TIER_COLORS[tier])

        position_tables[pos] = (rows, row_colors)
        log.info(
            "Position %s: %d rows | Elite=%d Strong=%d Solid=%d Average=%d Fade=%d",
            pos, len(ranked),
            tiers.count("Elite"), tiers.count("Strong"), tiers.count("Solid"),
            tiers.count("Average"), tiers.count("Fade"),
        )

    return position_tables


# ════
# 5. Post to Google Sheets — one tab per position + Color Key
# ════

def post_to_sheets(tab: str, rows: list[list], row_colors: list[str]) -> bool:
    """
    POST a single tab's rows to Google Sheets, including row background colors.

    Payload fields:
        tab        – sheet/tab name
        clear      – wipe the tab before writing
        rows       – list of rows (first row is headers)
        rowColors  – parallel list of hex color strings, one per row
                     (Apps Script applies these as row background colors)
    """
    if not GOOGLE_WEBAPP_URL:
        log.error("GOOGLE_WEBAPP_URL is not set — cannot export.")
        return False

    payload = {
        "tab":       tab,
        "clear":     True,
        "rows":      rows,
        "rowColors": row_colors,
    }
    log.info("POSTing %d rows to Google Sheets tab '%s' …", len(rows) - 1, tab)
    try:
        resp = requests.post(GOOGLE_WEBAPP_URL, json=payload, timeout=30)
        resp.raise_for_status()
        log.info("Tab '%s' export successful. Response: %s", tab, resp.text[:300])
        return True
    except requests.exceptions.Timeout:
        log.error("Google Sheets POST timed out (tab '%s').", tab)
    except requests.exceptions.HTTPError as exc:
        log.error("HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
    except requests.exceptions.RequestException as exc:
        log.error("Google Sheets POST failed (tab '%s'): %s", tab, exc)
    return False


def post_color_key_tab() -> bool:
    """
    Post the Color Key legend tab explaining what each row highlight means.
    The first cell of each row is left blank — the row fill color IS the key.
    """
    rows       = [COLOR_KEY_HEADERS] + COLOR_KEY_ROWS
    row_colors = [HEADER_COLOR]      + COLOR_KEY_ROW_COLORS
    return post_to_sheets(tab="Color Key", rows=rows, row_colors=row_colors)


# ════
# Entrypoint
# ════

if __name__ == "__main__":
    log.info("═══ NFL DFS Projection Pipeline — START ═══")

    sleeper_players = fetch_sleeper_players()
    if not sleeper_players:
        log.error("No Sleeper data — cannot continue.")
        sys.exit(1)

    baselines   = fetch_baselines()
    multipliers = fetch_multipliers()

    # Build one table per position (rows + parallel row colors)
    position_tables = build_rows(sleeper_players, baselines, multipliers)

    all_ok = True

    # Post each position to its own Google Sheets tab
    for pos in sorted(position_tables):
        rows, row_colors = position_tables[pos]
        ok = post_to_sheets(tab=pos, rows=rows, row_colors=row_colors)
        if not ok:
            all_ok = False

    # Post the Color Key legend tab last
    if not post_color_key_tab():
        all_ok = False

    log.info(
        "═══ Pipeline %s ═══",
        "COMPLETE ✓" if all_ok else "FINISHED — one or more sheet exports failed",
    )
    sys.exit(0 if all_ok else 1)
