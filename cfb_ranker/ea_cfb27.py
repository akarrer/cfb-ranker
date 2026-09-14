"""Live loader for EA SPORTS College Football 27 ratings.

EA's ratings site is a Next.js app. There is no usable public REST API from a
client, but the server-rendered data is exposed at the Next.js data endpoint:

    /_next/data/<buildId>/games/ea-sports-college-football/ratings.json
        ?franchiseSlug=ea-sports-college-football[&team=<id>]

Strategy:
  1. discover the (rotating) buildId from the ratings HTML,
  2. read the base feed to enumerate teams (conference -> teams -> id),
  3. pull each team's full roster (one request per team; no pagination needed).

Responses are cached to data/raw/ea/ so re-runs are instant and offline-friendly.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .model import Player, Team

RATINGS_URL = "https://www.ea.com/games/ea-sports-college-football/ratings"
DATA_TMPL = (
    "https://www.ea.com/_next/data/{build_id}/games/"
    "ea-sports-college-football/ratings.json?franchiseSlug=ea-sports-college-football"
)
HEADERS = {"User-Agent": "Mozilla/5.0 (cfb-ranker; research use)"}


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(url: str, timeout: int = 30) -> dict:
    # EA serves UTF-8; decode explicitly (Windows default cp1252 chokes on it).
    return json.loads(_get(url, timeout).decode("utf-8"))


def discover_build_id() -> str:
    html = _get(RATINGS_URL).decode("utf-8", errors="replace")
    m = re.search(r'"buildId":"([^"]+)"', html)
    if not m:
        raise RuntimeError("Could not find Next.js buildId on EA ratings page.")
    return m.group(1)


def _team_index(base_props: dict) -> list[dict]:
    """Return [{id, name, conference}] from the base feed's teamGroups."""
    filters = base_props["pageProps"]["ratingsFilters"]
    out = []
    for group in filters.get("teamGroups", []):
        conf = group.get("label", group.get("id", ""))
        for t in group.get("teams", []):
            out.append({"id": t["id"], "name": t["label"], "conference": conf})
    # de-dupe (a team can appear once per group; ids are unique)
    seen, uniq = set(), []
    for t in out:
        if t["id"] not in seen:
            seen.add(t["id"])
            uniq.append(t)
    return uniq


def _players_from_items(items: list[dict], team_name: str) -> list[Player]:
    players = []
    for it in items:
        pos = it.get("position") or {}
        ptype = (pos.get("positionType") or {}).get("id", "")
        players.append(
            Player(
                name=f"{it.get('firstName','').strip()} {it.get('lastName','').strip()}".strip(),
                team=team_name,
                position=pos.get("id", ""),
                position_type=ptype,
                overall=int(it.get("overallRating") or 0),
                school_year=it.get("schoolYear", ""),
            )
        )
    return players


def load_teams(
    cache_dir: Path,
    refresh: bool = False,
    polite_delay: float = 0.4,
    log=print,
) -> list[Team]:
    """Fetch (or load cached) all FBS teams with full rosters from EA CFB 27."""
    cache_dir = Path(cache_dir)
    ea_dir = cache_dir / "ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    index_path = ea_dir / "_index.json"

    if index_path.exists() and not refresh:
        index = json.loads(index_path.read_text(encoding="utf-8"))
        build_id = index["build_id"]
        team_list = index["teams"]
        log(f"[ea] using cached index ({len(team_list)} teams, build {build_id})")
    else:
        build_id = discover_build_id()
        log(f"[ea] discovered buildId={build_id}")
        base = _get_json(DATA_TMPL.format(build_id=build_id))
        team_list = _team_index(base)
        index_path.write_text(
            json.dumps({"build_id": build_id, "teams": team_list}, indent=2),
            encoding="utf-8",
        )
        log(f"[ea] indexed {len(team_list)} teams")

    teams: list[Team] = []
    for i, meta in enumerate(team_list, 1):
        tpath = ea_dir / f"{meta['id']}.json"
        if tpath.exists() and not refresh:
            data = json.loads(tpath.read_text(encoding="utf-8"))
        else:
            url = DATA_TMPL.format(build_id=build_id) + f"&team={meta['id']}"
            data = _get_json(url)
            tpath.write_text(json.dumps(data), encoding="utf-8")
            log(f"[ea] ({i}/{len(team_list)}) fetched {meta['name']}")
            time.sleep(polite_delay)  # be a good citizen

        items = data["pageProps"]["ratingDetails"]["items"]
        team = Team(
            name=meta["name"],
            conference=meta["conference"],
            division="FBS",
            ea_team_id=meta["id"],
        )
        team.players = _players_from_items(items, meta["name"])
        teams.append(team)

    return teams
