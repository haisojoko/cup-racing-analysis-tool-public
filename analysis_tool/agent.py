"""The manual agent loop: stream a reply, run any tool calls, repeat until the
model is done. Manual (not an SDK tool-runner) so we can show every tool call for
auditability and pause for coordinator approval on profile prose changes.

`drive_turn` is the UI-agnostic core: it takes callbacks for streamed text
(`on_delta`), tool activity (`on_tool`), and the approval gate (`approval`), so
the same loop powers both the terminal REPL and the web portal. `run_turn` is the
terminal adapter (Rich console + a blocking y/N prompt).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel

from . import profiles
from .config import Config
from .data import resolve_driver
from .llm import stream_completion
from .tools import DISPATCH, SPECIAL_TOOLS, TOOL_SCHEMAS

# Sentinel tool name emitted via on_tool when the iteration cap is hit.
LIMIT_SIGNAL = "__iteration_limit__"


def _short(v: Any, n: int = 80) -> str:
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_call(name: str, args: Dict[str, Any]) -> str:
    """One-line, human-readable description of a tool call (used by both UIs)."""
    if name == "query_sql":
        return f"query_sql: {args.get('sql', '')}"
    summary = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
    return f"{name}({summary})"


def _dispatch_tool(cfg: Config, name: str, args: Dict[str, Any]) -> str:
    fn = DISPATCH.get(name)
    if fn is None:
        return f"ERROR: unknown tool '{name}'."
    try:
        return fn(cfg, **args)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR in {name}: {e}"


def drive_turn(
    cfg: Config,
    messages: List[Dict[str, Any]],
    *,
    approval: Callable[[Config, Dict[str, Any]], str],
    on_delta: Optional[Callable[[str], None]] = None,
    on_tool: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_retry: Optional[Callable[[int, float, Exception, int], None]] = None,
) -> None:
    """Drive one user turn to completion (model reply + any tool round-trips).

    `approval(cfg, args)` is called for `propose_summary_update`-style tools and
    must return the tool-result string the model will see. The terminal blocks on
    a y/N prompt; the web records a pending proposal and returns immediately.

    `on_retry` is forwarded to the LLM layer; see `llm.stream_completion`. Retries
    are safe here because a failed attempt raises before its assistant message is
    appended, so `messages` still holds a valid, unmutated request.
    """
    for step in range(cfg.max_tool_iterations):
        # Force a tool call on the first step of the turn so the model must gather
        # before it can assert anything; let it answer freely afterwards. This is a
        # structural grounding backstop that holds even on weaker models.
        tool_choice = "required" if (step == 0 and cfg.force_first_tool) else "auto"
        assistant = stream_completion(
            cfg, messages, TOOL_SCHEMAS, on_delta=on_delta, tool_choice=tool_choice,
            on_retry=on_retry,
        )
        messages.append(assistant)

        tool_calls = assistant.get("tool_calls")
        if not tool_calls:
            return

        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if on_tool is not None:
                on_tool(name, args)
            if name in SPECIAL_TOOLS:
                result = approval(cfg, args)
            else:
                result = _dispatch_tool(cfg, name, args)
            messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result}
            )

    if on_tool is not None:
        on_tool(LIMIT_SIGNAL, {})


# --------------------------- shared proposal helpers ------------------------ #

def proposal_context(cfg: Config, args: Dict[str, Any]) -> Dict[str, str]:
    """Resolve a proposal's driver/section and the current section text."""
    driver = args.get("driver", "")
    section = args.get("section", "")
    canon = resolve_driver(cfg, driver) or driver
    current = profiles.get_section_text(cfg, canon, section) or "(section currently empty)"
    return {
        "driver": canon,
        "section": section,
        "current": current,
        "proposed": args.get("proposed_text", ""),
        "rationale": args.get("rationale", ""),
    }


# --------------------------- terminal adapter ------------------------------- #

def _handle_proposal(cfg: Config, console: Console, args: Dict[str, Any]) -> str:
    ctx = proposal_context(cfg, args)
    body = (
        f"[bold]Driver:[/] {ctx['driver']}    [bold]Section:[/] {ctx['section']}\n\n"
        f"[bold]Current:[/]\n{ctx['current']}\n\n"
        f"[bold]Proposed:[/]\n{ctx['proposed']}\n\n"
        f"[bold]Rationale:[/] {ctx['rationale']}"
    )
    console.print(Panel(body, title="Profile update proposed — your call", border_style="yellow"))
    answer = console.input("[bold yellow]Apply this update? [y/N] [/]").strip().lower()
    if answer in ("y", "yes"):
        return profiles.apply_summary_update(cfg, ctx["driver"], ctx["section"], ctx["proposed"])
    return "Coordinator DECLINED the proposed update. Profile prose left unchanged."


def run_turn(cfg: Config, console: Console, messages: List[Dict[str, Any]]) -> None:
    """Terminal turn: stream to the console, print tool activity, block on approval."""

    def on_tool(name: str, args: Dict[str, Any]) -> None:
        if name == LIMIT_SIGNAL:
            console.print("(stopped: reached the tool-iteration limit for this turn)", style="dim")
            return
        console.print("→ " + summarize_call(name, args), style="dim", markup=False)

    def approval(cfg_: Config, args: Dict[str, Any]) -> str:
        return _handle_proposal(cfg_, console, args)

    def on_retry(attempt: int, delay: float, err: Exception, discarded: int) -> None:
        # Printed text can't be unprinted; if the failed attempt had already
        # streamed prose, say so, or the fragment reads as part of the answer.
        if discarded > 0:
            console.print(
                "\n(partial answer above is incomplete and is being discarded)",
                style="yellow", markup=False,
            )
        console.print(
            f"(upstream hiccup: {type(err).__name__} — retry {attempt}/{cfg.max_retries} "
            f"in {delay:.1f}s)",
            style="dim", markup=False,
        )

    drive_turn(cfg, messages, approval=approval, on_delta=None, on_tool=on_tool,
               on_retry=on_retry)
