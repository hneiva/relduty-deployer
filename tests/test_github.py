"""The gh adapter, with the subprocess replaced.

The distinction that matters here is "no release for that tag", which is a real answer, from
"gh is broken", which is not. Conflating them would make an unreleased version and a missing
CLI look identical.
"""

import pytest

from relduty_deployer import github as github_module
from relduty_deployer.github import GhCliGitHubClient, GitHubError, GitHubUnavailableError, Release
from relduty_deployer.process import CommandError, CommandOutput

NOT_FOUND = '{"message":"Not Found","documentation_url":"https://docs.github.com/rest","status":"404"}'
PUBLISHED = '{"tag_name":"v3.120","draft":false,"prerelease":false,"published_at":"2026-07-21T14:25:22Z"}'


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces process.run, recording the argv it was given."""
    calls: list[tuple[str, ...]] = []
    responses: dict[str, CommandOutput] = {}

    async def run(argv, *, timeout, env=None):
        calls.append(tuple(argv))
        key = argv[-1]
        if key in responses:
            return responses[key]
        raise AssertionError(f"no canned response for {key}")

    monkeypatch.setattr(github_module, "run", run)
    return calls, responses


def ok(stdout: str) -> CommandOutput:
    return CommandOutput(returncode=0, stdout=stdout, stderr="")


def failed(stderr: str, code: int = 1) -> CommandOutput:
    return CommandOutput(returncode=code, stdout="", stderr=stderr)


async def test_a_published_release_is_returned(fake_run):
    calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.120"] = ok(PUBLISHED)

    release = await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")

    assert release == Release(tag="v3.120", published_at="2026-07-21T14:25:22Z")
    assert calls == [("gh", "api", "repos/mozilla-releng/balrog/releases/tags/v3.120")]


async def test_a_missing_release_is_none_not_an_error(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v9.999"] = failed(NOT_FOUND)

    assert await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v9.999") is None


async def test_a_draft_release_does_not_count_as_published(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.121"] = ok('{"tag_name":"v3.121","draft":true,"published_at":null}')

    assert await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.121") is None


async def test_a_real_failure_is_raised_rather_than_read_as_absent(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.120"] = failed("API rate limit exceeded")

    with pytest.raises(GitHubError, match="rate limit"):
        await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")


async def test_a_missing_gh_binary_is_distinguished_from_a_missing_release(monkeypatch):
    async def run(argv, *, timeout, env=None):
        raise CommandError("could not run gh api: No such file or directory")

    monkeypatch.setattr(github_module, "run", run)

    with pytest.raises(GitHubUnavailableError, match="could not run gh"):
        await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")


async def test_unparseable_output_is_an_error(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.120"] = ok("not json at all")

    with pytest.raises(GitHubError, match="unparseable JSON"):
        await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")


async def test_a_json_array_is_rejected(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.120"] = ok("[]")

    with pytest.raises(GitHubError, match="expected an object"):
        await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")


async def test_a_release_without_a_tag_is_rejected(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/tags/v3.120"] = ok('{"draft":false}')

    with pytest.raises(GitHubError, match="no tag_name"):
        await GhCliGitHubClient().published_release("mozilla-releng/balrog", "v3.120")


async def test_latest_release_uses_the_endpoint_that_excludes_drafts(fake_run):
    calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/latest"] = ok(PUBLISHED)

    release = await GhCliGitHubClient().latest_release("mozilla-releng/balrog")

    assert release is not None
    assert release.tag == "v3.120"
    assert calls == [("gh", "api", "repos/mozilla-releng/balrog/releases/latest")]


async def test_a_repository_with_no_releases_yields_none(fake_run):
    _calls, responses = fake_run
    responses["repos/mozilla-releng/balrog/releases/latest"] = failed(NOT_FOUND)

    assert await GhCliGitHubClient().latest_release("mozilla-releng/balrog") is None
