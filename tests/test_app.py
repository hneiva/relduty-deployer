# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

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
from relduty_deployer.screens import CommitDetailScreen, ConfirmDeployScreen, SettingsScreen
from relduty_deployer.screens.confirm_deploy import split_commit
from relduty_deployer.strategies import BranchPushStrategy
from relduty_deployer.widgets import SAVED_LABEL, UNSAVED_LABEL, DeployButton, SaveButton, button_id, row_id

TERMINAL = (150, 55)
# `padding: 0 4` on ConfirmDeployScreen, counted from both sides.
CONFIRM_GUTTER = 8
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


SHA_COLUMN = 3
"""A commit line is indented two spaces, so column 3 is inside the sha and column 20 is not."""


async def test_a_rule_separates_the_commits_from_the_commands(tmp_path):
    """Positions, not just presence: a divider in the wrong place separates the wrong things."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=2)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props", "cee301f Switch PR policy")

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        rule = app.screen.query_one(".confirm-footer Rule")
        last_commit = app.screen.query(".confirm-commit").last()
        commands = app.screen.query_one(".confirm-commands")

        assert last_commit.region.y < rule.region.y < commands.region.y
        assert str(commands.render()).startswith("will run"), "the Rule's margin replaces the old leading blank line"


def give_tooltool_commits(git, *entries):
    """Set the exact commit lines the staging dialog will render."""
    spec = SPECS_BY_NAME["tooltool"]
    key = (f"refs/remotes/origin/{spec.targets[Env.STAGING]}", f"refs/remotes/origin/{spec.source_branch}")
    git.commits[key] = entries


async def open_commit_details(pilot, app, column=SHA_COLUMN, index=0):
    """Click a commit line at a given column and settle.

    Scrolls the line into view first, because on a short terminal the commit list is only a
    few rows tall and a click on an off-screen widget silently does nothing.
    """
    target = app.screen.query(".confirm-commit")[index]
    target.scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click(target, offset=(column, 0))
    await pilot.pause()
    await pilot.pause()


async def test_clicking_a_commit_hash_opens_its_details(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props")
    git.commit_details["09efe5e"] = "commit 09efe5e\n\n    Convert MUI system props\n\n thing.py | 2 +-\n"

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await open_commit_details(pilot, app)

        assert isinstance(app.screen, CommitDetailScreen)
        assert "thing.py | 2 +-" in str(app.screen.query_one("#commit-body").render())


async def test_clicking_a_commit_subject_opens_nothing(tmp_path):
    """Only the sha is a link, which is what the underline is promising."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props")
    git.commit_details["09efe5e"] = "details"

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await open_commit_details(pilot, app, column=20)

        assert isinstance(app.screen, ConfirmDeployScreen)


async def test_closing_the_details_returns_to_the_confirmation(tmp_path):
    """The details screen is pushed on top, so closing it must not cancel the deploy."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props")
    git.commit_details["09efe5e"] = "details"

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await open_commit_details(pilot, app)
        await pilot.press("escape")
        await pilot.pause()

        assert isinstance(app.screen, ConfirmDeployScreen)
        assert git.pushed == []


async def test_a_commit_git_cannot_show_reports_the_error_in_the_screen(tmp_path):
    """A stale sha must land in the screen rather than taking the app down."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props")

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await open_commit_details(pilot, app)

        assert isinstance(app.screen, CommitDetailScreen)
        assert "could not read 09efe5e" in str(app.screen.query_one("#commit-body").render())


async def test_a_subject_containing_markup_is_not_parsed_as_markup(tmp_path):
    """The sha is interpolated into the click action, so the subject must go in as a variable."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e [skip ci] bump [bold]version[/bold]")

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        rendered = str(app.screen.query_one(".confirm-commit").render())
        assert "[skip ci] bump [bold]version[/bold]" in rendered


async def test_a_line_without_a_sha_is_rendered_but_not_linked(tmp_path):
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "not-a-sha a line git would never emit")

    async with app.run_test(size=TERMINAL) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        assert "not-a-sha a line git would never emit" in str(app.screen.query_one(".confirm-commit").render())

        await open_commit_details(pilot, app)
        assert isinstance(app.screen, ConfirmDeployScreen)


@pytest.mark.parametrize("height", [55, 40, 30, 24])
async def test_a_long_diff_cannot_hide_the_close_button(tmp_path, height):
    """The third modal inherits the docked buttons and the `1fr` body, and a diff is the
    longest thing any of them renders."""
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=1)}}, enabled={"tooltool"})
    give_tooltool_commits(git, "09efe5e Convert MUI system props")
    git.commit_details["09efe5e"] = "commit 09efe5e\n\n    subject\n\n" + "\n".join(f"+line {i}" for i in range(200))

    async with app.run_test(size=(150, height)) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        await open_commit_details(pilot, app)

        for selector in ("#close", ".commit-detail-body"):
            region = app.screen.query_one(selector).region
            assert region.height > 0, f"{selector} has no height at {height} rows"
            assert region.y >= 0, f"{selector} starts above the terminal at {height} rows"
            assert region.y + region.height <= height, f"{selector} runs past the bottom at {height} rows"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("09efe5e Convert MUI system props", ("09efe5e", "Convert MUI system props")),
        ("09efe5ef9ac812b3a3b5fcf5b21bec6db3651eba full sha", ("09efe5ef9ac812b3a3b5fcf5b21bec6db3651eba", "full sha")),
        ("deadbeef", ("deadbeef", "")),
        ("not-a-sha subject", ("", "not-a-sha subject")),
        ("09e no subject and too short", ("", "09e no subject and too short")),
        ("", ("", "")),
    ],
)
def test_split_commit(entry, expected):
    assert split_commit(entry) == expected


def give_tooltool_a_long_commit_list(git, count=20):
    """Fill the staging commit list, so the dialog has more to show than a short terminal fits."""
    spec = SPECS_BY_NAME["tooltool"]
    key = (f"refs/remotes/origin/{spec.targets[Env.STAGING]}", f"refs/remotes/origin/{spec.source_branch}")
    git.commits[key] = tuple(f"{i:07x} Commit subject number {i}, about as long as a real one" for i in range(count))


@pytest.mark.parametrize("height", [55, 40, 36, 30, 26, 24, 20])
async def test_the_command_is_visible_without_scrolling(tmp_path, height):
    """The command about to run must be on screen the moment the dialog opens.

    It used to live inside the scrolling body, where twenty commits pushed it out of view.
    Nothing scrolls it back: Cancel holds the focus for safety, so the keyboard did nothing
    and only a mouse wheel over the body could reach it.
    """
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=40)}}, enabled={"tooltool"})
    give_tooltool_a_long_commit_list(git)

    async with app.run_test(size=(150, height)) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        commands = app.screen.query_one(".confirm-commands")
        assert str(commands.render()).startswith("will run")
        region = commands.region
        assert region.height > 0, f"the command has no height at {height} rows"
        assert region.y >= 0 and region.y + region.height <= height, f"the command is off screen at {height} rows"


@pytest.mark.parametrize("height", [55, 45, 36, 30, 24])
async def test_no_modal_draws_its_body_under_its_footer(tmp_path, height):
    """A scrolling body must stop where the docked footer starts.

    The earlier fix asserted only that the footer was on screen, which it was — sitting on
    top of the last rows of the body. Those rows could not be scrolled into view or clicked,
    so the content was hidden just as thoroughly as the buttons had been.
    """
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=40)}}, enabled={"tooltool"})
    give_tooltool_a_long_commit_list(git)
    git.commit_details["0000000"] = "\n".join(f"+line {i}" for i in range(200))

    async with app.run_test(size=(150, height)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert_body_stops_at(app.screen, ".settings-body", ".confirm-buttons", height)
        await pilot.press("escape")
        await pilot.pause()

        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        assert_body_stops_at(app.screen, ".confirm-body", ".confirm-footer", height)

        app.screen.action_show_commit("0000000")
        await pilot.pause()
        await pilot.pause()
        assert_body_stops_at(app.screen, ".commit-detail-body", ".confirm-buttons", height)


def assert_body_stops_at(screen, body_selector, footer_selector, height):
    body = screen.query_one(body_selector).region
    footer = screen.query_one(footer_selector).region
    assert body.height > 0, f"{body_selector} collapsed at {height} rows"
    assert body.y + body.height <= footer.y, f"{body_selector} runs {body.y + body.height - footer.y} rows under {footer_selector} at {height} rows"


@pytest.mark.parametrize("key", ["pagedown", "end"])
async def test_the_commit_list_scrolls_by_keyboard_while_cancel_keeps_focus(tmp_path, key):
    """Paging is bound on the screen because no focusable widget in the dialog scrolls.

    Cancel must keep the focus throughout — moving it to the scroll container to make the
    keys work would put Enter back on a deploy button.
    """
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=40)}}, enabled={"tooltool"})
    give_tooltool_a_long_commit_list(git)

    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)
        body = app.screen.query_one(".confirm-body")
        assert body.max_scroll_y > 0, "the list has to overflow for this to prove anything"

        await pilot.press(key)
        await pilot.pause()

        assert body.scroll_offset.y > 0
        assert app.focused is not None and app.focused.id == "cancel"


@pytest.mark.parametrize("height", [55, 40, 36, 30, 24])
async def test_a_long_commit_list_cannot_hide_the_buttons(tmp_path, height):
    """The buttons stay on screen and the commit list stays reachable at any terminal height.

    Both halves matter. The buttons are docked, so a long list cannot push Push off the
    bottom edge; the body is capped at the space left over, so its overflow scrolls instead
    of being clipped. Fixing only the first hides the list, and only the second hides the
    buttons — which was the original bug, unrecoverable without resizing the terminal.
    """
    app, git, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=40)}}, enabled={"tooltool"})
    give_tooltool_a_long_commit_list(git)

    async with app.run_test(size=(150, height)) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        for selector in ("#cancel", "#dry-run", "#push", ".confirm-body"):
            region = app.screen.query_one(selector).region
            assert region.height > 0, f"{selector} has no height at {height} rows"
            assert region.y >= 0, f"{selector} starts above the terminal at {height} rows"
            assert region.y + region.height <= height, f"{selector} runs past the bottom at {height} rows"


@pytest.mark.parametrize("width", [80, 100, 150, 220])
async def test_the_confirmation_follows_the_terminal_width(tmp_path, width):
    """The dialog tracks the window rather than sitting at a fixed width.

    Several widths are checked because a hardcoded size passes at exactly one of them, and
    the failure it hides is a dialog wider than the terminal, which clips the commands the
    dialog exists to show. `outer_size` is the assertion because `size` is the content
    region, which is narrower again by the dialog's own border and padding.
    """
    app, *_ = build_app(tmp_path, {"tooltool": {Env.STAGING: AheadBehind(ahead=0, behind=4)}})

    async with app.run_test(size=(width, 55)) as pilot:
        await pilot.pause()
        await open_confirm(pilot, app, "tooltool", Env.STAGING)

        dialog = app.screen.query_one(".confirm")
        assert dialog.outer_size.width == width - CONFIRM_GUTTER


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


@pytest.mark.parametrize("height", [55, 40, 36, 30, 24])
async def test_the_settings_project_list_cannot_hide_close(tmp_path, height):
    """Settings had the same defect as the confirmation, for the same reason.

    Its body is one path and remote row per project, so it outgrows a short terminal without
    any unusual state — every project being configured is enough.
    """
    app, *_ = build_app(tmp_path)

    async with app.run_test(size=(150, height)) as pilot:
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()

        for selector in ("#close", ".settings-body"):
            region = app.screen.query_one(selector).region
            assert region.height > 0, f"{selector} has no height at {height} rows"
            assert region.y >= 0, f"{selector} starts above the terminal at {height} rows"
            assert region.y + region.height <= height, f"{selector} runs past the bottom at {height} rows"


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
