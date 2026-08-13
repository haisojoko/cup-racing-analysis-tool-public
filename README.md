# Cup Racing Analysis Tool

A conversational, **fact-grounded** AI analysis tool for the Cup Racing league
coordinator. It discusses the league's history, gives opinions backed by the data
(not vibes), reads driver psychology from behavioral signals plus recorded
qualitative sources, and conservatively maintains per-driver profiles.

It reuses a companion library, **`cup-racing-insights`**, for the data layer
(DuckDB + 47 SQL detectors + notability scoring) and wraps it in an LLM agent with
custom tools. The LLM runs through **LiteLLM** (OpenRouter by default).

> **Note:** `cup-racing-insights` is a separate package and is required to run
> this tool. Point the install step below at your local checkout of it. The real
> league's driver profiles and qualitative sources are **not** included here (they
> are private); fictional stand-ins live in [`examples/`](examples/) so the
> qualitative features are still demonstrable.


## How it stays honest

- Every factual claim comes from a tool call. `query_sql` is read-only (single
  `SELECT`/`WITH`, no DDL) so the agent can audit and never mutate the data.
- **Grounding is enforced structurally, not just requested:** the agent is forced
  to call a tool before its first reply each turn (`BUDDY_FORCE_FIRST_TOOL`, on by
  default), so even a weak/fast model must gather before it can assert a number,
  record, or ranking. The system prompt's hard rule ("no tool call yet this turn →
  no stats/rankings/head-to-heads") backs this up.
- The model is told exactly what the data does and does **not** contain (no lap
  times, weather, strategy, gaps) and not to invent the rest.
- Psychology is **dual-grounded**: quantitative `behavioral_profile` signals +
  qualitative `read_sources` (interviews, quotes, race events, pundit takes, the
  coordinator's attributed opinions), with provenance kept distinct.
- League **pundits** are first-class, biased media voices — Cee (critical/
  contrarian), Rajesh (optimistic), Vivienne (neutral/report-oriented). Their
  articles/interviews/snippets are stored via `record_pundit_take` with the
  pundit's lean attached, so a Cee hit-piece is never mistaken for the
  coordinator's view or a plain driver quote.

## Conservative memory (hybrid)

- `append_observation` auto-commits ONE dated, evidence-tagged line to a driver's
  Observations Log — but only past a materiality gate (needs evidence, rejects
  fluff and near-duplicates).
- The stable prose (Summary / Driving Profile / Psychology & Mindset) changes ONLY
  via `propose_summary_update`, which pauses for your **approval** in the chat.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
# install the data layer without its Playwright/Jinja deps:
.venv/bin/pip install -e ../cup-racing-insights --no-deps
cp .env.example .env        # then set OPENROUTER_API_KEY
```

## Use

```sh
.venv/bin/buddy rebuild     # markdown -> output/cup_racing.duckdb (re-run on data change)
.venv/bin/buddy serve       # browser portal at http://127.0.0.1:8770 (recommended)
.venv/bin/buddy chat        # terminal REPL (auto-builds the DB if missing)
```

### Browser portal (`buddy serve`)

Opens a local page (binds `127.0.0.1` only) with an input box, so you can paste full interviews or long transcripts that a
terminal would truncate at the TTY's ~1KB line cap. The page **always shows the
chat history** (persisted to `memory/chat_history.json`) and renders tool activity
inline. Profile-prose proposals appear as **Apply / Decline cards** instead of the
terminal's blocking y/N prompt; the durable memory model is identical. `Clear chat`
wipes the conversation only — driver profiles and sources are untouched.

Same OpenRouter wiring as the terminal: set `OPENROUTER_API_KEY` and `BUDDY_MODEL`
(a bare OpenRouter slug like `anthropic/claude-3.5-sonnet` is auto-routed through
OpenRouter).

Example prompts:
- "Is Toby underperforming this season versus his career?"
- "Give me a psychological read on Brie — what does the data and what we've recorded say?"
- "Who's the most interesting mid-pack driver right now, and why?"

## Layout

```
analysis_tool/
├── cli.py          # `buddy rebuild` / `buddy chat`
├── chat.py         # Rich REPL
├── agent.py        # manual tool loop + approval flow
├── llm.py          # LiteLLM streaming
├── prompts.py      # system prompt
├── config.py       # env-driven config
├── data.py         # read-only DuckDB access (wraps cup-racing-insights)
├── profiles.py     # materiality gate, profile/league memory, source store
└── tools/          # query_sql, run_detectors, behavioral_profile, memory tools …
memory/
├── league.md
├── profiles/<driver>.md
└── sources/{interviews,events}/
```

Data lives at `data/Cup_Racing_Complete_Data.md`; the DuckDB is built into `output/`.

## License

Code is released under the **MIT License** (see [LICENSE](LICENSE)). The race
results under `data/` describe a real amateur league, are included only so the
tool runs out of the box, and are **not** licensed for reuse or redistribution.
Real driver profiles and qualitative sources are private and not part of this
repo — see [`examples/`](examples/) for fictional stand-ins.
