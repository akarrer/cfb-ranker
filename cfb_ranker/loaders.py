"""Loaders for the pluggable external factors: schedule, PFF, snaps, CFB27 overrides.

All are optional. Missing factors are simply dropped and their weight is
redistributed across the factors you do have (see ranker.py). This is what lets
the app run end-to-end today on EA data alone, and get sharper as you add data.

CSV formats (headers required; team names must match EA labels, e.g. "Ohio State"):
  schedule.csv     home,away[,date]           (one row per game)
  pff.csv          team,pff_grade             (0-100)
  snaps.csv        team,player,snaps          (career or last-season snaps)
  cfb27_team.csv   team,overall[,offense,defense]   (official EA team overall override)
"""
from __future__ import annotations

import csv
import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

from .model import Team


def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# --- schedule --------------------------------------------------------------

def load_schedule_csv(path: Path) -> list[tuple[str, str]]:
    rows = _read_csv(path)
    return [(r["home"].strip(), r["away"].strip()) for r in rows if r.get("home") and r.get("away")]


def load_schedule_espn(
    year: int,
    cache_dir: Path,
    log=print,
) -> list[tuple[str, str]]:
    """Fetch a season's FBS games from ESPN's public API (no key required).

    Returns raw (home, away) using ESPN's school 'location' names; run the result
    through match_games_to_ea() to align names with EA team labels. Cached per year.
    """
    cache = Path(cache_dir) / f"espn_games_{year}.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = []
        # regular season (seasontype=2) weeks 1-16, plus bowls/playoff (seasontype=3)
        plan = [(2, w) for w in range(1, 17)] + [(3, w) for w in range(1, 6)]
        for stype, week in plan:
            url = (
                "https://site.api.espn.com/apis/site/v2/sports/football/college-football/"
                f"scoreboard?dates={year}&seasontype={stype}&week={week}&groups=80&limit=400"
            )
            try:
                d = json.loads(_http_get(url))
            except Exception:  # noqa: BLE001
                continue
            for e in d.get("events", []):
                comp = (e.get("competitions") or [{}])[0]
                home = away = None
                for c in comp.get("competitors", []):
                    loc = (c.get("team") or {}).get("location")
                    if c.get("homeAway") == "home":
                        home = loc
                    elif c.get("homeAway") == "away":
                        away = loc
                if home and away:
                    raw.append([home, away])
        cache.write_text(json.dumps(raw), encoding="utf-8")
        log(f"[espn] fetched {len(raw)} games for {year}")
    return [(h, a) for h, a in raw]


def apply_result_overrides(games: list[dict], path: Path, log=print) -> int:
    """Apply manual final-score overrides from a CSV.

    Columns: home,away,home_score,away_score  (team names in either orientation).
    Use for a bad data feed OR a deliberate judgment override (e.g. correcting a
    blown call). Matches on normalized names so the corrected result flows into
    both grading and the in-season ratings update. Returns count applied.
    """
    rows = _read_csv(path)
    idx = {}
    for r in rows:
        try:
            idx[frozenset((_norm(r["home"]), _norm(r["away"])))] = (
                r["home"].strip(), int(float(r["home_score"])), int(float(r["away_score"])))
        except (KeyError, ValueError):
            continue
    n = 0
    for g in games:
        hit = idx.get(frozenset((_norm(g["home"]), _norm(g["away"]))))
        if not hit:
            continue
        ov_home, ov_hs, ov_as = hit
        if _norm(ov_home) == _norm(g["home"]):
            g["home_score"], g["away_score"] = ov_hs, ov_as
        else:  # override listed with teams flipped vs the game's home/away
            g["home_score"], g["away_score"] = ov_as, ov_hs
        n += 1
    if n:
        log(f"[results] applied {n} manual score override(s)")
    return n


def load_results_espn(year: int, cache_dir: Path, log=print) -> list[dict]:
    """Fetch COMPLETED FBS games (final scores) for the season from ESPN's public API.

    Returns [{date, home, away, home_score, away_score, neutral}] using ESPN school
    'location' names (run through match_results_to_ea to align with model names).
    Not cached (results change as games finish); ESPN needs a curl-style UA.
    """
    out = []
    seen = set()  # dedupe FCS-at-FBS games that appear in both group feeds
    # groups 80 = FBS, 81 = FCS
    plan = [(g, 2, w) for g in (80, 81) for w in range(1, 17)]
    plan += [(g, 3, w) for g in (80, 81) for w in range(1, 6)]
    for grp, stype, week in plan:
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/football/college-football/"
            f"scoreboard?dates={year}&seasontype={stype}&week={week}&groups={grp}&limit=400"
        )
        try:
            d = json.loads(_http_get(url))
        except Exception:  # noqa: BLE001
            continue
        for e in d.get("events", []):
            if (e.get("status", {}).get("type", {}) or {}).get("state") != "post":
                continue
            comp = (e.get("competitions") or [{}])[0]
            home = away = None
            for c in comp.get("competitors", []):
                t = (c.get("team") or {}).get("location")
                s = c.get("score")
                try:
                    s = int(s)
                except (TypeError, ValueError):
                    s = None
                if c.get("homeAway") == "home":
                    home = (t, s)
                elif c.get("homeAway") == "away":
                    away = (t, s)
            if home and away and home[0] and away[0] and home[1] is not None and away[1] is not None:
                key = (e.get("id") or ((e.get("date") or "")[:10], home[0], away[0]))
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "date": (e.get("date") or "")[:10],
                    "home": home[0], "away": away[0],
                    "home_score": home[1], "away_score": away[1],
                    "neutral": bool(comp.get("neutralSite", False)),
                })
    log(f"[espn] {len(out)} completed games for {year}")
    return out


def _http_get(url: str, timeout: int = 30) -> str:
    # ESPN's edge blocks browser-style UAs but allows curl-style ones.
    req = urllib.request.Request(
        url, headers={"User-Agent": "curl/8.4.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


# ESPN school names vs EA labels differ in a handful of cases; map the exceptions.
_EA_ALIASES = {
    "connecticut": "UConn",
    "miami": "Miami",
    "miami (oh)": "Miami (Ohio)",
    "louisiana": "Louisiana",
    "louisiana monroe": "UL Monroe",
    "sam houston": "Sam Houston",
    "app state": "Appalachian State",
    "southern miss": "Southern Miss",
    "hawai'i": "Hawaii",
    "san jose state": "San José State",
    "uconn": "Connecticut",
    "florida atlantic": "FAU",
    "florida international": "FIU",
    "south florida": "USF",
    "ul monroe": "UL Monroe",
    "sam houston state": "Sam Houston",
    "california": "Cal",
    "massachusetts": "UMass",
    "middle tennessee": "Middle Tennessee State",
    "st. thomas": "St. Thomas (MN)",
    "saint thomas": "St. Thomas (MN)",
}


def _norm(s: str) -> str:
    """Lowercase and strip diacritics/punctuation so 'San José State' == 'san jose state'."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in s.lower() if ch.isalnum() or ch == " ").strip()


def match_games_to_ea(
    raw_games: list[tuple[str, str]], ea_names: list[str]
) -> list[tuple[str, str]]:
    """Resolve ESPN school names to EA team labels. Unmatched opponents (mostly
    FCS) are kept as-is so they still add schedule connectivity."""
    by_norm = {_norm(n): n for n in ea_names}
    alias_norm = {_norm(k): v for k, v in _EA_ALIASES.items()}

    def resolve(name: str) -> str:
        n = _norm(name)
        if n in by_norm:
            return by_norm[n]
        if n in alias_norm:
            return alias_norm[n]
        # Exact + accent-normalized + alias only. NO prefix matching: a more-specific
        # school ("Kentucky State", "West Virginia State", "Florida A&M") must never
        # collapse to the base name ("Kentucky", ...), and "Ohio State" != "Ohio".
        # Unmatched names are left raw (harmless graph node; skipped in grading).
        return name

    return [(resolve(h), resolve(a)) for h, a in raw_games]


def load_schedule_cfbd(
    year: int,
    cache_dir: Path,
    api_key: Optional[str] = None,
    log=print,
) -> list[tuple[str, str]]:
    """Fetch a season's games from the College Football Data API (free key).

    Set CFBD_API_KEY in your environment or pass api_key. Cached to raw/cfbd_<year>.json.
    """
    api_key = api_key or os.environ.get("CFBD_API_KEY")
    cache = Path(cache_dir) / f"cfbd_games_{year}.json"
    if cache.exists():
        games = json.loads(cache.read_text(encoding="utf-8"))
    else:
        if not api_key:
            raise RuntimeError(
                "No schedule.csv and no CFBD_API_KEY set. Get a free key at "
                "collegefootballdata.com, then: setx CFBD_API_KEY <key>"
            )
        url = f"https://api.collegefootballdata.com/games?year={year}&seasonType=both"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=45) as resp:
            games = json.loads(resp.read().decode("utf-8"))
        cache.write_text(json.dumps(games), encoding="utf-8")
        log(f"[cfbd] fetched {len(games)} games for {year}")
    out = []
    for g in games:
        h = g.get("homeTeam") or g.get("home_team")
        a = g.get("awayTeam") or g.get("away_team")
        if h and a:
            out.append((h, a))
    return out


# --- factor CSVs -----------------------------------------------------------

def apply_pff(teams: list[Team], path: Path) -> int:
    by_name = {t.name.lower(): t for t in teams}
    n = 0
    for r in _read_csv(path):
        t = by_name.get(r["team"].strip().lower())
        if t is not None and r.get("pff_grade"):
            t.pff_grade = float(r["pff_grade"])
            n += 1
    return n


def apply_snaps(teams: list[Team], path: Path) -> int:
    """Apply per-player career snaps (and optional starts) from snaps.csv.

    Columns: team,player,snaps[,starts]
    """
    idx: dict[tuple[str, str], tuple[Optional[int], Optional[int]]] = {}
    for r in _read_csv(path):
        try:
            snaps = int(float(r["snaps"])) if r.get("snaps") not in (None, "") else None
        except (KeyError, ValueError):
            snaps = None
        try:
            starts = int(float(r["starts"])) if r.get("starts") not in (None, "") else None
        except (KeyError, ValueError):
            starts = None
        if snaps is None and starts is None:
            continue
        idx[(r["team"].strip().lower(), r["player"].strip().lower())] = (snaps, starts)
    n = 0
    for t in teams:
        for p in t.players:
            hit = idx.get((t.name.lower(), p.name.lower()))
            if hit is not None:
                p.snaps, p.starts = hit
                n += 1
    return n


def read_nil_dict(path: Path) -> dict[str, float]:
    """Read a team,nil_value CSV into {team: value}."""
    out: dict[str, float] = {}
    for r in _read_csv(path):
        if r.get("team") and r.get("nil_value"):
            try:
                out[r["team"].strip()] = float(r["nil_value"])
            except ValueError:
                continue
    return out


def apply_nil(teams: list[Team], path: Path) -> int:
    """Apply NIL/roster valuations ($M) from nil.csv (team,nil_value). Display-only."""
    by_name = {t.name.lower(): t for t in teams}
    vals = read_nil_dict(path)
    n = 0
    for name, v in vals.items():
        t = by_name.get(name.lower())
        if t is not None:
            t.nil_value = v
            n += 1
    return n


def apply_cfb27_override(teams: list[Team], path: Path) -> int:
    by_name = {t.name.lower(): t for t in teams}
    n = 0
    for r in _read_csv(path):
        t = by_name.get(r["team"].strip().lower())
        if t is not None and r.get("overall"):
            t.cfb27_team_overall = float(r["overall"])
            n += 1
    return n
