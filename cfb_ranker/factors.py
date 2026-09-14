"""Derive per-team factor inputs from rosters, and normalize factors for combining."""
from __future__ import annotations

import statistics
from typing import Optional

from .model import CLASS_WEIGHT, Player, Team


def _qb_proven(qb: Player) -> float:
    """How much to trust a QB's rating, 0..1, by proven passing experience.

    Uses real career snaps/starts when supplied (snaps.csv); otherwise falls back
    to class year. An unproven blue-chipper (high rating, no game reps) scores low,
    so his rating barely moves team talent — a proven veteran's rating counts fully.
    """
    if qb.snaps is not None:
        return max(0.15, min(1.0, (qb.snaps + 40 * (qb.starts or 0)) / 600.0))
    if qb.starts is not None:
        return max(0.15, min(1.0, qb.starts / 13.0))
    return CLASS_WEIGHT.get(qb.school_year, 0.6)


# ---------------------------------------------------------------------------
# Team overall (the "CFB 27 rating" factor), derived from the roster.
# EA's own team overall is essentially the strength of the best 11 on each side
# plus depth. We approximate that from the player pool so it works for every
# team without needing EA's (unpublished-in-feed) team number. You can still
# override with an official number via data/input/cfb27_team.csv.
# ---------------------------------------------------------------------------

def _weighted_top(overalls: list[int], starters: int, decay: float = 0.90) -> float:
    """Mean of the top `starters` overalls, then a decaying tail for depth."""
    if not overalls:
        return 0.0
    ov = sorted(overalls, reverse=True)
    core = ov[:starters]
    core_val = sum(core) / len(core)
    # depth: remaining players contribute with geometric decay
    tail = ov[starters:]
    if tail:
        w = decay
        num = den = 0.0
        for x in tail:
            num += w * x
            den += w
            w *= decay
        tail_val = num / den
    else:
        tail_val = core_val
    return 0.85 * core_val + 0.15 * tail_val


def roster_team_overall(team: Team, qb_weight: float = 0.0) -> float:
    """Offense/defense best-unit blend + depth -> a 0-99-ish team overall.

    `qb_weight` (0..1) blends the team's best quarterback's overall on top of the
    roster number, so QB play carries more than its default ~1-of-11 share of the
    offense. qb_weight=0 leaves the QB weighted like any other offensive starter.
    """
    off = [p.overall for p in team.players if p.position_type == "offense"]
    de = [p.overall for p in team.players if p.position_type == "defense"]
    st = [p.overall for p in team.players if p.position_type == "specialteam"]

    off_ovr = _weighted_top(off, starters=11)
    def_ovr = _weighted_top(de, starters=11)
    st_ovr = _weighted_top(st, starters=2) if st else 0.0

    if off_ovr and def_ovr:
        base = 0.47 * off_ovr + 0.47 * def_ovr + 0.06 * (st_ovr or off_ovr)
    else:  # incomplete roster (shouldn't happen for FBS, but be safe)
        allo = [p.overall for p in team.players]
        base = _weighted_top(allo, starters=22)

    if qb_weight > 0:
        qbs = [p for p in team.players if p.position == "QB"]
        if qbs:
            qb = max(qbs, key=lambda p: p.overall)   # presumed starter
            proven = _qb_proven(qb)
            # trust the QB's rating in proportion to how proven he is; an unproven
            # passer's rating pulls talent only slightly off the roster baseline.
            base = base + qb_weight * proven * (qb.overall - base)
    return round(base, 2)


# ---------------------------------------------------------------------------
# Experience factor (snap proxy). Real snap counts, when supplied via
# snaps.csv, override the class-year heuristic per player.
# ---------------------------------------------------------------------------

# Academies are structurally old (four-year retention, no portal, freshmen redshirt),
# so they're excluded from the average-age factor to avoid an unfair edge.
SERVICE_ACADEMIES = {"Navy", "Army", "Air Force"}
_AGE = {"Freshman": 1.0, "Sophomore": 2.0, "Junior": 3.0, "Senior": 4.0}


def average_age_index(team: Team) -> Optional[float]:
    """Average class of the two-deep (1=Fr .. 4=Sr). None for teams with no roster
    or for service academies (excluded)."""
    if team.name in SERVICE_ACADEMIES:
        return None
    core = sorted(team.players, key=lambda p: p.overall, reverse=True)[:44]
    ages = [_AGE[p.school_year] for p in core if p.school_year in _AGE]
    return (sum(ages) / len(ages)) if ages else None


def class_year_index(team: Team) -> float:
    """0..1 veteran index from class years, talent-weighted over the two-deep.
    The fallback used when no real playing-time data is available."""
    core = sorted(team.players, key=lambda p: p.overall, reverse=True)[:44]
    if not core:
        return 0.5
    num = sum(p.class_weight * max(p.overall, 1) for p in core)
    den = sum(max(p.overall, 1) for p in core)
    return num / den if den else 0.5


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def compute_experience(teams, returning: dict[str, float] | None, weights: dict) -> None:
    """Set each team's experience_raw from a blend of real playing-time signals.

    Components (each min-max normalized across teams, then blended by weight;
    any component a team lacks is dropped and its weight redistributed):
      * snaps      — sum of career snaps over the two-deep   (from snaps.csv)
      * starts     — sum of career starts over the two-deep  (from snaps.csv)
      * returning  — % of last year's production returning   (CFBD)
      * age        — average class of the two-deep           (academies excluded)

    A team with real snaps/starts is rated on accumulated playing time; a team with
    only returning production uses that; average roster age is the veteran-ness proxy
    that also carries teams with no snap/returning data.
    """
    returning = returning or {}
    w = {
        "snaps": weights.get("exp_snaps_w", 0.35),
        "starts": weights.get("exp_starts_w", 0.20),
        "returning": weights.get("exp_returning_w", 0.30),
        "age": weights.get("exp_age_w", 0.15),
    }

    def core(t):
        return sorted(t.players, key=lambda p: p.overall, reverse=True)[:44]

    snaps_raw, starts_raw, age_raw = {}, {}, {}
    for t in teams:
        c = core(t)
        if c and any(p.snaps is not None for p in c):
            snaps_raw[t.name] = sum((p.snaps or 0) for p in c)
        if c and any(p.starts is not None for p in c):
            starts_raw[t.name] = sum((p.starts or 0) for p in c)
        a = average_age_index(t)   # avg class of two-deep; None for academies
        if a is not None:
            age_raw[t.name] = a

    comp = {
        "snaps": _minmax(snaps_raw),
        "starts": _minmax(starts_raw),
        "returning": _minmax({k: v for k, v in returning.items()}),
        "age": _minmax(age_raw),
    }

    for t in teams:
        num = den = 0.0
        for name, weight in w.items():
            v = comp[name].get(t.name)
            if v is not None:
                num += weight * v
                den += weight
        t.experience_raw = (num / den) if den else None


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def to_0_100(values: dict[str, float]) -> dict[str, float]:
    """Percentile-rank style min-max to 0..100 (robust, keeps spread readable)."""
    if not values:
        return {}
    vals = list(values.values())
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return {k: 50.0 for k in values}
    return {k: 100.0 * (v - lo) / (hi - lo) for k, v in values.items()}


def zscores(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    vals = list(values.values())
    mu = statistics.fmean(vals)
    sd = statistics.pstdev(vals) or 1.0
    return {k: (v - mu) / sd for k, v in values.items()}
