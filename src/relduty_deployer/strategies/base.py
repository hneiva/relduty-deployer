"""The strategy interface and the errors strategies raise."""

from __future__ import annotations

from typing import Protocol

from relduty_deployer.gitcmd import FetchSpec
from relduty_deployer.models import DeployAction, DeployResult, DeployStatus, Env
from relduty_deployer.projects import Project


class StrategyError(RuntimeError):
    """A strategy could not determine or carry out a deploy."""


class UnsafeDeployError(StrategyError):
    """A deploy was requested that the guardrails refuse to perform."""


class WrongRemoteError(StrategyError):
    """The configured remote does not point at the canonical repository."""


class UnknownStrategyError(StrategyError):
    """A project names a strategy that is not registered."""


class Strategy(Protocol):
    """How one family of projects gets deployed.

    A Protocol rather than a base class: the implementations share no code at all — one
    needs only git, the other also needs GitHub and two HTTP endpoints — and a structural
    interface lets test doubles satisfy it without inheriting anything.
    """

    name: str

    def fetch_spec(self, project: Project) -> FetchSpec:
        """Declare what must be fetched before `status` can be trusted."""
        ...

    async def status(self, project: Project, env: Env) -> DeployStatus:
        """Report the current state of one environment. Must not change anything."""
        ...

    async def plan(self, project: Project, env: Env) -> DeployAction:
        """Resolve exactly what a deploy would do, for the user to approve."""
        ...

    async def execute(self, project: Project, env: Env, action: DeployAction, *, dry_run: bool) -> DeployResult:
        """Carry out an approved action, re-checking safety first."""
        ...
