"""Memory tools: profile reads, the gated auto-commit observation, league memory,
and qualitative source capture. Prose rewrites go through propose_summary_update,
which the agent loop handles interactively (see agent.py) — not here.
"""

from __future__ import annotations

from typing import List, Optional

from .. import profiles
from ..config import Config
from ..data import resolve_driver


def _canon(cfg: Config, driver: str) -> Optional[str]:
    # Profiles can exist for any name, but prefer the canonical data name so a
    # driver's profile and their stats line up.
    return resolve_driver(cfg, driver) or (driver.strip() or None)


def read_profile(cfg: Config, driver: str = "") -> str:
    name = _canon(cfg, driver)
    if not name:
        return "ERROR: no driver given."
    return profiles.read_profile(cfg, name)


def read_league_memory(cfg: Config) -> str:
    return profiles.read_league_memory(cfg)


def append_observation(
    cfg: Config,
    driver: str = "",
    observation: str = "",
    evidence: str = "",
    source: str = "data",
    tags: str = "",
) -> str:
    name = _canon(cfg, driver)
    if not name:
        return "ERROR: no driver given."
    return profiles.append_observation(cfg, name, observation, evidence, source, tags)


def update_league_memory(cfg: Config, note: str = "", evidence: str = "") -> str:
    return profiles.update_league_memory(cfg, note, evidence)


def record_correction(cfg: Config, fact: str = "", supersedes: str = "") -> str:
    return profiles.record_correction(cfg, fact, supersedes)


def record_quote(cfg: Config, driver: str = "", quote: str = "", occasion: str = "") -> str:
    name = _canon(cfg, driver)
    if not name:
        return "ERROR: no driver given."
    return profiles.record_quote(cfg, name, quote, occasion)


def note_coordinator_opinion(cfg: Config, driver: str = "", opinion: str = "") -> str:
    name = _canon(cfg, driver)
    if not name:
        return "ERROR: no driver given."
    return profiles.note_coordinator_opinion(cfg, name, opinion)


def record_interview(cfg: Config, text: str = "", drivers: Optional[List[str]] = None, occasion: str = "") -> str:
    resolved = [(_canon(cfg, d) or d) for d in (drivers or [])]
    return profiles.record_interview(cfg, text, resolved, occasion)


def record_race_event(
    cfg: Config,
    season_id: str = "",
    description: str = "",
    drivers: Optional[List[str]] = None,
    venue: str = "",
    race: str = "",
) -> str:
    resolved = [(_canon(cfg, d) or d) for d in (drivers or [])]
    return profiles.record_race_event(cfg, season_id, description, resolved, venue, race)


def record_pundit_take(
    cfg: Config,
    pundit: str = "",
    text: str = "",
    drivers: Optional[List[str]] = None,
    form: str = "take",
    occasion: str = "",
) -> str:
    resolved = [(_canon(cfg, d) or d) for d in (drivers or [])]
    return profiles.record_pundit_take(cfg, pundit, text, resolved, form, occasion)


def read_sources(cfg: Config, driver: str = "") -> str:
    name = _canon(cfg, driver)
    if not name:
        return "ERROR: no driver given."
    return profiles.read_sources(cfg, name)
