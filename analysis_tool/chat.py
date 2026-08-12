"""Interactive Rich REPL. Builds the DB if missing, assembles the system prompt,
and runs the read→reply→tools loop until the coordinator exits.
"""

from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.panel import Panel

from . import data
from .agent import run_turn
from .config import Config, load_config
from .prompts import build_system_prompt

_QUIT = {"exit", "quit", ":q"}


def run_chat(cfg: Optional[Config] = None) -> None:
    cfg = cfg or load_config()
    console = Console()
    cfg.ensure_dirs()

    if not data.db_exists(cfg):
        console.print("No database yet — building it from the data file…", style="yellow")
        counts = data.rebuild(cfg)
        console.print(
            "Built " + cfg.db_path.name + ": " + ", ".join(f"{k}={v}" for k, v in counts.items()),
            style="green",
        )

    if not cfg.api_key:
        console.print(
            Panel(
                "No API key found. Copy .env.example to .env and set OPENROUTER_API_KEY.",
                title="Missing API key",
                border_style="red",
            )
        )
        return

    roster = data.list_driver_names(cfg)
    messages = [{"role": "system", "content": build_system_prompt(cfg, roster)}]

    console.print(
        Panel(
            "[bold]Cup Racing Analysis Tool[/]\n"
            "Fact-grounded analysis partner for the league coordinator.\n"
            f"Model: {cfg.model}  ·  Drivers: {len(roster)}\n"
            "Type 'exit' to quit.",
            border_style="cyan",
        )
    )

    while True:
        try:
            user = console.input("\n[bold cyan]you ›[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye.", style="cyan")
            break
        if not user:
            continue
        if user.lower() in _QUIT:
            console.print("bye.", style="cyan")
            break
        messages.append({"role": "user", "content": user})
        console.print("[bold green]buddy ›[/]")
        try:
            run_turn(cfg, console, messages)
        except KeyboardInterrupt:
            console.print("\n(interrupted)", style="dim")
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Error:[/] {e}")
