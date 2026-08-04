"""Deploying by pushing the source branch onto an environment branch.

This is how scriptworker-scripts, shipit, k8s-autoscale, and tooltool all deploy: a push
to the environment's branch on the canonical repository triggers a Taskcluster build,
which CloudOps then rolls out.
"""

from __future__ import annotations

from relduty_deployer.gitcmd import FetchSpec, GitClient, push_argv
from relduty_deployer.models import ActionKind, DeployAction, DeployResult, DeployStatus, Env
from relduty_deployer.projects import BRANCH_PUSH, Project
from relduty_deployer.strategies.base import UnsafeDeployError, WrongRemoteError

DEFAULT_COMMIT_LIMIT = 20


def points_at_repo(url: str, repo: str) -> bool:
    """Whether a git remote URL refers to `repo`, across the SSH and HTTPS spellings.

    Handles `git@github.com:owner/name.git`, `https://github.com/owner/name`, and
    `ssh://git@github.com/owner/name`.
    """
    normalised = url.strip().lower().removesuffix(".git").replace(":", "/")
    return normalised.endswith(f"/{repo.lower()}")


class BranchPushStrategy:
    """Fast-forwards an environment branch to the tip of the source branch."""

    name = BRANCH_PUSH

    def __init__(self, *, git: GitClient, commit_limit: int = DEFAULT_COMMIT_LIMIT) -> None:
        self._git = git
        self._commit_limit = commit_limit

    def fetch_spec(self, project: Project) -> FetchSpec:
        """Branch tips are all this needs; tags are irrelevant to these deploys."""
        return FetchSpec()

    async def status(self, project: Project, env: Env) -> DeployStatus:
        """Compare the environment's branch against the source branch."""
        if env not in project.spec.targets:
            return DeployStatus.unimplemented(f"{project.name} has no {env} branch")

        await self._assert_canonical_remote(project)

        path = project.settings.path
        source_ref = project.source_ref()
        target_ref = project.target_ref(env)
        counts = await self._git.ahead_behind(path, target_ref=target_ref, source_ref=source_ref)
        source_sha = await self._git.rev_parse(path, source_ref)
        target_sha = await self._git.rev_parse(path, target_ref)
        return DeployStatus.from_counts(
            counts,
            tooltip=f"{source_ref} {source_sha[:10]} → {target_ref} {target_sha[:10]}",
        )

    async def plan(self, project: Project, env: Env) -> DeployAction:
        """Resolve the push that would deploy `env`."""
        status = await self.status(project, env)
        if not status.deployable:
            raise UnsafeDeployError(f"{project.name} {env} is {status.label}; refusing to build a deploy plan")

        path = project.settings.path
        remote = project.settings.remote
        source_branch = project.spec.source_branch
        target_branch = project.target(env)
        sha = await self._git.rev_parse(path, project.source_ref())
        commits = await self._git.commit_list(
            path,
            target_ref=project.target_ref(env),
            source_ref=project.source_ref(),
            limit=self._commit_limit,
        )
        return DeployAction(
            kind=ActionKind.PUSH,
            description=f"{remote}  {source_branch} @ {sha[:10]}  →  {target_branch}",
            argv=push_argv(path, remote, sha=sha, target_branch=target_branch, dry_run=False),
            sha=sha,
            commits=commits,
            truncated=max(0, status.behind - len(commits)),
            warning=project.spec.warnings.get(env, ""),
            # Each repo's README documents the branch-name form. It resolves to the same
            # commit whenever the local branch matches its remote-tracking ref, and this
            # tool pushes the sha instead so that what ships is what was measured.
            documented_equivalent=f"git push {remote} {source_branch}:{target_branch}",
        )

    async def execute(self, project: Project, env: Env, action: DeployAction, *, dry_run: bool) -> DeployResult:
        """Push, after re-checking that the deploy is still safe."""
        if action.kind is not ActionKind.PUSH:
            raise UnsafeDeployError(f"{project.name} {env}: cannot push a {action.kind} action")

        if not dry_run:
            # The dialog may have been open for minutes; someone else may have deployed.
            status = await self.status(project, env)
            if not status.deployable:
                raise UnsafeDeployError(f"{project.name} {env} changed while the dialog was open, and is now {status.label}")

        return await self._git.push(
            project.settings.path,
            project.settings.remote,
            sha=action.sha,
            target_branch=project.target(env),
            dry_run=dry_run,
        )

    async def _assert_canonical_remote(self, project: Project) -> None:
        """Refuse to work against a remote that is not the canonical repository.

        Deploys only fire on pushes to the mozilla-releng repo. A push to a personal fork
        succeeds and deploys nothing, which is the quietest way this tool could fail, so
        the mismatch is surfaced as an error rather than left to be discovered later.
        """
        url = await self._git.remote_url(project.settings.path, project.settings.remote)
        if not points_at_repo(url, project.github_repo):
            raise WrongRemoteError(
                f"remote {project.settings.remote!r} is {url}, which is not a clone of {project.github_repo}; pushing there would deploy nothing"
            )
