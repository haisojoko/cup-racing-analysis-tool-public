"""Parse a copy-pasted race-day / round spreadsheet into structured results.

The coordinator keeps race days in a wide Google-Sheet layout: two header rows and
one repeating 5-column block per race (Pos, Pole, Fastest Lap, Penalty, Points),
with a Total column at the end. An LLM is unreliable at aligning that sparse,
tab-delimited grid — so we parse it deterministically here and hand the model
clean structured data plus a computed summary to narrate. Same division of labour
as the rest of the buddy: Python establishes the facts, the model interprets them.

Layout expected (tab-separated, as pasted from Sheets/Excel):

    Race    Driver  Race 1  ...     Race 2  ...     ...     Total Day Points
    <Venue>         Pos Pole Fastest Lap Penalty Points  Pos ...
    <car+team>  <driver>  <r1: 5 cells>  <r2: 5 cells>  ...  <total>

Pole / Fastest Lap cells hold a mark ("1") when achieved, else blank. Penalty is
points deducted (blank = none). A row with blank Pos and 0 Points = did not race.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# The fixed column order inside one race block.
_BLOCK = ("pos", "pole", "fastest_lap", "penalty", "points")


def _cells(line: str) -> List[str]:
    return [c.strip() for c in line.split("\t")]


def _to_int(cell: str) -> Optional[int]:
    cell = (cell or "").strip()
    if not cell:
        return None
    try:
        return int(float(cell))
    except ValueError:
        return None


def _is_mark(cell: str) -> bool:
    """A Pole / Fastest Lap cell counts as achieved if it holds any non-empty,
    non-zero mark (usually '1')."""
    cell = (cell or "").strip()
    return bool(cell) and cell not in ("0", "-", "x", "X", "no", "No")


def _split_entry(entry: str) -> Dict[str, str]:
    """Best-effort split of a "car + team" label like 'F1 90 McLaren' into a car
    and a team. We take the trailing token as the team (team names are single
    words here) and the rest as the car; the raw label is always kept."""
    entry = (entry or "").strip()
    parts = entry.split()
    if len(parts) >= 2:
        return {"entry": entry, "car": " ".join(parts[:-1]), "team": parts[-1]}
    return {"entry": entry, "car": entry, "team": entry}


def parse_round_sheet(text: str) -> Dict[str, Any]:
    """Parse a pasted round sheet. Raises ValueError if it doesn't look like one."""
    if not text or not text.strip():
        raise ValueError("empty input")

    rows = [_cells(l) for l in text.splitlines() if l.strip("\t").strip()]

    # The sub-header row anchors everything: it contains the repeated 'Pos'/'Points'
    # labels, and its first cell is the venue.
    sub_idx = None
    for i, r in enumerate(rows):
        lowered = [c.lower() for c in r]
        if "pos" in lowered and "points" in lowered:
            sub_idx = i
            break
    if sub_idx is None:
        raise ValueError(
            "could not find the header row (expected 'Pos … Points' columns). "
            "Paste the full sheet including its header rows."
        )

    sub = rows[sub_idx]
    venue = sub[0].strip() if sub and sub[0].strip() else None
    pos_cols = [i for i, c in enumerate(sub) if c.lower() == "pos"]
    if not pos_cols:
        raise ValueError("no race blocks found")
    n_races = len(pos_cols)

    # Race labels come from the top header row if present ('Race 1' …), else index.
    race_labels: List[str] = []
    top = rows[sub_idx - 1] if sub_idx > 0 else []
    for k, start in enumerate(pos_cols):
        label = ""
        if start < len(top) and top[start].strip():
            label = top[start].strip()
        race_labels.append(label or f"Race {k + 1}")

    # Total column: labelled 'Total Day Points' in a header row, else just after
    # the last block.
    total_col = None
    for r in rows[: sub_idx + 1]:
        for i, c in enumerate(r):
            if "total" in c.lower():
                total_col = i
                break
        if total_col is not None:
            break
    if total_col is None:
        total_col = pos_cols[-1] + len(_BLOCK)

    drivers: List[Dict[str, Any]] = []
    for r in rows[sub_idx + 1 :]:
        if len(r) < 2 or not r[1].strip():
            continue  # need at least an entry + a driver name
        info = _split_entry(r[0])
        driver = r[1].strip()

        races: List[Dict[str, Any]] = []
        for k, start in enumerate(pos_cols):
            block = {name: (r[start + off] if start + off < len(r) else "")
                     for off, name in enumerate(_BLOCK)}
            pts = _to_int(block["points"])
            races.append({
                "race": race_labels[k],
                "pos": _to_int(block["pos"]),
                "pole": _is_mark(block["pole"]),
                "fastest_lap": _is_mark(block["fastest_lap"]),
                "penalty": _to_int(block["penalty"]) or 0,
                "points": pts if pts is not None else 0,
            })

        stated_total = _to_int(r[total_col]) if total_col < len(r) else None
        summed = sum(rc["points"] for rc in races)
        total = stated_total if stated_total is not None else summed
        participated = any(rc["pos"] is not None or rc["points"] for rc in races)

        drivers.append({
            "driver": driver,
            **info,
            "races": races,
            "total_points": total,
            "participated": participated,
        })

    return {
        "venue": venue,
        "n_races": n_races,
        "races": race_labels,
        "drivers": drivers,
        "summary": _summarize(race_labels, drivers),
    }


def _summarize(race_labels: List[str], drivers: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_race: List[Dict[str, Any]] = []
    for k, label in enumerate(race_labels):
        winner = pole = fl = None
        penalties: List[Dict[str, Any]] = []
        for d in drivers:
            rc = d["races"][k]
            if rc["pos"] == 1:
                winner = d["driver"]
            if rc["pole"]:
                pole = d["driver"]
            if rc["fastest_lap"]:
                fl = d["driver"]
            if rc["penalty"]:
                penalties.append({"driver": d["driver"], "penalty": rc["penalty"]})
        per_race.append({
            "race": label, "winner": winner, "pole": pole,
            "fastest_lap": fl, "penalties": penalties,
        })

    ran = [d for d in drivers if d["participated"]]
    standings = sorted(ran, key=lambda d: d["total_points"], reverse=True)
    day_standings = [{"driver": d["driver"], "team": d["team"],
                      "points": d["total_points"]} for d in standings]

    team_totals: Dict[str, int] = {}
    for d in ran:
        team_totals[d["team"]] = team_totals.get(d["team"], 0) + d["total_points"]
    team_points = [{"team": t, "points": p}
                   for t, p in sorted(team_totals.items(), key=lambda kv: -kv[1])]

    non_participants = [d["driver"] for d in drivers if not d["participated"]]

    return {
        "per_race": per_race,
        "day_standings": day_standings,
        "team_points": team_points,
        "non_participants": non_participants,
    }
