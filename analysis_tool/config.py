"""Environment-driven configuration. Secrets come from the environment / .env —
never hardcoded. Paths default to the project layout but are all overridable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = the directory that contains the `analysis_tool` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env from the project root if present (does not override real env vars).
load_dotenv(PROJECT_ROOT / ".env")


def _path(env: str, default: Path) -> Path:
    val = os.getenv(env)
    return Path(val).expanduser() if val else default


# OpenRouter's OpenAI-compatible endpoint. LiteLLM defaults to this for
# ``openrouter/`` models, but we keep it explicit so the route is obvious.
OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"


def _normalize_openrouter_model(model: str) -> str:
    """This is an OpenRouter project. OpenRouter's own model slugs are bare
    (``anthropic/claude-3.5-sonnet``) — exactly what you copy off openrouter.ai.
    LiteLLM, however, needs an ``openrouter/`` prefix, or it routes a bare
    ``anthropic/...`` slug to Anthropic-direct and your OpenRouter key 401s.
    So we force everything through OpenRouter unless another explicit LiteLLM
    provider prefix was given.
    """
    m = model.strip()
    if m.startswith("openrouter/"):
        return m
    # A leading non-Anthropic provider prefix means the user deliberately picked
    # a different LiteLLM route; respect it. A bare ``anthropic/...`` slug is the
    # common OpenRouter copy-paste, so that goes through OpenRouter too.
    other_providers = ("openai/", "azure/", "gemini/", "vertex_ai/", "bedrock/",
                       "groq/", "mistral/", "ollama/", "together_ai/", "cohere/")
    if any(m.startswith(p) for p in other_providers):
        return m
    return "openrouter/" + m


@dataclass
class Config:
    model: str
    api_key: str | None
    api_base: str | None
    referer: str | None
    title: str | None
    data_path: Path
    races_dir: Path
    db_path: Path
    memory_dir: Path
    profiles_dir: Path
    sources_dir: Path
    interviews_dir: Path
    events_dir: Path
    punditry_dir: Path
    league_memory_path: Path
    corrections_path: Path
    max_tool_iterations: int
    sql_row_cap: int
    force_first_tool: bool
    max_retries: int
    retry_base_delay: float
    retry_max_delay: float

    def ensure_dirs(self) -> None:
        for d in (
            self.db_path.parent,
            self.memory_dir,
            self.profiles_dir,
            self.sources_dir,
            self.interviews_dir,
            self.events_dir,
            self.punditry_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    memory_dir = _path("BUDDY_MEMORY", PROJECT_ROOT / "memory")
    sources_dir = memory_dir / "sources"
    model = _normalize_openrouter_model(
        os.getenv("BUDDY_MODEL", "anthropic/claude-3.5-sonnet")
    )
    # Default to OpenRouter's base for openrouter/ models; an explicit
    # BUDDY_API_BASE always wins (e.g. a proxy or a self-hosted gateway).
    api_base = os.getenv("BUDDY_API_BASE") or (
        OPENROUTER_API_BASE if model.startswith("openrouter/") else None
    )
    return Config(
        model=model,
        # OpenRouter key only — no OPENAI_API_KEY fallback, since handing an
        # OpenAI key to the OpenRouter endpoint just 401s and masks the real
        # problem. LiteLLM also reads OPENROUTER_API_KEY from the env, but we
        # pass it explicitly so the route is unambiguous.
        api_key=os.getenv("OPENROUTER_API_KEY"),
        api_base=api_base,
        # OpenRouter optionally attributes traffic via these headers.
        referer=os.getenv("OPENROUTER_REFERER", "https://localhost/cup-racing-analysis-tool"),
        title=os.getenv("OPENROUTER_TITLE", "Cup Racing Analysis Tool"),
        data_path=_path("BUDDY_DATA", PROJECT_ROOT / "data" / "Cup_Racing_Complete_Data.md"),
        # Per-session telemetry dataset (lap/pace, overtakes, contacts, grid) produced
        # by cup-racing-race-processor. A PARTIAL overlay on the official results —
        # only sessions whose results.json survived. See telemetry.augment_telemetry.
        races_dir=_path("BUDDY_RACES", PROJECT_ROOT / "data" / "races"),
        db_path=_path("BUDDY_DB", PROJECT_ROOT / "output" / "cup_racing.duckdb"),
        memory_dir=memory_dir,
        profiles_dir=memory_dir / "profiles",
        sources_dir=sources_dir,
        interviews_dir=sources_dir / "interviews",
        events_dir=sources_dir / "events",
        punditry_dir=sources_dir / "punditry",
        league_memory_path=memory_dir / "league.md",
        # Curated ground-truth invariants, injected verbatim into every system
        # prompt (not fetched on demand), so a fact the model keeps getting wrong
        # is always in front of it. See profiles.read_corrections / record_correction.
        corrections_path=memory_dir / "corrections.md",
        max_tool_iterations=int(os.getenv("BUDDY_MAX_TOOL_ITERS", "12")),
        sql_row_cap=int(os.getenv("BUDDY_SQL_ROW_CAP", "200")),
        # Force a tool call before the first reply of each turn (grounding backstop).
        force_first_tool=os.getenv("BUDDY_FORCE_FIRST_TOOL", "1").strip().lower()
        not in ("0", "false", "no", ""),
        # Transient-fault retries per model call (OpenRouter routes to flaky
        # upstream providers; a mid-stream drop is the common failure). 0 disables.
        max_retries=int(os.getenv("BUDDY_MAX_RETRIES", "3")),
        retry_base_delay=float(os.getenv("BUDDY_RETRY_BASE_DELAY", "1.0")),
        retry_max_delay=float(os.getenv("BUDDY_RETRY_MAX_DELAY", "15.0")),
    )
