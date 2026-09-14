"""Offline validation of the FCS synthesis + recruiting prior + ranking path,
using synthetic CFBD-shaped data (no network / no API key needed).

Run:  python tests/test_fcs_offline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cfb_ranker import fcs, ranker
from cfb_ranker.model import Player, Team


def make_fbs(name, tier):
    """tier in 0..1 scales player overalls."""
    t = Team(name=name, conference="Test-FBS", division="FBS")
    base = 60 + int(30 * tier)
    for i in range(12):
        t.add_player(Player(name=f"{name} O{i}", team=name, position="WR",
                            position_type="offense", overall=base - i, school_year="Junior"))
    for i in range(12):
        t.add_player(Player(name=f"{name} D{i}", team=name, position="CB",
                            position_type="defense", overall=base - i, school_year="Senior"))
    return t


def main():
    # --- synthetic universe ---
    fbs = [make_fbs("Alpha State", 1.0), make_fbs("Beta Tech", 0.6), make_fbs("Gamma U", 0.2)]
    fcs_names = ["North Cardinal", "South Falcon", "East Bison"]

    recruiting = {"Alpha State": 290, "Beta Tech": 210, "Gamma U": 150,
                  "North Cardinal": 120, "South Falcon": 90, "East Bison": 70}
    returning = {"Alpha State": 0.75, "Beta Tech": 0.55, "Gamma U": 0.40,
                 "North Cardinal": 0.80, "South Falcon": 0.50, "East Bison": 0.30}
    portal = {"Alpha State": 6.0, "Beta Tech": 1.0, "Gamma U": -2.0,
              "North Cardinal": 2.5, "South Falcon": 0.0, "East Bison": -3.0}

    weights = dict(ranker.DEFAULT_WEIGHTS)

    # 1) FBS recruiting prior blends without breaking talent order
    from cfb_ranker import factors
    for t in fbs:
        t.cfb27_team_overall = factors.roster_team_overall(t)
    pre = {t.name: t.cfb27_team_overall for t in fbs}
    n = fcs.apply_fbs_recruiting_prior(fbs, recruiting, weights["fbs_recruiting_prior"])
    assert n == 3, f"expected 3 FBS blended, got {n}"
    assert fbs[0].cfb27_team_overall > fbs[1].cfb27_team_overall > fbs[2].cfb27_team_overall
    print(f"[ok] recruiting prior applied to {n} FBS teams; order preserved")
    print("     pre :", {k: round(v, 1) for k, v in pre.items()})
    print("     post:", {t.name: round(t.cfb27_team_overall, 1) for t in fbs})

    # 2) FCS talent synthesis on EA-comparable scale
    talent = fcs.synthesize_fcs_talent(fcs_names, recruiting, returning, portal, weights)
    print("[ok] FCS talent:", {k: round(v, 1) for k, v in talent.items()})
    assert all(weights["fcs_talent_min"] - 0.01 <= v <= weights["fcs_talent_max"] + 0.01
               for v in talent.values()), "FCS talent out of configured range"
    assert talent["North Cardinal"] > talent["East Bison"], "FCS ordering wrong"

    # 3) experience blend: give Beta Tech real snaps+starts so it out-experiences
    #    Alpha State despite Alpha being more talented/veteran by class year.
    for p in fbs[1].players:      # Beta Tech
        p.snaps, p.starts = 700, 12
    for p in fbs[0].players:      # Alpha State — few career snaps (young contributors)
        p.snaps, p.starts = 80, 1

    # 4) build full slate and rank together
    teams = list(fbs)
    for name in fcs_names:
        t = Team(name=name, conference="Test-FCS", division="FCS")
        t.cfb27_team_overall = talent[name]
        teams.append(t)

    # a little schedule so SOS has a graph (round-robin-ish)
    games = [("Alpha State", "Beta Tech"), ("Beta Tech", "Gamma U"),
             ("Gamma U", "North Cardinal"), ("North Cardinal", "South Falcon"),
             ("South Falcon", "East Bison"), ("Alpha State", "Gamma U")]

    ranked = ranker.rank(teams, weights, games=games, returning=returning,
                         log=lambda *a, **k: None)

    exp = {t.name: t.experience_raw for t in ranked if t.experience_raw is not None}
    assert exp["Beta Tech"] > exp["Alpha State"], \
        "snaps+starts should lift Beta Tech's experience above Alpha State"
    print("\n[ok] experience blend (snaps+starts+returning): "
          f"Beta Tech={exp['Beta Tech']:.2f} > Alpha State={exp['Alpha State']:.2f}")
    print("\n[ok] combined ranking:")
    for t in ranked:
        print(f"   {t.rank:>2}  {t.name:<15} {t.division:<4} rating={t.rating:6.2f} "
              f"talent={t.scores.get('cfb27_talent', float('nan')):5.1f} sos={t.sos:+.2f}")

    # sanity: all ratings finite, FBS top team beats FCS bottom team
    assert all(t.rating == t.rating for t in ranked), "NaN rating"
    a = next(t for t in ranked if t.name == "Alpha State")
    e = next(t for t in ranked if t.name == "East Bison")
    assert a.rating > e.rating, "top FBS should outrate bottom FCS"
    # most FCS should land in the lower half
    fcs_ranks = [t.rank for t in ranked if t.division == "FCS"]
    assert min(fcs_ranks) >= 2, "an FCS team implausibly ranked #1 in this synthetic set"
    print("\nALL ASSERTIONS PASSED")


if __name__ == "__main__":
    main()
