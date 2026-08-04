"""Deploy strategies and the registry that resolves them."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from relduty_deployer.gitcmd import GitClient
from relduty_deployer.github import GitHubClient
from relduty_deployer.projects import Project
from relduty_deployer.strategies.balrog import BalrogStrategy
from relduty_deployer.strategies.base import (
    Strategy,
    StrategyError,
    UnknownStrategyError,
    UnsafeDeployError,
    WrongRemoteError,
)
from relduty_deployer.strategies.branch_push import BranchPushStrategy
from relduty_deployer.versions import VersionProbe

__all__ = [
    "BalrogStrategy",
    "BranchPushStrategy",
    "Strategy",
    "StrategyError",
    "UnknownStrategyError",
    "UnsafeDeployError",
    "WrongRemoteError",
    "build_strategies",
    "resolve",
]


def build_strategies(*, git: GitClient, github: GitHubClient, versions: VersionProbe) -> Mapping[str, Strategy]:
    """Construct the strategy registry.

    Built once in the composition root and passed down. Deliberately not a module-level
    mutable dict registered into at import time: that would make import order
    load-bearing and leave tests unable to substitute fakes.
    """
    implementations: tuple[Strategy, ...] = (
        BranchPushStrategy(git=git),
        BalrogStrategy(git=git, github=github, versions=versions),
    )
    return MappingProxyType({implementation.name: implementation for implementation in implementations})


def resolve(strategies: Mapping[str, Strategy], project: Project) -> Strategy:
    """Find the strategy a project declares."""
    try:
        return strategies[project.spec.strategy]
    except KeyError:
        raise UnknownStrategyError(f"project {project.name!r} declares strategy {project.spec.strategy!r}; known strategies: {sorted(strategies)}") from None
