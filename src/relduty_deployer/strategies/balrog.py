# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Balrog's deploy status.

Balrog has no deploy branches. It ships when a GitHub release tagged `v<version>` is
published — the release is what triggers the image build — and it reaches production only
when someone syncs and promotes the rollout in ArgoCD. Neither step is something this tool
should do on the user's behalf, so both environments are reported read-only and both
buttons open the runbook.

Status therefore comes from two places rather than from a branch comparison:

* staging — whether the version declared on the source branch has a published release
* production — what the public Dockerflow endpoints say is actually running
"""

from __future__ import annotations

import tomllib

from relduty_deployer.gitcmd import FetchSpec, GitClient, GitError
from relduty_deployer.github import GitHubClient, GitHubError, GitHubUnavailableError
from relduty_deployer.models import ActionKind, DeployAction, DeployResult, DeployStatus, Env, StatusKind
from relduty_deployer.projects import BALROG, BALROG_DOCS_URL, Project
from relduty_deployer.strategies.base import StrategyError, UnsafeDeployError
from relduty_deployer.versions import DeployedVersion, ProbeError, VersionProbe

VERSION_URLS = {
    Env.STAGING: "https://stage.balrog.nonprod.webservices.mozgcp.net/__version__",
    Env.PROD: "https://aus-api.mozilla.org/__version__",
}

DOCS_ANCHORS = {
    Env.STAGING: "#deploying-to-stage",
    Env.PROD: "#pushing-to-production",
}


class BalrogStrategy:
    """Reports balrog's deploy state without ever changing it."""

    name = BALROG

    def __init__(self, *, git: GitClient, github: GitHubClient, versions: VersionProbe) -> None:
        self._git = git
        self._github = github
        self._versions = versions

    def fetch_spec(self, project: Project) -> FetchSpec:
        """Tags are the deploy trigger here, and a plain fetch may not bring them all."""
        return FetchSpec(tags=True)

    async def status(self, project: Project, env: Env) -> DeployStatus:
        if env is Env.STAGING:
            return await self._staging_status(project)
        return await self._prod_status(project)

    async def plan(self, project: Project, env: Env) -> DeployAction:
        """Balrog is never deployed from here; the action is to read the runbook."""
        return DeployAction(
            kind=ActionKind.OPEN_URL,
            description=f"balrog {env} is deployed by hand",
            url=self._docs_url(env),
        )

    async def execute(self, project: Project, env: Env, action: DeployAction, *, dry_run: bool) -> DeployResult:
        raise UnsafeDeployError(f"balrog {env} is not automated: staging needs a published GitHub release and production needs an ArgoCD sync and promotion")

    def _docs_url(self, env: Env) -> str:
        return f"{BALROG_DOCS_URL}{DOCS_ANCHORS[env]}"

    async def _source_version(self, project: Project) -> str:
        """Read the declared version from the source ref, never from the working tree.

        The checkout may sit on a feature branch whose version is already bumped while the
        fetched source branch still carries the previous one, so the working tree can
        disagree with both the source branch and production.
        """
        blob = await self._git.show_file(project.settings.path, project.source_ref(), "pyproject.toml")
        try:
            version = tomllib.loads(blob)["project"]["version"]
        except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
            raise StrategyError(f"could not read [project].version from {project.source_ref()}:pyproject.toml: {exc}") from exc
        return str(version)

    async def _staging_status(self, project: Project) -> DeployStatus:
        """Green when the version on the source branch has a published release."""
        url = self._docs_url(Env.STAGING)
        version = await self._source_version(project)
        tag = f"v{version}"

        try:
            release = await self._github.published_release(project.github_repo, tag)
        except GitHubUnavailableError as exc:
            # Without gh there is no way to know whether a release was published, so say so
            # rather than guessing.
            return DeployStatus.unimplemented("gh unavailable", tooltip=str(exc))
        except GitHubError as exc:
            return DeployStatus.failed(str(exc))

        if release is not None:
            return DeployStatus(
                kind=StatusKind.UP_TO_DATE,
                action=ActionKind.OPEN_URL,
                url=url,
                detail=tag,
                tooltip=f"{tag} published {release.published_at}",
            )

        behind, note = await self._commits_since_latest_release(project)
        return DeployStatus(
            kind=StatusKind.BEHIND,
            behind=behind,
            action=ActionKind.OPEN_URL,
            url=url,
            detail=f"{tag} unreleased",
            tooltip=f"{project.spec.source_branch} declares {version}, which has no published release. {note}",
        )

    async def _commits_since_latest_release(self, project: Project) -> tuple[int, str]:
        """How far the source branch has moved past the newest published release."""
        try:
            latest = await self._github.latest_release(project.github_repo)
        except GitHubError as exc:
            return 0, f"latest release unknown: {exc}"
        if latest is None:
            return 0, "this repository has no published releases"

        resolved = await self._resolve(project, latest.tag)
        if resolved is None:
            return 0, f"{latest.tag} is the latest release but is not in the local clone, so commits could not be counted"

        try:
            counts = await self._git.ahead_behind(project.settings.path, target_ref=resolved, source_ref=project.source_ref())
        except GitError as exc:
            return 0, f"could not compare against {latest.tag}: {exc}"
        return counts.behind, f"{counts.behind} commits since {latest.tag}"

    async def _prod_status(self, project: Project) -> DeployStatus:
        """Compare what production runs against what staging runs.

        That mirrors the real promotion order — source branch, then stage, then prod — so
        production is up to date exactly when it has caught up with staging.
        """
        url = self._docs_url(Env.PROD)
        try:
            prod = await self._versions.probe(VERSION_URLS[Env.PROD])
            stage = await self._versions.probe(VERSION_URLS[Env.STAGING])
        except ProbeError as exc:
            return DeployStatus.failed(str(exc))

        if prod.version == stage.version:
            return DeployStatus(
                kind=StatusKind.UP_TO_DATE,
                action=ActionKind.OPEN_URL,
                url=url,
                detail=prod.version,
                tooltip=f"production and stage both run {prod.version} ({prod.commit[:10]}); rollout is manual in ArgoCD",
            )

        behind, note = await self._distance(project, prod, stage)
        return DeployStatus(
            kind=StatusKind.BEHIND,
            behind=behind,
            action=ActionKind.OPEN_URL,
            url=url,
            detail=f"prod {prod.version} · stage {stage.version}",
            tooltip=f"{note} Rollout is manual in ArgoCD.",
        )

    async def _distance(self, project: Project, prod: DeployedVersion, stage: DeployedVersion) -> tuple[int, str]:
        """Commits between the production and staging deployments, if they can be resolved."""
        prod_rev = await self._resolve(project, prod.commit, f"v{prod.version}")
        stage_rev = await self._resolve(project, stage.commit, f"v{stage.version}")
        if prod_rev is None or stage_rev is None:
            return 0, f"production runs {prod.version} and stage runs {stage.version}; the commits are not in the local clone, so only versions were compared."

        try:
            counts = await self._git.ahead_behind(project.settings.path, target_ref=prod_rev, source_ref=stage_rev)
        except GitError as exc:
            return 0, f"production runs {prod.version} and stage runs {stage.version}; could not count commits: {exc}."
        return counts.behind, f"production is {counts.behind} commits behind stage."

    async def _resolve(self, project: Project, *candidates: str) -> str | None:
        """The first candidate revision that exists in the local clone.

        A deployed commit is often missing locally — a clone that has not been fetched in a
        while genuinely will not contain what production is running — so callers must be
        able to degrade instead of failing.
        """
        for candidate in candidates:
            if candidate and await self._git.has_commit(project.settings.path, candidate):
                return candidate
        return None
