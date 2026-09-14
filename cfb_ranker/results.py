"""In-season update: nudge ratings toward actual game outcomes.

Two parts per completed game:
  * margin term  - compare the actual (capped) margin to what the ratings expected
                   and move both teams a fraction of the surprise;
  * win/loss term - an Elo-style result update on top of the margin, so losing
                   costs rating on its own: the loser pays in proportion to how
                   likely it was to win, and an upset loss hurts the most.
                   `loss_mult` > 1 makes the loser's penalty bigger than the
                   winner's gain.
"""
from __future__ import annotations

import math

HFA = 2.5


def _win_prob(expected_margin: float, sigma: float) -> float:
    """P(home wins) from an expected margin, assuming normal game-to-game noise."""
    return 0.5 * (1.0 + math.erf(expected_margin / (sigma * math.sqrt(2.0))))


def apply_results(
    ratings: dict[str, float],
    games: list[dict],
    k: float = 0.12,
    hfa: float = HFA,
    mov_cap: float = 28.0,
    wl_k: float = 0.0,
    sigma: float = 14.0,
    loss_mult: float = 1.0,
) -> tuple[dict[str, float], int]:
    """Return (adjusted_ratings, games_applied).

    games: dicts with resolved model names in home/away + home_score/away_score/neutral.
    """
    adj = dict(ratings)
    applied = 0
    for g in games:
        h, a = g.get("home"), g.get("away")
        if h not in adj or a not in adj:
            continue
        edge = 0.0 if g.get("neutral") else hfa
        raw_expected = adj[h] - adj[a] + edge
        margin = g["home_score"] - g["away_score"]

        # margin term. Cap BOTH sides: capping only the actual margin made a
        # 70-point win by a 46-point favorite read as an 18-point underperformance.
        capped_margin = max(-mov_cap, min(mov_cap, margin))
        capped_expected = max(-mov_cap, min(mov_cap, raw_expected))
        delta = k * (capped_margin - capped_expected) / 2.0
        dh, da = delta, -delta

        # win/loss term
        if wl_k and margin != 0:
            p_home = _win_prob(raw_expected, sigma)
            home_won = margin > 0
            wl = wl_k * ((1.0 if home_won else 0.0) - p_home)
            wh, wa = wl, -wl
            if home_won:
                wa *= loss_mult
            else:
                wh *= loss_mult
            dh += wh
            da += wa

        adj[h] += dh
        adj[a] += da
        applied += 1
    return adj, applied
