"""Command-line interface for cfb-ranker.

    python -m cfb_ranker fetch                 # pull EA CFB 27 data (cached)
    python -m cfb_ranker rank [--top 25]       # compute & print rankings, write CSV
    python -m cfb_ranker calibrate --reference data/input/sagarin.csv
    python -m cfb_ranker export                 # regenerate rankings.html report
    python -m cfb_ranker weights               # show current weights
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from . import calibrate as calib
from . import cfbd, ea_cfb27, factors, fcs, grade as grademod, loaders, ranker
from .model import Team

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
INPUT = DATA / "input"
OUTPUT = DATA / "output"
CONFIG = ROOT / "config" / "weights.json"


def _load_weights() -> dict:
    if CONFIG.exists():
        return {**ranker.DEFAULT_WEIGHTS, **json.loads(CONFIG.read_text(encoding="utf-8"))}
    return dict(ranker.DEFAULT_WEIGHTS)


def _save_weights(w: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(w, indent=2), encoding="utf-8")


def _load_teams(refresh: bool) -> list:
    return ea_cfb27.load_teams(RAW, refresh=refresh)


def _enrich_with_cfbd(teams: list, weights: dict, year: int) -> dict:
    """Add recruiting/portal/returning data. Adds FCS teams with a synthesized
    talent prior. Returns the returning-production dict (empty if no CFBD key)."""
    try:
        divisions = cfbd.load_team_divisions(year, RAW)
    except cfbd.CFBDUnavailable as e:
        print(f"[cfbd] skipped ({e}) -- FBS only, no recruiting/portal/FCS.")
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"[cfbd] error loading teams ({e}); continuing FBS-only.")
        return {}

    recruiting = cfbd.load_recruiting(year, RAW)
    returning = cfbd.load_returning(year, RAW)
    portal = cfbd.load_portal_net(year, RAW)

    # 1) small recruiting prior blended into FBS roster talent. Roster talent is
    #    normally derived inside rank(); compute it here first so the prior has
    #    something to blend with.
    for t in teams:
        if t.division == "FBS" and t.cfb27_team_overall is None and t.players:
            t.cfb27_team_overall = factors.roster_team_overall(t, weights.get("qb_weight", 0.0))
    n = fcs.apply_fbs_recruiting_prior(teams, recruiting, weights.get("fbs_recruiting_prior", 0.10))
    print(f"[cfbd] recruiting prior blended into {n} FBS teams")

    # 2) create FCS teams and synthesize their talent (experience comes from the
    #    blended returning-production pass in ranker.compute_experience)
    have = {t.name for t in teams}
    fcs_names = [s for s, meta in divisions.items()
                 if meta.get("classification") == "fcs" and s not in have]
    talent = fcs.synthesize_fcs_talent(fcs_names, recruiting, returning, portal, weights)
    for name in fcs_names:
        t = Team(name=name, conference=divisions[name].get("conference", "FCS"), division="FCS")
        t.cfb27_team_overall = talent[name]
        teams.append(t)
    print(f"[cfbd] added {len(fcs_names)} FCS teams (talent from recruiting+returning+portal)")

    # FCS Top-25 poll anchor (fixes the weak recruiting/portal synthesis)
    poll_path = INPUT / "fcs_poll.csv"
    if poll_path.exists():
        names_norm = {loaders._norm(t.name): t.name for t in teams if t.division == "FCS"}
        _POLL_ALIAS = {"southeastern louisiana": "SE Louisiana"}
        poll_ranks = {}
        for r in loaders._read_csv(poll_path):
            nm, rk = (r.get("team") or "").strip(), r.get("poll_rank")
            if not nm or not rk:
                continue
            key = loaders._norm(nm)
            resolved = names_norm.get(key) or _POLL_ALIAS.get(key)
            if resolved:
                poll_ranks[resolved] = int(rk)
        anchored = fcs.apply_fcs_poll(teams, poll_ranks)
        print(f"[input] FCS Top-25 poll anchored {anchored}/25 teams")
    return returning


def _compute_nil_baseline(teams: list) -> None:
    """Baseline value per team for the 'vs conference' comparison only.

    Uses the On3 figure where we have one; for other P4 teams, falls back to the
    model estimates in nil_model.csv, rescaled onto the On3 level by the median
    On3/model ratio so the conference average is apples-to-apples. Display still
    shows only real On3 numbers; this baseline is never shown as a team's value.
    """
    import statistics
    model_path = INPUT / "nil_model.csv"
    model = loaders.read_nil_dict(model_path) if model_path.exists() else {}
    on3 = {t.name: t.nil_value for t in teams if t.nil_value is not None}
    ratios = [on3[k] / model[k] for k in on3 if k in model and model[k]]
    scale = statistics.median(ratios) if ratios else 1.0
    for t in teams:
        if t.nil_value is not None:
            t.nil_baseline = t.nil_value
        elif t.name in model:
            t.nil_baseline = round(model[t.name] * scale, 1)
    if model:
        print(f"[input] NIL conference baseline from nil_model.csv (scale x{scale:.2f})")


def _apply_optional_inputs(teams: list):
    """Wire in any CSVs the user dropped into data/input/ plus CFBD enrichment.
    Returns (games, returning) — either may be None/empty."""
    pff = INPUT / "pff.csv"
    if pff.exists():
        n = loaders.apply_pff(teams, pff)
        print(f"[input] PFF grades applied to {n} teams")
    snaps = INPUT / "snaps.csv"
    if snaps.exists():
        n = loaders.apply_snaps(teams, snaps)
        print(f"[input] snaps/starts applied to {n} players")
    ov = INPUT / "cfb27_team.csv"
    if ov.exists():
        n = loaders.apply_cfb27_override(teams, ov)
        print(f"[input] official CFB27 team overalls applied to {n} teams")
    nil = INPUT / "nil.csv"
    if nil.exists():
        n = loaders.apply_nil(teams, nil)
        print(f"[input] NIL valuations applied to {n} teams (display-only)")
        _compute_nil_baseline(teams)

    # CFBD enrichment: recruiting prior for FBS + full talent for FCS teams
    returning = _enrich_with_cfbd(teams, _load_weights(), year=2026)

    ea_names = [t.name for t in teams]
    games = None
    sched = INPUT / "schedule.csv"
    if sched.exists():
        games = loaders.match_games_to_ea(loaders.load_schedule_csv(sched), ea_names)
        print(f"[input] schedule.csv: {len(games)} games")
    else:
        # try CFBD if a key is available, else fall back to ESPN's public API (no key)
        try:
            games = loaders.match_games_to_ea(loaders.load_schedule_cfbd(2026, RAW), ea_names)
        except Exception:  # noqa: BLE001
            try:
                raw = loaders.load_schedule_espn(2026, RAW)
                games = loaders.match_games_to_ea(raw, ea_names)
                print(f"[input] ESPN schedule (2026): {len(games)} games -> SOS active")
            except Exception as e:  # noqa: BLE001
                print(f"[input] no schedule loaded ({e}). SOS weight will be redistributed.")
    return games, returning


def _print_table(teams: list, top: int) -> None:
    cols = ["#", "Team", "Conf", "Rating", "Base", "SOS", "Talent", "Exp", "PFF"]
    widths = [4, 22, 10, 8, 6, 7, 7, 6, 6]
    line = "  ".join(c.ljust(w) for c, w in zip(cols, widths))
    print("\n" + line)
    print("-" * len(line))
    for t in teams[:top]:
        row = [
            str(t.rank),
            t.name[:22],
            t.conference[:10],
            f"{t.rating:.2f}",
            f"{t.base_rating:.0f}",
            f"{t.sos:+.2f}",
            f"{t.scores.get('cfb27_talent', float('nan')):.0f}",
            f"{t.scores.get('experience', float('nan')):.0f}",
            (f"{t.scores.get('pff'):.0f}" if 'pff' in t.scores else "-"),
        ]
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))


def _write_csv(teams: list) -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "rankings.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["rank", "team", "conference", "division", "rating",
                     "base_rating", "sos", "talent_score", "experience_score",
                     "pff_score", "nil_value", "nil_baseline"])
        for t in teams:
            wr.writerow([t.rank, t.name, t.conference, t.division, t.rating,
                         t.base_rating, t.sos,
                         t.scores.get("cfb27_talent", ""),
                         t.scores.get("experience", ""),
                         t.scores.get("pff", ""),
                         t.nil_value if t.nil_value is not None else "",
                         t.nil_baseline if t.nil_baseline is not None else ""])
    return path


def cmd_fetch(args):
    teams = _load_teams(refresh=args.refresh)
    tot_players = sum(len(t.players) for t in teams)
    print(f"[fetch] {len(teams)} teams, {tot_players} players cached in {RAW/'ea'}")


def _load_results(names: list[str]) -> list[dict]:
    """Completed games from ESPN, team names resolved to model names."""
    try:
        results = loaders.load_results_espn(2026, RAW)
    except Exception as e:  # noqa: BLE001
        print(f"[results] none loaded ({e})")
        return []
    pairs = [(g["home"], g["away"]) for g in results]
    for g, (h, a) in zip(results, loaders.match_games_to_ea(pairs, names)):
        g["home"], g["away"] = h, a
    ov = INPUT / "result_overrides.csv"
    if ov.exists():
        loaders.apply_result_overrides(results, ov)
    return results


def cmd_rank(args):
    teams = _load_teams(refresh=args.refresh)
    games, returning = _apply_optional_inputs(teams)
    weights = _load_weights()
    results = None if args.no_results else _load_results([t.name for t in teams])
    ranked = ranker.rank(teams, weights, games=games, returning=returning,
                         week=args.week, results=results)
    _print_table(ranked, args.top)
    path = _write_csv(ranked)
    # remember the week so `export` can show the weights in effect for it
    (OUTPUT / "rank_meta.json").write_text(json.dumps({"week": args.week}), encoding="utf-8")
    print(f"\n[rank] full table -> {path}")


def cmd_calibrate(args):
    ref_path = Path(args.reference)
    if not ref_path.exists():
        sys.exit(f"reference file not found: {ref_path}")
    reference = {}
    with open(ref_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                reference[r["team"].strip()] = float(r["rating"])
            except (KeyError, ValueError):
                continue
    teams = _load_teams(refresh=False)
    games, returning = _apply_optional_inputs(teams)
    weights = _load_weights()
    best, score = calib.calibrate(teams, weights, reference, games=games, returning=returning)
    merged = {**weights, **best}
    _save_weights(merged)
    print(f"[calibrate] saved tuned weights -> {CONFIG}  (Spearman={score:.4f})")


def cmd_export(args):
    """Regenerate the interactive HTML report from the latest rankings.csv."""
    rankings = OUTPUT / "rankings.csv"
    if not rankings.exists():
        sys.exit("No rankings yet — run `python -m cfb_ranker rank` first.")
    template = (ROOT / "cfb_ranker" / "report_template.html").read_text(encoding="utf-8")
    csv_text = rankings.read_text(encoding="utf-8").strip()
    w = {**ranker.DEFAULT_WEIGHTS, **_load_weights()}
    meta = OUTPUT / "rank_meta.json"
    week = json.loads(meta.read_text(encoding="utf-8"))["week"] if meta.exists() else 0
    shift = ranker.talent_shift(week, w)
    w["cfb27_talent"] -= shift
    w["sos"] += shift
    html = (template.replace("__RANKINGS_CSV__", csv_text)
            .replace("__W_TALENT__", f'{w["cfb27_talent"]:.2f}')
            .replace("__W_SOS__", f'{w["sos"]:.2f}')
            .replace("__W_EXP__", f'{w["experience"]:.2f}'))
    out = ROOT / "rankings.html"
    out.write_text(html, encoding="utf-8")
    n = csv_text.count("\n")  # rows minus header
    print(f"[export] wrote {out} ({n} teams)")


def cmd_grade(args):
    """Grade the model's ratings against actual completed-game outcomes."""
    teams = _load_teams(refresh=args.refresh)
    games, returning = _apply_optional_inputs(teams)
    weights = _load_weights()
    ranked = ranker.rank(teams, weights, games=games, returning=returning, week=args.week)
    ratings = {t.name: t.rating for t in ranked}
    names = list(ratings.keys())

    results = loaders.load_results_espn(2026, RAW)
    if not results:
        sys.exit("No completed games found yet.")
    pairs = [(g["home"], g["away"]) for g in results]
    resolved = loaders.match_games_to_ea(pairs, names)
    for g, (h, a) in zip(results, resolved):
        g["home"], g["away"] = h, a

    rep = grademod.grade(ratings, results)
    _print_grade(rep, args.week)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / "grade_report.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(["date", "away", "home", "score", "neutral", "pred_winner",
                     "pred_margin", "actual_margin", "abs_error", "correct"])
        for r in rep["rows"]:
            wr.writerow([r["date"], r["away"], r["home"], r["score"], r["neutral"],
                         r["pred_winner"], r["pred_margin"], r["actual_margin"],
                         r["abs_error"], r["correct"]])
    print(f"\n[grade] per-game detail -> {path}")


def _print_grade(rep, week):
    rows = sorted(rep["rows"], key=lambda r: (not r["correct"], -r["abs_error"]))
    print(f"\n=== PREDICTION GRADE (ratings @ week {week}) ===")
    print(f"Straight-up: {rep['correct']}/{rep['graded']} "
          f"({rep['su_pct']:.0f}%)   |   Margin MAE: {rep['mae']:.1f} pts\n")
    cols = ["", "Matchup", "Score", "Pred", "Pred m", "Act m", "Err"]
    w = [2, 30, 9, 14, 7, 6, 5]
    print("  ".join(c.ljust(x) for c, x in zip(cols, w)))
    print("-" * 78)
    for r in rows:
        mark = "OK " if r["correct"] else "MISS"
        match = f"{r['away']} @ {r['home']}"[:30]
        line = [mark, match, r["score"],
                r["pred_winner"][:14], f"{r['pred_margin']:+.1f}",
                f"{r['actual_margin']:+d}", f"{r['abs_error']:.0f}"]
        print("  ".join(str(c).ljust(x) for c, x in zip(line, w)))
    if rep["skipped"]:
        print("\nSkipped (team not in model):")
        for s in rep["skipped"]:
            print(f"  {s['away']} @ {s['home']}  (missing: {s['missing']})")


def cmd_weights(args):
    print(json.dumps(_load_weights(), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="cfb_ranker", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch", help="download EA CFB 27 ratings (cached)")
    pf.add_argument("--refresh", action="store_true", help="ignore cache, re-download")
    pf.set_defaults(func=cmd_fetch)

    pr = sub.add_parser("rank", help="compute rankings")
    pr.add_argument("--top", type=int, default=25)
    pr.add_argument("--refresh", action="store_true")
    pr.add_argument("--week", type=int, default=0,
                    help="season week (0=preseason); SOS ramps to full weight by week 4")
    pr.add_argument("--no-results", action="store_true",
                    help="ignore completed-game outcomes (pure projection)")
    pr.set_defaults(func=cmd_rank)

    pc = sub.add_parser("calibrate", help="auto-tune weights vs a reference")
    pc.add_argument("--reference", required=True, help="CSV with columns team,rating")
    pc.set_defaults(func=cmd_calibrate)

    pe = sub.add_parser("export", help="regenerate rankings.html from rankings.csv")
    pe.set_defaults(func=cmd_export)

    pg = sub.add_parser("grade", help="grade predictions vs actual completed games")
    pg.add_argument("--week", type=int, default=0,
                    help="week of the ratings being graded (0 = preseason prediction)")
    pg.add_argument("--refresh", action="store_true")
    pg.set_defaults(func=cmd_grade)

    pw = sub.add_parser("weights", help="print current weights")
    pw.set_defaults(func=cmd_weights)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
