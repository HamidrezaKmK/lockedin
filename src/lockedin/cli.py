"""Command-line interface for lockedin.

    lockedin serve     # launch the multi-user web UI
    lockedin doctor    # check the default local model (Ollama) is reachable
"""
from __future__ import annotations

import logging
import json

import typer

app = typer.Typer(add_completion=False,
                  help="lockedin — the ultimate research assistant for grad students.")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _dev_auth():
    """Authenticate DEV_USERNAME/DEV_PASSWORD and return (user, home)."""
    import os

    from dotenv import load_dotenv

    from . import auth, paths

    load_dotenv(paths.base_root() / ".env")
    user = (os.environ.get("DEV_USERNAME") or "").strip().lower()
    pw = os.environ.get("DEV_PASSWORD") or ""
    if not user or not pw:
        typer.secho("✗ Set DEV_USERNAME and DEV_PASSWORD in a project-root .env "
                    "(copy .env.example). ", fg="red")
        raise typer.Exit(1)
    if not auth.verify_password(user, pw):
        typer.secho(f"✗ Wrong username or password for '{user}'.", fg="red")
        raise typer.Exit(1)
    return user, paths.user_home(user)


@app.command()
def doctor():
    """Check that the default local model (Ollama / Qwen) is reachable."""
    from . import models, paths

    # health_check needs a workspace to read config from; use a throwaway base context.
    res = models.health_check(paths.base_root())
    mark = "✓" if res["ok"] else "✗"
    color = "green" if res["ok"] else "red"
    typer.secho(f"{mark} {res['message']}", fg=color)
    if not res["ok"] and res.get("active") == "qwen":
        typer.echo("  Hint: `ollama serve` and `ollama pull qwen2.5:7b-instruct`.")
    raise typer.Exit(0 if res["ok"] else 1)


@app.command()
def editguide():
    """Print the canonical report Editing Guide (markdown) to stdout.

    Single source of truth for report-formatting conventions. Needs no auth or .env: it just
    emits static text."""
    from . import reports

    typer.echo(reports.guide_section("Editing Guide"))


@app.command(name="serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1", help="Host/interface to bind."),
    port: int = typer.Option(8000, help="Port for the web server."),
):
    """Launch the local web server. Reads optional runtime settings from .env."""
    from dotenv import load_dotenv

    from . import paths

    load_dotenv(paths.base_root() / ".env")

    from .server import serve

    typer.secho(f"lockedin → http://{host}:{port}   (Ctrl-C to stop)", fg="cyan")
    serve(host=host, port=port)


@app.command()
def devmode():
    """Verify DEV_USERNAME/DEV_PASSWORD (from .env) and print that user's workspace.

    Lets an agent (or you) edit a user's reports directly on disk without running the server,
    gated on the account password. Reads a project-root ``.env`` and exits non-zero on mismatch.
    """
    from . import assets, bubbles, models, paths

    user, home = _dev_auth()
    typer.secho(f"✓ Authenticated as '{user}'.", fg="green")
    with paths.use_root(home):
        model = models.get_active_config(home).active
        # Only approved bubbles have a materialized report workspace to edit; suggested ones
        # are still waiting in the UI and have no pages, so the report assistant ignores them.
        bubs = [b for b in bubbles.all_bubbles() if b.get("approved")]
        asset_metas = assets.list_assets()
        n_assets = len(asset_metas)
        rows = [(b["slug"], b.get("name") or b["slug"], len(bubbles.list_pages(b["slug"])))
                for b in bubs]
        bubbles.refresh_citation_files([b["slug"] for b in bubs])
    typer.echo(f"  workspace:    {home}")
    typer.echo(f"  reports dir:  {home / 'REPORTS'}")
    typer.echo(f"  assets dir:   {home / 'ASSETS'}  ({n_assets} PDFs)")
    typer.echo(f"  active model: {model}")
    typer.echo("  approved bubbles (name — slug; edit .md files under REPORTS/<slug>/pages/):")
    for slug, name, n in rows:
        typer.echo(f"    - {name} — {slug}  ({n} pages; papers: REPORTS/{slug}/_lockedin_papers.md)")
    if not rows:
        typer.echo("    (none approved yet — approve a bubble in the app first)")
    typer.echo("  paper metadata is not embedded here; read only the selected bubble's "
               "REPORTS/<slug>/_lockedin_papers.md when paper details or citations are needed.")


@app.command(name="refresh-asset-metadata")
def refresh_asset_metadata(force: bool = typer.Option(False, "--force",
                                                       help="Re-extract metadata already present."),
                           all_users: bool = typer.Option(False, "--all-users",
                                                           help="Process every registered user workspace.")):
    """Use each account's active model to backfill paper titles and authors."""
    from . import assets, auth, paths, tagger

    if all_users:
        targets=[(rec["username"], paths.user_home(rec["username"])) for rec in auth.list_users()]
    else:
        user, home = _dev_auth()
        targets=[(user, home)]
    processed = 0
    failed = 0
    users = 0
    for user, home in targets:
        if not home.exists():
            continue
        with paths.use_root(home):
            asset_metas = assets.list_assets()
        for meta in asset_metas:
            if meta.get("metadata_extracted") and not force:
                continue
            try:
                tagger.extract_paper_metadata(home, meta["pdf_id"], force=force)
                processed += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                typer.secho(f"! {user}/{meta.get('pdf_id')}: {e}", fg="yellow")
        users += 1
    typer.echo(f"Metadata refresh across {users} users: {processed} processed, {failed} failed.")


@app.command()
def slackbot():
    """Run the Slack bot (socket mode). Reads tokens from .env. Users authenticate on first use."""
    import os

    from dotenv import load_dotenv

    from . import paths

    load_dotenv(paths.base_root() / ".env")  # must happen before slackbot module is imported

    from .slackbot import run

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        typer.secho("Set SLACK_BOT_TOKEN and SLACK_APP_TOKEN in your .env file.", fg="red")
        raise typer.Exit(1)

    typer.secho("Starting lockedin Slack bot …", fg="cyan")
    run(slack_bot_token=bot_token, slack_app_token=app_token)


if __name__ == "__main__":
    app()
