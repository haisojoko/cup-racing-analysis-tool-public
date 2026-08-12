# Cup Racing Analysis Tool Function Reference

This document describes the current Python harness and LLM tool surface in
`cup-racing-analysis-tool`. It covers every Python function/method in
`analysis_tool/` and `tests/`, plus the model-facing tools declared in
`analysis_tool/tools/__init__.py`.

## High-level harness

The app wraps deterministic Cup Racing data tools in a manual LLM agent loop.
The deterministic parts establish facts; the model interprets those facts.

1. `load_config()` reads `.env`/environment settings, normalizes the model route,
   and points the app at the source markdown, DuckDB, and memory directories.
2. `buddy rebuild` calls `data.rebuild()`, which parses the league markdown into
   DuckDB via `cup_racing_insights.db.rebuild()` (the five core tables), then
   augments the same DB with `extras.augment_db()` (career CPI + Elo-style Career
   Rating tables) and `telemetry.augment_telemetry()` (a partial per-session
   pace/overtakes/contacts overlay + a coverage table).
3. `buddy chat` starts a terminal REPL. `buddy serve` starts a FastAPI web portal.
   Both use the same agent core.
4. `build_system_prompt()` creates the grounding policy, roster, data-shape rules,
   WSR calibration, field-context rules, memory policy, psychology method, and
   tool-use instructions.
5. `drive_turn()` streams a model reply through LiteLLM, collects tool calls, runs
   tools, appends tool results to the message history, and repeats until the model
   answers without more tool calls.
6. Normal tools dispatch through `DISPATCH`. `propose_summary_update` is special:
   it is intercepted by the agent/session so stable profile prose changes only
   after coordinator approval.
7. Data tools are read-only. `query_sql()` has a SQL guard, and all DB access uses
   `duckdb.connect(..., read_only=True)`.
8. Memory is hybrid. `append_observation()` can auto-add one dated evidence-backed
   observation after a materiality gate. Stable sections (`Summary`,
   `Driving Profile`, `Psychology & Mindset`) only change via approved proposals.
9. The web portal persists model context and a UI transcript in
   `memory/chat_history.json`, streams turns over SSE, and renders pending proposal
   cards for Apply/Decline.

The static browser code in `analysis_tool/web/static/app.js` is not Python, but
it completes the web harness: it loads `/api/history`, posts user text to
`/api/submit_stream`, consumes SSE events (`delta`, `tool`, `proposal`, `error`,
`final`), posts approval decisions to `/api/approve`, and clears chat history via
`/api/clear`.

## Model-facing tool surface

These are the tools visible to the LLM. Each schema is declared with `_fn()` in
`analysis_tool/tools/__init__.py`.

| Tool | Implementation | What it does |
| --- | --- | --- |
| `list_drivers` | `data_tools.list_drivers` | Returns every driver with headline career stats from `career_stats`. Used for roster/name resolution. |
| `query_sql` | `data_tools.query_sql` | Runs one guarded read-only `SELECT`/`WITH` query against DuckDB and returns JSON rows. Reaches every table — the five core ones, the career CPI/Rating tables, and the partial telemetry overlay (the full schema is packed into its tool description). Primary grounding tool for exact facts. |
| `interpret_round_sheet` | `data_tools.interpret_round_sheet` -> `roundsheet.parse_round_sheet` | Parses a pasted tab-separated race-day sheet into structured driver/race results and summaries. Used instead of asking the model to align sparse columns. |
| `get_driver_career` | `data_tools.get_driver_career` | Returns one driver's `career_stats` row, their `career_cpi` and `career_rating` rows, plus per-season WSR/rank, season type/car, and title flags. |
| `season_field_context` | `data_tools.season_field_context` | Returns season-level field depth, participation, competitiveness, incident proxies, and a per-driver comparison table so season takes are judged against context. |
| `run_detectors` | `data_tools.run_detectors` | Runs external `cup_racing_insights` detectors, scores insights, and returns ranked notable findings. |
| `behavioral_profile` | `psychology.behavioral_profile` | Computes results-only behavioral signals: consistency, discipline, bounce-back, quali/race profile, season arc, trajectory. |
| `read_profile` | `profile_tools.read_profile` -> `profiles.read_profile` | Reads a driver's stored markdown profile. |
| `read_sources` | `profile_tools.read_sources` -> `profiles.read_sources` | Reads qualitative source material tagged to a driver: logs, interviews, race events, punditry. |
| `read_league_memory` | `profile_tools.read_league_memory` -> `profiles.read_league_memory` | Reads league-level memory notes. |
| `append_observation` | `profile_tools.append_observation` -> `profiles.append_observation` | Appends one durable observation if evidence, substance, and dedup checks pass. |
| `propose_summary_update` | Special tool, intercepted by `drive_turn()` | Creates an approval proposal for stable profile prose. The tool has no direct dispatch handler. |
| `update_league_memory` | `profile_tools.update_league_memory` -> `profiles.update_league_memory` | Appends a dated league-level note, optionally with evidence. |
| `record_correction` | `profile_tools.record_correction` -> `profiles.record_correction` | Adds a durable ground-truth fact to `corrections.md`, which is injected verbatim into every system prompt (deduped). Use for facts the model keeps getting wrong. |
| `record_quote` | `profile_tools.record_quote` -> `profiles.record_quote` | Records a driver quote as a quote-sourced observation. |
| `note_coordinator_opinion` | `profile_tools.note_coordinator_opinion` -> `profiles.note_coordinator_opinion` | Records J's attributed view as opinion, not fact. |
| `record_interview` | `profile_tools.record_interview` -> `profiles.record_interview` | Stores an interview source file and logs linked driver observations. |
| `record_pundit_take` | `profile_tools.record_pundit_take` -> `profiles.record_pundit_take` | Stores pundit material with pundit name, form, and known lean. |
| `record_race_event` | `profile_tools.record_race_event` -> `profiles.record_race_event` | Stores narrative race-event material not captured by structured race data. |

## `analysis_tool/config.py`

- `PROJECT_ROOT`: Directory containing the project, used to locate `.env`, data,
  output, and memory defaults.
- `_path(env, default)`: Reads an environment variable as a `Path`; falls back to
  `default`. Expands `~`. Used by `load_config()`.
- `_normalize_openrouter_model(model)`: Ensures bare OpenRouter slugs like
  `anthropic/...` become `openrouter/anthropic/...` for LiteLLM. Explicit non-
  OpenRouter provider prefixes are respected.
- `Config`: Dataclass carrying model/API settings (incl. the OpenRouter
  `referer`/`title` attribution headers), data paths (source markdown, the
  `races_dir` telemetry input, DuckDB), memory paths (profiles, sources, league
  memory, `corrections_path`), the SQL row cap, max tool iterations, first-tool
  enforcement, and the transient-fault retry knobs (`max_retries`,
  `retry_base_delay`, `retry_max_delay`).
- `Config.ensure_dirs()`: Creates the DB parent, memory root, profile directory,
  source directories, and punditry/interview/event directories.
- `load_config()`: Loads environment-backed settings into `Config`. It sets the
  OpenRouter API base automatically for `openrouter/` models, enables forced first
  tool calls unless `BUDDY_FORCE_FIRST_TOOL` is falsey, and reads the retry budget
  from `BUDDY_MAX_RETRIES`/`BUDDY_RETRY_BASE_DELAY`/`BUDDY_RETRY_MAX_DELAY` (0
  disables retries).

## `analysis_tool/data.py`

- `rebuild(cfg)`: Ensures directories exist, rebuilds the five core tables from the
  source markdown via `cup_racing_insights.db.rebuild()`, then augments the same DB
  with `extras.augment_db()` (career CPI + Career Rating) and
  `telemetry.augment_telemetry()` (per-session telemetry + coverage). Returns the
  merged per-table row counts.
- `db_exists(cfg)`: Checks whether `cfg.db_path` exists.
- `read_only(cfg)`: Context manager opening a read-only DuckDB connection and
  always closing it. All query/detector code uses this to avoid mutation.
- `list_driver_names(cfg)`: Reads driver names from `career_stats`, ordered by
  points descending. Used by prompts, sessions, and name resolution.
- `weighted_score_calibration(cfg)`: Computes distribution anchors from
  `weighted_scores` for prompt calibration: count, median, mean, p75, p90, max,
  all-time best driver/season, and champion score range. Returns `None` if the DB
  is missing or incompatible.
- `resolve_season(cfg, season_id)`: Resolves free-text season references to a
  canonical `season_id`. Handles exact IDs, lowercase IDs, bare numbers such as
  `17` -> `S17`, and unique substring matches.
- `resolve_driver(cfg, name)`: Resolves free text to a canonical driver name.
  Tries case-insensitive exact match first, then a unique substring match.

## `analysis_tool/extras.py`

Parses the two newer career-metric sections of the data markdown that the core
`cup_racing_insights` parser ignores, and loads them into the DuckDB as extra
tables (`career_cpi`, `career_rating`, `rating_trajectory`). Percentages are stored
as fractions to match the core-table convention.

- `_split_row(line)`: Splits one markdown table row on `|`, trimming the outer pipes
  and each cell.
- `_is_separator(line)`: True for a markdown `---|---` separator row.
- `_num(cell)`: Parses a numeric cell; a trailing `%` becomes a fraction; blank/NA
  returns `None`.
- `_int(cell)`: `_num()` rounded to an int, or `None`.
- `_read_table(lines, header)`: Returns the data rows of the first markdown table
  following a given `## Header`, skipping the column-header and separator rows.
- `parse_extras(data_path)`: Parses the CPI Rankings, Current Ratings, and Rating
  Trajectory tables into `{career_cpi, career_rating, rating_trajectory}` lists of
  row dicts. Missing sections yield empty lists.
- `augment_db(cfg)`: Opens the DuckDB read-write, `CREATE OR REPLACE`s the three
  tables, inserts the parsed rows, and returns per-table row counts. Called right
  after the core rebuild; safe to run repeatedly.

## `analysis_tool/telemetry.py`

Loads the per-session telemetry dataset (`data/races/seasons/*.json`, written by
`cup-racing-race-processor`) into the DuckDB as `race_telemetry` and
`telemetry_coverage`. This is a PARTIAL overlay in its own coordinate system — it
joins to the official tables only by `driver`/`season_id`/`venue_order`, never by
race number or venue text — and coverage is made explicit so the incompleteness is
queryable.

- `_grid_pos(grid, label)`: 1-based start position of a driver in the grid list, or
  `None`.
- `_race_best_median(pace, labels=None)`: Fastest median lap in a race, for a
  track-independent pace-gap baseline; `labels` restricts it to one car class so a
  multi-class field isn't measured against the fastest car overall.
- `_parse_race_rows(season_id, venue, venue_order, event_date, race_num, race, driver_classes=None)`:
  Produces one row per driver in a single telemetry race — pace, overtakes, position
  changes, contacts, grid/finish, qualifying-vs-finish, and a class-scoped
  `pace_gap_to_leader_pct` and field size on multi-class seasons.
- `parse_telemetry(races_dir)`: Parses every season JSON under `races_dir/seasons/`
  into flat `race_telemetry` rows. A missing/empty directory yields an empty list.
- `_coverage_rows(con, race_rows)`: Season-level coverage — telemetry race count vs
  the OFFICIAL race count (read from the already-built `race_results`) — so the gap
  is a first-class, queryable fact.
- `augment_telemetry(cfg)`: Opens the DuckDB read-write, `CREATE OR REPLACE`s both
  telemetry tables, inserts the parsed rows plus coverage, and returns per-table row
  counts. Called right after the core rebuild + extras; safe to run repeatedly; a
  missing dataset yields empty tables.

## `analysis_tool/prompts.py`

Module-level constants hold the large static prompt blocks: `_DATA_LEGEND` (what the
league records, stewards' penalty placements, reverse-grid format, the telemetry
overlay, and multi-class seasons), `_WEIGHTED_SCORE_TIERS`, `_CAREER_METRICS` (WSR
vs CPI vs Career Rating), and `_RATING_SYSTEM` (how the Elo-style rating is computed).

- `_weighted_score_scale(cfg)`: Calls `data.weighted_score_calibration()` and
  formats a WSR interpretation guide. Falls back to hard-coded scale anchors if
  calibration cannot be read.
- `_ground_truth_block(cfg)`: Reads `profiles.correction_facts()` and renders the
  coordinator's curated corrections as a GROUND-TRUTH block the model must never
  contradict. Empty string when there are no corrections on file.
- `build_system_prompt(cfg, roster)`: Builds the full system prompt. It injects
  today's date, coordinator identity rules, the ground-truth corrections block, tool
  grounding rules, the superlatives/universals guardrails, the data legend (incl. the
  telemetry overlay and multi-class rules), WSR scale, the three career metrics and
  the rating system, round-sheet handling, field-context instructions, psychology
  policy, pundit attribution, memory policy, voice guidance, and current roster.

## `analysis_tool/llm.py`

Two module-level tuples classify errors: `_FATAL` (auth/permission/not-found/
bad-request/budget — never retried, the request itself is wrong) and `_RETRYABLE`
(timeouts, connection blips, rate limits, provider capacity, and mid-stream faults
via `ServiceUnavailableError`/bare `APIError`). `_NO_FORCED_TOOLS` remembers models
whose provider rejected forced tools so the wasted round-trip is skipped process-wide.

- `_looks_like_forced_tool_rejection(err)`: Heuristic that checks a LiteLLM
  `BadRequestError` string for signs that an upstream provider rejected
  `tool_choice="required"` rather than a genuinely malformed request.
- `_is_retryable(err)`: True only if `err` is in `_RETRYABLE` and not in `_FATAL`
  (fatal is checked first, since most fatal errors subclass `APIError`).
- `_backoff_delay(cfg, attempt)`: Exponential backoff with full jitter, capped at
  `retry_max_delay`.
- `stream_completion(cfg, messages, tools, on_delta=None, tool_choice="auto", on_retry=None)`:
  Public LiteLLM wrapper. Downgrades forced tool choice to `auto` if this model
  already rejected forced tools (recording it in `_NO_FORCED_TOOLS`), and retries
  transient upstream failures with backoff up to `cfg.max_retries`.
  `on_retry(attempt, delay, err, discarded_chunks)` fires before each sleep;
  `discarded_chunks` is how many `on_delta` calls the FAILED attempt made, so the
  caller can drop exactly that many trailing chunks (the retry regenerates the reply
  from scratch).
- `_run_completion(cfg, messages, tools, on_delta, tool_choice, emitted)`: Makes the
  streaming LiteLLM call, sends text deltas to `on_delta` or stdout, accumulates
  content, counts emitted chunks into `emitted[0]`, reconstructs streamed tool-call
  fragments into OpenAI-style `tool_calls`, and returns an assistant message.

## `analysis_tool/tools/__init__.py`

- `SPECIAL_TOOLS`: Set containing `propose_summary_update`, which is handled by
  the approval flow instead of normal dispatch.
- `_fn(name, description, properties, required=None)`: Helper that builds one
  OpenAI-style function tool schema.
- `TOOL_SCHEMAS`: Ordered list of all schemas exposed to the model.
- `DISPATCH`: Map from normal tool name to Python handler. `drive_turn()` uses it
  through `_dispatch_tool()`.

## `analysis_tool/tools/data_tools.py`

- `query_sql(cfg, sql="")`: Strips the query, rejects empty strings, semicolons,
  non-`SELECT`/`WITH` statements, and forbidden write/DDL keywords. Executes via
  `read_only()`, fetches up to `cfg.sql_row_cap + 1`, truncates if needed, and
  returns JSON with columns, rows, row count, and optional truncation note.
- `interpret_round_sheet(cfg, text="")`: Calls `parse_round_sheet()` and returns
  its JSON. Converts `ValueError` into a model-readable error explaining the
  expected Google Sheets/Excel layout.
- `_std(xs)`: Returns population standard deviation rounded to two decimals when
  there are at least two values; otherwise returns `None`.
- `season_field_context(cfg, season_id="")`: Resolves a season and returns JSON
  context for judging season-specific claims. It includes season metadata, total
  race count, field size, scorer/non-scorer counts, full-season vs part-time
  participation, average participation, champion/top-3 points share, WSR top/
  median/bottom/gap, field-level DNS/penalty/finish-volatility proxies, a sorted
  per-driver table, season-local WSR ranks, and a caveat that these are context
  and indirect incident signals rather than proof of contact or strategy.
- `run_detectors(cfg, driver="", min_score=0.0, limit=25)`: Resolves the driver,
  runs `cup_racing_insights.detectors.run_all()`, scores with
  `cup_racing_insights.scoring.score_all()`, filters by score, limits results,
  and returns JSON insight records.
- `list_drivers(cfg)`: Queries selected career columns and returns JSON roster
  records ordered by total points.
- `get_driver_career(cfg, driver="")`: Resolves a driver, fetches their full
  `career_stats` row, joined per-season `weighted_scores`/`seasons` rows, and their
  `career_cpi` and `career_rating` rows, then returns career, cpi, rating and season
  JSON in one payload.
- `_row_dict(con, sql, *params)`: Fetches a single row as a dict, or `{}` if none.
  Tolerates a missing table so an un-augmented DB still works (used for the optional
  CPI/rating reads).

## `analysis_tool/tools/profile_tools.py`

These wrappers canonicalize driver names before calling `profiles.py`, so memory
and data use the same driver spelling when possible.

- `_canon(cfg, driver)`: Returns `resolve_driver()` result, or a non-empty stripped
  input name, or `None`.
- `read_profile(cfg, driver="")`: Resolves the driver and returns their profile,
  or an error if no driver was supplied.
- `read_league_memory(cfg)`: Returns league memory through `profiles`.
- `append_observation(cfg, driver="", observation="", evidence="", source="data", tags="")`:
  Resolves the driver and forwards to the gated profile observation append.
- `update_league_memory(cfg, note="", evidence="")`: Forwards to league memory
  append.
- `record_correction(cfg, fact="", supersedes="")`: Forwards to
  `profiles.record_correction` to add a ground-truth fact that is injected into every
  future system prompt.
- `record_quote(cfg, driver="", quote="", occasion="")`: Resolves the driver and
  records a quote-sourced observation.
- `note_coordinator_opinion(cfg, driver="", opinion="")`: Resolves the driver and
  records a coordinator-opinion observation.
- `record_interview(cfg, text="", drivers=None, occasion="")`: Resolves every
  listed driver and stores the interview source.
- `record_race_event(cfg, season_id="", description="", drivers=None, venue="", race="")`:
  Resolves every listed driver and stores a race-event source.
- `record_pundit_take(cfg, pundit="", text="", drivers=None, form="take", occasion="")`:
  Resolves every listed driver and stores a pundit source with lean metadata.
- `read_sources(cfg, driver="")`: Resolves the driver and returns all qualitative
  sources for them.

## `analysis_tool/tools/psychology.py`

- `_round(x, n=2)`: Rounds numeric values; returns `None` for non-numbers.
- `behavioral_profile(cfg, driver="")`: Resolves the driver and queries their
  race results plus per-season WSR trajectory. It computes totals, penalty/DNS
  rates, per-season average finish/stdev, bounce-back deltas after poor races,
  average finish after DNS, pole-to-win conversion, wins without pole, early-vs-
  late season form, best/worst/latest WSR rank, and a caveat describing the data's
  limits. It returns JSON and leaves interpretation to the model.

## `analysis_tool/roundsheet.py`

- `_cells(line)`: Splits one tab-separated sheet row and strips each cell.
- `_to_int(cell)`: Converts a cell to int, accepting float-looking strings like
  `"1.0"`. Empty or invalid cells become `None`.
- `_is_mark(cell)`: Treats any non-empty, non-zero, non-negative marker as true
  for pole/fastest-lap cells.
- `_split_entry(entry)`: Splits a car/team label into raw `entry`, `car`, and
  trailing-token `team`. Keeps raw value even when the split is imperfect.
- `parse_round_sheet(text)`: Validates and parses a pasted two-header-row sheet.
  It finds the sub-header row by `Pos`/`Points`, derives race labels and total
  column, parses every driver row into per-race result dictionaries, computes
  totals and participation, then returns venue, race count, labels, drivers, and
  `_summarize()` output. Raises `ValueError` for non-sheets.
- `_summarize(race_labels, drivers)`: Computes per-race winner/pole/fastest lap/
  penalties, day standings by points, team point totals, and non-participants.

## `analysis_tool/profiles.py`

- `pundit_lean(name)`: Looks up a pundit's known lean by lowercase name. Unknown
  names return `"unknown lean"`.
- `_today()`: Current local date as ISO string. Used in profile frontmatter and
  memory entries.
- `_stamp()`: Current timestamp as `YYYYMMDD-HHMMSS`. Used for source filenames.
- `slugify(name)`: Lowercases a name, replaces non-alphanumeric runs with hyphens,
  trims hyphens, and falls back to `"unknown"`.
- `_normalize(text)`: Lowercase text normalization for dedup comparisons:
  punctuation to spaces and repeated whitespace collapsed.
- `_obs_core(entry)`: Extracts the durable observation text from a stored log
  line by removing date prefix and evidence/source/tags tail, then normalizes it.
- `profile_path(cfg, driver)`: Returns the markdown profile path for a driver.
- `_split_frontmatter(content)`: Parses simple YAML-ish frontmatter between
  leading `---` markers into a dict and body string. Missing frontmatter returns
  empty dict plus original content.
- `_render_frontmatter(fm)`: Renders known frontmatter keys in stable order:
  `driver`, `created`, `summary_updated`, `last_observation`.
- `_get_section(content, header)`: Extracts text under a `## Header` until the
  next level-2 header.
- `_set_section(content, header, new_body)`: Replaces or appends a level-2 section
  and returns normalized markdown with a trailing newline.
- `_skeleton(driver)`: Creates a new profile template with frontmatter, empty
  stable sections, and an `Observations Log`.
- `ensure_profile(cfg, driver)`: Creates memory directories and a profile skeleton
  if missing, then returns the path.
- `read_profile(cfg, driver)`: Returns profile markdown, or a no-profile message
  without creating a file.
- `get_section_text(cfg, driver, section)`: Reads one profile section, returning
  empty string if the profile is missing.
- `list_observations(content)`: Reads the `Observations Log` section and returns
  bullet text without the leading `- `.
- `materiality_gate(observation, evidence, existing)`: Decides whether a new
  observation is durable enough. Rejects too-short observations, missing evidence,
  transient mood notes backed only by mood/opinion wording, exact/substring
  duplicates, and high-similarity near-duplicates. Returns `(ok, reason)`.
- `append_observation(cfg, driver, observation, evidence, source="data", tags="")`:
  Normalizes the source, ensures the profile exists, runs `materiality_gate()`,
  appends a dated evidence/source/tagged bullet to `Observations Log`, updates
  `last_observation`, writes the file, and returns a status string.
- `apply_summary_update(cfg, driver, section, proposed_text)`: Applies an approved
  replacement to one stable prose section only. Refuses invalid sections, updates
  `summary_updated`, writes the profile, and returns a status string.
- `read_league_memory(cfg)`: Returns league memory markdown or a no-memory message.
- `update_league_memory(cfg, note, evidence="")`: Rejects very short notes,
  creates the league memory file if needed, appends a dated note plus optional
  evidence, and returns a status string.
- `read_corrections(cfg)`: Returns the raw `corrections.md` text (or empty string).
- `correction_facts(cfg)`: Returns just the correction bullet lines (stitching
  wrapped continuations back together), for verbatim injection into the system prompt.
- `record_correction(cfg, fact="", supersedes="")`: Appends a durable ground-truth
  fact to `corrections.md` (deduped, with an optional `supersedes` note). This is the
  always-injected channel, so it overrides the model's recollection on every future
  turn.
- `_write_source_file(directory, kind, drivers, occasion, body, extra=None)`:
  Creates a timestamped source markdown file with frontmatter for kind/date,
  optional extra metadata, tagged drivers, occasion, and body.
- `record_quote(cfg, driver, quote, occasion="")`: Rejects empty quotes and stores
  the quote as a quote-sourced driver observation via `append_observation()`.
- `note_coordinator_opinion(cfg, driver, opinion)`: Rejects too-short opinions and
  stores J's stated view as a coordinator-opinion observation.
- `record_interview(cfg, text, drivers, occasion="")`: Rejects short transcript
  text, writes an interview source file, appends linked interview observations for
  each tagged driver, and returns the relative source path.
- `record_race_event(cfg, season_id, description, drivers, venue="", race="")`:
  Rejects short descriptions, writes an event source file with season/venue/race
  context, appends linked observations, and returns the relative path.
- `record_pundit_take(cfg, pundit, text, drivers, form="take", occasion="")`:
  Requires a pundit name and substantive text, gets the pundit's lean, writes a
  punditry source file with extra frontmatter, appends pundit-sourced driver
  observations, and returns the relative path.
- `_source_files_for(directory, driver)`: Scans source markdown files in one
  directory and returns those whose frontmatter `drivers` list includes the
  lowercase target driver.
- `read_sources(cfg, driver)`: Gathers qualitative profile log entries and full
  tagged source files from interviews, race events, and punditry. Returns a
  no-sources message if nothing is found.

## `analysis_tool/agent.py`

- `_short(v, n=80)`: JSON-serializes non-strings and truncates long values for
  one-line tool summaries.
- `summarize_call(name, args)`: Formats a human-readable tool call. `query_sql`
  shows the SQL directly; other tools show `key=value` argument summaries.
- `_dispatch_tool(cfg, name, args)`: Looks up a normal tool in `DISPATCH`, calls it
  as `fn(cfg, **args)`, and converts unknown tools, `TypeError`, or other
  exceptions into tool-result error strings.
- `drive_turn(cfg, messages, *, approval, on_delta=None, on_tool=None, on_retry=None)`:
  UI-agnostic agent loop. For up to `cfg.max_tool_iterations`, it calls
  `stream_completion()` with `tool_choice="required"` on the first step when
  configured, appends the assistant message, runs each tool call, routes special
  tools through `approval`, appends tool-result messages, and stops when the
  assistant has no tool calls. `on_retry` is forwarded to the LLM layer (retries are
  safe here — a failed attempt raises before its message is appended, so `messages`
  stays valid). If the cap is reached, it emits `LIMIT_SIGNAL` through `on_tool`.
- `proposal_context(cfg, args)`: Resolves proposal driver/section context for
  approval UIs. It canonicalizes the driver, reads the current section text, and
  returns current/proposed/rationale fields.
- `_handle_proposal(cfg, console, args)`: Terminal approval flow. Renders a Rich
  panel showing current text, proposed text, and rationale; applies via
  `profiles.apply_summary_update()` only on `y/yes`.
- `run_turn(cfg, console, messages)`: Terminal adapter for `drive_turn()`. Defines:
  - nested `on_tool(name, args)`: Prints tool activity or iteration-limit notice.
  - nested `approval(cfg_, args)`: Calls `_handle_proposal()`.
  - nested `on_retry(attempt, delay, err, discarded)`: Warns that any partial answer
    already streamed is being discarded, then prints the upstream-hiccup/retry notice.

## `analysis_tool/chat.py`

- `run_chat(cfg=None)`: Terminal REPL. Loads config, ensures directories, rebuilds
  the DB if missing, refuses to continue without an API key, builds roster/system
  prompt, then loops over user input and calls `run_turn()`. Handles quit commands,
  Ctrl-C/EOF, and displays errors.

## `analysis_tool/cli.py`

- `rebuild()`: Typer command for `buddy rebuild`. Loads config, calls
  `data.rebuild()`, and prints row counts.
- `chat()`: Typer command for `buddy chat`. Calls `run_chat()`.
- `serve(port=8770, no_browser=False)`: Typer command for `buddy serve`. Loads
  config, ensures/rebuilds the DB, warns if no API key, creates `AnalysisSession` and
  the FastAPI app, optionally opens the browser, and runs Uvicorn on `127.0.0.1`
  (default port 8770, kept distinct from the LOOM/RPG portal's 8765).

## `analysis_tool/session.py`

- `_sanitize_messages(msgs)`: Repairs persisted model context before sending it
  to OpenRouter. It changes tool-only assistant content to `None`, keeps complete
  assistant-tool-result blocks, drops incomplete tool-call turns, and drops orphan
  tool messages.
- `AnalysisSession.__init__(cfg)`: Creates a single-user persistent session. Ensures
  directories, sets `chat_history.json`, builds roster/system prompt, initializes
  model messages, UI transcript, pending approvals, proposal id counter, a turn
  lock, and then loads saved history.
- `AnalysisSession._load()`: Reads saved history JSON if present. Rebuilds model
  messages with a fresh system prompt, sanitizes non-system messages, restores UI
  transcript, pending proposals, and the current proposal id.
- `AnalysisSession._save()`: Atomically writes messages, transcript, and pending
  proposals to `chat_history.json` via a temp file and replace.
- `AnalysisSession.status()`: Returns model name, driver count, and API-key presence
  for the UI.
- `AnalysisSession.history()`: Returns transcript, pending proposal list, and status.
- `AnalysisSession._pending_list()`: Returns pending proposals as a list of dicts.
- `AnalysisSession._record_pending(args)`: Increments proposal id, builds
  `proposal_context()`, stores the pending proposal, and returns it.
- `AnalysisSession.approve(pid, yes)`: Applies or declines a pending proposal. On yes,
  calls `profiles.apply_summary_update()`; on no, leaves prose unchanged. It logs a
  system transcript note, saves history, and returns updated pending state.
- `AnalysisSession.clear()`: Clears model messages, UI transcript, pending proposals,
  and proposal id counter. Profiles and sources are untouched.
- `AnalysisSession.submit_stream(text)`: Async generator for one web turn. It validates
  text/API key, sanitizes message context, appends the user turn, then starts a
  worker thread that runs `drive_turn()`. It bridges sync callbacks to async SSE
  events through a queue, accumulates assistant text for transcript persistence,
  saves at the end, and yields a final status/pending event. Defines:
  - nested `on_delta(t)`: Stores assistant text parts and queues `delta` events.
  - nested `on_tool(name, args)`: Queues `tool` events using `summarize_call()`.
  - nested `approval(_cfg, args)`: Records a pending proposal, queues a `proposal`
    event, and returns a tool result telling the model the change is awaiting a
    coordinator decision.
  - nested `on_retry(attempt, delay, err, discarded)`: Drops the failed attempt's
    trailing chunks from `parts` (the saved transcript) and queues a `reset` event
    (so the browser clears the live bubble) plus a `notice` event.
  - nested `worker()`: Serializes turns with `_turn_lock`, runs `drive_turn()`,
    queues errors if raised, and signals completion with `_SENTINEL`.

## `analysis_tool/web/app.py`

- `create_app(session)`: Stores the active `AnalysisSession` in module state and
  returns the FastAPI app.
- `SubmitBody`: Pydantic request model for `/api/submit_stream`; contains `text`.
- `ApproveBody`: Pydantic request model for `/api/approve`; contains `id` and
  `yes`.
- `root()`: Serves `static/index.html`.
- `api_history()`: Returns `session.history()` or an initialization error.
- `api_submit_stream(body)`: Returns an SSE `StreamingResponse`. Defines:
  - nested `event_stream()`: Emits an initialization error if needed; otherwise
    iterates `session.submit_stream()` and serializes each event as an SSE frame.
- `api_approve(body)`: Applies or declines a pending proposal through
  `session.approve()`.
- `api_clear()`: Clears conversation history through `session.clear()`.

## `analysis_tool/__init__.py` and `analysis_tool/web/__init__.py`

These modules only contain package docstrings plus `__version__` in
`analysis_tool/__init__.py`; they define no functions.

## `tests/test_analysis_tool.py`

Runs two ways: `pytest -q`, or `python tests/test_analysis_tool.py` (no pytest, via
`_main`). Integration checks require the DuckDB to be built (`buddy rebuild`).

- `_tmp_cfg()`: Builds a real config (real DB/data) but redirects the memory,
  profile, and source paths to a throwaway directory, so tests can mutate memory
  without touching real files.
- `test_query_sql_guard_blocks_writes()`: Verifies `query_sql()` rejects write/DDL
  statements and multi-statement SQL.
- `test_query_sql_allows_select()`: Verifies a simple read query returns JSON with
  expected columns and rows.
- `test_materiality_gate()`: Checks acceptance of a substantive evidenced
  observation and rejection of no-evidence, too-short, and duplicate notes.
- `test_append_observation_roundtrip_and_dedup()`: Confirms observations are
  written to a temporary profile and duplicates are rejected.
- `test_summary_update_and_sources()`: Confirms approved summary updates write to
  a section, and quote/race-event sources are visible through `read_sources()`.
- `test_pundit_take_is_attributed_not_coordinator_opinion()`: Ensures pundit takes
  are stored with pundit attribution/lean and not as coordinator opinions.
- `test_round_sheet_parses_and_summarizes()`: Verifies round-sheet parsing for
  winners, poles, fastest laps, penalties, standings, team totals, and sit-outs.
- `test_round_sheet_rejects_non_sheet()`: Confirms non-sheet input raises
  `ValueError`.
- `test_integration_detectors_and_behavioral()`: Requires a built DB, then checks
  detectors and behavioral profile return usable output for the top driver.
- `test_integration_extra_career_tables()`: Requires a built DB; checks the three
  augmented tables (`career_cpi`, `career_rating`, `rating_trajectory`) exist and are
  populated, that no separator/garbage rows leaked in, that rates are stored as
  fractions (≤ 1.0), and that every rated driver also has a CPI row.
- `test_integration_get_driver_career_includes_new_metrics()`: Requires a built DB;
  confirms `get_driver_career()` returns non-null CPI and rating for the top driver.
- `test_integration_season_field_context()`: Requires a built DB, resolves a real
  season in multiple forms, verifies `season_field_context()` returns coherent field
  counts, total races, sorted per-driver points, season-local WSR rank, a caveat, and
  a clean unknown-season message.
- `_retry_cfg(**over)`: Config with zero-length backoff (so retry tests never sleep)
  and overridable retry knobs.
- `_midstream_error()`: Builds the real-world `MidStreamFallbackError` (a
  ServiceUnavailableError that quotes a `code:400` body) used to exercise retries.
- `test_retry_classification()`: Verifies `llm._is_retryable()` retries transient
  faults (mid-stream, service-unavailable, rate-limit, bare `APIError`) and refuses
  fatal ones (auth, context-window, bad-request).
- `test_retry_recovers_from_midstream_failure()`: A mid-stream drop is retried and
  the turn still succeeds on the second attempt.
- `test_retry_reports_partial_chunks_for_discard()`: The failed attempt's streamed
  chunk count is reported through `on_retry` so the UI can drop exactly those.
- `test_retry_gives_up_after_max_and_respects_zero()`: Retries are bounded by
  `max_retries`, and `max_retries=0` disables them entirely.
- `test_session_discards_only_failed_attempt_text()`: The transcript trim keeps
  earlier-step prose and drops only the failed attempt's tail.
- `_main()`: Lightweight test runner for `python tests/test_analysis_tool.py`;
  discovers callable globals whose names start with `test_`, prints pass/fail, and
  returns process status.
