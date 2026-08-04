"""The balrog strategy, entirely offline.

The headline case is the state balrog was actually in when this was written: the source
branch declared 3.121, the newest published release was v3.120, and both environments were
running 3.120.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fakes import CANONICAL_URL, FakeGitClient

from relduty_deployer.github import GitHubError, GitHubUnavailableError, Release
from relduty_deployer.models import ActionKind, AheadBehind, Env, StatusKind
from relduty_deployer.projects import SPECS_BY_NAME, Project, ProjectSettings
from relduty_deployer.strategies import UnsafeDeployError
from relduty_deployer.strategies.balrog import VERSION_URLS, BalrogStrategy
from relduty_deployer.versions import DeployedVersion, ProbeError

SOURCE = "refs/remotes/origin/main"
PROD_SHA = "e" * 40
STAGE_SHA = "e" * 40


@dataclass
class FakeGitHubClient:
    """Release lookups from an in-memory table."""

    releases: dict[str, Release] = field(default_factory=dict)
    latest: Release | None = None
    unavailable: bool = False
    error: str = ""

    async def published_release(self, repo: str, tag: str) -> Release | None:
        self._maybe_fail()
        return self.releases.get(tag)

    async def latest_release(self, repo: str) -> Release | None:
        self._maybe_fail()
        return self.latest

    def _maybe_fail(self) -> None:
        if self.unavailable:
            raise GitHubUnavailableError("gh is not installed")
        if self.error:
            raise GitHubError(self.error)


@dataclass
class FakeVersionProbe:
    """Canned /__version__ payloads keyed by URL."""

    payloads: dict[str, DeployedVersion] = field(default_factory=dict)
    error: str = ""

    async def probe(self, url: str) -> DeployedVersion:
        if self.error:
            raise ProbeError(self.error)
        try:
            return self.payloads[url]
        except KeyError:
            raise ProbeError(f"no canned payload for {url}") from None


def make_project():
    return Project(spec=SPECS_BY_NAME["balrog"], settings=ProjectSettings(path=Path("/repo")))


def pyproject(version: str) -> str:
    return f'[project]\nname = "balrog"\nversion = "{version}"\n'


def make_git(source_version="3.121", **kwargs):
    return FakeGitClient(
        files={(SOURCE, "pyproject.toml"): pyproject(source_version)},
        default_remote_url=CANONICAL_URL.format(repo="balrog"),
        **kwargs,
    )


def make_strategy(git=None, github=None, versions=None):
    return BalrogStrategy(
        git=git or make_git(),
        github=github or FakeGitHubClient(),
        versions=versions or FakeVersionProbe(),
    )


def live_probe(prod="3.120", stage="3.120"):
    return FakeVersionProbe(
        payloads={
            VERSION_URLS[Env.PROD]: DeployedVersion(version=prod, commit=PROD_SHA, url=VERSION_URLS[Env.PROD]),
            VERSION_URLS[Env.STAGING]: DeployedVersion(version=stage, commit=STAGE_SHA, url=VERSION_URLS[Env.STAGING]),
        }
    )


async def test_staging_is_behind_when_the_declared_version_has_no_release():
    # The state at the time of writing: main declares 3.121, latest release is v3.120.
    git = make_git("3.121", known_objects={"v3.120"}, counts={("v3.120", SOURCE): AheadBehind(ahead=0, behind=2)})
    github = FakeGitHubClient(latest=Release(tag="v3.120", published_at="2026-07-21T14:25:22Z"))

    status = await make_strategy(git=git, github=github).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.BEHIND
    assert status.behind == 2
    assert status.label == "v3.121 unreleased · 2 behind"
    assert status.variant == "warning"


async def test_staging_is_up_to_date_when_the_declared_version_is_published():
    git = make_git("3.120")
    github = FakeGitHubClient(releases={"v3.120": Release(tag="v3.120", published_at="2026-07-21T14:25:22Z")})

    status = await make_strategy(git=git, github=github).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.UP_TO_DATE
    assert status.label == "v3.120 · Up to date"
    assert status.variant == "success"


async def test_a_draft_release_does_not_count_as_deployed():
    # FakeGitHubClient holds only published releases, mirroring the real client which maps
    # a draft to None; so a drafted v3.121 leaves staging behind.
    git = make_git("3.121")
    github = FakeGitHubClient(latest=Release(tag="v3.120", published_at="2026-07-21T14:25:22Z"))

    status = await make_strategy(git=git, github=github).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.BEHIND


async def test_the_version_is_read_from_the_source_ref_not_the_working_tree():
    # A real hazard: the balrog checkout sat on a feature branch declaring a bumped version
    # while origin/main still carried the previous one.
    git = make_git("3.119")
    github = FakeGitHubClient(releases={"v3.119": Release(tag="v3.119", published_at="2026-07-14T16:05:05Z")})

    status = await make_strategy(git=git, github=github).status(make_project(), Env.STAGING)

    assert status.detail == "v3.119"


async def test_staging_survives_a_release_tag_missing_from_the_local_clone():
    # No known_objects, so v3.120 cannot be resolved and no count is possible.
    git = make_git("3.121")
    github = FakeGitHubClient(latest=Release(tag="v3.120", published_at="2026-07-21T14:25:22Z"))

    status = await make_strategy(git=git, github=github).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.BEHIND
    assert status.behind == 0
    assert status.label == "v3.121 unreleased"
    assert "not in the local clone" in status.tooltip


async def test_staging_degrades_when_gh_is_unavailable():
    status = await make_strategy(github=FakeGitHubClient(unavailable=True)).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.NOT_IMPLEMENTED
    assert "gh" in status.label


async def test_staging_reports_a_github_failure_as_an_error():
    status = await make_strategy(github=FakeGitHubClient(error="rate limited")).status(make_project(), Env.STAGING)

    assert status.kind is StatusKind.ERROR


async def test_a_malformed_pyproject_fails_with_a_readable_message():
    git = FakeGitClient(files={(SOURCE, "pyproject.toml"): "this is not toml ["})

    with pytest.raises(Exception, match="could not read"):
        await make_strategy(git=git).status(make_project(), Env.STAGING)


async def test_prod_is_up_to_date_when_it_matches_stage():
    status = await make_strategy(versions=live_probe(prod="3.120", stage="3.120")).status(make_project(), Env.PROD)

    assert status.kind is StatusKind.UP_TO_DATE
    assert status.label == "3.120 · Up to date"
    assert status.variant == "success"


async def test_prod_counts_commits_behind_stage_when_both_are_resolvable():
    git = make_git(known_objects={"a" * 40, "b" * 40}, counts={("a" * 40, "b" * 40): AheadBehind(ahead=0, behind=7)})
    versions = FakeVersionProbe(
        payloads={
            VERSION_URLS[Env.PROD]: DeployedVersion(version="3.119", commit="a" * 40, url=VERSION_URLS[Env.PROD]),
            VERSION_URLS[Env.STAGING]: DeployedVersion(version="3.120", commit="b" * 40, url=VERSION_URLS[Env.STAGING]),
        }
    )

    status = await make_strategy(git=git, versions=versions).status(make_project(), Env.PROD)

    assert status.kind is StatusKind.BEHIND
    assert status.behind == 7
    assert status.label == "prod 3.119 · stage 3.120 · 7 behind"


async def test_prod_falls_back_to_the_version_tag_when_the_sha_is_absent():
    # Tier two: the deployed sha is missing but the vX.Y tag resolves.
    git = make_git(known_objects={"v3.119", "v3.120"}, counts={("v3.119", "v3.120"): AheadBehind(ahead=0, behind=4)})
    versions = FakeVersionProbe(
        payloads={
            VERSION_URLS[Env.PROD]: DeployedVersion(version="3.119", commit="a" * 40, url=VERSION_URLS[Env.PROD]),
            VERSION_URLS[Env.STAGING]: DeployedVersion(version="3.120", commit="b" * 40, url=VERSION_URLS[Env.STAGING]),
        }
    )

    status = await make_strategy(git=git, versions=versions).status(make_project(), Env.PROD)

    assert status.behind == 4


async def test_prod_compares_versions_when_nothing_resolves_locally():
    # Tier three, which is the state a stale clone is genuinely in: report the difference
    # honestly rather than raising or claiming to be up to date.
    versions = FakeVersionProbe(
        payloads={
            VERSION_URLS[Env.PROD]: DeployedVersion(version="3.119", commit="a" * 40, url=VERSION_URLS[Env.PROD]),
            VERSION_URLS[Env.STAGING]: DeployedVersion(version="3.120", commit="b" * 40, url=VERSION_URLS[Env.STAGING]),
        }
    )

    status = await make_strategy(versions=versions).status(make_project(), Env.PROD)

    assert status.kind is StatusKind.BEHIND
    assert status.label == "prod 3.119 · stage 3.120"
    assert "only versions were compared" in status.tooltip


async def test_prod_reports_an_unreachable_endpoint_as_an_error():
    status = await make_strategy(versions=FakeVersionProbe(error="connection refused")).status(make_project(), Env.PROD)

    assert status.kind is StatusKind.ERROR
    assert status.deployable is False


@pytest.mark.parametrize("env", [Env.STAGING, Env.PROD])
async def test_neither_environment_is_ever_deployable(env):
    git = make_git("3.121", known_objects={"v3.120"}, counts={("v3.120", SOURCE): AheadBehind(ahead=0, behind=2)})
    github = FakeGitHubClient(latest=Release(tag="v3.120", published_at=""))

    status = await make_strategy(git=git, github=github, versions=live_probe("3.119", "3.120")).status(make_project(), env)

    assert status.deployable is False


@pytest.mark.parametrize(
    ("env", "anchor"),
    [(Env.STAGING, "#deploying-to-stage"), (Env.PROD, "#pushing-to-production")],
)
async def test_the_button_opens_the_runbook(env, anchor):
    action = await make_strategy().plan(make_project(), env)

    assert action.kind is ActionKind.OPEN_URL
    assert action.url.endswith(anchor)
    assert action.url.startswith("https://mozilla-balrog.readthedocs.io/")
    assert action.argv == ()


async def test_staging_status_links_to_the_stage_runbook():
    github = FakeGitHubClient(latest=Release(tag="v3.120", published_at=""))

    status = await make_strategy(github=github).status(make_project(), Env.STAGING)

    assert status.url == "https://mozilla-balrog.readthedocs.io/en/latest/infrastructure.html#deploying-to-stage"
    assert status.clickable is True


@pytest.mark.parametrize("env", [Env.STAGING, Env.PROD])
async def test_execute_always_refuses(env):
    from relduty_deployer.models import DeployAction

    with pytest.raises(UnsafeDeployError, match="not automated"):
        await make_strategy().execute(make_project(), env, DeployAction(kind=ActionKind.OPEN_URL, description=""), dry_run=False)


def test_balrog_asks_for_tags_when_fetching():
    # Release tags are the deploy trigger, and a plain fetch may not bring them all.
    assert make_strategy().fetch_spec(make_project()).tags is True
