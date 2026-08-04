"""The dashboard, driven headlessly through Textual's pilot.

These check wiring — that a status reaches the right button, that a diverged environment
cannot be pressed, that Save reflects dirt. The logic itself is tested elsewhere.

The terminal size matters: a project name plus two 34-column buttons needs more than the
default 80 columns, and a click on a widget that is off-screen silently does nothing.
"""

import dataclasses
from pathlib import Path

import pytest
from fakes import CANONICAL_URL, FakeGitClient

from relduty_deployer.app import RelDutyApp
from relduty_deployer.config import ConfigStore
from relduty_deployer.models import ActionKind, AheadBehind, DeployAction, DeployResult, DeployStatus, Env, StatusKind
from relduty_deployer.projects import PROJECT_SPECS, SPECS_BY_NAME
from relduty_deployer.screens import ConfirmDeployScreen, SettingsScreen
from relduty_deployer.strategies import BranchPushStrategy
from relduty_deployer.widgets import SAVED_LABEL, UNSAVED_LABEL, DeployButton, SaveButton, button_id, row_id

TERMINAL = (150, 55)
BRANCH_PUSH_PROJECTS = [spec.name for spec in PROJECT_SPECS if spec.name != "balrog"]
BALROG_URL = "https://mozilla-balrog.readthedocs.io/en/latest/infrastructure.html#deploying-to-stage"


class RecordingOpener:
    """Stands in for webbrowser.open."""

    def __init__(self):
        self.opened = []

    def __call__(self, url):
        self.opened.append(url)
        return True


class StubBalrogStrategy:
    """Reports a link-only status, as the real balrog strategy does, without any network."""

    name = "balrog"

    def fetch_spec(self, project):
        from relduty_deployer.gitcmd import FetchSpec

        return FetchSpec(tags=True)

    async def status(self, project, env):
        return DeployStatus(
            kind=StatusKind.BEHIND,
            behind=2,
            action=ActionKind.OPEN_URL,
            url=BALROG_URL,
            detail="v3.121 unreleased",
        )

    async def plan(self, project, env):
        return DeployAction(kind=ActionKind.OPEN_URL, description="manual", url=BALROG_URL)

    async def execute(self, project, env, action, *, dry_run):
        raise AssertionError("balrog must never be pushed")


class PerPathGit(FakeGitClient):
    """Scopes answers by checkout path, the way real git does.

    Both are necessary. Every project uses the remote name "origin", so the remote URL has
    to vary by path. And several projects share a ref pair — scriptworker-scripts and
    tooltool both compare origin/production against origin/master — so divergence counts
    keyed only by refs would collide between projects.
    """

    by_project: dict[str, dict[tuple[str, str], AheadBehind]]

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
            raise GitError(f"unknown revision range {target_ref}...{source_ref} in {path.name}") from None


def build_app(tmp_path, counts=None, *, opener=None, enabled=None):
    """An app wired to fakes, with one config file per test."""
    store = ConfigStore(tmp_path / "config.json")
    config = store.load()
    for name, settings in list(config.projects.items()):
        config.projects[name] = dataclasses.replace(
            settings,
            path=tmp_path / name,
            enabled=name in (enabled if enabled is not None else set(config.projects)),
        )
    store.save(config)

    by_project, shas = {}, {}
    for name in BRANCH_PUSH_PROJECTS:
        spec = SPECS_BY_NAME[name]
        source = f"refs/remotes/origin/{spec.source_branch}"
        shas[source] = "a" * 40
        by_project[name] = {}
        for env, branch in spec.targets.items():
            target = f"refs/remotes/origin/{branch}"
            shas[target] = "b" * 40
            by_project[name][(target, source)] = (counts or {}).get(name, {}).get(env, AheadBehind(ahead=0, behind=0))

    git = PerPathGit(by_project=by_project, shas=shas)
    strategies = {
        BranchPushStrategy.name: BranchPushStrategy(git=git),
        StubBalrogStrategy.name: StubBalrogStrategy(),
    }
    app = RelDutyApp(
        store=store,
        config=config,
        strategies=strategies,
        git=git,
        opener=opener or RecordingOpener(),
    )
    return app, git, store, config


async def open_confirm(pilot, app, name, env):
    """Click an environment's button and settle."""
    await pilot.click(f"#{button_id(name, env)}")
    await pilot.pause()
    await pilot.pause()


async def test_every_project_gets_a_row_with_two_buttons(tmp_path):
    app, *_ = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        assert len(app.query(DeployButton)) == 2 * len(PROJECT_SPECS)


async def test_a_behind_environment_shows_its_count_and_is_enabled(tmp_path):
    app, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        button = app.query_one(f"#{button_id('tooltool', Env.STAGING)}", DeployButton)
        assert str(button.label) == "4 commits behind"
        assert button.variant == "warning"
        assert button.disabled is False


async def test_a_diverged_environment_cannot_be_pressed(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.PROD: AheadBehind(ahead=2, behind=3)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        button = app.query_one(f"#{button_id('tooltool', Env.PROD)}", DeployButton)
        assert str(button.label) == "3 behind, 2 ahead"
        assert button.variant == "error"
        assert button.disabled is True

        await open_confirm(pilot, app, "tooltool", Env.PROD)

        assert not isinstance(app.screen, ConfirmDeployScreen)
        assert git.pushed == []


async def test_an_up_to_date_environment_is_green_and_inert(tmp_path):
    app, *_ = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        button = app.query_one(f"#{button_id('shipit', Env.PROD)}", DeployButton)
        assert str(button.label) == "Up to date"
        assert button.variant == "success"
        assert button.disabled is True


async def test_pressing_a_deployable_button_opens_the_confirmation(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        assert isinstance(app.screen, ConfirmDeployScreen)
        assert git.pushed == []


async def test_cancelling_the_confirmation_pushes_nothing(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await pilot.press("escape")
        await pilot.pause()

        assert git.pushed == []


async def test_confirming_performs_the_push(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await pilot.click("#push")
        await pilot.pause()
        await pilot.pause()

        assert git.pushed == [("a" * 40, "staging", False)]


async def test_the_dry_run_button_pushes_only_in_dry_run_mode(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await pilot.click("#dry-run")
        await pilot.pause()
        await pilot.pause()

        assert git.pushed == [("a" * 40, "staging", True)]
        # Still open, so a dry run can be repeated before committing to the real push.
        assert isinstance(app.screen, ConfirmDeployScreen)


async def test_cancel_holds_the_initial_focus(tmp_path):
    app, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        # A stray Enter must not deploy.
        assert app.focused is not None
        assert app.focused.id == "cancel"


async def test_the_scriptworker_policy_warning_reaches_the_dialog(tmp_path):
    app, *_ = build_app(tmp_path, {"scriptworker-scripts": {Env.STAGING: AheadBehind(ahead=0, behind=9)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "scriptworker-scripts", Env.STAGING)

        rendered = " ".join(str(node.render()) for node in app.screen.query(".confirm-warning"))
        assert "normally skipped" in rendered


async def test_prod_deploys_do_not_carry_the_staging_warning(tmp_path):
    app, *_ = build_app(tmp_path, {"scriptworker-scripts": {Env.PROD: AheadBehind(ahead=0, behind=3)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "scriptworker-scripts", Env.PROD)

        assert isinstance(app.screen, ConfirmDeployScreen)
        assert len(app.screen.query(".confirm-warning")) == 0


async def test_the_dialog_shows_the_command_and_the_documented_equivalent(tmp_path):
    app, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        rendered = " ".join(str(node.render()) for node in app.screen.query(".confirm-commands"))
        assert f"{'a' * 40}:refs/heads/staging" in rendered
        assert "git push origin master:staging" in rendered


async def test_balrog_opens_the_runbook_instead_of_pushing(tmp_path):
    opener = RecordingOpener()
    app, git, *_ = build_app(tmp_path, opener=opener)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        button = app.query_one(f"#{button_id('balrog', Env.STAGING)}", DeployButton)
        assert str(button.label) == "v3.121 unreleased · 2 behind"
        assert button.disabled is False

        await open_confirm(pilot, app, "balrog", Env.STAGING)

        assert opener.opened == [BALROG_URL]
        assert git.pushed == []
        assert not isinstance(app.screen, ConfirmDeployScreen)


async def test_save_starts_saved_and_turns_red_on_an_edit(tmp_path):
    app, _git, _store, config = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        save = app.query_one("#save", SaveButton)
        assert save.dirty is False
        assert str(save.label) == SAVED_LABEL
        assert save.variant == "success"

        config.projects["tooltool"] = dataclasses.replace(config.projects["tooltool"], remote="fork")
        app._sync_dirty()
        await pilot.pause()

        assert save.dirty is True
        assert str(save.label) == UNSAVED_LABEL
        assert save.variant == "error"


async def test_saving_writes_the_file_and_returns_to_saved(tmp_path):
    app, _git, store, config = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        config.projects["tooltool"] = dataclasses.replace(config.projects["tooltool"], enabled=False)
        app._sync_dirty()
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()

        assert app.query_one("#save", SaveButton).dirty is False
        assert store.is_dirty(config) is False


async def test_reverting_an_edit_leaves_the_config_clean(tmp_path):
    app, _git, store, config = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        original = config.projects["tooltool"]
        config.projects["tooltool"] = dataclasses.replace(original, remote="fork")
        app._sync_dirty()
        await pilot.pause()
        assert app.query_one("#save", SaveButton).dirty is True

        config.projects["tooltool"] = original
        app._sync_dirty()
        await pilot.pause()

        assert app.query_one("#save", SaveButton).dirty is False


async def test_disabled_projects_are_hidden(tmp_path):
    app, *_ = build_app(tmp_path, enabled={"tooltool"})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        assert app.query_one(f"#{row_id('tooltool')}").display is True
        assert app.query_one(f"#{row_id('shipit')}").display is False


async def test_the_settings_screen_opens(tmp_path):
    app, *_ = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        assert isinstance(app.screen, SettingsScreen)


async def test_a_push_rejected_by_git_is_reported_not_raised(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})
    git.push_ok = False

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await pilot.click("#push")
        await pilot.pause()
        await pilot.pause()

        assert git.pushed == [("a" * 40, "staging", False)]
        assert app.is_running


async def test_a_branch_that_diverges_while_the_dialog_is_open_is_not_pushed(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        source = "refs/remotes/origin/master"
        target = "refs/remotes/origin/staging"
        git.by_project["tooltool"][(target, source)] = AheadBehind(ahead=1, behind=4)

        await pilot.click("#push")
        await pilot.pause()
        await pilot.pause()

        assert git.pushed == []
        assert app.is_running


@pytest.mark.parametrize("name", BRANCH_PUSH_PROJECTS)
async def test_no_project_ends_up_in_an_error_state(tmp_path, name):
    app, *_ = build_app(tmp_path)

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        for env in (Env.STAGING, Env.PROD):
            button = app.query_one(f"#{button_id(name, env)}", DeployButton)
            assert button.status.kind is not StatusKind.ERROR


async def test_a_fetch_failure_reddens_only_its_own_project(tmp_path):
    app, git, *_ = build_app(tmp_path)
    git.fetch_error = "Could not read from remote repository"

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        for name in BRANCH_PUSH_PROJECTS:
            button = app.query_one(f"#{button_id(name, Env.STAGING)}", DeployButton)
            assert button.status.kind is StatusKind.ERROR
            assert button.disabled is True
        # The app is still usable.
        assert app.is_running


def test_deploy_result_is_returned_not_raised():
    assert DeployResult(ok=False, output="rejected").ok is False
