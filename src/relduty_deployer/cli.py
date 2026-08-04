"""Command line entry point.

This is the composition root: the only place that builds a concrete git client, GitHub
client, or version probe. Everything else receives them as arguments, which is what lets
the tests substitute fakes.
"""

from __future__ import annotations

import asyncio
import sys

import click

from relduty_deployer import __version__
from relduty_deployer.config import ConfigError, ConfigStore
from relduty_deployer.config import build_projects as build_project_list
from relduty_deployer.gitcmd import SubprocessGitClient
from relduty_deployer.github import GhCliGitHubClient
from relduty_deployer.models import Env
from relduty_deployer.refresh import refresh_all
from relduty_deployer.strategies import build_strategies
from relduty_deployer.versions import HttpxVersionProbe


def _build(config):
    """Construct the collaborators and the strategy registry from a loaded config."""
    git = SubprocessGitClient(fetch_timeout=float(config.fetch_timeout_seconds))
    strategies = build_strategies(git=git, github=GhCliGitHubClient(), versions=HttpxVersionProbe())
    return git, strategies


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="relduty-deployer")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Deploy Mozilla Release Engineering projects to staging and production."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(dashboard)


@main.command()
def dashboard() -> None:
    """Open the deploy dashboard."""
    from relduty_deployer.app import RelDutyApp

    try:
        store = ConfigStore()
        config = store.load()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    git, strategies = _build(config)
    RelDutyApp(store=store, config=config, strategies=strategies, git=git).run()


@main.command()
@click.option("--no-fetch", is_flag=True, help="Report from the refs already in each clone, without fetching first.")
def status(no_fetch: bool) -> None:
    """Print how far each environment is behind, without deploying anything."""
    try:
        store = ConfigStore()
        config = store.load()
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    for note in store.notes:
        click.echo(f"note: {note}", err=True)

    projects = tuple(project for project in build_project_list(config) if project.settings.enabled)
    if not projects:
        raise click.ClickException("no projects are enabled")

    git, strategies = _build(config)
    if not no_fetch:
        click.echo(f"fetching {len(projects)} projects…", err=True)
    results = asyncio.run(refresh_all(projects, strategies, git, fetch=not no_fetch))

    width = max(len(result.project.name) for result in results)
    for result in results:
        if result.failed:
            click.echo(f"{result.project.name:<{width}}  error: {result.error.splitlines()[0]}")
            continue
        parts = [f"{env}: {result.statuses[env].label}" for env in Env if env in result.statuses]
        click.echo(f"{result.project.name:<{width}}  " + "   ".join(parts))


if __name__ == "__main__":
    sys.exit(main())
