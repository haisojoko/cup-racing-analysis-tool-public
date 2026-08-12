"""Data-grounding tools: read-only SQL, the insights detector library, and a
couple of convenience reads. These are how the agent turns claims into facts.
"""

from __future__ import annotations

import json
import re
import statistics

from cup_racing_insights import detectors as cri_detectors
from cup_racing_insights import scoring as cri_scoring

from ..config import Config
from ..data import list_driver_names, read_only, resolve_driver, resolve_season
from ..roundsheet import parse_round_sheet

# Keywords that must never appear in a query. The connection is already
# read-only (DuckDB rejects writes), so this is a second layer that returns a
# clean message instead of a driver-facing SQL error.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|install|load|export|truncate)\b",
    re.IGNORECASE,
)


def query_sql(cfg: Config, sql: str = "") -> str:
    cleaned = (sql or "").strip().rstrip(";").strip()
    if not cleaned:
        return "ERROR: empty query."
    if ";" in cleaned:
        return "ERROR: only a single statement is allowed (no ';')."
    low = cleaned.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "ERROR: only read-only SELECT / WITH queries are allowed."
    if _FORBIDDEN.search(low):
        return "ERROR: query contains a forbidden (write/DDL) keyword."
    try:
        with read_only(cfg) as con:
            cur = con.execute(cleaned)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(cfg.sql_row_cap + 1)
    except Exception as e:  # noqa: BLE001
        return f"SQL error: {e}"
    truncated = len(rows) > cfg.sql_row_cap
    rows = rows[: cfg.sql_row_cap]
    result = {"columns": cols, "rows": [list(r) for r in rows], "row_count": len(rows)}
    if truncated:
        result["note"] = f"truncated to first {cfg.sql_row_cap} rows; refine the query"
    return json.dumps(result, default=str)


def interpret_round_sheet(cfg: Config, text: str = "") -> str:
    """Parse a pasted race-day / round spreadsheet into structured results plus a
    computed summary (winners, poles, fastest laps, penalties, day standings,
    team totals). Deterministic parsing so the model narrates facts, not guesses."""
    try:
        parsed = parse_round_sheet(text)
    except ValueError as e:
        return (
            f"ERROR: this doesn't parse as a round sheet ({e}). Expected the pasted "
            "Google-Sheet layout: a 'Race / Driver' header, a sub-header row whose "
            "first cell is the venue followed by repeating 'Pos, Pole, Fastest Lap, "
            "Penalty, Points' blocks per race, then one row per driver. Ask the "
            "coordinator to paste the full sheet including both header rows."
        )
    return json.dumps(parsed, default=str)


def _std(xs) -> float | None:
    return round(statistics.pstdev(xs), 2) if len(xs) > 1 else None


def season_field_context(cfg: Config, season_id: str = "") -> str:
    """Field-level context for one season, so a driver's numbers are read against the
    field they actually beat — not in a vacuum. Deterministic; the model interprets.

    Reports: how many drivers ran and how many scored at all, participation shape
    (full-season vs part-timers, DNS load), how top-heavy the points were, the
    weighted-score spread and any runaway champion, and finishing-position volatility
    plus penalty/DNS load as the ONLY available incident proxies (no incident data is
    recorded). Includes a compact per-driver table for cross-driver variance deltas."""
    sid = resolve_season(cfg, season_id)
    if not sid:
        return (
            f"Unknown season '{season_id}'. Seasons look like 'S17' or 'S18a'. "
            "Query the seasons table (query_sql) to list them."
        )

    with read_only(cfg) as con:
        meta_cur = con.execute(
            "SELECT season_id, season_num, season_sub, type, car, venues, "
            "races_per_venue, wdc, wcc FROM seasons WHERE season_id = ?",
            [sid],
        )
        meta_cols = [d[0] for d in meta_cur.description]
        meta = dict(zip(meta_cols, meta_cur.fetchone()))
        total_races = con.execute(
            "SELECT count(*) FROM (SELECT DISTINCT venue_order, race_num "
            "FROM race_results WHERE season_id = ?)",
            [sid],
        ).fetchone()[0]
        agg = con.execute(
            """
            SELECT driver,
                   count(*) AS entries,
                   sum(CASE WHEN dns THEN 1 ELSE 0 END) AS dns,
                   coalesce(sum(points), 0) AS points,
                   sum(CASE WHEN penalty IS NOT NULL AND penalty <> 0 THEN 1 ELSE 0 END) AS penalised,
                   sum(CASE WHEN position = 1 THEN 1 ELSE 0 END) AS wins,
                   sum(CASE WHEN position IS NOT NULL AND position <= 3 THEN 1 ELSE 0 END) AS podiums
            FROM race_results WHERE season_id = ? GROUP BY driver
            """,
            [sid],
        ).fetchall()
        pos_rows = con.execute(
            "SELECT driver, position FROM race_results "
            "WHERE season_id = ? AND dns = FALSE AND position IS NOT NULL",
            [sid],
        ).fetchall()
        ws_rows = con.execute(
            "SELECT driver, weighted_score FROM weighted_scores WHERE season_id = ?",
            [sid],
        ).fetchall()

    if not agg:
        return f"No race results found for season {sid}."

    positions: dict[str, list[int]] = {}
    for drv, pos in pos_rows:
        positions.setdefault(drv, []).append(pos)
    ws_map = {d: w for d, w in ws_rows}

    field_size = len(agg)
    total_points = sum(r[3] for r in agg)
    per_driver = []
    for drv, entries, dns, points, penalised, wins, podiums in agg:
        pos_list = positions.get(drv, [])
        per_driver.append({
            "driver": drv,
            "entries": entries,
            "participation": round(entries / total_races, 2) if total_races else None,
            "dns": dns,
            "penalised_races": penalised,
            "points": points,
            "wins": wins,
            "podiums": podiums,
            "avg_finish": round(statistics.mean(pos_list), 2) if pos_list else None,
            "finish_stdev": _std(pos_list),
            "weighted_score": round(ws_map[drv], 3) if drv in ws_map else None,
        })

    # Season-local weighted-score rank (the stored WSR rank is GLOBAL, not per-season).
    ranked = sorted(
        [d for d in per_driver if d["weighted_score"] is not None],
        key=lambda d: d["weighted_score"], reverse=True,
    )
    for i, d in enumerate(ranked, start=1):
        d["season_wsr_rank"] = i
    per_driver.sort(key=lambda d: (d["points"], d["weighted_score"] or 0), reverse=True)

    scorers = [d for d in per_driver if d["points"] > 0]
    full_season = [d for d in per_driver if d["entries"] >= total_races]
    part_timers = [d for d in per_driver if d["entries"] < total_races]
    points_desc = sorted((d["points"] for d in per_driver), reverse=True)
    top3_share = round(sum(points_desc[:3]) / total_points, 3) if total_points else None
    champ_share = round(points_desc[0] / total_points, 3) if total_points and points_desc else None

    ws_vals = sorted(ws_map.values(), reverse=True)
    stdevs = [d["finish_stdev"] for d in per_driver if d["finish_stdev"] is not None]

    result = {
        "season": sid,
        "meta": {
            "season_num": meta.get("season_num"),
            "season_sub": meta.get("season_sub"),
            "type": meta.get("type"),
            "car": meta.get("car"),
            "wdc": meta.get("wdc"),
            "wcc": meta.get("wcc"),
            "total_races": total_races,
        },
        "field": {
            "drivers_entered": field_size,
            "drivers_who_scored": len(scorers),
            "drivers_who_never_scored": field_size - len(scorers),
            "full_season_runners": len(full_season),
            "part_timers": len(part_timers),
            "avg_participation": round(
                statistics.mean([d["participation"] for d in per_driver if d["participation"] is not None]), 2
            ) if per_driver else None,
        },
        "competitiveness": {
            "champion_points_share": champ_share,
            "top3_points_share": top3_share,
            "weighted_score_top": ws_vals[0] if ws_vals else None,
            "weighted_score_median": round(statistics.median(ws_vals), 3) if ws_vals else None,
            "weighted_score_bottom": ws_vals[-1] if ws_vals else None,
            "top1_to_top2_ws_gap": round(ws_vals[0] - ws_vals[1], 3) if len(ws_vals) > 1 else None,
            "note": "high champion/top3 share or a large top1-to-top2 gap = a top-heavy, "
                    "less-competitive season; a small gap = a tight title fight.",
        },
        "incident_proxies": {
            "total_dns": sum(d["dns"] for d in per_driver),
            "total_penalised_races": sum(d["penalised_races"] for d in per_driver),
            "field_median_finish_stdev": round(statistics.median(stdevs), 2) if stdevs else None,
            "note": "the league records NO incident/contact data. High per-driver "
                    "finish_stdev, DNS and penalties are the only (indirect) signals of a "
                    "messy/incident-prone campaign — treat as suggestive, never as proof.",
        },
        "per_driver": per_driver,
        "_caveat": (
            "Read a driver's season against THIS field: the same weighted_score or "
            "finishing average means more in a deep, full-participation season than in a "
            "thin one where few cars scored. Small fields, low participation, or a runaway "
            "leader all warrant an explicit caveat on any superlative."
        ),
    }
    return json.dumps(result, default=str)


def run_detectors(cfg: Config, driver: str = "", min_score: float = 0.0, limit: int = 25) -> str:
    canon = resolve_driver(cfg, driver)
    if not canon:
        return f"Unknown driver '{driver}'. Call list_drivers for valid names."
    with read_only(cfg) as con:
        insights = cri_detectors.run_all(con, canon)
    insights = cri_scoring.score_all(insights)
    out = []
    for ins in insights:
        if ins.score < min_score:
            continue
        out.append(
            {
                "kind": ins.kind,
                "category": ins.category.value,
                "headline": ins.headline,
                "score": round(ins.score, 3),
                "sources": ins.sources,
                "payload": ins.payload,
            }
        )
        if len(out) >= limit:
            break
    return json.dumps({"driver": canon, "count": len(out), "insights": out}, default=str)


def list_drivers(cfg: Config) -> str:
    cols = ["driver", "wdc", "wcc", "wins", "podiums", "poles", "points", "races"]
    with read_only(cfg) as con:
        rows = con.execute(
            f"SELECT {', '.join(cols)} FROM career_stats ORDER BY points DESC"
        ).fetchall()
    drivers = [dict(zip(cols, r)) for r in rows]
    return json.dumps({"count": len(drivers), "drivers": drivers}, default=str)


def get_driver_career(cfg: Config, driver: str = "") -> str:
    canon = resolve_driver(cfg, driver)
    if not canon:
        return f"Unknown driver '{driver}'. Call list_drivers for valid names."
    with read_only(cfg) as con:
        cur = con.execute("SELECT * FROM career_stats WHERE driver = ?", [canon])
        career_cols = [d[0] for d in cur.description]
        career_row = cur.fetchone()
        career = dict(zip(career_cols, career_row)) if career_row else {}
        ws = con.execute(
            """
            SELECT w.season_id, s.season_num, s.type, s.car, w.rank, w.weighted_score,
                   s.wdc = ? AS won_wdc
            FROM weighted_scores w JOIN seasons s USING (season_id)
            WHERE w.driver = ?
            ORDER BY s.season_num, s.season_sub
            """,
            [canon, canon],
        )
        ws_cols = [d[0] for d in ws.description]
        seasons = [dict(zip(ws_cols, r)) for r in ws.fetchall()]
        cpi = _row_dict(con, "SELECT * FROM career_cpi WHERE driver = ?", canon)
        rating = _row_dict(con, "SELECT * FROM career_rating WHERE driver = ?", canon)
    return json.dumps(
        {"driver": canon, "career": career, "cpi": cpi, "rating": rating, "seasons": seasons},
        default=str,
    )


def _row_dict(con, sql: str, *params) -> dict:
    """Fetch a single row as a dict, or {} if none. Tolerates a missing table so an
    un-augmented DB still works."""
    try:
        cur = con.execute(sql, list(params))
    except Exception:  # noqa: BLE001 — table not present (older DB build)
        return {}
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else {}
