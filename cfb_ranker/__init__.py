"""cfb_ranker — a transparent, adjustable ranking model for Division I (FBS + FCS) football.

Formula philosophy (see README) is Sagarin-inspired:
  * Every team's rating depends on its opponents' ratings, which depend on THEIR
    opponents' ratings ... a simultaneous system solved by iteration to a fixed point.
  * Strength of schedule is a truncated version of that recursion: common opponents
    walked up to 3 levels deep.
"""

__version__ = "0.1.0"
