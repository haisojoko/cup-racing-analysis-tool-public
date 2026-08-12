"""Tool registry: OpenAI-style schemas passed to the model + a name→handler
dispatch map used by the agent loop.

`propose_summary_update` is declared here so the model can call it, but it has no
handler — the agent loop intercepts it to run the coordinator approval flow.
"""

from __future__ import annotations

from typing import Callable, Dict

from ..config import Config
from . import data_tools, profile_tools, psychology

# Tool name handled specially by the agent (interactive approval), not dispatched.
SPECIAL_TOOLS = {"propose_summary_update"}


def _fn(name, description, properties, required=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


_STR = {"type": "string"}
_DRIVER = {"type": "string", "description": "Driver name (case-insensitive)."}

TOOL_SCHEMAS = [
    _fn(
        "list_drivers",
        "List every driver with headline career stats (titles, wins, podiums, poles, points, races). Use to find exact names or get a roster overview.",
        {},
    ),
    _fn(
        "query_sql",
        "Run a single read-only SELECT/WITH query against the league DuckDB and get JSON rows. "
        "Tables: seasons(season_id,season_num,season_sub,type,car,venues,races_per_venue,wdc,wcc,reverse_grid); "
        "career_stats(driver,wdc,wcc,wins,podiums,poles,fls,points,races,win_pct,pod_pct,pts_per_race,fl_pct,top5,top5_pct); "
        "race_results(season_id,venue,venue_order,race_num,driver,car,position,points,is_pole,is_fastest_lap,penalty,dns); "
        "weighted_scores(driver,season_id,rank,weighted_score,...); "
        "team_standings(season_id,team_label,team_name,members,points,season_rank); "
        "career_cpi(driver,rank,cpi,avg_ws,peak_ws,avg_pts_rate,avg_top5_rate,wdcs,wccs) — career composite quality; "
        "career_rating(driver,rank,rating,last_season,career_races,seasons,wdc,wcc) — Bayesian form rating, everyone starts 1000; "
        "rating_trajectory(driver,through_season,rating,change,score,expected,upset_bonus,confidence,field_size) — per-season Elo checkpoints. "
        "TELEMETRY (partial overlay, S4-S23 only, missing races — see the TELEMETRY note in your system prompt): "
        "race_telemetry(season_id,venue,venue_order,event_date,race_num,grid_source,grid_confidence,is_reverse_race,driver,car,car_class,grid_pos,finish_pos,class_finish_pos,laps_completed,best_lap_ms,pace_median_ms,pace_avg_ms,pace_best_ms,pace_laps_used,pace_gap_to_leader_pct,overtakes_made,pos_gained,pos_lost,pos_net,qual_pos,qual_to_finish_delta,contacts_involved,field_size,class_field_size) — per-session pace/overtakes/contacts; times in ms; join to official tables ONLY by driver/season_id/venue_order, NEVER by race_num or venue text; car_class/class_finish_pos/class_field_size are NULL except on multi-class seasons (S14/S18a/S18b), where pace_gap_to_leader_pct is measured within the driver's own class and finish_pos stays the overall order; "
        "telemetry_coverage(season_id,events_present,races_present,official_races,coverage_pct,first_event_date,last_event_date) — how much telemetry exists vs the official race count per season. "
        "Rates/percentages are stored as fractions (0.83 = 83%). "
        "This is the primary grounding tool — prefer it for any specific factual claim.",
        {"sql": {"type": "string", "description": "A single SELECT or WITH statement, no semicolon."}},
        ["sql"],
    ),
    _fn(
        "interpret_round_sheet",
        "Parse a copy-pasted race-day / round SPREADSHEET (tab-separated, straight from Google Sheets/Excel) into clean structured results. "
        "Use this WHENEVER the coordinator pastes a wide tabular block with per-race Pos/Pole/Fastest Lap/Penalty/Points columns — do NOT try to read that grid yourself. "
        "Returns each driver's per-race result plus a computed summary: per-race winner, pole-sitter, fastest lap, and penalties; the day standings; team point totals; and who sat out. "
        "Pass the pasted text through VERBATIM (all rows, both header rows, tabs intact). This data is NOT in the DuckDB — it's a fresh round the coordinator is sharing.",
        {"text": {"type": "string", "description": "The raw pasted sheet text, exactly as copied (tabs and all header rows preserved)."}},
        ["text"],
    ),
    _fn(
        "get_driver_career",
        "Convenience read: a driver's career_stats row, their career CPI and current Elo-style Career Rating, plus their per-season weighted-score rank, season type/car, and which seasons they won. Saves multiple queries.",
        {"driver": _DRIVER},
        ["driver"],
    ),
    _fn(
        "season_field_context",
        "Field-level context for ONE season so a driver's numbers are judged against the field they actually beat, not in a vacuum. "
        "Call this before any season-specific verdict, driver-vs-field claim, or cross-driver comparison anchored in a season. "
        "Returns: how many drivers entered and how many scored at all; participation shape (full-season runners vs part-timers, DNS load); "
        "how top-heavy the points were (champion / top-3 share) and the weighted-score spread with any runaway leader; finishing-position "
        "volatility plus penalty/DNS load as the ONLY (indirect) incident proxies; and a per-driver table (points, avg finish, finish stdev, "
        "weighted score, season rank) for cross-driver variance deltas. Accepts 'S17', '17', or 'S18a'.",
        {"season_id": {"type": "string", "description": "Season reference, e.g. 'S17', '17', or 'S18a'."}},
        ["season_id"],
    ),
    _fn(
        "run_detectors",
        "Run the full cup-racing-insights detector library for a driver and get ranked, notability-scored insights (streaks, records, firsts, venue dominance, trajectory, uniqueness, discipline, etc.). Great for 'what's notable / interesting about X'.",
        {
            "driver": _DRIVER,
            "min_score": {"type": "number", "description": "Drop insights below this score (0..1+). Default 0."},
            "limit": {"type": "integer", "description": "Max insights to return. Default 25."},
        },
        ["driver"],
    ),
    _fn(
        "behavioral_profile",
        "Compute results-only behavioral signals for a driver: consistency (per-season finish variance), discipline (penalty/DNS rates), bounce-back after bad races, quali-vs-race (pole-to-win conversion, wins without pole), season-arc (early vs late form), and trajectory. The quantitative half of a psychology read — interpret it, don't treat it as proof.",
        {"driver": _DRIVER},
        ["driver"],
    ),
    _fn(
        "read_profile",
        "Read a driver's stored profile (Summary, Driving Profile, Psychology & Mindset, Observations Log). Do this before discussing a driver in depth or proposing any profile change.",
        {"driver": _DRIVER},
        ["driver"],
    ),
    _fn(
        "read_sources",
        "Read all recorded qualitative material for a driver — interviews, quotes, race-event narratives, pundit takes (with each pundit's lean), and the coordinator's attributed opinions. The qualitative half of a psychology read.",
        {"driver": _DRIVER},
        ["driver"],
    ),
    _fn(
        "read_league_memory",
        "Read league-level notes (recurring themes, open questions, the coordinator's standing interests).",
        {},
    ),
    _fn(
        "append_observation",
        "Append ONE dated, evidence-backed line to a driver's Observations Log. Use only when you've established something materially new and durable. Gated: empty evidence, fluff, and near-duplicates are rejected. Does NOT touch the prose sections.",
        {
            "driver": _DRIVER,
            "observation": {"type": "string", "description": "The durable finding, one sentence."},
            "evidence": {"type": "string", "description": "Concrete backing: a stat/query result, quote, interview, or event. Required."},
            "source": {"type": "string", "enum": ["data", "interview", "quote", "coordinator-opinion", "race-event", "pundit"], "description": "Provenance of the observation."},
            "tags": {"type": "string", "description": "Optional comma-separated tags, e.g. 'consistency, S21'."},
        },
        ["driver", "observation", "evidence"],
    ),
    _fn(
        "propose_summary_update",
        "Propose a rewrite of a driver's stable prose section (Summary, Driving Profile, or Psychology & Mindset). This does NOT apply immediately — the coordinator must approve it. Use sparingly, only for a material, well-supported shift; routine findings go to append_observation instead.",
        {
            "driver": _DRIVER,
            "section": {"type": "string", "enum": ["Summary", "Driving Profile", "Psychology & Mindset"]},
            "proposed_text": {"type": "string", "description": "The full replacement text for the section."},
            "rationale": {"type": "string", "description": "Why this change is warranted now, and the evidence behind it."},
        },
        ["driver", "section", "proposed_text", "rationale"],
    ),
    _fn(
        "update_league_memory",
        "Append a dated league-level note (a recurring theme, open question, or standing interest). Lighter gate than driver observations.",
        {"note": _STR, "evidence": {"type": "string", "description": "Optional backing."}},
        ["note"],
    ),
    _fn(
        "record_correction",
        "Record a durable GROUND-TRUTH fact the coordinator has established — especially when the coordinator corrects something you got wrong. "
        "Unlike append_observation (a per-driver log you must choose to read), a correction is injected into EVERY future turn's system prompt and "
        "must never be contradicted afterward. Use it for load-bearing facts you keep misremembering or over-generalizing (e.g. 'X is NOT the only driver to have done Y'). "
        "State the fact as a complete, self-contained sentence.",
        {
            "fact": {"type": "string", "description": "The corrected fact, self-contained (e.g. 'Josie has been beaten for the WDC by Toby (S16), Lee (S18b), and James — not only James')."},
            "supersedes": {"type": "string", "description": "Optional: the wrong belief this replaces, for context."},
        },
        ["fact"],
    ),
    _fn(
        "record_quote",
        "Record something a driver said, with provenance. Feeds psychology as a quote (not a statistic).",
        {"driver": _DRIVER, "quote": _STR, "occasion": {"type": "string", "description": "Optional context, e.g. 'post-S22 debrief'."}},
        ["driver", "quote"],
    ),
    _fn(
        "note_coordinator_opinion",
        "Record the coordinator's own stated view about a driver, attributed to the coordinator (weighed as opinion, never as a measured fact). Offer this when J voices a notable, durable opinion.",
        {"driver": _DRIVER, "opinion": _STR},
        ["driver", "opinion"],
    ),
    _fn(
        "record_interview",
        "Store an interview transcript under memory/sources, tagging the drivers it concerns. Feeds psychology.",
        {
            "text": {"type": "string", "description": "The interview text."},
            "drivers": {"type": "array", "items": {"type": "string"}, "description": "Drivers the interview concerns."},
            "occasion": {"type": "string", "description": "Optional context/title."},
        },
        ["text"],
    ),
    _fn(
        "record_pundit_take",
        "Record a LEAGUE PUNDIT's published material — an article, interview, snippet, or thought (often built off driver quotes). "
        "Pundits are third-party media voices with a known lean: Cee (critical/contrarian, attacks drivers), Rajesh (positive/optimistic), "
        "Vivienne (neutral, report-oriented). This is NOT the coordinator's opinion and NOT a plain driver quote — use this whenever J relays "
        "something one of the pundits said or wrote. Feeds psychology with provenance and lean attached.",
        {
            "pundit": {"type": "string", "description": "Pundit name (e.g. Cee, Rajesh, Vivienne)."},
            "text": {"type": "string", "description": "The pundit's words — the article/snippet/thought, as relayed."},
            "drivers": {"type": "array", "items": {"type": "string"}, "description": "Drivers the take concerns."},
            "form": {"type": "string", "description": "What form it took: article, interview, snippet, thought, analysis. Default 'take'."},
            "occasion": {"type": "string", "description": "Optional context/title (e.g. 'post-S22 column')."},
        },
        ["pundit", "text"],
    ),
    _fn(
        "record_race_event",
        "Store a narrative of a race event not captured in the structured results (an incident, a battle, a dramatic moment), tagging the drivers involved. Feeds psychology.",
        {
            "season_id": {"type": "string", "description": "e.g. 'S22'."},
            "description": {"type": "string", "description": "What happened."},
            "drivers": {"type": "array", "items": {"type": "string"}, "description": "Drivers involved."},
            "venue": {"type": "string", "description": "Optional venue."},
            "race": {"type": "string", "description": "Optional race identifier."},
        },
        ["season_id", "description"],
    ),
]

# name -> handler(cfg, **kwargs) -> str
DISPATCH: Dict[str, Callable[..., str]] = {
    "list_drivers": data_tools.list_drivers,
    "query_sql": data_tools.query_sql,
    "interpret_round_sheet": data_tools.interpret_round_sheet,
    "get_driver_career": data_tools.get_driver_career,
    "season_field_context": data_tools.season_field_context,
    "run_detectors": data_tools.run_detectors,
    "behavioral_profile": psychology.behavioral_profile,
    "read_profile": profile_tools.read_profile,
    "read_sources": profile_tools.read_sources,
    "read_league_memory": profile_tools.read_league_memory,
    "append_observation": profile_tools.append_observation,
    "update_league_memory": profile_tools.update_league_memory,
    "record_correction": profile_tools.record_correction,
    "record_quote": profile_tools.record_quote,
    "note_coordinator_opinion": profile_tools.note_coordinator_opinion,
    "record_interview": profile_tools.record_interview,
    "record_race_event": profile_tools.record_race_event,
    "record_pundit_take": profile_tools.record_pundit_take,
}

__all__ = ["TOOL_SCHEMAS", "DISPATCH", "SPECIAL_TOOLS", "Config"]
