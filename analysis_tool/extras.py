"""Parse the newer career-metric sections of the data markdown and load them into
the DuckDB as extra tables.

The sibling `cup_racing_insights` parser builds the five core tables (seasons,
career_stats, race_results, weighted_scores, team_standings). The data file has
since grown two more top-level sections that the core parser ignores:

- '# Cup Racing Career Performance Index (CPI)'  -> career_cpi
- '# Cup Racing Career Rating' (Elo-style)        -> career_rating + rating_trajectory

Rather than fork the sibling library, we parse those sections here and append the
tables to the same DuckDB right after the core rebuild. Percentages are stored as
fractions (83.0% -> 0.83) to match the convention in career_stats/weighted_scores.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from .config import Config


def _split_row(line: str) -> List[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _is_separator(line: str) -> bool:
    body = line.strip().strip("|").strip()
    return bool(body) and set(body) <= set("-:| ")


def _num(cell: str) -> Optional[float]:
    """Parse a numeric cell; percentages become fractions. Returns None if blank/NA."""
    s = (cell or "").strip().replace(",", "")
    if not s or s in ("-", "—", "N/A", "n/a"):
        return None
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return v / 100.0 if pct else v


def _int(cell: str) -> Optional[int]:
    v = _num(cell)
    return int(round(v)) if v is not None else None


def _read_table(lines: List[str], header: str) -> List[List[str]]:
    """Return the data rows of the first markdown table following `header`."""
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == header:
            start = i
            break
    if start is None:
        return []
    i = start + 1
    n = len(lines)
    while i < n and not lines[i].lstrip().startswith("|"):
        i += 1
    if i >= n:
        return []
    i += 1  # skip column-header row
    if i < n and _is_separator(lines[i]):
        i += 1  # skip separator row
    rows: List[List[str]] = []
    while i < n and lines[i].lstrip().startswith("|"):
        if not _is_separator(lines[i]):  # tolerate stray separator rows mid-table
            rows.append(_split_row(lines[i]))
        i += 1
    return rows


def parse_extras(data_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Parse the CPI and Career Rating sections. Missing sections yield empty lists."""
    lines = Path(data_path).read_text(encoding="utf-8").splitlines()

    cpi: List[Dict[str, Any]] = []
    # Rank | Driver | CPI | Avg WS | Peak WS | Avg Pts Rate | Avg Top5 Rate | WDCs | WCCs
    for c in _read_table(lines, "## CPI Rankings"):
        if len(c) < 9 or not c[0][:1].isdigit():
            continue
        cpi.append({
            "driver": c[1], "rank": _int(c[0]), "cpi": _num(c[2]),
            "avg_ws": _num(c[3]), "peak_ws": _num(c[4]),
            "avg_pts_rate": _num(c[5]), "avg_top5_rate": _num(c[6]),
            "wdcs": _int(c[7]) or 0, "wccs": _int(c[8]) or 0,
        })

    rating: List[Dict[str, Any]] = []
    # Rank | Driver | Rating | Last Season | Career Races | Seasons | WDC | WCC
    for c in _read_table(lines, "## Current Ratings"):
        if len(c) < 8 or not c[0][:1].isdigit():
            continue
        rating.append({
            "driver": c[1], "rank": _int(c[0]), "rating": _num(c[2]),
            "last_season": c[3], "career_races": _int(c[4]) or 0,
            "seasons": _int(c[5]) or 0, "wdc": _int(c[6]) or 0, "wcc": _int(c[7]) or 0,
        })

    trajectory: List[Dict[str, Any]] = []
    # Driver | Through Season | Rating | Change | Score | Expected | Upset Bonus | Confidence | Field Size
    for c in _read_table(lines, "## Rating Trajectory (All Checkpoints)"):
        if len(c) < 9 or c[0].lower() == "driver":
            continue
        trajectory.append({
            "driver": c[0], "through_season": c[1], "rating": _num(c[2]),
            "change": _num(c[3]), "score": _num(c[4]), "expected": _num(c[5]),
            "upset_bonus": _num(c[6]), "confidence": _num(c[7]), "field_size": _int(c[8]),
        })

    return {"career_cpi": cpi, "career_rating": rating, "rating_trajectory": trajectory}


_SCHEMA = """
CREATE OR REPLACE TABLE career_cpi (
    driver TEXT, rank INTEGER, cpi DOUBLE, avg_ws DOUBLE, peak_ws DOUBLE,
    avg_pts_rate DOUBLE, avg_top5_rate DOUBLE, wdcs INTEGER, wccs INTEGER
);
CREATE OR REPLACE TABLE career_rating (
    driver TEXT, rank INTEGER, rating DOUBLE, last_season TEXT,
    career_races INTEGER, seasons INTEGER, wdc INTEGER, wcc INTEGER
);
CREATE OR REPLACE TABLE rating_trajectory (
    driver TEXT, through_season TEXT, rating DOUBLE, change DOUBLE, score DOUBLE,
    expected DOUBLE, upset_bonus DOUBLE, confidence DOUBLE, field_size INTEGER
);
"""

_COLS = {
    "career_cpi": ["driver", "rank", "cpi", "avg_ws", "peak_ws", "avg_pts_rate",
                   "avg_top5_rate", "wdcs", "wccs"],
    "career_rating": ["driver", "rank", "rating", "last_season", "career_races",
                      "seasons", "wdc", "wcc"],
    "rating_trajectory": ["driver", "through_season", "rating", "change", "score",
                          "expected", "upset_bonus", "confidence", "field_size"],
}


def augment_db(cfg: Config) -> Dict[str, int]:
    """Parse the extra sections and (re)create their tables in the DuckDB.

    Called right after the core rebuild. Returns per-table row counts. Safe to run
    repeatedly (tables are CREATE OR REPLACE)."""
    parsed = parse_extras(cfg.data_path)
    con = duckdb.connect(str(cfg.db_path))  # read-write
    try:
        con.execute(_SCHEMA)
        counts: Dict[str, int] = {}
        for table, cols in _COLS.items():
            rows = parsed[table]
            if rows:
                placeholders = ", ".join(["?"] * len(cols))
                con.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})",
                    [[r.get(col) for col in cols] for r in rows],
                )
            counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()
    return counts
