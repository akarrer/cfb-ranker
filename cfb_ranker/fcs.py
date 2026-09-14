"""Synthesize talent for teams EA doesn't cover (FCS) and a recruiting prior for FBS.

FCS talent prior (until SOS takes over once games are played):
    index = w_rec * recruiting + w_ret * returning_production + w_port * portal_net
    talent_overall = map(index) onto an EA-comparable scale [fcs_min .. fcs_max]

Each signal is min-max normalized *within the group being scored* and any missing
signal is dropped with its weight redistributed, so a team with only recruiting
data is still placed sensibly.
"""
from __future__ import annotations

from typing import Optional

from .model import Team


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-9:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _blend_index(
    names: list[str],
    signals: dict[str, dict[str, float]],
    weights: dict[str, float],
) -> dict[str, float]:
    """Weighted blend of several 0..1 signals; missing signals drop out per team."""
    norm = {sig: _minmax(vals) for sig, vals in signals.items()}
    out = {}
    for name in names:
        num = den = 0.0
        for sig, w in weights.items():
            v = norm.get(sig, {}).get(name)
            if v is not None:
                num += w * v
                den += w
        out[name] = (num / den) if den else 0.5
    return out


def synthesize_fcs_talent(
    fcs_names: list[str],
    recruiting: dict[str, float],
    returning: dict[str, float],
    portal_net: dict[str, float],
    params: dict,
) -> dict[str, float]:
    """Return {team: talent_overall} on an EA-comparable scale for FCS teams."""
    weights = {
        "recruiting": params.get("fcs_recruiting_w", 0.40),
        "returning": params.get("fcs_returning_w", 0.35),
        "portal": params.get("fcs_portal_w", 0.25),
    }
    signals = {"recruiting": recruiting, "returning": returning, "portal": portal_net}
    idx = _blend_index(fcs_names, signals, weights)

    lo = params.get("fcs_talent_min", 38.0)
    hi = params.get("fcs_talent_max", 70.0)
    return {name: lo + (hi - lo) * idx[name] for name in fcs_names}


def apply_fcs_poll(teams, poll_ranks: dict[str, int], top: float = 72.0,
                   bottom: float = 56.0, unranked_cap: float = 55.0) -> int:
    """Anchor FCS talent to the FCS Top-25 poll.

    Ranked teams get a talent value from `top` (rank 1) down to `bottom` (rank 25);
    unranked FCS teams keep their (weaker) synthesized talent, capped below the poll
    floor so a ranked team always outranks an unranked one. Returns count anchored.
    """
    n = 25
    ranked = 0
    for t in teams:
        if t.division != "FCS":
            continue
        r = poll_ranks.get(t.name)
        if r is not None:
            t.cfb27_team_overall = round(top - (top - bottom) * ((r - 1) / (n - 1)), 2)
            ranked += 1
        elif t.cfb27_team_overall is not None:
            t.cfb27_team_overall = min(t.cfb27_team_overall, unranked_cap)
    return ranked


def apply_fbs_recruiting_prior(
    teams: list[Team],
    recruiting: dict[str, float],
    prior_weight: float,
) -> int:
    """Blend a small recruiting prior into each FBS team's roster-derived overall.

    talent' = (1 - w) * roster_overall + w * (recruiting mapped onto roster scale)
    Returns the number of teams adjusted.
    """
    if prior_weight <= 0:
        return 0
    fbs = [t for t in teams if t.division == "FBS" and t.cfb27_team_overall is not None]
    if not fbs:
        return 0
    ov = {t.name: t.cfb27_team_overall for t in fbs}
    ov_lo, ov_hi = min(ov.values()), max(ov.values())

    rec = {t.name: recruiting[t.name] for t in fbs if t.name in recruiting}
    if len(rec) < 2:
        return 0
    rec_norm = _minmax(rec)  # 0..1 among FBS teams that have recruiting

    n = 0
    for t in fbs:
        if t.name in rec_norm:
            rec_on_scale = ov_lo + (ov_hi - ov_lo) * rec_norm[t.name]
            t.cfb27_team_overall = (1 - prior_weight) * t.cfb27_team_overall + prior_weight * rec_on_scale
            n += 1
    return n
