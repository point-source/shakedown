"""Command-line entrypoint for shakedown. Verbs per PRD §10."""
from __future__ import annotations

import logging
from pathlib import Path

import click

from shakedown import __version__
from shakedown import config as config_module
from shakedown.filesystem import FilesystemError, ensure_same_filesystem

DEFAULT_CONFIG_PATH = Path("/config/shakedown.yaml")

log = logging.getLogger("shakedown")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _load_config(ctx: click.Context, *, check_filesystem: bool = True) -> config_module.Config:
    cfg_path: Path = ctx.obj["config_path"]
    try:
        cfg = config_module.load(cfg_path)
    except config_module.ConfigError as e:
        click.echo(f"error: {e}", err=True)
        ctx.exit(2)
    if check_filesystem:
        try:
            ensure_same_filesystem(cfg.archive_root, cfg.library_root)
        except FilesystemError as e:
            click.echo(f"error: {e}", err=True)
            ctx.exit(2)
    return cfg


@click.group()
@click.version_option(__version__, prog_name="shakedown")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=DEFAULT_CONFIG_PATH,
    show_default=True,
    help="Path to shakedown.yaml.",
)
@click.option("-v", "--verbose", is_flag=True, help="Verbose logging.")
@click.pass_context
def main(ctx: click.Context, config_path: Path, verbose: bool) -> None:
    """Durable mirroring of public music archives onto a local NAS."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.command()
@click.option("--source", "source_filter", help="Restrict to a single source name.")
@click.option("--collection", "collection_filter", help="Restrict to a single collection name.")
@click.option("--dry-run", is_flag=True, help="Show plan without fetching or staging.")
@click.option(
    "--refresh-metadata",
    is_flag=True,
    help="Re-resolve and rewrite metadata.json sidecars for preserve-opted collections "
    "without re-downloading media.",
)
@click.pass_context
def sync(
    ctx: click.Context,
    source_filter: str | None,
    collection_filter: str | None,
    dry_run: bool,
    refresh_metadata: bool,
) -> None:
    """Run a sync now (PRD §9). Default: all configured collections."""
    if dry_run and refresh_metadata:
        click.echo("error: --dry-run and --refresh-metadata are mutually exclusive", err=True)
        ctx.exit(2)
    cfg = _load_config(ctx)
    from shakedown.sync import run_sync

    exit_code = run_sync(
        cfg,
        source_filter=source_filter,
        collection_filter=collection_filter,
        dry_run=dry_run,
        refresh_metadata=refresh_metadata,
    )
    ctx.exit(exit_code)


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Show last run per collection, item counts, disk usage, drift."""
    cfg = _load_config(ctx)
    from shakedown.status import print_status

    print_status(cfg, as_json=as_json)


@main.command()
@click.option("--source", "source_filter", help="Restrict to a single source name.")
@click.option("--collection", "collection_filter", help="Restrict to a single collection name.")
@click.option(
    "--live-handoff",
    is_flag=True,
    help="Permit explicit live handoff validation where supported.",
)
@click.pass_context
def validate(
    ctx: click.Context,
    source_filter: str | None,
    collection_filter: str | None,
    live_handoff: bool,
) -> None:
    """Preflight a configured deployment before a large mirror begins."""
    cfg = _load_config(ctx, check_filesystem=False)
    from shakedown.validate import run_validate

    ctx.exit(
        run_validate(
            cfg,
            source_filter=source_filter,
            collection_filter=collection_filter,
            live_handoff=live_handoff,
        )
    )


@main.command("release-validate")
@click.option(
    "--deterministic-only",
    is_flag=True,
    help="Run only fake-source release scenarios; explicitly skips the real IA seam.",
)
@click.option(
    "--real-source-only",
    is_flag=True,
    help="Run only the pinned real Internet Archive seam.",
)
@click.pass_context
def release_validate(
    ctx: click.Context, deterministic_only: bool, real_source_only: bool
) -> None:
    """Run the opt-in bounded release validation gate."""
    if deterministic_only and real_source_only:
        click.echo("error: choose at most one narrower release validation mode", err=True)
        ctx.exit(2)
    from shakedown.release_validation import run_release_validation

    ctx.exit(
        run_release_validation(
            deterministic=not real_source_only,
            real_source=not deterministic_only,
        )
    )


@main.command()
@click.option("--source", "source_filter")
@click.option("--collection", "collection_filter")
@click.option("--deep", is_flag=True, help="Hash on-disk files and compare to recorded manifests.")
@click.option("--reconform", is_flag=True, help="With --deep, re-fetch drifted files.")
@click.option("--list", "list_drift", is_flag=True, help="Print drifted file paths instead of summary.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt for --reconform.")
@click.pass_context
def verify(
    ctx: click.Context,
    source_filter: str | None,
    collection_filter: str | None,
    deep: bool,
    reconform: bool,
    list_drift: bool,
    yes: bool,
) -> None:
    """Verify on-disk archive against recorded manifests (PRD §5)."""
    if reconform and not deep:
        click.echo("error: --reconform requires --deep", err=True)
        ctx.exit(2)
    cfg = _load_config(ctx)
    from shakedown.verify import run_verify

    exit_code = run_verify(
        cfg,
        source_filter=source_filter,
        collection_filter=collection_filter,
        deep=deep,
        reconform=reconform,
        list_drift=list_drift,
        assume_yes=yes,
    )
    ctx.exit(exit_code)


@main.command()
@click.option("--source", "source_filter")
@click.option("--collection", "collection_filter")
@click.pass_context
def restage(ctx: click.Context, source_filter: str | None, collection_filter: str | None) -> None:
    """Rebuild the staging hardlink tree without re-downloading."""
    cfg = _load_config(ctx)
    from shakedown.restage import run_restage

    exit_code = run_restage(
        cfg, source_filter=source_filter, collection_filter=collection_filter
    )
    ctx.exit(exit_code)


@main.command()
@click.pass_context
def reconcile(ctx: click.Context) -> None:
    """Walk the archive tree and rebuild state DB from scratch (disaster recovery)."""
    cfg = _load_config(ctx)
    from shakedown.reconcile import run_reconcile

    exit_code = run_reconcile(cfg)
    ctx.exit(exit_code)


@main.group()
def item() -> None:
    """Single-item operations."""


@item.command("show")
@click.argument("identifier")
@click.pass_context
def item_show(ctx: click.Context, identifier: str) -> None:
    cfg = _load_config(ctx)
    from shakedown import status as status_module

    status_module.show_item(cfg, identifier)


@item.command("refetch")
@click.argument("identifier")
@click.pass_context
def item_refetch(ctx: click.Context, identifier: str) -> None:
    cfg = _load_config(ctx)
    from shakedown.sync import refetch_item

    ctx.exit(refetch_item(cfg, identifier))


@item.command("forget")
@click.argument("identifier")
@click.pass_context
def item_forget(ctx: click.Context, identifier: str) -> None:
    cfg = _load_config(ctx)
    from shakedown.sync import forget_item

    forget_item(cfg, identifier)


@main.command()
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.pass_context
def serve(ctx: click.Context, host: str, port: int) -> None:
    """Start the optional HTTP control plane (PRD §10)."""
    cfg = _load_config(ctx)
    try:
        from shakedown.server import serve as run_server
    except ImportError as e:
        click.echo(f"error: serve dependencies not installed: {e}", err=True)
        click.echo("install with: pip install 'shakedown[serve]'", err=True)
        ctx.exit(2)
    run_server(cfg, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
