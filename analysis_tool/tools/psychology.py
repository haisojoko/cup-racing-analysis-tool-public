"""behavioral_profile — the quantitative half of psychology grounding.

Computes results-only behavioral signals (consistency, discipline, bounce-back,
quali-vs-race, season-arc, attrition, trajectory) and returns them as labelled
numbers. It deliberately does NOT interpret: the model reads these alongside the
qualitative sources (read_sources) and frames mindset as inference. Nothing here
invents data the league doesn't capture (no lap times, gaps, weather, strategy).
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from typing import List, Optional

from ..config import Config
from ..data import read_only, resolve_driver


def _round(x: Optional[float], n: int = 2) -> Optional[float]:
    return round(x, n) if isinstance(x, (int, float)) else None


def behavioral_profile(cfg: Config, driver: str = "") -> str:
    canon = resolve_driver(cfg, driver)
    if not canon:
        return f"Unknown driver '{driver}'. Call list_drivers for valid names."

    with read_only(cfg) as con:
        races = con.execute(
            """
            SELECT s.season_num, s.season_sub, r.season_id, r.venue_order, r.race_num,
                   r.position, r.dns, r.penalty, r.is_pole
            FROM race_results r JOIN seasons s USING (season_id)
            WHERE r.driver = ?
            ORDER BY s.season_num, s.season_sub, r.venue_order, r.race_num
            """,
            [canon],
        ).fetchall()
        traj_rows = con.execute(
            """
            SELECT w.season_id, s.season_num, w.rank, w.weighted_score
            FROM weighted_scores w JOIN seasons s USING (season_id)
            WHERE w.driver = ?
            ORDER BY s.season_num, s.season_sub
            """,
            [canon],
        ).fetchall()

    if not races:
        return f"No race results found for {canon}."

    # columns: season_num, season_sub, season_id, venue_order, race_num, position, dns, penalty, is_pole
    total = len(races)
    finished = [r for r in races if not r[6] and r[5] is not None]
    positions = [r[5] for r in finished]
    penalties = sum(1 for r in races if r[7] and r[7] != 0)
    dns_count = sum(1 for r in races if r[6])
    poles = sum(1 for r in races if r[8])
    wins = sum(1 for r in finished if r[5] == 1)
    pole_wins = sum(1 for r in finished if r[5] == 1 and r[8])
    career_avg_pos = statistics.mean(positions) if positions else None

    # Per-season consistency + discipline.
    by_season = defaultdict(list)
    for r in races:
        by_season[r[2]].append(r)
    per_season = []
    for sid, rows in by_season.items():
        fin = [x[5] for x in rows if not x[6] and x[5] is not None]
        per_season.append(
            {
                "season": sid,
                "races": len(rows),
                "finished": len(fin),
                "avg_finish": _round(statistics.mean(fin)) if fin else None,
                "finish_stdev": _round(statistics.pstdev(fin)) if len(fin) > 1 else None,
                "penalties": sum(1 for x in rows if x[7] and x[7] != 0),
                "dns": sum(1 for x in rows if x[6]),
            }
        )

    # Bounce-back: after a below-career-average finish, what's the next finished result?
    recovery_deltas: List[float] = []
    after_dns_positions: List[int] = []
    for i, r in enumerate(races):
        nxt = next((races[j] for j in range(i + 1, total) if not races[j][6] and races[j][5] is not None), None)
        if nxt is None:
            continue
        if r[6]:  # this race was a DNS
            after_dns_positions.append(nxt[5])
        elif r[5] is not None and career_avg_pos is not None and r[5] > career_avg_pos:
            recovery_deltas.append(nxt[5] - r[5])  # negative = improved after a bad race

    # Season-arc: early vs late form within each season (>=4 finished races).
    arc_deltas: List[float] = []
    for sid, rows in by_season.items():
        fin = [x[5] for x in rows if not x[6] and x[5] is not None]
        if len(fin) >= 4:
            half = len(fin) // 2
            early = statistics.mean(fin[:half])
            late = statistics.mean(fin[-half:])
            arc_deltas.append(late - early)  # negative = stronger end of season

    trajectory = [
        {"season": t[0], "wsr_rank": t[2], "weighted_score": _round(t[3], 3)} for t in traj_rows
    ]
    ranks = [t[2] for t in traj_rows if t[2] is not None]

    result = {
        "driver": canon,
        "totals": {
            "races": total,
            "finished": len(finished),
            "career_avg_finish": _round(career_avg_pos),
            "wins": wins,
            "poles": poles,
        },
        "discipline": {
            "penalised_races": penalties,
            "penalty_rate": _round(penalties / total, 3),
            "dns": dns_count,
            "dns_rate": _round(dns_count / total, 3),
        },
        "consistency_by_season": per_season,
        "bounce_back": {
            "avg_delta_after_below_avg_finish": _round(statistics.mean(recovery_deltas)) if recovery_deltas else None,
            "sample": len(recovery_deltas),
            "avg_finish_after_dns": _round(statistics.mean(after_dns_positions)) if after_dns_positions else None,
            "note": "negative avg_delta = tends to improve in the race after a bad result",
        },
        "quali_vs_race": {
            "poles": poles,
            "wins": wins,
            "pole_to_win_conversion": _round(pole_wins / poles, 3) if poles else None,
            "wins_without_pole": wins - pole_wins,
            "note": "high wins_without_pole relative to wins suggests racecraft over one-lap pace",
        },
        "season_arc": {
            "avg_late_minus_early_finish": _round(statistics.mean(arc_deltas)) if arc_deltas else None,
            "seasons_measured": len(arc_deltas),
            "note": "negative = consistently stronger in the back half of seasons",
        },
        "trajectory": {
            "best_wsr_rank": min(ranks) if ranks else None,
            "worst_wsr_rank": max(ranks) if ranks else None,
            "latest_wsr_rank": ranks[-1] if ranks else None,
            "by_season": trajectory,
        },
        "_caveat": (
            "Signals derive ONLY from finishing positions, points, poles, fastest laps, "
            "penalties and DNS. No lap/sector times, gaps, weather, tyre/pit strategy, or "
            "incident detail exist — do not infer those. Interpret as evidence, not proof."
        ),
    }
    return json.dumps(result, default=str)
