"""Auto-tune factor weights against a reference rating (e.g. Sagarin).

'Weights need to be set and continuously adjusted' -> this does the adjusting.
We hill-climb the factor weights to maximize rank agreement (Spearman) with a
trusted reference. Start with Sagarin as the reference to anchor the model, then
switch the reference to actual results (margin) once the season is underway.

reference.csv format:  team,rating
"""
from __future__ import annotations

import copy
from typing import Optional

from . import ranker
from .model import Team

TUNABLE = ["cfb27_talent", "experience", "pff", "sos"]


def spearman(a: dict[str, float], b: dict[str, float]) -> float:
    common = [k for k in a if k in b]
    if len(common) < 3:
        return 0.0

    def ranks(d):
        order = sorted(common, key=lambda k: d[k])
        return {k: i for i, k in enumerate(order)}

    ra, rb = ranks(a), ranks(b)
    n = len(common)
    d2 = sum((ra[k] - rb[k]) ** 2 for k in common)
    return 1 - (6 * d2) / (n * (n * n - 1))


def _score(teams, weights, games, reference, returning) -> float:
    graded = ranker.rank(copy.deepcopy(teams), weights, games=games,
                         returning=returning, log=lambda *a, **k: None)
    model = {t.name: t.rating for t in graded}
    return spearman(model, reference)


def calibrate(
    teams: list[Team],
    weights: dict,
    reference: dict[str, float],
    games: Optional[list] = None,
    returning: Optional[dict] = None,
    steps: int = 40,
    log=print,
) -> tuple[dict, float]:
    """Coordinate hill-climb on the tunable weights. Returns (best_weights, score)."""
    w = {**ranker.DEFAULT_WEIGHTS, **(weights or {})}

    # only tune factors that actually have data (probe one ranking to see which
    # factor scores were produced); avoids assigning weight to empty factors.
    probe = ranker.rank(copy.deepcopy(teams), w, games=games,
                        returning=returning, log=lambda *a, **k: None)
    present = set()
    for t in probe:
        present.update(t.scores.keys())
    tunable = [k for k in TUNABLE if k in present]
    if games:
        tunable.append("sos")
    log(f"[calibrate] tuning: {', '.join(tunable)}")

    best = _score(teams, w, games, reference, returning)
    log(f"[calibrate] start Spearman={best:.4f}")

    step = 0.08
    for it in range(steps):
        improved = False
        for key in tunable:
            for delta in (step, -step):
                cand = dict(w)
                cand[key] = max(0.0, cand[key] + delta)
                sc = _score(teams, cand, games, reference, returning)
                if sc > best + 1e-6:
                    w, best = cand, sc
                    improved = True
        if not improved:
            step *= 0.5
            if step < 0.005:
                break
    # renormalize the tuned factor weights to sum to 1 for readability
    tot = sum(w[k] for k in tunable) or 1.0
    for k in tunable:
        w[k] = round(w[k] / tot, 4)
    log(f"[calibrate] done  Spearman={best:.4f}  weights=" +
        ", ".join(f"{k}={w[k]}" for k in tunable))
    return w, best
