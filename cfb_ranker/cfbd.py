"""College Football Data (CFBD) API loaders: team universe, recruiting, transfer
portal, and returning production.

These power two things:
  1. a small recruiting *prior* blended into FBS talent, and
  2. the full talent estimate for FCS teams, which EA CFB 27 does not include:
        FCS talent  =  recruiting  +  returning production (last year)
                       +  added production (portal in)  -  losses (portal out)
     -- a preseason prior that carries the FCS teams until enough games are
        played for strength-of-schedule to take over.

All endpoints require a free key: get one at collegefootballdata.com and set
CFBD_API_KEY in your environment. Responses are cached under data/raw/.
Field parsing is defensive: CFBD field names have shifted over time, so we probe
a few likely names and degrade gracefully when data is missing.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

BASE = "https://api.collegefootballdata.com"

# star -> approximate "player value" points, used when an explicit rating is absent
STAR_VALUE = {5: 0.98, 4: 0.90, 3: 0.82, 2: 0.75, 1: 0.70, 0: 0.70}


class CFBDUnavailable(RuntimeError):
    pass


_NO_KEY = (
    "CFBD_API_KEY not set. Get a free key at collegefootballdata.com, then "
    "on Windows: setx CFBD_API_KEY <key>  (reopen the shell)."
)


def _key(explicit: Optional[str]) -> str:
    """The key if we have one, else '' — a cached response needs no key."""
    return explicit or os.environ.get("CFBD_API_KEY") or ""


def _get(path: str, params: dict, key: str, cache_dir: Path, cache_name: str) -> list:
    cache = Path(cache_dir) / cache_name
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if not key:
        raise CFBDUnavailable(_NO_KEY)
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _first(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


# ---------------------------------------------------------------------------
# team universe (who is FBS vs FCS)
# ---------------------------------------------------------------------------

def load_team_divisions(year: int, cache_dir: Path, key: Optional[str] = None) -> dict[str, dict]:
    """Return {school: {'classification','conference'}} for all teams CFBD knows.

    The `division` query param is unreliable (it returns a superset spanning FBS,
    FCS, D-II and D-III), so we pull everything once and bucket on each record's
    real `classification` field. Callers filter to 'fbs'/'fcs' as needed.
    """
    key = _key(key)
    rows = _get("/teams", {"year": year}, key, cache_dir, f"cfbd_teams_{year}.json")
    out: dict[str, dict] = {}
    for t in rows:
        school = _first(t, "school", "team")
        if not school:
            continue
        out[school] = {
            "classification": _first(t, "classification", default=""),
            "conference": _first(t, "conference", default=""),
        }
    return out


# ---------------------------------------------------------------------------
# recruiting (team composite points)
# ---------------------------------------------------------------------------

def load_recruiting(year: int, cache_dir: Path, key: Optional[str] = None) -> dict[str, float]:
    """{school: recruiting_points}. CFBD serves the composite team rankings."""
    key = _key(key)
    rows = _get("/recruiting/teams", {"year": year}, key, cache_dir,
                f"cfbd_recruiting_{year}.json")
    out = {}
    for r in rows:
        school = _first(r, "team", "school")
        pts = _first(r, "points", "rating")
        if school and pts is not None:
            out[school] = float(pts)
    return out


# ---------------------------------------------------------------------------
# returning production (last year's production that returns)
# ---------------------------------------------------------------------------

def load_returning(year: int, cache_dir: Path, key: Optional[str] = None) -> dict[str, float]:
    """{school: returning_fraction 0..1} — overall percent production returning."""
    key = _key(key)
    rows = _get("/player/returning", {"year": year}, key, cache_dir,
                f"cfbd_returning_{year}.json")
    out = {}
    for r in rows:
        school = _first(r, "team", "school")
        pct = _first(r, "percentPPA", "percent_ppa", "totalPPA", "total_ppa")
        if school is None or pct is None:
            continue
        val = float(pct)
        if val > 1.5:  # looks like a raw PPA total, not a fraction; squash to 0..1
            val = max(0.0, min(1.0, val / 100.0))
        out[school] = val
    return out


# ---------------------------------------------------------------------------
# transfer portal (added vs lost production)
# ---------------------------------------------------------------------------

def load_portal_net(year: int, cache_dir: Path, key: Optional[str] = None) -> dict[str, float]:
    """{school: net_portal_value} = sum(incoming values) - sum(outgoing values).

    Uses each transfer's explicit rating when present, else a star-based value.
    Positive = net talent gained via the portal, negative = net lost.
    """
    key = _key(key)
    rows = _get("/player/portal", {"year": year}, key, cache_dir,
                f"cfbd_portal_{year}.json")
    net: dict[str, float] = {}
    for r in rows:
        rating = _first(r, "rating")
        if rating is None:
            stars = int(_first(r, "stars", default=0) or 0)
            value = STAR_VALUE.get(stars, 0.70)
        else:
            value = float(rating)
        dest = _first(r, "destination")
        origin = _first(r, "origin")
        if dest:
            net[dest] = net.get(dest, 0.0) + value
        if origin:
            net[origin] = net.get(origin, 0.0) - value
    return net
