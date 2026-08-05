# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The iscript strategy, entirely offline.

Two halves that behave differently: the first column opens a pull request against
ronin_puppet, and production is an ordinary branch push. The pull request half is the one
worth testing hard, because it is the only place this tool writes a commit.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakes import FakeGitClient

from relduty_deployer.github import PullRequest
from relduty_deployer.models import ActionKind, AheadBehind, DeployAction, Env, StatusKind
from relduty_deployer.projects import (
    ISCRIPT_BUMP_TITLE,
    ISCRIPT_REVISION_FILE,
    SPECS_BY_NAME,
    Project,
    ProjectSettings,
)
from relduty_deployer.strategies.base import StrategyError, UnsafeDeployError
from relduty_deployer.strategies.iscript import (
    IscriptStrategy,
    bump_branch,
    read_pinned_revision,
    replace_pinned_revision,
)

PINNED = "b87f6cef82c69545f77cc0cc6dd38fda86ebb3d1"
LATEST = "3a91c4d0000000000000000000000000000000ff"
REPO = "mozilla-platform-ops/ronin_puppet"
SOURCE_REF = "refs/remotes/origin/master"
TARGET_REF = "refs/remotes/origin/macos-signer-latest"

# Shaped like the real file: the key is nested under `scriptworker_config._`, quoted, and
# surrounded by settings that must survive untouched.
COMMON_YAML = f"""\
scriptworker_config:
    _: &defaults
        sign_chain_of_trust: true
        verify_cot_signature: true
        scriptworker_scripts_revision: "{PINNED}"

    ff-prod:
        <<: *defaults
"""


@dataclass
class FakeGitHubClient:
    """Branch tips and pull requests from an in-memory table."""

    heads: dict[tuple[str, str], str] = field(default_factory=dict)
    open_prs: dict[str, PullRequest] = field(default_factory=dict)
    created: list[tuple[str, str, str, str]] = field(default_factory=list)
    next_number: int = 1502

    async def branch_head(self, repo: str, branch: str) -> str:
        return self.heads[(repo, branch)]

    async def open_pull_request(self, repo: str, title: str) -> PullRequest | None:
        return self.open_prs.get(title)

    async def create_pull_request(self, repo: str, *, head: str, base: str, title: str) -> PullRequest:
        self.created.append((repo, head, base, title))
        pull = PullRequest(number=self.next_number, url=f"https://github.com/{repo}/pull/{self.next_number}", title=title)
        self.open_prs[title] = pull
        return pull


def make(*, pinned=PINNED, latest=LATEST, open_pr=None, behind=0, ahead=0, yaml=None):
    """A strategy wired to fakes, plus the project it acts on."""
    git = FakeGitClient(
        files={(SOURCE_REF, ISCRIPT_REVISION_FILE): (yaml if yaml is not None else COMMON_YAML.replace(PINNED, pinned))},
        shas={SOURCE_REF: "a" * 40, TARGET_REF: "b" * 40},
        counts={(TARGET_REF, SOURCE_REF): AheadBehind(ahead=ahead, behind=behind)},
        commits={(TARGET_REF, SOURCE_REF): ("1111111 puppet change",)},
        default_remote_url=f"git@github.com:{REPO}.git",
    )
    github = FakeGitHubClient(heads={("mozilla-releng/scriptworker-scripts", "master"): latest})
    if open_pr is not None:
        github.open_prs[ISCRIPT_BUMP_TITLE] = open_pr
    project = Project(spec=SPECS_BY_NAME["iscript"], settings=ProjectSettings(path=Path("/repos/ronin_puppet")))
    return IscriptStrategy(git=git, github=github), project, git, github


def test_reading_the_pinned_revision():
    assert read_pinned_revision(COMMON_YAML) == PINNED


def test_reading_a_file_with_no_revision_fails_loudly():
    with pytest.raises(StrategyError, match="no scriptworker_scripts_revision"):
        read_pinned_revision("scriptworker_config:\n    _: {}\n")


def test_two_different_pinned_revisions_are_refused():
    """Rewriting both would be a guess about which one the signers actually read."""
    doubled = COMMON_YAML + '        scriptworker_scripts_revision: "0000000000000000000000000000000000000000"\n'
    with pytest.raises(StrategyError, match="more than one"):
        read_pinned_revision(doubled)


def test_replacing_the_revision_touches_nothing_else():
    updated = replace_pinned_revision(COMMON_YAML, LATEST)

    assert read_pinned_revision(updated) == LATEST
    # Byte-for-byte identical apart from the sha, so the diff in the PR is one line.
    assert updated == COMMON_YAML.replace(PINNED, LATEST)
    assert '        scriptworker_scripts_revision: "' in updated, "indentation and quoting are preserved"


def test_the_branch_name_is_derived_from_the_revision():
    assert bump_branch(LATEST) == "iscript-bump-3a91c4d"
    assert bump_branch(LATEST) == bump_branch(LATEST), "deterministic, so re-running reuses the branch"


async def test_a_current_pin_is_up_to_date():
    strategy, project, *_ = make(pinned=LATEST, latest=LATEST)

    status = await strategy.status(project, Env.STAGING)

    assert status.kind is StatusKind.UP_TO_DATE
    assert status.clickable is False


async def test_a_stale_pin_offers_a_bump():
    strategy, project, *_ = make()

    status = await strategy.status(project, Env.STAGING)

    assert status.label == "bump available"
    assert status.action is ActionKind.CREATE_PR
    assert status.actionable is True
    assert status.deployable is False, "a pull request is not a fast-forward push"


async def test_an_existing_bump_pr_is_offered_instead_of_a_second_one():
    existing = PullRequest(number=1502, url=f"https://github.com/{REPO}/pull/1502", title=ISCRIPT_BUMP_TITLE)
    strategy, project, *_ = make(open_pr=existing)

    status = await strategy.status(project, Env.STAGING)

    assert status.label == "PR #1502 open"
    assert status.action is ActionKind.OPEN_URL
    assert status.url == existing.url


async def test_production_is_an_ordinary_branch_push(tmp_path):
    strategy, project, *_ = make(behind=3)

    status = await strategy.status(project, Env.PROD)

    assert status.label == "3 commits behind"
    assert status.deployable is True


async def test_planning_a_bump_names_both_revisions():
    strategy, project, *_ = make()

    action = await strategy.plan(project, Env.STAGING)

    assert action.kind is ActionKind.CREATE_PR
    assert PINNED[:10] in action.description
    assert LATEST[:10] in action.description
    assert action.sha == LATEST


async def test_planning_a_bump_that_is_not_needed_is_refused():
    strategy, project, *_ = make(pinned=LATEST, latest=LATEST)

    with pytest.raises(UnsafeDeployError, match="refusing to build a bump plan"):
        await strategy.plan(project, Env.STAGING)


async def test_a_dry_run_writes_nothing_and_opens_nothing():
    strategy, project, git, github = make()
    action = await strategy.plan(project, Env.STAGING)

    result = await strategy.execute(project, Env.STAGING, action, dry_run=True)

    assert result.ok and result.dry_run
    assert LATEST[:10] in result.output
    assert git.committed == [], "a dry run must not build a commit"
    assert git.pushed == [], "a pushed branch is already a side effect"
    assert github.created == []


async def test_executing_a_bump_commits_pushes_and_opens_one_pr():
    strategy, project, git, github = make()
    action = await strategy.plan(project, Env.STAGING)

    result = await strategy.execute(project, Env.STAGING, action, dry_run=False)

    file, content, message = git.committed[0]
    assert file == ISCRIPT_REVISION_FILE
    assert read_pinned_revision(content) == LATEST
    assert message == ISCRIPT_BUMP_TITLE, "the commit says the same thing as the pull request"
    assert git.pushed == [("c" * 40, "iscript-bump-3a91c4d", False)]
    assert github.created == [(REPO, "iscript-bump-3a91c4d", "master", ISCRIPT_BUMP_TITLE)]
    assert result.ok
    assert "PR #1502" in result.output


async def test_a_pr_opened_while_the_dialog_was_open_stops_the_bump():
    """The same race the deploy path re-checks for, and the reason execute asks again."""
    strategy, project, git, github = make()
    action = await strategy.plan(project, Env.STAGING)
    github.open_prs[ISCRIPT_BUMP_TITLE] = PullRequest(number=1600, url="https://example.invalid/1600", title=ISCRIPT_BUMP_TITLE)

    with pytest.raises(UnsafeDeployError, match="PR #1600 already bumps"):
        await strategy.execute(project, Env.STAGING, action, dry_run=False)

    assert git.committed == []
    assert git.pushed == []


async def test_a_push_action_cannot_be_used_to_bump():
    strategy, project, git, _github = make()

    with pytest.raises(UnsafeDeployError, match="cannot bump"):
        await strategy.execute(project, Env.STAGING, DeployAction(kind=ActionKind.PUSH, description="x"), dry_run=False)

    assert git.pushed == []


async def test_production_execute_goes_through_the_branch_push_guards():
    strategy, project, git, _github = make(behind=2)
    action = await strategy.plan(project, Env.PROD)

    await strategy.execute(project, Env.PROD, action, dry_run=False)

    assert git.pushed == [("a" * 40, "macos-signer-latest", False)]
    assert git.committed == [], "shipping puppet writes no commit"
