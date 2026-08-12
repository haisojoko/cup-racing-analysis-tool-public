"""Driver profiles, league memory, and the qualitative source store.

The hybrid update policy lives here:

* `append_observation` is the auto-commit half — it appends ONE dated,
  evidence-tagged line to a driver's Observations Log, but only after passing
  `materiality_gate` (needs evidence, must be substantive, must not duplicate a
  recent entry). This keeps profiles from churning on every passing remark.
* `apply_summary_update` is the approval half — it rewrites the stable prose
  sections (Summary / Driving Profile / Psychology & Mindset). The agent loop
  only calls it after the coordinator approves a proposal, so prose never changes
  silently.

Qualitative sources (interviews, quotes, race events, coordinator opinions) are
recorded with explicit provenance so a quote is never confused with a statistic.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from pathlib import Path
from typing import List, Tuple

from .config import Config

# Prose sections that only change via coordinator-approved proposals.
APPROVAL_GATED_SECTIONS = ["Summary", "Driving Profile", "Psychology & Mindset"]
ALL_SECTIONS = APPROVAL_GATED_SECTIONS + ["Observations Log"]

VALID_SOURCES = {"data", "interview", "quote", "coordinator-opinion", "race-event", "pundit"}

# League pundits — third-party media voices, each with a known lean. Their takes
# are NOT the coordinator's opinions and NOT driver quotes; weigh them with the
# lean in mind. Keyed by lowercase name.
PUNDITS = {
    "cee": "critical/contrarian — builds adversarial narratives and controversial "
           "takes that directly attack drivers; discount the heat, look for the kernel.",
    "rajesh": "positive/optimistic — focuses on driver successes and achievements; "
              "encouraging in tone, so weigh against his upbeat slant.",
    "vivienne": "neutral/report-oriented — cares about the substance of interviews and "
                "quotes over conclusions; her own opinions are sparing and measured.",
}


def pundit_lean(name: str) -> str:
    return PUNDITS.get((name or "").strip().lower(), "unknown lean")

_TRANSIENT = re.compile(
    r"\b(today|right now|at the moment|currently feeling|in a mood|seems off today)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _today() -> str:
    return date.today().isoformat()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "unknown"


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _obs_core(entry: str) -> str:
    """The bare observation text of a stored log line, stripped of the date
    prefix and the Evidence/source/tags tail — so dedup compares like with like.
    """
    s = entry
    if "—" in s:
        s = s.split("—", 1)[1]
    idx = s.lower().find("evidence:")
    if idx != -1:
        s = s[:idx]
    return _normalize(s)


def profile_path(cfg: Config, driver: str) -> Path:
    return cfg.profiles_dir / f"{slugify(driver)}.md"


# --------------------------------------------------------------------------- #
# Frontmatter + section parsing
# --------------------------------------------------------------------------- #

_FM_KEYS = ["driver", "created", "summary_updated", "last_observation"]


def _split_frontmatter(content: str) -> Tuple[dict, str]:
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            fm = {}
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, parts[2].lstrip("\n")
    return {}, content


def _render_frontmatter(fm: dict) -> str:
    lines = ["---"]
    for k in _FM_KEYS:
        if fm.get(k):
            lines.append(f"{k}: {fm[k]}")
    lines.append("---")
    return "\n".join(lines)


def _get_section(content: str, header: str) -> str:
    body: List[str] = []
    capturing = False
    for line in content.splitlines():
        if line.strip() == f"## {header}":
            capturing = True
            continue
        if capturing and line.startswith("## "):
            break
        if capturing:
            body.append(line)
    return "\n".join(body).strip()


def _set_section(content: str, header: str, new_body: str) -> str:
    lines = content.splitlines()
    out: List[str] = []
    i, n = 0, len(lines)
    replaced = False
    while i < n:
        line = lines[i]
        if line.strip() == f"## {header}":
            out.append(line)
            i += 1
            while i < n and not lines[i].startswith("## "):
                i += 1
            out.append(new_body.rstrip())
            out.append("")
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        out.append(f"## {header}")
        out.append(new_body.rstrip())
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- #
# Profile lifecycle
# --------------------------------------------------------------------------- #

def _skeleton(driver: str) -> str:
    today = _today()
    fm = {"driver": driver, "created": today, "summary_updated": today, "last_observation": today}
    return (
        _render_frontmatter(fm) + "\n\n"
        "## Summary\n"
        "_No summary yet. Built over time via coordinator-approved updates._\n\n"
        "## Driving Profile\n"
        "- Strengths: _TBD_\n"
        "- Weaknesses: _TBD_\n"
        "- Tendencies: _TBD_\n\n"
        "## Psychology & Mindset\n"
        "_No psychological read yet._\n\n"
        "## Observations Log\n"
    )


def ensure_profile(cfg: Config, driver: str) -> Path:
    cfg.ensure_dirs()
    p = profile_path(cfg, driver)
    if not p.exists():
        p.write_text(_skeleton(driver), encoding="utf-8")
    return p


def read_profile(cfg: Config, driver: str) -> str:
    p = profile_path(cfg, driver)
    if not p.exists():
        return f"(No profile for {driver} yet — nothing has been recorded.)"
    return p.read_text(encoding="utf-8")


def get_section_text(cfg: Config, driver: str, section: str) -> str:
    p = profile_path(cfg, driver)
    if not p.exists():
        return ""
    return _get_section(p.read_text(encoding="utf-8"), section)


def list_observations(content: str) -> List[str]:
    section = _get_section(content, "Observations Log")
    return [ln[2:].strip() for ln in section.splitlines() if ln.strip().startswith("- ")]


# --------------------------------------------------------------------------- #
# Materiality gate
# --------------------------------------------------------------------------- #

def materiality_gate(observation: str, evidence: str, existing: List[str]) -> Tuple[bool, str]:
    obs = (observation or "").strip()
    ev = (evidence or "").strip()
    if len(obs) < 15:
        return False, "Rejected: observation is too short to be a durable note."
    if not ev:
        return False, (
            "Rejected: an observation needs concrete evidence — a stat/query result, "
            "a driver quote, an interview, a race event, or an attributed opinion."
        )
    if _TRANSIENT.search(obs) and ev.lower().startswith(("coordinator", "feeling", "mood")):
        return False, "Rejected: reads as a transient mood, not a durable trait."
    core_new = _normalize(obs)
    for e in existing:
        core_e = _obs_core(e)
        if not core_e:
            continue
        if core_new in core_e or core_e in core_new:
            return False, "Rejected: near-duplicate of an existing log entry."
        if difflib.SequenceMatcher(None, core_new, core_e).ratio() > 0.85:
            return False, "Rejected: near-duplicate of an existing log entry."
    return True, "ok"


# --------------------------------------------------------------------------- #
# Auto-commit half: observations
# --------------------------------------------------------------------------- #

def append_observation(
    cfg: Config,
    driver: str,
    observation: str,
    evidence: str,
    source: str = "data",
    tags: str = "",
) -> str:
    source = source if source in VALID_SOURCES else "data"
    ensure_profile(cfg, driver)
    p = profile_path(cfg, driver)
    content = p.read_text(encoding="utf-8")

    ok, reason = materiality_gate(observation, evidence, list_observations(content))
    if not ok:
        return reason

    obs = observation.strip().rstrip(".") + "."
    tag_str = f" [tags: {tags}]" if tags.strip() else ""
    entry = f"- {_today()} — {obs} Evidence: {evidence.strip().rstrip('.')}. [source: {source}]{tag_str}"

    log = _get_section(content, "Observations Log")
    new_log = (log + "\n" + entry).strip() if log else entry
    content = _set_section(content, "Observations Log", new_log)

    fm, body = _split_frontmatter(content)
    fm["last_observation"] = _today()
    content = _render_frontmatter(fm) + "\n\n" + body.strip() + "\n"
    p.write_text(content, encoding="utf-8")
    return f"Recorded observation for {driver}:\n{entry}"


# --------------------------------------------------------------------------- #
# Approval half: prose rewrites (called only after coordinator approval)
# --------------------------------------------------------------------------- #

def apply_summary_update(cfg: Config, driver: str, section: str, proposed_text: str) -> str:
    if section not in APPROVAL_GATED_SECTIONS:
        return (
            f"Refused: '{section}' is not an approval-gated prose section. "
            f"Valid sections: {', '.join(APPROVAL_GATED_SECTIONS)}."
        )
    ensure_profile(cfg, driver)
    p = profile_path(cfg, driver)
    content = p.read_text(encoding="utf-8")
    content = _set_section(content, section, proposed_text.strip())
    fm, body = _split_frontmatter(content)
    fm["summary_updated"] = _today()
    content = _render_frontmatter(fm) + "\n\n" + body.strip() + "\n"
    p.write_text(content, encoding="utf-8")
    return f"Updated '{section}' for {driver}."


# --------------------------------------------------------------------------- #
# League memory
# --------------------------------------------------------------------------- #

def read_league_memory(cfg: Config) -> str:
    p = cfg.league_memory_path
    if not p.exists():
        return "(No league memory recorded yet.)"
    return p.read_text(encoding="utf-8")


def update_league_memory(cfg: Config, note: str, evidence: str = "") -> str:
    note = (note or "").strip()
    if len(note) < 10:
        return "Rejected: league note is too short to be useful."
    cfg.ensure_dirs()
    p = cfg.league_memory_path
    if not p.exists():
        p.write_text("# Cup Racing — League Memory\n\n", encoding="utf-8")
    ev = f" Evidence: {evidence.strip().rstrip('.')}." if evidence.strip() else ""
    line = f"- {_today()} — {note.rstrip('.')}.{ev}\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
    return f"Recorded league note:\n{line.strip()}"


# --------------------------------------------------------------------------- #
# Ground-truth corrections (always injected into the system prompt)
# --------------------------------------------------------------------------- #

_CORRECTIONS_HEADER = (
    "# Cup Racing — Ground-Truth Corrections\n\n"
    "Curated, load-bearing facts the coordinator has established. These are\n"
    "injected verbatim into every system prompt as GROUND TRUTH. The model must\n"
    "never contradict a line here. Coordinator-curated; added via record_correction.\n"
)


def read_corrections(cfg: Config) -> str:
    """The raw corrections file (or empty string if none). Used both by the tool
    and by prompt assembly — prompt assembly injects only the bullet lines."""
    p = cfg.corrections_path
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


def correction_facts(cfg: Config) -> List[str]:
    """The correction bullets only (no header/preamble), for prompt injection."""
    facts: List[str] = []
    for raw in read_corrections(cfg).splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("- "):
            facts.append(line.lstrip()[2:].strip())
        elif facts and line.startswith("  ") and line.strip():
            # A wrapped continuation of the previous bullet.
            facts[-1] += " " + line.strip()
    return facts


def record_correction(cfg: Config, fact: str = "", supersedes: str = "") -> str:
    """Add a durable ground-truth fact to corrections.md. Unlike the observation
    log, this goes into the ALWAYS-INJECTED channel, so it overrides the model's
    recollection on every future turn. Use for facts the model keeps getting wrong.
    """
    fact = " ".join((fact or "").split())
    if len(fact) < 10:
        return "Rejected: a correction needs a clear, self-contained fact."
    cfg.ensure_dirs()
    p = cfg.corrections_path
    if not p.exists():
        p.write_text(_CORRECTIONS_HEADER + "\n", encoding="utf-8")
    existing = {f.lower() for f in correction_facts(cfg)}
    if fact.lower() in existing:
        return "Already recorded — that correction is present."
    note = fact.rstrip(".") + "."
    if supersedes.strip():
        note += f" (Supersedes: {supersedes.strip().rstrip('.')}.)"
    with p.open("a", encoding="utf-8") as f:
        f.write(f"- {note}\n")
    return (
        "Recorded as ground truth. It will be injected into every future turn "
        f"and must not be contradicted:\n- {note}"
    )


# --------------------------------------------------------------------------- #
# Qualitative source store (feeds psychology)
# --------------------------------------------------------------------------- #

def _write_source_file(
    directory: Path,
    kind: str,
    drivers: List[str],
    occasion: str,
    body: str,
    extra: dict | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    slug = slugify(occasion or (drivers[0] if drivers else kind))
    path = directory / f"{_stamp()}-{slug}.md"
    fm = ["---", f"kind: {kind}", f"date: {_today()}"]
    for k, v in (extra or {}).items():
        fm.append(f"{k}: {v}")
    fm += [
        f"drivers: {', '.join(drivers) if drivers else ''}",
        f"occasion: {occasion}",
        "---",
        "",
        body.strip(),
        "",
    ]
    path.write_text("\n".join(fm), encoding="utf-8")
    return path


def record_quote(cfg: Config, driver: str, quote: str, occasion: str = "") -> str:
    quote = (quote or "").strip()
    if len(quote) < 5:
        return "Rejected: quote is empty/too short."
    occ = f" ({occasion.strip()})" if occasion.strip() else ""
    return append_observation(
        cfg, driver,
        observation=f'Said: "{quote}"',
        evidence=f'driver quote{occ}',
        source="quote",
        tags="quote",
    )


def note_coordinator_opinion(cfg: Config, driver: str, opinion: str) -> str:
    opinion = (opinion or "").strip()
    if len(opinion) < 10:
        return "Rejected: opinion is too short to record."
    return append_observation(
        cfg, driver,
        observation=f"Coordinator's view: {opinion}",
        evidence="coordinator's stated opinion (not a measured stat)",
        source="coordinator-opinion",
        tags="opinion",
    )


def record_interview(cfg: Config, text: str, drivers: List[str], occasion: str = "") -> str:
    text = (text or "").strip()
    if len(text) < 20:
        return "Rejected: interview text is too short to store."
    drivers = drivers or []
    path = _write_source_file(cfg.interviews_dir, "interview", drivers, occasion, text)
    rel = path.relative_to(cfg.memory_dir)
    for d in drivers:
        append_observation(
            cfg, d,
            observation=f"Gave an interview{(' (' + occasion + ')') if occasion else ''}",
            evidence=f"interview: {rel}",
            source="interview",
            tags="interview",
        )
    who = ", ".join(drivers) if drivers else "no tagged drivers"
    return f"Stored interview at {rel} (drivers: {who})."


def record_race_event(
    cfg: Config,
    season_id: str,
    description: str,
    drivers: List[str],
    venue: str = "",
    race: str = "",
) -> str:
    description = (description or "").strip()
    if len(description) < 15:
        return "Rejected: event description is too short to store."
    drivers = drivers or []
    loc = " ".join(x for x in [season_id, venue, race] if x).strip()
    occasion = loc or season_id
    body = f"**{loc}**\n\n{description}" if loc else description
    path = _write_source_file(cfg.events_dir, "race-event", drivers, occasion, body)
    rel = path.relative_to(cfg.memory_dir)
    for d in drivers:
        append_observation(
            cfg, d,
            observation=f"Involved in a race event at {loc or season_id}",
            evidence=f"race-event: {rel}",
            source="race-event",
            tags="race-event",
        )
    who = ", ".join(drivers) if drivers else "no tagged drivers"
    return f"Stored race event at {rel} (drivers: {who})."


def record_pundit_take(
    cfg: Config,
    pundit: str,
    text: str,
    drivers: List[str],
    form: str = "take",
    occasion: str = "",
) -> str:
    """Store a pundit's published material (article / interview / snippet / thought).

    Attributed to the named pundit with their known lean — distinct from a driver
    quote and from the coordinator's own opinion.
    """
    pundit = (pundit or "").strip()
    text = (text or "").strip()
    if not pundit:
        return "Rejected: which pundit? (e.g. Cee, Rajesh, Vivienne)."
    if len(text) < 15:
        return "Rejected: pundit text is too short to store."
    form = (form or "take").strip().lower()
    drivers = drivers or []
    lean = pundit_lean(pundit)
    title = f"{pundit} — {form}" + (f" ({occasion})" if occasion else "")
    body = f"**{title}**\n_Lean: {lean}_\n\n{text}"
    path = _write_source_file(
        cfg.punditry_dir, "pundit", drivers, occasion or title, body,
        extra={"pundit": pundit, "form": form},
    )
    rel = path.relative_to(cfg.memory_dir)
    for d in drivers:
        append_observation(
            cfg, d,
            observation=f"{pundit} ({form}) weighed in on {d}",
            evidence=f"pundit: {rel} — {pundit}'s lean: {lean}",
            source="pundit",
            tags=f"pundit, {slugify(pundit)}",
        )
    who = ", ".join(drivers) if drivers else "no tagged drivers"
    return f"Stored {pundit}'s {form} at {rel} (drivers: {who})."


def _source_files_for(directory: Path, driver: str) -> List[Path]:
    out = []
    if not directory.exists():
        return out
    target = driver.strip().lower()
    for f in sorted(directory.glob("*.md")):
        fm, _ = _split_frontmatter(f.read_text(encoding="utf-8"))
        tagged = [d.strip().lower() for d in fm.get("drivers", "").split(",") if d.strip()]
        if target in tagged:
            out.append(f)
    return out


def read_sources(cfg: Config, driver: str) -> str:
    """All qualitative material relevant to a driver, for psychology synthesis."""
    parts: List[str] = []

    # Qualitative observations already in the profile log.
    content = read_profile(cfg, driver)
    qual = [
        o for o in list_observations(content)
        if any(f"[source: {s}]" in o for s in
               ("interview", "quote", "coordinator-opinion", "race-event", "pundit"))
    ]
    if qual:
        parts.append("### Qualitative log entries\n" + "\n".join(f"- {o}" for o in qual))

    for label, directory in (
        ("Interviews", cfg.interviews_dir),
        ("Race events", cfg.events_dir),
        ("Punditry", cfg.punditry_dir),
    ):
        files = _source_files_for(directory, driver)
        for f in files:
            parts.append(f"### {label[:-1]}: {f.relative_to(cfg.memory_dir)}\n" + f.read_text(encoding="utf-8").strip())

    if not parts:
        return (f"(No qualitative sources recorded for {driver} yet — quotes, interviews, "
                "race events, pundit takes, or coordinator opinions.)")
    return "\n\n".join(parts)
