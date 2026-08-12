"""Data access over the insights DuckDB.

Reuses `cup_racing_insights` as a library: `db.rebuild` to (re)build the database
from the markdown, and a read-only connection for all query/detector work so the
chat agent can never mutate the data.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

import duckdb

from cup_racing_insights import db as cri_db

from .config import Config


def rebuild(cfg: Config) -> dict:
    """Parse the markdown into DuckDB. Returns per-table row counts.

    Builds the five core tables via the shared insights library, then augments the
    same DB with the newer career-metric tables (CPI, Career Rating) the core parser
    doesn't cover — see `extras.augment_db` — and the per-session telemetry tables
    (pace, overtakes, contacts, coverage) — see `telemetry.augment_telemetry`.
    """
    from . import extras, telemetry  # local import to avoid a cycle at module load

    cfg.ensure_dirs()
    counts = cri_db.rebuild(data_path=cfg.data_path, db_path=cfg.db_path)
    counts.update(extras.augment_db(cfg))
    counts.update(telemetry.augment_telemetry(cfg))
    return counts


def db_exists(cfg: Config) -> bool:
    return cfg.db_path.exists()


@contextmanager
def read_only(cfg: Config) -> Iterator[duckdb.DuckDBPyConnection]:
    """A read-only connection. Guarantees the agent cannot write to the data."""
    con = duckdb.connect(str(cfg.db_path), read_only=True)
    try:
        yield con
    finally:
        con.close()


def list_driver_names(cfg: Config) -> list[str]:
    with read_only(cfg) as con:
        rows = con.execute(
            "SELECT driver FROM career_stats ORDER BY points DESC"
        ).fetchall()
    return [r[0] for r in rows]


def weighted_score_calibration(cfg: Config) -> Optional[dict]:
    """Summarize the weighted_score distribution so the prompt can teach the model
    the league's real scale (0.2 is average, not poor; champions live at 0.65+).

    Computed from the DB so the calibration self-adjusts as seasons are added.
    Returns None if the table is missing/empty so the caller can fall back.
    """
    try:
        with read_only(cfg) as con:
            row = con.execute(
                """
                SELECT count(*),
                       median(weighted_score),
                       avg(weighted_score),
                       quantile_cont(weighted_score, 0.75),
                       quantile_cont(weighted_score, 0.90),
                       max(weighted_score)
                FROM weighted_scores
                """
            ).fetchone()
            if not row or not row[0]:
                return None
            n, med, mean, p75, p90, mx = row
            top = con.execute(
                """
                SELECT driver, season_id FROM weighted_scores
                ORDER BY weighted_score DESC LIMIT 1
                """
            ).fetchone()
            champ = con.execute(
                """
                SELECT min(weighted_score), median(weighted_score)
                FROM weighted_scores WHERE has_wdc = TRUE
                """
            ).fetchone()
    except Exception:  # noqa: BLE001 — missing table/column, unbuilt DB, etc.
        return None
    return {
        "n": int(n),
        "median": round(med, 3),
        "mean": round(mean, 3),
        "p75": round(p75, 3),
        "p90": round(p90, 3),
        "max": round(mx, 3),
        "max_driver": top[0] if top else None,
        "max_season": top[1] if top else None,
        "champ_min": round(champ[0], 3) if champ and champ[0] is not None else None,
        "champ_median": round(champ[1], 3) if champ and champ[1] is not None else None,
    }


def resolve_season(cfg: Config, season_id: str) -> Optional[str]:
    """Match a free-text season reference to a canonical season_id.

    Handles 'S17', 's17', '17', and 'S18a' style inputs. Returns None if there is
    no unambiguous match.
    """
    if not season_id:
        return None
    target = season_id.strip().lower()
    with read_only(cfg) as con:
        ids = [r[0] for r in con.execute("SELECT season_id FROM seasons").fetchall()]
    for s in ids:  # exact
        if s.lower() == target:
            return s
    for s in ids:  # bare number -> S<number>
        if s.lower() == f"s{target}":
            return s
    subs = [s for s in ids if target in s.lower()]  # unique substring
    if len(subs) == 1:
        return subs[0]
    return None


def resolve_driver(cfg: Config, name: str) -> Optional[str]:
    """Case-insensitive match of a free-text name to a canonical driver name.

    Returns the canonical name, or None if there is no unambiguous match.
    """
    if not name:
        return None
    target = name.strip().lower()
    names = list_driver_names(cfg)
    # exact (case-insensitive)
    for n in names:
        if n.lower() == target:
            return n
    # unique prefix / substring fallback
    subs = [n for n in names if target in n.lower()]
    if len(subs) == 1:
        return subs[0]
    return None
