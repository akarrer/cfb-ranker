"""Strength of schedule: common opponents walked up to 3 levels deep.

Conceptual base (Sagarin): a team's rating depends on its opponents' ratings,
which depend on THEIR opponents' ratings, recursively -- an infinite regress
that Sagarin solves as a simultaneous system converging to a fixed point.

'Common opponents 3 levels deep' is that recursion truncated at depth 3:
  level 1 = teams you played
  level 2 = teams your opponents played (opponents-of-opponents)
  level 3 = one hop further
Each deeper level is discounted (decay). We then fold SOS back into the ratings
and iterate a few times, mirroring the Sagarin convergence on a finite window.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable


class ScheduleGraph:
    """Undirected multigraph of games. Nodes = team names, edges = games played/scheduled."""

    def __init__(self) -> None:
        self.adj: dict[str, list[str]] = defaultdict(list)

    def add_game(self, a: str, b: str) -> None:
        if not a or not b or a == b:
            return
        self.adj[a].append(b)
        self.adj[b].append(a)

    @classmethod
    def from_games(cls, games: Iterable[tuple[str, str]]) -> "ScheduleGraph":
        g = cls()
        for a, b in games:
            g.add_game(a, b)
        return g

    def teams(self) -> set[str]:
        return set(self.adj.keys())


def schedule_strength(
    graph: ScheduleGraph,
    rating: dict[str, float],
    max_depth: int = 3,
    level_decay: float = 0.5,
) -> dict[str, float]:
    """For each team, a decayed, distance-weighted average of the ratings of all
    teams reachable within `max_depth` hops on the schedule graph.

    Multiplicity matters: playing (or being connected to) a strong team through
    many shared paths raises SOS more -- this is the 'common opponents' signal.
    """
    out: dict[str, float] = {}
    all_teams = graph.teams() | set(rating.keys())
    mean_rating = (sum(rating.values()) / len(rating)) if rating else 0.0

    for team in all_teams:
        # BFS accumulating a weight per reached node = decay^level * path_count
        weight_at: dict[str, float] = defaultdict(float)
        frontier = {team: 1.0}
        visited_mult: dict[str, float] = defaultdict(float)
        for level in range(1, max_depth + 1):
            nxt: dict[str, float] = defaultdict(float)
            decay = level_decay ** (level - 1)
            for node, mult in frontier.items():
                for nb in graph.adj.get(node, ()):
                    if nb == team:
                        continue
                    nxt[nb] += mult
            for nb, mult in nxt.items():
                weight_at[nb] += decay * mult
            frontier = nxt
            if not frontier:
                break

        num = den = 0.0
        for other, w in weight_at.items():
            r = rating.get(other, mean_rating)
            num += w * r
            den += w
        out[team] = (num / den) if den else mean_rating

    return out
