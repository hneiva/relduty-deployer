# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Command shapes, and one real repository to prove the orientation is right.

The argv assertions are the regression net for the single most dangerous bug in this
tool: swapping ahead and behind would offer a push at exactly the moment one must be
refused.
"""

import subprocess
from pathlib import Path

import pytest

from relduty_deployer.gitcmd import (
    FetchSpec,
    GitError,
    SubprocessGitClient,
    commit_list_argv,
    fetch_argv,
    push_argv,
    rev_list_count_argv,
)

PATH = Path("/repo")


def test_divergence_puts_the_target_on_the_left_with_three_dots():
    argv = rev_list_count_argv(PATH, target_ref="refs/remotes/origin/dev", source_ref="refs/remotes/origin/master")

    assert argv == (
        "git",
        "-C",
        "/repo",
        "rev-list",
        "--left-right",
        "--count",
        "refs/remotes/origin/dev...refs/remotes/origin/master",
    )


def test_divergence_range_is_symmetric_not_asymmetric():
    argv = rev_list_count_argv(PATH, target_ref="dev", source_ref="master")
    # Two dots would silently print a single number instead of a pair.
    assert argv[-1].count(".") == 3


def test_commit_list_uses_a_two_dot_range_from_target_to_source():
    argv = commit_list_argv(PATH, target_ref="dev", source_ref="master", limit=20)

    assert argv[-1] == "dev..master"
    assert "..." not in argv[-1]
    # One extra so the caller can tell the list was truncated.
    assert "--max-count=21" in argv


def test_fetch_targets_one_named_remote():
    argv = fetch_argv(PATH, "origin", spec=FetchSpec())

    assert argv[-1] == "origin"
    # --all would also hit a personal fork remote.
    assert "--all" not in argv
    assert "--tags" not in argv


def test_fetch_asks_for_tags_only_when_the_strategy_needs_them():
    assert "--tags" in fetch_argv(PATH, "origin", spec=FetchSpec(tags=True))
    assert "--prune" in fetch_argv(PATH, "origin", spec=FetchSpec(prune=True))


def test_push_sends_a_resolved_sha_to_a_fully_qualified_branch():
    argv = push_argv(PATH, "origin", sha="8f3c1ad0f2", target_branch="production", dry_run=False)

    assert argv == ("git", "-C", "/repo", "push", "origin", "8f3c1ad0f2:refs/heads/production")


def test_push_never_forces():
    for dry_run in (True, False):
        argv = push_argv(PATH, "origin", sha="abc", target_branch="dev", dry_run=dry_run)
        assert not any(arg.startswith("--force") for arg in argv)


def test_dry_run_push_is_the_same_command_plus_a_flag():
    real = push_argv(PATH, "origin", sha="abc", target_branch="dev", dry_run=False)
    dry = push_argv(PATH, "origin", sha="abc", target_branch="dev", dry_run=True)

    assert "--dry-run" in dry
    assert tuple(arg for arg in dry if arg != "--dry-run") == real


def _git(repo: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True)


@pytest.fixture
def diverged_repo(tmp_path):
    """A repo where `deploy` is 2 commits behind `main` and 1 commit ahead of it.

    The counts are deliberately unequal, so a transposition cannot pass.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(repo, "branch", "deploy")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main one")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "main two")
    _git(repo, "checkout", "-q", "deploy")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "hotfix on deploy")
    _git(repo, "checkout", "-q", "main")
    return repo


async def test_ahead_behind_orientation_against_a_real_repo(diverged_repo):
    counts = await SubprocessGitClient().ahead_behind(diverged_repo, target_ref="deploy", source_ref="main")

    assert counts.behind == 2, "main has 2 commits deploy is missing"
    assert counts.ahead == 1, "deploy has 1 commit main is missing"


async def test_a_fast_forward_target_reports_no_commits_ahead(diverged_repo):
    _git(diverged_repo, "branch", "-f", "clean", "HEAD~1")

    counts = await SubprocessGitClient().ahead_behind(diverged_repo, target_ref="clean", source_ref="main")

    assert (counts.ahead, counts.behind) == (0, 1)


async def test_commit_list_returns_what_a_deploy_would_ship(diverged_repo):
    commits = await SubprocessGitClient().commit_list(diverged_repo, target_ref="deploy", source_ref="main", limit=10)

    assert len(commits) == 2
    assert "main two" in commits[0]
    assert "main one" in commits[1]


async def test_commit_list_is_capped_at_the_limit(diverged_repo):
    commits = await SubprocessGitClient().commit_list(diverged_repo, target_ref="deploy", source_ref="main", limit=1)

    assert len(commits) == 1


async def test_an_unknown_ref_raises_a_readable_error(diverged_repo):
    client = SubprocessGitClient()

    with pytest.raises(GitError, match="rev-list"):
        await client.ahead_behind(diverged_repo, target_ref="does-not-exist", source_ref="main")


async def test_has_commit_distinguishes_present_from_absent(diverged_repo):
    client = SubprocessGitClient()
    sha = await client.rev_parse(diverged_repo, "main")

    assert await client.has_commit(diverged_repo, sha) is True
    assert await client.has_commit(diverged_repo, "0" * 40) is False


async def test_show_file_reads_from_the_ref_not_the_working_tree(diverged_repo):
    (diverged_repo / "version.txt").write_text("from working tree\n")
    _git(diverged_repo, "add", "version.txt")
    _git(diverged_repo, "commit", "-q", "-m", "add version")
    (diverged_repo / "version.txt").write_text("uncommitted edit\n")

    content = await SubprocessGitClient().show_file(diverged_repo, "main", "version.txt")

    assert content.strip() == "from working tree"


async def test_show_commit_returns_the_message_the_stat_and_the_patch(diverged_repo):
    """Abbreviated, because the dialog only ever has the short sha from `log --oneline`."""
    (diverged_repo / "thing.txt").write_text("hello\n")
    _git(diverged_repo, "add", "thing.txt")
    _git(diverged_repo, "commit", "-q", "-m", "add thing")
    sha = await SubprocessGitClient().rev_parse(diverged_repo, "HEAD")

    details = await SubprocessGitClient().show_commit(diverged_repo, sha[:7])

    assert "add thing" in details
    assert "thing.txt" in details, "--stat should name the changed file"
    assert "+hello" in details, "--patch should carry the added line"


async def test_show_commit_rejects_an_object_that_is_not_there(diverged_repo):
    with pytest.raises(GitError):
        await SubprocessGitClient().show_commit(diverged_repo, "0" * 40)


async def test_a_missing_repository_fails_loudly(tmp_path):
    with pytest.raises(GitError):
        await SubprocessGitClient().rev_parse(tmp_path / "nope", "HEAD")
