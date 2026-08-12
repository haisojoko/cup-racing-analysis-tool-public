"""Typer entry point. `buddy rebuild` builds the DuckDB; `buddy chat` starts the
interactive partner."""

from __future__ import annotations

import typer

from . import data
from .chat import run_chat
from .config import load_config

app = typer.Typer(add_completion=False, help="Cup Racing Analysis Tool")


@app.command()
def rebuild() -> None:
    """Parse the data markdown into the DuckDB (re-run after data changes)."""
    cfg = load_config()
    counts = data.rebuild(cfg)
    typer.echo(f"Rebuilt {cfg.db_path}")
    for k, v in counts.items():
        typer.echo(f"  {k}: {v}")


@app.command()
def chat() -> None:
    """Start the interactive, fact-grounded chat partner."""
    run_chat()


@app.command()
def serve(
    port: int = typer.Option(8770, help="Port to bind on localhost (distinct from the LOOM/RPG portal's 8765 so their browser caches never collide)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't auto-open the browser."),
) -> None:
    """Launch the browser portal (no paste limit; always shows chat history)."""
    import webbrowser

    import uvicorn

    from .session import AnalysisSession
    from .web.app import create_app

    cfg = load_config()
    cfg.ensure_dirs()
    if not data.db_exists(cfg):
        typer.echo("No database yet — building it from the data file…")
        counts = data.rebuild(cfg)
        typer.echo("Built " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if not cfg.api_key:
        typer.echo("WARNING: no OPENROUTER_API_KEY set — the portal loads but chat will error.")

    session = AnalysisSession(cfg)
    web_app = create_app(session)
    url = f"http://127.0.0.1:{port}"
    typer.echo(f"\nCup Racing Analysis Tool — portal at {url}")
    typer.echo(f"Model: {cfg.model}  ·  Drivers: {len(session.roster)}  ·  Ctrl-C to stop.")
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
