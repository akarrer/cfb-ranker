"""Combine factors into a final rating and run the iterative SOS convergence."""
from __future__ import annotations

import statistics
from typing import Optional

from . import factors
from .model import Team
from .sos import ScheduleGraph, schedule_strength

# Rating is presented on a Sagarin-like points scale: a difference of N points
# is roughly the predicted neutral-field margin. We center at 75 with a spread
# so the numbers read like Sagarin/Massey ratings.
# Points scale, applied WITHIN each division. Spread is in rating points so a
# rating difference ~ the predicted margin (Sagarin-style). Calibrated against
# Bovada/DraftKings weekend spreads: raw model gaps ran ~1.6x too small, so the
# per-division SPREAD is widened to make model lines match the market.
FBS_CENTER, FBS_SPREAD = 69.0, 12.0
# FCS center fit to 80 FBS-vs-FCS market lines (weeks 1-2): the model undersold the
# division gap by a median 11 pts, which also inflated FBS teams that routed FCS foes.
FCS_CENTER, FCS_SPREAD = 37.0, 9.5
# legacy single-scale fallback for any team of unknown division
SCALE_CENTER = 75.0
SCALE_SPREAD = 8.0


DEFAULT_WEIGHTS = {
    # relative importance of each factor (auto-normalized over factors present)
    "cfb27_talent": 0.45,   # roster overall from EA CFB 27 (the video game)
    "qb_weight": 0.12,      # extra weight on the starting QB inside talent (0 = none)
    "experience": 0.15,     # snaps + starts + returning production + age (blended)
    "pff": 0.15,            # external PFF team grade (optional)
    "sos": 0.25,            # strength of schedule, common opponents 3 deep
    # engine settings
    "sos_iterations": 6,
    "sos_max_depth": 3,
    "sos_level_decay": 0.5,
    # SOS is projected (not yet played) early in the year, so down-weight it until
    # enough games exist. Below sos_full_week the SOS weight is scaled by
    # sos_preseason_factor, then ramps linearly to full weight at sos_full_week.
    "sos_full_week": 4,
    "sos_preseason_factor": 0.25,
    # in-season results update (Elo-style nudge toward outcomes)
    "results_k": 0.12,
    "results_mov_cap": 28.0,
    # win/loss term on top of the margin update (rating points per unit of W - P(win))
    "results_wl_k": 0.0,
    "results_sigma": 14.0,
    "results_loss_mult": 1.0,   # >1 = the loser pays more than the winner gains
    # experience blend (snaps + starts + returning production + class-year fallback)
    "exp_snaps_w": 0.35,
    "exp_starts_w": 0.20,
    "exp_returning_w": 0.30,
    "exp_age_w": 0.15,      # average roster age/class within experience (academies excluded)
    # FCS teams sit ~17 pts below FBS (Sagarin-like gap) -- only used as a fallback
    # for FCS teams with NO recruiting/portal/returning data to synthesize talent.
    "fcs_baseline_offset": -17.0,
    # recruiting/portal (CFBD) settings
    "fbs_recruiting_prior": 0.10,   # small recruiting blend into FBS roster talent
    "fcs_talent_min": 38.0,         # EA-comparable scale FCS talent maps onto
    "fcs_talent_max": 70.0,
    "fcs_recruiting_w": 0.40,       # weights inside the FCS talent prior
    "fcs_returning_w": 0.35,
    "fcs_portal_w": 0.25,
}


def _prep_factor_inputs(teams: list[Team], returning: dict | None, weights: dict) -> None:
    for t in teams:
        # Only derive talent from a roster when the team actually has one (FBS).
        if t.cfb27_team_overall is None and t.players:
            t.cfb27_team_overall = factors.roster_team_overall(t, weights.get("qb_weight", 0.0))

    # Experience: blend real playing-time signals across all teams at once
    # (needs a cross-team pass to normalize snaps/starts/returning). This
    # overwrites any per-team experience placeholders (e.g. FCS returning).
    factors.compute_experience(teams, returning, weights)


def _normalized_factor_scores(teams: list[Team]) -> dict[str, dict[str, float]]:
    """Return {factor: {team: 0..100}} for each non-SOS factor that has data."""
    scores: dict[str, dict[str, float]] = {}

    talent = {t.name: t.cfb27_team_overall for t in teams if t.cfb27_team_overall is not None}
    if talent:
        scores["cfb27_talent"] = factors.to_0_100(talent)

    exp = {t.name: t.experience_raw for t in teams if t.experience_raw is not None}
    if exp:
        scores["experience"] = factors.to_0_100(exp)

    pff = {t.name: t.pff_grade for t in teams if t.pff_grade is not None}
    # only use PFF if a meaningful share of teams have it
    if len(pff) >= max(2, len(teams) // 2):
        scores["pff"] = factors.to_0_100(pff)

    return scores


def sos_weight_factor(week: int, w: dict) -> float:
    """0..1 multiplier on the SOS weight given the current week of the season.

    Held at sos_preseason_factor through the early weeks, then ramps linearly to
    1.0 at sos_full_week. Week 0 = preseason (SOS is pure projection)."""
    full = max(1, int(w.get("sos_full_week", 4)))
    pf = float(w.get("sos_preseason_factor", 0.25))
    if week >= full:
        return 1.0
    if week <= 0:
        return pf
    return pf + (1.0 - pf) * (week / full)


def rank(
    teams: list[Team],
    weights: dict,
    games: Optional[list[tuple[str, str]]] = None,
    returning: Optional[dict] = None,
    week: int = 0,
    results: Optional[list[dict]] = None,
    log=print,
) -> list[Team]:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    _prep_factor_inputs(teams, returning, w)

    factor_scores = _normalized_factor_scores(teams)
    have_sos = bool(games)

    # ---- weight normalization over the factors actually present -----------
    active = {k: w[k] for k in factor_scores}
    sos_factor = sos_weight_factor(week, w)
    if have_sos:
        active["sos"] = w["sos"] * sos_factor
    total = sum(active.values()) or 1.0
    active = {k: v / total for k, v in active.items()}
    log(f"[rank] week {week} (SOS x{sos_factor:.2f}) active weights: "
        + ", ".join(f"{k}={v:.2f}" for k, v in active.items()))

    names = [t.name for t in teams]

    # base rating (everything except SOS), on the 0..100 factor scale
    base100: dict[str, float] = {}
    for name in names:
        s = 0.0
        wsum = 0.0
        for f, sc in factor_scores.items():
            if name in sc:
                s += active[f] * sc[name]
                wsum += active[f]
        base100[name] = (s / wsum) if wsum else 50.0

    div_by_name = {t.name: t.division for t in teams}

    # convert base to points scale, z-scored WITHIN each division so a 30-spot rank
    # gap inside FBS maps to a realistic point spread (pooling FBS+FCS compressed it).
    def to_points(scores100: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        params = {"FBS": (FBS_CENTER, FBS_SPREAD), "FCS": (FCS_CENTER, FCS_SPREAD)}
        seen = set()
        for dv, (center, spread) in params.items():
            grp = {n: scores100[n] for n in scores100 if div_by_name.get(n) == dv}
            for n, v in factors.zscores(grp).items():
                out[n] = center + spread * v
                seen.add(n)
        for n in scores100:  # any other/unknown division
            if n not in seen:
                out[n] = SCALE_CENTER + SCALE_SPREAD * factors.zscores(scores100).get(n, 0.0)
        return out

    # ---- iterative SOS convergence (Sagarin-style fixed point) ------------
    # Work on the 0..100 scale. Each pass recomputes schedule strength from the
    # CURRENT combined ratings, re-normalizes it to 0..100 (so the scale is stable
    # no matter how many weak teams are in the pool), and blends it back onto the
    # FIXED base by sos_share. Because SOS is applied to `base100` (not added on
    # top of the previous total) it converges instead of compounding.
    graph = ScheduleGraph.from_games(games) if have_sos else None
    sos_share = active.get("sos", 0.0)
    combined = dict(base100)
    sos100: dict[str, float] = {n: 50.0 for n in names}

    # normalize SOS *within division* so schedule differences aren't washed out by
    # the FBS>>FCS talent gap — a team's schedule is judged against its peers'.
    div_of = {t.name: t.division for t in teams}
    divisions = set(div_of.values())

    if graph is not None and sos_share > 0:
        for _ in range(int(w["sos_iterations"])):
            ss = schedule_strength(
                graph, combined,
                max_depth=int(w["sos_max_depth"]),
                level_decay=float(w["sos_level_decay"]),
            )
            sos100 = {}
            for dv in divisions:
                grp = {n: ss.get(n, 0.0) for n in names if div_of[n] == dv}
                sos100.update(factors.to_0_100(grp))
            combined = {n: (1 - sos_share) * base100[n] + sos_share * sos100[n] for n in names}

    # ---- convert to points and finalize -----------------------------------
    base_points = to_points(base100)
    rating = to_points(combined)
    for t in teams:
        # baseline gap only for FCS teams we couldn't estimate any talent for
        if t.division == "FCS" and t.cfb27_team_overall is None:
            rating[t.name] += w["fcs_baseline_offset"]

    # ---- in-season update: nudge ratings toward actual outcomes ------------
    results_applied = 0
    if results:
        from . import results as resmod
        rating, results_applied = resmod.apply_results(
            rating, results,
            k=float(w.get("results_k", 0.12)),
            mov_cap=float(w.get("results_mov_cap", 28.0)),
            wl_k=float(w.get("results_wl_k", 0.0)),
            sigma=float(w.get("results_sigma", 14.0)),
            loss_mult=float(w.get("results_loss_mult", 1.0)),
        )
        log(f"[rank] applied {results_applied} game results (k={w.get('results_k', 0.12)})")

    for t in teams:
        n = t.name
        t.base_rating = round(base100[n], 2)
        t.sos = round(rating[n] - base_points[n], 2)  # points attributable to schedule
        t.rating = round(rating[n], 2)
        t.scores = {f: round(factor_scores[f][n], 1) for f in factor_scores if n in factor_scores[f]}
        if have_sos:
            t.scores["sos"] = round(sos100[n], 1)

    teams.sort(key=lambda t: t.rating, reverse=True)
    for i, t in enumerate(teams, 1):
        t.rank = i
    return teams
