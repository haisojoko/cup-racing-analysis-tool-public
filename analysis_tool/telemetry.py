"""Load the per-session telemetry dataset into the DuckDB as extra tables.

`cup-racing-race-processor` writes a rich, additive dataset under `data/races/`
(one JSON file per season) with things the official results markdown never
captured: lap/pace, on-track overtakes, contacts, inferred grids, and
qualifying-vs-race deltas. See `data/races/SCHEMA.md` for the full contract.

Like `extras.augment_db`, we parse it here and append tables to the same DuckDB
right after the core rebuild, rather than forking the sibling library. Two tables:

- `race_telemetry`  — one row per (season, telemetry-race, driver): pace, overtakes,
  position changes, contacts, grid/finish. The analytical substrate.
- `telemetry_coverage` — one row per season present in telemetry: how many races
  the telemetry actually holds vs how many the OFFICIAL results record. Makes the
  incompleteness explicit and queryable.

IMPORTANT — this is a PARTIAL OVERLAY, not the authority:
- It exists only for seasons the processor covered (S4-S23 as of writing; NOT
  S1-S3, NOT the current S24). Even within a covered season it is missing races,
  because not every race-week's results.json was saved. Missing telemetry never
  means a race didn't happen — the official `race_results` remains complete and
  authoritative for standings, points, positions, poles, and DNS.
- It is its OWN coordinate system. Telemetry `race_num` is "1..n of the sessions
  that survived", NOT the official race number, and telemetry `venue` strings are
  processor slugs ("Rt Daytona") that differ from the official venue names
  ("Daytona Road Course"). So telemetry joins to the official tables safely only
  by `driver`, `season_id`, and `venue_order` — never by race number or venue text.
- Per-race reverse-grid detection is `grid_source = 'reversed-previous'` (empirical),
  which is more reliable here than assuming even races are the reversed ones.
- Times are milliseconds. `null` means "unknown/unavailable", never zero.
- Multi-class seasons (S14, S18a, S18b, S24a) carry `car_class`, `class_finish_pos`
  and `class_field_size`; all three are NULL elsewhere. `pace_gap_to_leader_pct` is
  measured against the fastest car in the driver's OWN class, because the classes
  are seconds a lap apart and a whole-field baseline would rank the car, not the
  driver. `finish_pos` stays the overall order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from .config import Config


def _grid_pos(grid: List[str], label: str) -> Optional[int]:
    try:
        return grid.index(label) + 1
    except ValueError:
        return None


def _race_best_median(pace: Dict[str, Any], labels: Optional[set] = None) -> Optional[float]:
    """Fastest median lap in the race, for a track-independent pace-gap baseline.

    ``labels`` restricts the baseline to one car class. On a multi-class season
    a whole-field baseline measures the car rather than the driver — the two
    S14 classes are 16 seconds a lap apart — so every class gets its own.
    """
    medians = [
        p["medianMs"]
        for label, p in pace.items()
        if isinstance(p, dict) and p.get("medianMs") is not None
        and (labels is None or label in labels)
    ]
    return min(medians) if medians else None


def _parse_race_rows(
    season_id: str,
    venue: str,
    venue_order: int,
    event_date: str,
    race_num: int,
    race: Dict[str, Any],
    driver_classes: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """One row per driver in a single telemetry race.

    ``driver_classes`` maps driver label -> car class on a multi-class season
    (empty otherwise). It scopes the pace baseline and the field size to the
    driver's own class, so a GT3 car is not scored against a Hypercar.
    """
    driver_classes = driver_classes or {}
    grid = race.get("grid") or []
    grid_source = race.get("gridSource")
    grid_conf = race.get("gridConfidence")
    is_reverse = grid_source == "reversed-previous"

    pace = race.get("pace") or {}
    pos_changes = race.get("positionChanges")  # dict OR null (untrusted grid)
    qvr = race.get("qualVsRace")  # dict OR null (no qualifying preceded)
    overtakes = race.get("overtakes") or []
    contacts = race.get("contacts") or []
    result = race.get("result") or []
    drivers_meta = race.get("drivers") or {}

    # One pace baseline per class (a single None-keyed baseline when the season
    # is single-class, which is every season but four).
    labels_by_class: Dict[Optional[str], set] = {}
    for label in pace:
        labels_by_class.setdefault(driver_classes.get(label), set()).add(label)
    best_median_by_class = {
        cls: _race_best_median(pace, labels if driver_classes else None)
        for cls, labels in labels_by_class.items()
    }

    # Per-driver rollups from the list-shaped fields.
    ot_made: Dict[str, int] = {}
    for o in overtakes:
        d = o.get("driver")
        if d is not None:
            ot_made[d] = ot_made.get(d, 0) + 1
    contact_ct: Dict[str, int] = {}
    for c in contacts:
        for key in ("driver1", "driver2"):
            d = c.get(key)
            if d is not None:
                contact_ct[d] = contact_ct.get(d, 0) + 1
    result_by_label = {r.get("driverKey"): r for r in result if r.get("driverKey")}

    # Every driver who appears anywhere in this race.
    labels = set(grid) | set(pace) | set(result_by_label) | set(drivers_meta)
    field_size = len(result)
    class_field_size: Dict[str, int] = {}
    for r in result:
        cls = driver_classes.get(r.get("driverKey"))
        if cls:
            class_field_size[cls] = class_field_size.get(cls, 0) + 1

    rows: List[Dict[str, Any]] = []
    for label in sorted(labels):
        p = pace.get(label) or {}
        res = result_by_label.get(label) or {}
        pc = (pos_changes or {}).get(label) if isinstance(pos_changes, dict) else None
        qr = (qvr or {}).get(label) if isinstance(qvr, dict) else None
        meta = drivers_meta.get(label) or {}

        car_class = driver_classes.get(label)
        best_median = best_median_by_class.get(car_class)
        median = p.get("medianMs")
        gap_pct = (
            round(median / best_median - 1.0, 4)
            if median is not None and best_median
            else None
        )

        rows.append({
            "season_id": season_id,
            "venue": venue,
            "venue_order": venue_order,
            "event_date": event_date,
            "race_num": race_num,
            "grid_source": grid_source,
            "grid_confidence": grid_conf,
            "is_reverse_race": is_reverse,
            "driver": label,
            "car": res.get("car") or meta.get("car"),
            "car_class": car_class,
            "grid_pos": _grid_pos(grid, label),
            "finish_pos": res.get("position"),
            "class_finish_pos": res.get("classPosition"),
            "laps_completed": res.get("laps"),
            "best_lap_ms": res.get("bestLapMs"),
            "pace_median_ms": median,
            "pace_avg_ms": p.get("avgMs"),
            "pace_best_ms": p.get("bestMs"),
            "pace_laps_used": p.get("lapsUsed"),
            "pace_gap_to_leader_pct": gap_pct,
            "overtakes_made": ot_made.get(label, 0),
            "pos_gained": pc.get("gained") if pc else None,
            "pos_lost": pc.get("lost") if pc else None,
            "pos_net": pc.get("net") if pc else None,
            "qual_pos": qr.get("qualPosition") if qr else None,
            "qual_to_finish_delta": qr.get("delta") if qr else None,
            "contacts_involved": contact_ct.get(label, 0),
            "field_size": field_size,
            "class_field_size": class_field_size.get(car_class) if car_class else None,
        })
    return rows


def parse_telemetry(races_dir: Path) -> List[Dict[str, Any]]:
    """Parse every season file under `races_dir/seasons/` into flat race_telemetry
    rows. Missing/empty directory yields an empty list (an un-backfilled install)."""
    seasons_dir = Path(races_dir) / "seasons"
    if not seasons_dir.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(seasons_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        season_id = doc.get("season")
        # Season-level: a driver's class is fixed for the season. Absent on
        # every single-class season, which leaves the class columns NULL.
        driver_classes = (doc.get("classes") or {}).get("drivers") or {}
        for event in doc.get("events", []):
            venue = event.get("venue")
            venue_order = event.get("venueOrder")
            event_date = event.get("date")
            races = event.get("races") or {}
            for rk, race in races.items():
                try:
                    race_num = int(rk)
                except (TypeError, ValueError):
                    continue
                rows.extend(
                    _parse_race_rows(
                        season_id, venue, venue_order, event_date, race_num, race,
                        driver_classes,
                    )
                )
    return rows


_SCHEMA = """
CREATE OR REPLACE TABLE race_telemetry (
    season_id             TEXT,
    venue                 TEXT,
    venue_order           INTEGER,
    event_date            TEXT,
    race_num              INTEGER,
    grid_source           TEXT,
    grid_confidence       TEXT,
    is_reverse_race       BOOLEAN,
    driver                TEXT,
    car                   TEXT,
    car_class             TEXT,     -- NULL on single-class seasons
    grid_pos              INTEGER,
    finish_pos            INTEGER,   -- overall, whole field
    class_finish_pos      INTEGER,   -- within car_class; NULL when single-class
    laps_completed        INTEGER,
    best_lap_ms           INTEGER,
    pace_median_ms        INTEGER,
    pace_avg_ms           INTEGER,
    pace_best_ms          INTEGER,
    pace_laps_used        INTEGER,
    pace_gap_to_leader_pct DOUBLE,
    overtakes_made        INTEGER,
    pos_gained            INTEGER,
    pos_lost              INTEGER,
    pos_net               INTEGER,
    qual_pos              INTEGER,
    qual_to_finish_delta  INTEGER,
    contacts_involved     INTEGER,
    field_size            INTEGER,
    class_field_size      INTEGER    -- NULL on single-class seasons
);
CREATE OR REPLACE TABLE telemetry_coverage (
    season_id        TEXT,
    events_present   INTEGER,
    races_present    INTEGER,
    official_races   INTEGER,
    coverage_pct     DOUBLE,
    first_event_date TEXT,
    last_event_date  TEXT
);
"""

_RACE_COLS = [
    "season_id", "venue", "venue_order", "event_date", "race_num", "grid_source",
    "grid_confidence", "is_reverse_race", "driver", "car", "car_class", "grid_pos",
    "finish_pos", "class_finish_pos", "laps_completed", "best_lap_ms", "pace_median_ms", "pace_avg_ms", "pace_best_ms",
    "pace_laps_used", "pace_gap_to_leader_pct", "overtakes_made", "pos_gained",
    "pos_lost", "pos_net", "qual_pos", "qual_to_finish_delta", "contacts_involved",
    "field_size", "class_field_size",
]


def _coverage_rows(
    con: duckdb.DuckDBPyConnection, race_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Season-level coverage: telemetry race count vs the OFFICIAL race count (from
    the already-built race_results), so the gap between them is a first-class fact."""
    by_season: Dict[str, Dict[str, Any]] = {}
    for r in race_rows:
        s = by_season.setdefault(r["season_id"], {
            "events": set(), "races": set(), "dates": []
        })
        s["events"].add(r["venue_order"])
        s["races"].add((r["venue_order"], r["race_num"]))
        if r["event_date"]:
            s["dates"].append(r["event_date"])

    try:
        official = dict(con.execute(
            "SELECT season_id, count(*) FROM (SELECT DISTINCT season_id, venue_order, "
            "race_num FROM race_results) GROUP BY season_id"
        ).fetchall())
    except Exception:  # noqa: BLE001 — race_results not built yet
        official = {}

    out: List[Dict[str, Any]] = []
    for sid, s in by_season.items():
        races_present = len(s["races"])
        off = official.get(sid)
        dates = sorted(d for d in s["dates"] if d)
        out.append({
            "season_id": sid,
            "events_present": len(s["events"]),
            "races_present": races_present,
            "official_races": off,
            "coverage_pct": round(races_present / off, 3) if off else None,
            "first_event_date": dates[0] if dates else None,
            "last_event_date": dates[-1] if dates else None,
        })
    out.sort(key=lambda r: r["season_id"])
    return out


_COVERAGE_COLS = [
    "season_id", "events_present", "races_present", "official_races",
    "coverage_pct", "first_event_date", "last_event_date",
]


def augment_telemetry(cfg: Config) -> Dict[str, int]:
    """Parse the telemetry dataset and (re)create its tables in the DuckDB.

    Called right after the core rebuild + extras. Returns per-table row counts.
    Safe to run repeatedly (CREATE OR REPLACE). A missing dataset yields empty
    tables so the rest of the harness still works."""
    race_rows = parse_telemetry(cfg.races_dir)
    con = duckdb.connect(str(cfg.db_path))  # read-write
    try:
        con.execute(_SCHEMA)
        if race_rows:
            placeholders = ", ".join(["?"] * len(_RACE_COLS))
            con.executemany(
                f"INSERT INTO race_telemetry VALUES ({placeholders})",
                [[r.get(c) for c in _RACE_COLS] for r in race_rows],
            )
        cov_rows = _coverage_rows(con, race_rows)
        if cov_rows:
            placeholders = ", ".join(["?"] * len(_COVERAGE_COLS))
            con.executemany(
                f"INSERT INTO telemetry_coverage VALUES ({placeholders})",
                [[r.get(c) for c in _COVERAGE_COLS] for r in cov_rows],
            )
        return {
            "race_telemetry": con.execute(
                "SELECT COUNT(*) FROM race_telemetry"
            ).fetchone()[0],
            "telemetry_coverage": con.execute(
                "SELECT COUNT(*) FROM telemetry_coverage"
            ).fetchone()[0],
        }
    finally:
        con.close()
