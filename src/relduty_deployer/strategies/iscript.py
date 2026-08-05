# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""iscript's deploy status.

iscript is not deployed from its own repository. The mac signers run whatever
scriptworker-scripts revision ronin_puppet pins in `data/common.yaml`, so there are two
separate jobs: moving that pin forward, which is a pull request, and shipping ronin_puppet
itself to the signers, which is an ordinary branch push to `macos-signer-latest`.

There is no staging. The first column bumps the pin instead, which is why this strategy
exists rather than reusing the branch-push one for both halves.
"""

from __future__ import annotations

import re

from relduty_deployer.gitcmd import FetchSpec, GitClient, push_argv
from relduty_deployer.github import GitHubClient
from relduty_deployer.models import ActionKind, DeployAction, DeployResult, DeployStatus, Env, StatusKind
from relduty_deployer.projects import (
    ISCRIPT,
    ISCRIPT_BUMP_TITLE,
    ISCRIPT_PINNED_BRANCH,
    ISCRIPT_PINNED_REPO,
    ISCRIPT_REVISION_FILE,
    ISCRIPT_REVISION_KEY,
    Project,
)
from relduty_deployer.strategies.base import StrategyError, UnsafeDeployError
from relduty_deployer.strategies.branch_push import BranchPushStrategy

BUMP_ENV = Env.STAGING
"""iscript has no staging, so the staging slot carries the revision bump instead."""

BRANCH_PREFIX = "iscript-bump-"

# Matches the pinned revision while keeping its indentation and quoting, so the rewritten
# file differs from the original by exactly the sha. The key appears once, under
# `scriptworker_config._`.
#
# Horizontal whitespace only, never `\s`: `\s*$` at the end would swallow the line's own
# newline and any blank line after it, silently reflowing the file around the change.
_REVISION_LINE = re.compile(
    rf'^(?P<prefix>[ \t]*{re.escape(ISCRIPT_REVISION_KEY)}:[ \t]*)(?P<quote>["\']?)(?P<sha>[0-9a-fA-F]+)(?P=quote)[ \t]*$',
    re.MULTILINE,
)


def read_pinned_revision(content: str) -> str:
    """The scriptworker-scripts revision `content` pins."""
    matches = _REVISION_LINE.findall(content)
    if not matches:
        raise StrategyError(f"no {ISCRIPT_REVISION_KEY} found in {ISCRIPT_REVISION_FILE}")
    if len({sha for _, _, sha in matches}) > 1:
        raise StrategyError(f"{ISCRIPT_REVISION_FILE} pins more than one {ISCRIPT_REVISION_KEY}: {sorted({s for _, _, s in matches})}")
    return matches[0][2]


def replace_pinned_revision(content: str, sha: str) -> str:
    """Rewrite the pinned revision, leaving every other byte of the file alone."""
    updated, count = _REVISION_LINE.subn(rf"\g<prefix>\g<quote>{sha}\g<quote>", content)
    if not count:
        raise StrategyError(f"no {ISCRIPT_REVISION_KEY} found in {ISCRIPT_REVISION_FILE}")
    return updated


def bump_branch(sha: str) -> str:
    """The branch a bump to `sha` is pushed to. Deterministic, so re-running reuses it."""
    return f"{BRANCH_PREFIX}{sha[:7]}"


class IscriptStrategy:
    """Bumps the pinned scriptworker-scripts revision, and ships ronin_puppet to the signers."""

    name = ISCRIPT

    def __init__(self, *, git: GitClient, github: GitHubClient, branch_push: BranchPushStrategy | None = None) -> None:
        self._git = git
        self._github = github
        # Production is an ordinary branch push, so it is delegated rather than reimplemented:
        # that keeps the canonical-remote check and the fast-forward-only rule in one place.
        self._branch_push = branch_push if branch_push is not None else BranchPushStrategy(git=git)

    def fetch_spec(self, project: Project) -> FetchSpec:
        return FetchSpec()

    async def status(self, project: Project, env: Env) -> DeployStatus:
        if env is not BUMP_ENV:
            return await self._branch_push.status(project, env)
        return await self._bump_status(project)

    async def _bump_status(self, project: Project) -> DeployStatus:
        """Whether the pin is current, already has a PR open, or needs one."""
        pinned = await self._pinned_revision(project)
        latest = await self._github.branch_head(ISCRIPT_PINNED_REPO, ISCRIPT_PINNED_BRANCH)
        tooltip = f"{ISCRIPT_REVISION_FILE} pins {pinned[:10]}; {ISCRIPT_PINNED_REPO}@{ISCRIPT_PINNED_BRANCH} is {latest[:10]}"

        if pinned == latest:
            return DeployStatus(kind=StatusKind.UP_TO_DATE, tooltip=tooltip)

        existing = await self._github.open_pull_request(project.github_repo, ISCRIPT_BUMP_TITLE)
        if existing is not None:
            # Offer the open PR rather than a second one that would compete with it.
            return DeployStatus(
                kind=StatusKind.BEHIND,
                detail=f"PR #{existing.number} open",
                action=ActionKind.OPEN_URL,
                url=existing.url,
                tooltip=f"{tooltip}\n{existing.url}",
            )
        return DeployStatus(kind=StatusKind.BEHIND, detail="bump available", action=ActionKind.CREATE_PR, tooltip=tooltip)

    async def _pinned_revision(self, project: Project) -> str:
        """Read from the source ref rather than the working tree, which may be mid-edit."""
        content = await self._git.show_file(project.settings.path, project.source_ref(), ISCRIPT_REVISION_FILE)
        return read_pinned_revision(content)

    async def plan(self, project: Project, env: Env) -> DeployAction:
        if env is not BUMP_ENV:
            return await self._branch_push.plan(project, env)

        status = await self._bump_status(project)
        if status.action is not ActionKind.CREATE_PR:
            raise UnsafeDeployError(f"{project.name}: {status.label}; refusing to build a bump plan")

        pinned = await self._pinned_revision(project)
        latest = await self._github.branch_head(ISCRIPT_PINNED_REPO, ISCRIPT_PINNED_BRANCH)
        branch = bump_branch(latest)
        remote = project.settings.remote
        return DeployAction(
            kind=ActionKind.CREATE_PR,
            description=f"{ISCRIPT_REVISION_KEY}  {pinned[:10]}  →  {latest[:10]}",
            argv=push_argv(project.settings.path, remote, sha="<new commit>", target_branch=branch, dry_run=False),
            sha=latest,
            remote_url=await self._git.remote_url(project.settings.path, remote),
            commits=(),
            documented_equivalent=f"python {ISCRIPT_REVISION_FILE.rsplit('/', 1)[0]}/update-scriptworker-revisions.py",
        )

    async def execute(self, project: Project, env: Env, action: DeployAction, *, dry_run: bool) -> DeployResult:
        if env is not BUMP_ENV:
            return await self._branch_push.execute(project, env, action, dry_run=dry_run)

        if action.kind is not ActionKind.CREATE_PR:
            raise UnsafeDeployError(f"{project.name}: cannot bump with a {action.kind} action")

        latest = action.sha
        branch = bump_branch(latest)
        base = project.spec.source_branch
        remote = project.settings.remote
        path = project.settings.path

        if dry_run:
            # Nothing is written and no pull request is opened: a dry run of a bump can only
            # report what it would do, because a pushed branch is already a side effect.
            pinned = await self._pinned_revision(project)
            return DeployResult(
                ok=True,
                output=(
                    f"would rewrite {ISCRIPT_REVISION_KEY} from {pinned[:10]} to {latest[:10]},\n"
                    f"push the commit to {remote}/{branch},\n"
                    f'and open "{ISCRIPT_BUMP_TITLE}" against {base}'
                ),
                dry_run=True,
            )

        existing = await self._github.open_pull_request(project.github_repo, ISCRIPT_BUMP_TITLE)
        if existing is not None:
            raise UnsafeDeployError(f"{project.name}: PR #{existing.number} already bumps this revision: {existing.url}")

        content = await self._git.show_file(path, project.source_ref(), ISCRIPT_REVISION_FILE)
        updated = replace_pinned_revision(content, latest)
        if updated == content:
            raise UnsafeDeployError(f"{project.name}: {ISCRIPT_REVISION_FILE} already pins {latest[:10]}")

        commit = await self._git.commit_replacing_file(
            path,
            base_ref=project.source_ref(),
            file=ISCRIPT_REVISION_FILE,
            content=updated,
            message=ISCRIPT_BUMP_TITLE,
        )
        pushed = await self._git.push(path, remote, sha=commit, target_branch=branch, dry_run=False)
        if not pushed.ok:
            return pushed

        pull = await self._github.create_pull_request(project.github_repo, head=branch, base=base, title=ISCRIPT_BUMP_TITLE)
        return DeployResult(
            ok=True,
            output=f"{pushed.output}\nopened PR #{pull.number}: {pull.url}",
            argv=pushed.argv,
            dry_run=False,
        )
