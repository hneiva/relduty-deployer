"""Bringing one project up to date and reading the state of its environments.

Shared by the terminal UI and the `status` command so that both report identically.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

from relduty_deployer.gitcmd import GitClient, GitError
from relduty_deployer.github import GitHubError
from relduty_deployer.models import DeployStatus, Env
from relduty_deployer.projects import Project
from relduty_deployer.strategies import resolve
from relduty_deployer.strategies.base import Strategy, StrategyError
from relduty_deployer.versions import ProbeError

# Anything a strategy can legitimately fail with becomes a red environment rather than an
# unhandled exception. Anything else is a bug and is allowed to propagate.
EXPECTED_FAILURES = (GitError, StrategyError, GitHubError, ProbeError)


@dataclass(frozen=True)
class ProjectStatus:
    """The state of every environment of one project, or why it could not be read."""

    project: Project
    statuses: Mapping[Env, DeployStatus] = field(default_factory=dict)
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


def strategy_for(project: Project, strategies: Mapping[str, Strategy]) -> Strategy | None:
    """The project's strategy, or None if it names one that is not registered."""
    try:
        return resolve(strategies, project)
    except StrategyError:
        return None


async def refresh(project: Project, strategy: Strategy, git: GitClient, *, fetch: bool = True) -> ProjectStatus:
    """Fetch the project's remote, then read each environment's status.

    A fetch failure is reported for the whole project, since without up-to-date
    remote-tracking refs every environment's answer would be stale and misleading. A
    per-environment failure only reddens that environment.
    """
    if fetch:
        try:
            await git.fetch(project.settings.path, project.settings.remote, spec=strategy.fetch_spec(project))
        except GitError as exc:
            return ProjectStatus(project=project, error=str(exc))

    statuses: dict[Env, DeployStatus] = {}
    for env in project.spec.environments:
        try:
            statuses[env] = await strategy.status(project, env)
        except EXPECTED_FAILURES as exc:
            statuses[env] = DeployStatus.failed(str(exc))
    return ProjectStatus(project=project, statuses=statuses)


async def refresh_all(
    projects: tuple[Project, ...],
    strategies: Mapping[str, Strategy],
    git: GitClient,
    *,
    fetch: bool = True,
) -> list[ProjectStatus]:
    """Refresh every project concurrently, preserving the given order.

    A project naming an unregistered strategy is reported as failed rather than aborting the
    whole run, so one misconfigured entry cannot hide every other project's status.
    """

    async def one(project: Project) -> ProjectStatus:
        strategy = strategy_for(project, strategies)
        if strategy is None:
            return ProjectStatus(project=project, error=f"unknown strategy {project.spec.strategy!r}")
        return await refresh(project, strategy, git, fetch=fetch)

    return list(await asyncio.gather(*(one(project) for project in projects)))
