"""Grade the model's ratings against actual game outcomes.

For each completed game we predict:
    margin(home) = rating[home] - rating[away] + home_field_advantage (0 if neutral)
    predicted winner = home if margin > 0 else away
and compare to the final score. Reports straight-up accuracy and margin error.
"""
from __future__ import annotations

HFA = 2.5  # home-field advantage in rating points


def grade(ratings: dict[str, float], games: list[dict], hfa: float = HFA) -> dict:
    """games: dicts with resolved model names in home/away plus scores + neutral."""
    rows, skipped = [], []
    for g in games:
        rh, ra = ratings.get(g["home"]), ratings.get(g["away"])
        if rh is None or ra is None:
            skipped.append({**g, "missing": g["home"] if rh is None else g["away"]})
            continue
        edge = hfa if not g.get("neutral") else 0.0
        pred = rh - ra + edge                    # predicted home margin
        actual = g["home_score"] - g["away_score"]
        su = (pred > 0) == (actual > 0)
        pred_winner = g["home"] if pred > 0 else g["away"]
        act_winner = g["home"] if actual > 0 else g["away"]
        rows.append({
            "date": g.get("date", ""),
            "away": g["away"], "home": g["home"],
            "score": f"{g['away_score']}-{g['home_score']}",
            "neutral": g.get("neutral", False),
            "pred_margin": round(pred, 1),
            "actual_margin": actual,
            "abs_error": round(abs(pred - actual), 1),
            "pred_winner": pred_winner, "actual_winner": act_winner,
            "correct": su,
        })

    graded = len(rows)
    correct = sum(1 for r in rows if r["correct"])
    mae = (sum(r["abs_error"] for r in rows) / graded) if graded else 0.0
    return {
        "rows": rows, "skipped": skipped,
        "graded": graded, "correct": correct,
        "su_pct": (100.0 * correct / graded) if graded else 0.0,
        "mae": mae,
    }
