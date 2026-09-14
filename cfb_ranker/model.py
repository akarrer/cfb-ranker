"""Core data structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# Class year -> playing-experience weight. Seniors/veterans anchor the roster;
# this is a coarse stand-in for snap counts until real snap data is supplied.
CLASS_WEIGHT = {
    "Freshman": 0.40,
    "Sophomore": 0.70,
    "Junior": 0.90,
    "Senior": 1.00,
}


@dataclass
class Player:
    name: str
    team: str
    position: str            # EA position id, e.g. "QB", "WR", "LT", "CB"
    position_type: str       # "offense" | "defense" | "specialteam"
    overall: int             # EA CFB 27 overall rating (0-99)
    school_year: str         # Freshman | Sophomore | Junior | Senior
    # Optional real playing-time data (from snaps.csv if available; else None).
    snaps: Optional[int] = None      # career snaps played
    starts: Optional[int] = None     # career games started

    @property
    def class_weight(self) -> float:
        return CLASS_WEIGHT.get(self.school_year, 0.6)


@dataclass
class Team:
    name: str
    conference: str = ""
    division: str = "FBS"          # "FBS" | "FCS"
    ea_team_id: Optional[int] = None
    players: list[Player] = field(default_factory=list)

    # --- raw factor inputs (populated by loaders) ---------------------------
    cfb27_team_overall: Optional[float] = None   # roster-derived or official override
    experience_raw: Optional[float] = None       # 0..1 blended veteran index (snaps/starts/returning/age)
    pff_grade: Optional[float] = None            # external, 0..100 (optional)
    nil_value: Optional[float] = None            # NIL/roster valuation ($M, On3); display-only, not in rating
    nil_baseline: Optional[float] = None         # scale-corrected value for conference-average comparison only

    # --- computed outputs ---------------------------------------------------
    sos: float = 0.0             # strength-of-schedule points adjustment
    base_rating: float = 0.0     # rating from talent+experience+pff (pre-SOS)
    rating: float = 0.0          # final points rating (Sagarin-like scale)
    rank: int = 0

    # normalized 0-100 factor scores, kept for the output table / explainability
    scores: dict = field(default_factory=dict)

    def add_player(self, p: Player) -> None:
        self.players.append(p)
