# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The status command, with the real collaborators swapped out."""

import dataclasses
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fakes import CANONICAL_URL, FakeGitClient

from relduty_deployer import cli as cli_module
from relduty_deployer.config import SCHEMA_VERSION, ConfigStore
from relduty_deployer.models import AheadBehind
from relduty_deployer.projects import BRANCH_PUSH, PROJECT_SPECS, SPECS_BY_NAME
from relduty_deployer.strategies import BranchPushStrategy

# By strategy, not by excluding balrog: iscript is not a branch-push project either.
BRANCH_PUSH_PROJECTS = [spec.name for spec in PROJECT_SPECS if spec.strategy == BRANCH_PUSH]


class PerPathGit(FakeGitClient):
    """Scopes divergence counts and remote URLs by checkout path, as real git does."""

    def __init__(self, *, by_project, **kwargs):
        super().__init__(**kwargs)
        self.by_project = by_project

    async def remote_url(self, path: Path, remote: str) -> str:
        return CANONICAL_URL.format(repo=path.name)

    async def ahead_behind(self, path: Path, *, target_ref: str, source_ref: str) -> AheadBehind:
        from relduty_deployer.gitcmd import GitError

        try:
            return self.by_project[path.name][(target_ref, source_ref)]
        except KeyError:
            raise GitError(f"unknown range in {path.name}") from None


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Point the CLI at a throwaway config and a fake git, and return the fake."""
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli_module, "ConfigStore", lambda *a, **k: ConfigStore(config_path))

    by_project, shas = {}, {}
    for name in BRANCH_PUSH_PROJECTS:
        spec = SPECS_BY_NAME[name]
        source = f"refs/remotes/origin/{spec.source_branch}"
        shas[source] = "a" * 40
        by_project[name] = {}
        for _env, branch in spec.targets.items():
            target = f"refs/remotes/origin/{branch}"
            shas[target] = "b" * 40
            by_project[name][(target, source)] = AheadBehind(ahead=0, behind=0)

    git = PerPathGit(by_project=by_project, shas=shas)

    # build_projects runs before _build, so the enabled set has to be on disk already.
    original_load = ConfigStore.load

    def load(self):
        config = original_load(self)
        for name, settings in list(config.projects.items()):
            config.projects[name] = dataclasses.replace(settings, path=Path("/repos") / name, enabled=name in BRANCH_PUSH_PROJECTS)
        return config

    monkeypatch.setattr(ConfigStore, "load", load)
    monkeypatch.setattr(cli_module, "_build", lambda config: (git, {BranchPushStrategy.name: BranchPushStrategy(git=git)}))
    return git, config_path


def test_status_reports_every_enabled_project(wired):
    git, _config_path = wired

    result = CliRunner().invoke(cli_module.main, ["status", "--no-fetch"])

    assert result.exit_code == 0, result.output
    for name in BRANCH_PUSH_PROJECTS:
        assert name in result.output
    assert "Up to date" in result.output


def test_no_fetch_does_not_touch_any_remote(wired):
    git, _config_path = wired

    CliRunner().invoke(cli_module.main, ["status", "--no-fetch"])

    assert git.fetched == []


def test_status_fetches_by_default(wired):
    git, _config_path = wired

    result = CliRunner().invoke(cli_module.main, ["status"])

    assert result.exit_code == 0, result.output
    assert sorted(path.name for path, _remote, _spec in git.fetched) == sorted(BRANCH_PUSH_PROJECTS)


def test_a_behind_environment_is_printed_with_its_count(wired):
    git, _config_path = wired
    git.by_project["tooltool"][("refs/remotes/origin/staging", "refs/remotes/origin/master")] = AheadBehind(ahead=0, behind=4)

    result = CliRunner().invoke(cli_module.main, ["status", "--no-fetch"])

    assert "4 commits behind" in result.output


def test_a_fetch_failure_is_reported_without_aborting_the_run(wired):
    git, _config_path = wired
    git.fetch_error = "Could not read from remote repository"

    result = CliRunner().invoke(cli_module.main, ["status"])

    assert result.exit_code == 0, result.output
    assert result.output.count("error:") == len(BRANCH_PUSH_PROJECTS)


def test_status_creates_the_config_on_first_run(wired):
    _git, config_path = wired
    assert not config_path.exists()

    CliRunner().invoke(cli_module.main, ["status", "--no-fetch"])

    assert json.loads(config_path.read_text())["schema_version"] == SCHEMA_VERSION


def test_an_unreadable_config_fails_with_a_message_not_a_traceback(wired):
    _git, config_path = wired
    config_path.write_text(json.dumps({"schema_version": SCHEMA_VERSION + 5, "projects": {}}))

    result = CliRunner().invoke(cli_module.main, ["status"])

    assert result.exit_code != 0
    assert "upgrade relduty-deployer" in result.output


def test_the_version_flag_works():
    result = CliRunner().invoke(cli_module.main, ["--version"])

    assert result.exit_code == 0
    assert "relduty-deployer" in result.output
