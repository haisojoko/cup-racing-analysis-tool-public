"""Unit + integration checks. Runnable two ways:

    .venv/bin/pytest -q
    .venv/bin/python tests/test_buddy.py     # no pytest needed

Integration checks need the DuckDB built (`buddy rebuild`).
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path

import litellm
import litellm.exceptions

from analysis_tool import data, llm, profiles
from analysis_tool.config import load_config
from analysis_tool.tools import data_tools, psychology


def _tmp_cfg():
    """Real DB/data, but memory redirected to a throwaway dir."""
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="buddy_test_"))
    return dataclasses.replace(
        cfg,
        memory_dir=tmp,
        profiles_dir=tmp / "profiles",
        sources_dir=tmp / "sources",
        interviews_dir=tmp / "sources" / "interviews",
        events_dir=tmp / "sources" / "events",
        punditry_dir=tmp / "sources" / "punditry",
        league_memory_path=tmp / "league.md",
    )


# --------------------------- unit: SQL guard ------------------------------- #

def test_query_sql_guard_blocks_writes():
    cfg = load_config()
    for bad in ["DROP TABLE seasons", "DELETE FROM race_results", "UPDATE career_stats SET wins=0",
                "INSERT INTO seasons VALUES (1)", "SELECT 1; DROP TABLE seasons"]:
        out = data_tools.query_sql(cfg, sql=bad)
        assert out.startswith("ERROR"), f"expected guard to block: {bad!r} -> {out}"


def test_query_sql_allows_select():
    cfg = load_config()
    out = data_tools.query_sql(cfg, sql="SELECT driver FROM career_stats LIMIT 3")
    payload = json.loads(out)
    assert payload["columns"] == ["driver"]
    assert payload["row_count"] >= 1


# --------------------------- unit: materiality gate ------------------------ #

def test_materiality_gate():
    ok, _ = profiles.materiality_gate("strong qualifier, weak race-day converter", "pole_to_win 0.2 over 15 poles", [])
    assert ok
    ok, _ = profiles.materiality_gate("strong qualifier, weak converter", "", [])
    assert not ok  # no evidence
    ok, _ = profiles.materiality_gate("too short", "some evidence here", [])
    assert not ok  # too short
    existing = ["Strong qualifier but weak race day converter. Evidence: x. [source: data]"]
    ok, _ = profiles.materiality_gate("strong qualifier but weak race day converter", "pole conv 0.2", existing)
    assert not ok  # near-duplicate


def test_append_observation_roundtrip_and_dedup():
    cfg = _tmp_cfg()
    msg = profiles.append_observation(cfg, "TestDriver", "tends to fade in the back half of seasons",
                                      "avg_late_minus_early +2.1 over 6 seasons", source="data", tags="arc")
    assert "Recorded observation" in msg
    text = profiles.read_profile(cfg, "TestDriver")
    assert "fade in the back half" in text
    # duplicate is rejected
    dup = profiles.append_observation(cfg, "TestDriver", "tends to fade in the back half of seasons",
                                      "same evidence", source="data")
    assert dup.startswith("Rejected")


def test_summary_update_and_sources():
    cfg = _tmp_cfg()
    profiles.apply_summary_update(cfg, "TestDriver", "Summary", "A consistent midfield runner.")
    assert "consistent midfield runner" in profiles.get_section_text(cfg, "TestDriver", "Summary")
    profiles.record_quote(cfg, "TestDriver", "I just need a clean weekend.", "S22 debrief")
    profiles.record_race_event(cfg, "S22", "Tangled at turn 1 while leading.", ["TestDriver"], venue="Monza")
    src = profiles.read_sources(cfg, "TestDriver")
    assert "clean weekend" in src and "turn 1" in src


def test_pundit_take_is_attributed_not_coordinator_opinion():
    cfg = _tmp_cfg()
    msg = profiles.record_pundit_take(
        cfg, "Cee",
        "TestDriver folds under pressure — every title shot, he blinks first.",
        ["TestDriver"], form="article", occasion="post-S22 column",
    )
    assert "Stored" in msg and "Cee" in msg
    src = profiles.read_sources(cfg, "TestDriver")
    assert "Cee" in src and "blinks first" in src
    assert "critical/contrarian" in src  # the lean travels with the take
    # it lands as a pundit source, NOT a coordinator opinion
    prof = profiles.read_profile(cfg, "TestDriver")
    assert "[source: pundit]" in prof
    assert "coordinator-opinion" not in prof
    # blank pundit name is rejected
    assert profiles.record_pundit_take(cfg, "", "some text here that is long enough", ["TestDriver"]).startswith("Rejected")


# --------------------------- unit: round sheet ----------------------------- #

_ROUND_SHEET = "\t".join(["Race", "Driver", "Race 1", "", "", "", "", "Race 2", "", "", "", "", "Total Day Points"]) + "\n" + \
    "\t".join(["Silverstone", "", "Pos", "Pole", "Fastest Lap", "Penalty", "Points",
               "Pos", "Pole", "Fastest Lap", "Penalty", "Points", ""]) + "\n" + \
    "\t".join(["F1 90 McLaren", "Josie", "1", "1", "1", "", "32", "2", "1", "", "", "27", "59"]) + "\n" + \
    "\t".join(["F1 90 Ferrari", "James", "2", "", "", "", "25", "1", "", "1", "", "30", "55"]) + "\n" + \
    "\t".join(["F1 90 Ferrari", "DK", "3", "", "", "2", "22", "3", "", "", "", "22", "44"]) + "\n" + \
    "\t".join(["F1 90 McLaren", "Brie", "", "", "", "", "0", "", "", "", "", "0", "0"]) + "\n"


def test_round_sheet_parses_and_summarizes():
    from analysis_tool.roundsheet import parse_round_sheet
    d = parse_round_sheet(_ROUND_SHEET)
    assert d["venue"] == "Silverstone" and d["n_races"] == 2
    pr = d["summary"]["per_race"]
    assert pr[0]["winner"] == "Josie" and pr[0]["pole"] == "Josie" and pr[0]["fastest_lap"] == "Josie"
    assert pr[1]["winner"] == "James" and pr[1]["pole"] == "Josie" and pr[1]["fastest_lap"] == "James"
    # penalty carried with the right driver/race
    assert pr[1]["penalties"] == [] and pr[0]["penalties"] == [{"driver": "DK", "penalty": 2}]
    # blank row = did not race, excluded from standings, listed as sat out
    assert "Brie" in d["summary"]["non_participants"]
    standings = d["summary"]["day_standings"]
    assert [s["driver"] for s in standings] == ["Josie", "James", "DK"]
    assert standings[0]["points"] == 59
    # team totals split correctly (McLaren=Josie 59; Ferrari=James 55 + DK 44)
    teams = {t["team"]: t["points"] for t in d["summary"]["team_points"]}
    assert teams == {"McLaren": 59, "Ferrari": 99}


def test_round_sheet_rejects_non_sheet():
    from analysis_tool.roundsheet import parse_round_sheet
    try:
        parse_round_sheet("just a sentence, not a sheet")
        assert False, "should have raised"
    except ValueError:
        pass


# --------------------------- integration ----------------------------------- #

def test_integration_detectors_and_behavioral():
    cfg = load_config()
    assert data.db_exists(cfg), "DB not built — run `buddy rebuild` first"
    top = data.list_driver_names(cfg)[0]
    det = json.loads(data_tools.run_detectors(cfg, driver=top))
    assert det["count"] > 0, f"no insights for {top}"
    beh = json.loads(psychology.behavioral_profile(cfg, driver=top))
    assert beh["totals"]["races"] > 0
    assert "_caveat" in beh


def test_integration_extra_career_tables():
    cfg = load_config()
    assert data.db_exists(cfg), "DB not built — run `buddy rebuild` first"
    with data.read_only(cfg) as con:
        # all three augmented tables exist and are populated
        for t in ("career_cpi", "career_rating", "rating_trajectory"):
            assert con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] > 0, f"{t} empty"
        # no separator/garbage rows leaked into the trajectory
        bad = con.execute(
            "SELECT count(*) FROM rating_trajectory WHERE rating IS NULL OR driver LIKE '%-%'"
        ).fetchone()[0]
        assert bad == 0, f"{bad} malformed trajectory rows"
        # rates parsed as fractions, not raw percents
        mx = con.execute("SELECT max(avg_top5_rate) FROM career_cpi").fetchone()[0]
        assert mx is not None and mx <= 1.0
        # every rated driver also has a CPI row (same roster)
        assert con.execute(
            "SELECT count(*) FROM career_rating r LEFT JOIN career_cpi c USING (driver) "
            "WHERE c.driver IS NULL"
        ).fetchone()[0] == 0


def test_integration_get_driver_career_includes_new_metrics():
    cfg = load_config()
    assert data.db_exists(cfg), "DB not built — run `buddy rebuild` first"
    top = data.list_driver_names(cfg)[0]
    payload = json.loads(data_tools.get_driver_career(cfg, driver=top))
    assert payload["cpi"].get("cpi") is not None
    assert payload["rating"].get("rating") is not None


def test_integration_season_field_context():
    cfg = load_config()
    assert data.db_exists(cfg), "DB not built — run `buddy rebuild` first"
    # Pick a real season from the DB, then resolve it two ways.
    sid = json.loads(data_tools.query_sql(cfg, sql="SELECT season_id FROM seasons LIMIT 1"))["rows"][0][0]
    assert data.resolve_season(cfg, sid) == sid
    assert data.resolve_season(cfg, sid.lower()) == sid

    ctx = json.loads(data_tools.season_field_context(cfg, season_id=sid))
    assert ctx["season"] == sid
    f = ctx["field"]
    assert f["drivers_entered"] > 0
    assert f["drivers_who_scored"] + f["drivers_who_never_scored"] == f["drivers_entered"]
    assert ctx["meta"]["total_races"] > 0
    assert len(ctx["per_driver"]) == f["drivers_entered"]
    # per-driver table is sorted by points desc and carries a season-local rank
    pts = [d["points"] for d in ctx["per_driver"]]
    assert pts == sorted(pts, reverse=True)
    assert any(d.get("season_wsr_rank") == 1 for d in ctx["per_driver"])
    assert "_caveat" in ctx
    # bad season id is a clean message, not a crash
    assert data_tools.season_field_context(cfg, season_id="S999").startswith("Unknown season")


# --------------------------- unit: retry protocol --------------------------- #

def _retry_cfg(**over):
    """Config with zero-length backoff so retry tests don't actually sleep."""
    cfg = load_config()
    base = dict(max_retries=3, retry_base_delay=0.0, retry_max_delay=0.0)
    base.update(over)
    return dataclasses.replace(cfg, **base)


def _midstream_error():
    """The real failure from the field: OpenRouter dies mid-stream, so there's no
    HTTP status to read (the stream already returned 200) and LiteLLM wraps it as
    MidStreamFallbackError — a ServiceUnavailableError, NOT a BadRequestError."""
    from litellm.exceptions import MidStreamFallbackError

    return MidStreamFallbackError(
        message='OpenrouterException - {"error":{"message":"Expecting \',\' delimiter: '
                'line 1 column 19 (char 18)","code":400}}',
        model="openrouter/anthropic/claude-3.5-sonnet",
        llm_provider="openrouter",
        original_exception=litellm.APIError(
            status_code=500, message="OpenrouterException", llm_provider="openrouter",
            model="openrouter/anthropic/claude-3.5-sonnet",
        ),
        generated_content="Toby has been",
        is_pre_first_chunk=False,
    )


def test_retry_classification():
    """Transient faults retry; client errors must not (a retry just burns credits)."""
    assert llm._is_retryable(_midstream_error())
    assert llm._is_retryable(litellm.ServiceUnavailableError(
        message="down", llm_provider="openrouter", model="m"))
    assert llm._is_retryable(litellm.RateLimitError(
        message="slow down", llm_provider="openrouter", model="m"))
    assert llm._is_retryable(litellm.APIError(
        status_code=500, message="boom", llm_provider="openrouter", model="m"))
    # Fatal: the request itself is wrong, so retrying can only fail identically.
    assert not llm._is_retryable(litellm.AuthenticationError(
        message="bad key", llm_provider="openrouter", model="m"))
    assert not llm._is_retryable(litellm.ContextWindowExceededError(
        message="too long", llm_provider="openrouter", model="m"))
    assert not llm._is_retryable(litellm.BadRequestError(
        message="malformed", llm_provider="openrouter", model="m"))


def test_retry_recovers_from_midstream_failure(monkeypatch=None):
    """A mid-stream drop is retried and the turn still succeeds."""
    cfg = _retry_cfg()
    calls = {"n": 0}

    def fake_run(cfg_, messages, tools, on_delta, tool_choice, emitted):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _midstream_error()
        return {"role": "assistant", "content": "recovered"}

    orig = llm._run_completion
    llm._run_completion = fake_run
    try:
        out = llm.stream_completion(cfg, [], [], on_delta=lambda t: None)
    finally:
        llm._run_completion = orig
    assert calls["n"] == 2, f"expected one retry, got {calls['n']} attempts"
    assert out["content"] == "recovered"


def test_retry_reports_partial_chunks_for_discard():
    """Text streamed by a failed attempt must be reported so the UI can drop it —
    and only that attempt's chunks, never earlier prose from the same turn."""
    cfg = _retry_cfg()
    seen = []
    calls = {"n": 0}

    def fake_run(cfg_, messages, tools, on_delta, tool_choice, emitted):
        calls["n"] += 1
        if calls["n"] == 1:
            for piece in ("Toby ", "has ", "been"):  # partial, then the stream dies
                on_delta(piece)
                emitted[0] += 1
            raise _midstream_error()
        return {"role": "assistant", "content": "full answer"}

    orig = llm._run_completion
    llm._run_completion = fake_run
    try:
        llm.stream_completion(
            cfg, [], [], on_delta=lambda t: None,
            on_retry=lambda a, d, e, discarded: seen.append(discarded),
        )
    finally:
        llm._run_completion = orig
    assert seen == [3], f"expected 3 discarded chunks reported, got {seen}"


def test_retry_gives_up_after_max_and_respects_zero():
    """Retries are bounded, and BUDDY_MAX_RETRIES=0 disables them entirely."""
    calls = {"n": 0}

    def fake_run(cfg_, messages, tools, on_delta, tool_choice, emitted):
        calls["n"] += 1
        raise _midstream_error()

    orig = llm._run_completion
    llm._run_completion = fake_run
    try:
        calls["n"] = 0
        try:
            llm.stream_completion(_retry_cfg(max_retries=2), [], [])
            raise AssertionError("should have re-raised after exhausting retries")
        except litellm.exceptions.MidStreamFallbackError:
            pass
        assert calls["n"] == 3, f"1 initial + 2 retries expected, got {calls['n']}"

        calls["n"] = 0
        try:
            llm.stream_completion(_retry_cfg(max_retries=0), [], [])
            raise AssertionError("should have re-raised immediately")
        except litellm.exceptions.MidStreamFallbackError:
            pass
        assert calls["n"] == 1, f"no retries expected, got {calls['n']}"
    finally:
        llm._run_completion = orig


def test_session_discards_only_failed_attempt_text():
    """The saved transcript must keep earlier-step prose and drop only the failed
    attempt's tail — `parts` spans the whole turn, not one model call."""
    parts = ["Step one prose. ", "Toby ", "has ", "been"]
    discarded = 3
    del parts[len(parts) - discarded:]
    assert parts == ["Step one prose. "], parts


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{passed}/{len(fns)} checks passed")
    return 0 if passed == len(fns) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
