"""The branch-push strategy, exercised entirely against a fake git."""

from pathlib import Path

import pytest
from fakes import CANONICAL_URL, FakeGitClient

from relduty_deployer.models import ActionKind, AheadBehind, Env, StatusKind
from relduty_deployer.projects import SPECS_BY_NAME, Project, ProjectSettings
from relduty_deployer.strategies import BranchPushStrategy, UnsafeDeployError, WrongRemoteError
from relduty_deployer.strategies.branch_push import points_at_repo

SOURCE = "refs/remotes/origin/master"
STAGING = "refs/remotes/origin/staging"
PROD = "refs/remotes/origin/production"


def make_project(name="tooltool", remote="origin"):
    return Project(spec=SPECS_BY_NAME[name], settings=ProjectSettings(path=Path("/repo"), remote=remote))


def make_git(*, ahead=0, behind=0, name="tooltool", commits=()):
    return FakeGitClient(
        counts={(STAGING, SOURCE): AheadBehind(ahead=ahead, behind=behind)},
        shas={SOURCE: "a" * 40, STAGING: "b" * 40},
        commits={(STAGING, SOURCE): commits},
        default_remote_url=CANONICAL_URL.format(repo=name),
    )


async def test_up_to_date_when_the_branches_match():
    status = await BranchPushStrategy(git=make_git()).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.UP_TO_DATE
    assert status.deployable is False


async def test_behind_is_deployable():
    status = await BranchPushStrategy(git=make_git(behind=4)).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.BEHIND
    assert status.behind == 4
    assert status.label == "4 commits behind"
    assert status.deployable is True


async def test_diverged_is_not_deployable():
    status = await BranchPushStrategy(git=make_git(behind=4, ahead=2)).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.DIVERGED
    assert status.label == "4 behind, 2 ahead"
    assert status.deployable is False


async def test_ahead_only_is_not_deployable():
    status = await BranchPushStrategy(git=make_git(ahead=3)).status(make_project(), Env.STAGING)

    assert status.deployable is False
    assert status.variant == "error"


async def test_the_tooltool_staging_ref_is_staging_not_dev():
    git = make_git(behind=1)

    await BranchPushStrategy(git=git).status(make_project(), Env.STAGING)

    # make_git only knows refs/remotes/origin/staging, so asking for dev would have raised.
    assert (STAGING, SOURCE) in git.counts


async def test_status_reports_the_shas_in_its_tooltip():
    status = await BranchPushStrategy(git=make_git(behind=1)).status(make_project(), Env.STAGING)

    assert "aaaaaaaaaa" in status.tooltip
    assert "bbbbbbbbbb" in status.tooltip


async def test_an_environment_without_a_target_is_unimplemented():
    git = FakeGitClient(default_remote_url=CANONICAL_URL.format(repo="balrog"))

    status = await BranchPushStrategy(git=git).status(make_project("balrog"), Env.STAGING)

    assert status.kind is StatusKind.NOT_IMPLEMENTED


async def test_a_fork_remote_is_rejected():
    git = make_git(behind=1)
    git.default_remote_url = "git@github.com:hneiva/tooltool.git"

    with pytest.raises(WrongRemoteError, match="would deploy nothing"):
        await BranchPushStrategy(git=git).status(make_project(), Env.STAGING)


async def test_plan_pushes_the_resolved_sha_to_a_qualified_ref():
    git = make_git(behind=2, commits=("aaa1111 second", "aaa0000 first"))

    action = await BranchPushStrategy(git=git).plan(make_project(), Env.STAGING)

    assert action.kind is ActionKind.PUSH
    assert action.sha == "a" * 40
    assert action.argv[-1] == f"{'a' * 40}:refs/heads/staging"
    assert "--force" not in " ".join(action.argv)
    assert action.commits == ("aaa1111 second", "aaa0000 first")
    assert action.documented_equivalent == "git push origin master:staging"


async def test_plan_reports_how_many_commits_it_could_not_list():
    git = make_git(behind=30, commits=tuple(f"sha{i}" for i in range(30)))

    action = await BranchPushStrategy(git=git, commit_limit=5).plan(make_project(), Env.STAGING)

    assert len(action.commits) == 5
    assert action.truncated == 25


async def test_plan_carries_the_policy_warning():
    git = FakeGitClient(
        counts={("refs/remotes/origin/dev", SOURCE): AheadBehind(ahead=0, behind=1)},
        shas={SOURCE: "a" * 40, "refs/remotes/origin/dev": "b" * 40},
        default_remote_url=CANONICAL_URL.format(repo="scriptworker-scripts"),
    )

    action = await BranchPushStrategy(git=git).plan(make_project("scriptworker-scripts"), Env.STAGING)

    assert "normally skipped" in action.warning


async def test_plan_refuses_a_diverged_branch():
    git = make_git(behind=2, ahead=1)

    with pytest.raises(UnsafeDeployError, match="refusing to build a deploy plan"):
        await BranchPushStrategy(git=git).plan(make_project(), Env.STAGING)


async def test_execute_pushes_what_was_planned():
    git = make_git(behind=2)
    strategy = BranchPushStrategy(git=git)
    project = make_project()
    action = await strategy.plan(project, Env.STAGING)

    result = await strategy.execute(project, Env.STAGING, action, dry_run=False)

    assert result.ok is True
    assert git.pushed == [("a" * 40, "staging", False)]


async def test_a_dry_run_pushes_nothing_for_real():
    git = make_git(behind=2)
    strategy = BranchPushStrategy(git=git)
    project = make_project()
    action = await strategy.plan(project, Env.STAGING)

    await strategy.execute(project, Env.STAGING, action, dry_run=True)

    assert git.pushed == [("a" * 40, "staging", True)]


async def test_execute_rechecks_and_refuses_if_the_branch_diverged_meanwhile():
    git = make_git(behind=2)
    strategy = BranchPushStrategy(git=git)
    project = make_project()
    action = await strategy.plan(project, Env.STAGING)

    # Someone pushed straight to the deploy branch while the dialog was open.
    git.counts[(STAGING, SOURCE)] = AheadBehind(ahead=1, behind=2)

    with pytest.raises(UnsafeDeployError, match="changed while the dialog was open"):
        await strategy.execute(project, Env.STAGING, action, dry_run=False)
    assert git.pushed == []


async def test_execute_refuses_a_non_push_action():
    from relduty_deployer.models import DeployAction

    strategy = BranchPushStrategy(git=make_git(behind=1))
    action = DeployAction(kind=ActionKind.OPEN_URL, description="docs", url="https://example.invalid")

    with pytest.raises(UnsafeDeployError, match="cannot push"):
        await strategy.execute(make_project(), Env.STAGING, action, dry_run=False)


async def test_a_rejected_push_is_reported_not_raised():
    git = make_git(behind=2)
    git.push_ok = False
    strategy = BranchPushStrategy(git=git)
    project = make_project()
    action = await strategy.plan(project, Env.STAGING)

    result = await strategy.execute(project, Env.STAGING, action, dry_run=False)

    assert result.ok is False
    assert "rejected" in result.output


def test_fetch_spec_does_not_ask_for_tags():
    assert BranchPushStrategy(git=FakeGitClient()).fetch_spec(make_project()).tags is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:mozilla-releng/tooltool.git", True),
        ("git@github.com:mozilla-releng/tooltool", True),
        ("https://github.com/mozilla-releng/tooltool.git", True),
        ("ssh://git@github.com/mozilla-releng/tooltool", True),
        ("git@github.com:mozilla-releng/TOOLTOOL.git", True),
        ("git@github.com:hneiva/tooltool.git", False),
        ("git@github.com:mozilla-releng/tooltool-fork.git", False),
        ("git@github.com:evil/mozilla-releng-tooltool.git", False),
    ],
)
def test_remote_url_matching(url, expected):
    assert points_at_repo(url, "mozilla-releng/tooltool") is expected
