# cfb-ranker

A transparent, continuously-tunable ranking model for **Division I college football**,
built on a Sagarin-inspired formula. Pulls live roster ratings from **EA SPORTS
College Football 27**, folds in **strength of schedule (common opponents, 3 levels
deep)**, roster **experience/snaps**, and an optional **PFF** grade, then writes a
ranked CSV. Standard-library Python only — no pip installs.

```bash
python -m cfb_ranker fetch      # download EA CFB 27 rosters (cached)
python -m cfb_ranker rank       # compute rankings -> table + data/output/rankings.csv
python -m cfb_ranker rank --top 40
python -m cfb_ranker calibrate --reference data/input/reference_coaches.csv
python -m cfb_ranker export      # regenerate the interactive rankings.html report
python -m cfb_ranker weights    # show current weights
```

Typical refresh loop: `rank --refresh` (pull latest EA ratings + recompute) → `export`
(rebuild `rankings.html`).

**Data is not included in this repo.** EA ratings, CFBD responses, and ESPN results are
downloaded on first run into `data/raw/` (git-ignored). Recruiting, portal, returning
production, and FCS coverage need a free CollegeFootballData key in the `CFBD_API_KEY`
environment variable; everything else works without one.

---

## 1. Conceptual base: what we borrowed from Sagarin

Jeff Sagarin's ratings are the reference point for the whole design:

- **Points scale.** Ratings read like a point spread: a team rated 92 vs one rated 84
  is ~8 points better on a neutral field. Our output is centered at 75 with an 8-point
  spread so numbers read the same way.
- **Two engines.** Sagarin runs `ELO-CHESS` (wins/losses + schedule only — what the BCS
  used) and `PREDICTOR` (uses margin of victory). The public number blends them.
- **The key idea — simultaneity.** A team's rating depends on its opponents' ratings,
  which depend on *their* opponents' ratings … forever. Sagarin solves this as one big
  simultaneous system that **converges to a fixed point** by iteration.

**Your "common opponents, 3 levels deep" is exactly that recursion, truncated at depth 3.**
We compute it, fold it back into the ratings, and iterate to convergence — a finite-window
Sagarin. See `cfb_ranker/sos.py`.

---

## 2. The formula

Each team gets four normalized (0–100) factor scores, combined by adjustable weights:

| Factor | Weight | Source | Notes |
|---|---|---|---|
| `cfb27_talent` | 0.45 | EA CFB 27 rosters | Team overall derived from the roster: best-11 offense + best-11 defense + depth. This **is** the video-game rating. |
| `sos` | 0.25 | Schedule graph | Common opponents 3 deep, Sagarin-style iterative. |
| `experience` | 0.15 | Roster class years / snaps | Veteran presence across the two-deep. Portal-aware (see below). |
| `pff` | 0.15 | `pff.csv` (you add) | Dropped automatically until you supply it. |

**Pipeline** (`cfb_ranker/ranker.py`):

1. Build a base rating from talent + experience + PFF (whatever is present), convert to a
   Sagarin-like points scale via z-score.
2. Apply the FCS baseline offset (see §5).
3. Iterate: compute SOS from current ratings → blend into ratings → repeat
   `sos_iterations` times until stable.
4. Rank, print, write `data/output/rankings.csv` (with the per-factor scores, for
   explainability).

**Weights auto-normalize over the factors that actually have data**, so the model always
runs — it just gets sharper as you add inputs.

### Experience — real playing time, blended

The most experienced team is the one whose players have logged the most snaps and starts.
`experience` is a **blend of real playing-time signals**, each normalized across all teams
and combined by weight (a team missing one signal just drops it):

| Signal | Weight | Source |
|---|---|---|
| Career **snaps** over the two-deep | `exp_snaps_w` 0.35 | `snaps.csv` (from PFF / a stats export) |
| Career **starts** over the two-deep | `exp_starts_w` 0.20 | `snaps.csv` |
| **Returning production** (% of last year's snaps back) | `exp_returning_w` 0.30 | CFBD (free key) |
| **Class-year** veteran index (fallback) | `exp_class_w` 0.15 | EA `schoolYear` |

This is portal-aware: a transfer senior with 700 career snaps counts as experienced wherever
those snaps happened. With no CSV and no CFBD key it runs on the class-year index alone, so it
always works. **Note on sources:** the NCAA's site does not publish per-player snap counts
(those are PFF); it offers games played + team plays. So snaps/starts come from `snaps.csv`
(`team,player,snaps,starts`), while returning production comes free from CFBD.

---

## 3. Data sources (and how to extend them)

| Data | Status | How |
|---|---|---|
| EA CFB 27 rosters/overalls | **Live, automatic** | Next.js data endpoint, one request per team, cached to `data/raw/ea/`. |
| Schedule (for SOS) | **Live, automatic** | ESPN public API (no key). CFBD API used first if `CFBD_API_KEY` is set. Or drop `data/input/schedule.csv`. |
| Recruiting / transfer portal / returning production | **Live with CFBD key** | College Football Data API. Powers the FBS recruiting prior **and** the FCS talent estimate (§5). Set `CFBD_API_KEY`. |
| PFF grades | You add | `data/input/pff.csv` — `team,pff_grade` (0–100). |
| Snap counts | You add | `data/input/snaps.csv` — `team,player,snaps`. |
| Official CFB27 team overall | You add (optional) | `data/input/cfb27_team.csv` — `team,overall` — overrides the roster-derived number. |

`*.example.csv` files in `data/input/` show each format. Team names must match EA labels
(e.g. `Ohio State`, `Ole Miss`). ESPN names are auto-mapped; add exceptions in
`_EA_ALIASES` (`cfb_ranker/loaders.py`) if you find a mismatch.

---

## 4. Calibrating the weights ("set and continuously adjusted")

`calibrate` hill-climbs the weights to maximize **Spearman rank agreement** with a
reference file (`team,rating`), and saves the result to `config/weights.json`.

```bash
python -m cfb_ranker calibrate --reference data/input/reference_coaches.csv
```

- **Preseason:** calibrate against a poll (or Sagarin's preseason) to anchor talent.
  Expect SOS weight → ~0, because polls are pure roster strength before anyone plays.
- **In season:** switch the reference to **actual results / Sagarin PREDICTOR** and SOS
  will earn real weight. This is the intended continuous loop: re-fetch, re-calibrate weekly.

It only tunes factors that have data, and reports the before/after correlation.

---

## 5. FBS vs FCS — how FCS teams get rated

EA CFB 27 ships **FBS only**, so FCS teams have no roster overalls. With a **CFBD key**, the
app estimates FCS talent from a preseason prior (`cfb_ranker/fcs.py`):

```
FCS talent = recruiting  +  returning production (last year)
             +  added production (portal in)  −  losses (portal out)
```

Each signal is normalized among FCS teams, blended by weight
(`fcs_recruiting_w` / `fcs_returning_w` / `fcs_portal_w`), and mapped onto an EA-comparable
overall scale (`fcs_talent_min`..`fcs_talent_max`) so FCS and FBS **rank on one list**.
Returning production also feeds the FCS team's `experience` score. This prior carries the
FCS teams **until enough games are played for SOS to take over** — exactly the intended
handoff. FBS teams separately get a small recruiting *prior* (`fbs_recruiting_prior`, default
0.10) blended into their roster talent.

FCS teams for which CFBD has no data fall back to a flat `fcs_baseline_offset` (−17 pts).
Validate the whole path offline (no key needed) with `python tests/test_fcs_offline.py`.

---

## 6. Suggested next factors (roadmap)

Ranked by expected bang-for-buck:

1. ✅ **Recruiting composite + transfer portal + returning production** — done via CFBD;
   powers the FCS talent estimate, the FBS recruiting prior (§5), and the experience blend.
2. ✅ **Experience from snaps + starts + returning production** — done; supply `snaps.csv`
   for the per-player snap/start component (returning production is automatic with a CFBD key).
3. **Margin-of-victory (capped) once games are played** — Sagarin's PREDICTOR engine; cap at
   ~24–28 to avoid rewarding blowouts.
5. **Recency weighting** — weight recent games more (Sagarin's RECENT).
6. **Returning coaching / scheme continuity** and **home/away + travel miles**.
7. **Blend, don't replace:** keep an ELO-CHESS-style results-only rating alongside the
   talent model and average them, like Sagarin's "golden mean."

---

## 7. Layout

```
cfb_ranker/
  ea_cfb27.py   live EA CFB 27 loader (buildId discovery + per-team rosters)
  factors.py    roster->team overall, experience index, normalization
  sos.py        schedule graph + common-opponents-3-deep iterative SOS
  cfbd.py       CFBD API: team divisions, recruiting, portal, returning production
  fcs.py        FCS talent synthesis + FBS recruiting prior
  loaders.py    schedule (ESPN/CFBD/CSV), PFF, snaps, official-overall CSVs
  ranker.py     factor combination + Sagarin-style convergence
  calibrate.py  Spearman hill-climb weight tuning
  cli.py        fetch / rank / calibrate / weights
tests/test_fcs_offline.py   offline validation of the FCS path (no key needed)
config/weights.json   editable weights
data/raw/             cached downloads      data/input/  your CSVs      data/output/  rankings.csv
```
