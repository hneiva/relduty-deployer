# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The confirmation screen: what a deploy will run, and the choice to run it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Rule, Static

from relduty_deployer.models import DeployAction, DeployResult, DeployStatus, Env
from relduty_deployer.projects import Project
from relduty_deployer.screens.commit_detail import CommitDetailScreen, ShowCommit

DryRun = Callable[[], Awaitable[DeployResult]]

_HEX = frozenset("0123456789abcdef")


def split_commit(entry: str) -> tuple[str, str]:
    """Split a `git log --oneline` entry into its sha and its subject.

    Returns an empty sha for anything that does not start with a plausible abbreviated
    sha, which is what keeps a surprising line rendering as plain text instead of turning
    into a link that cannot resolve.
    """
    sha, _, subject = entry.partition(" ")
    if 4 <= len(sha) <= 40 and set(sha) <= _HEX:
        return sha, subject
    return "", entry


class Decision(StrEnum):
    """What the user chose in the confirmation dialog."""

    PUSH = "push"
    CANCEL = "cancel"


class ConfirmDeployScreen(ModalScreen[Decision]):
    """Shows exactly what a deploy will run, and offers a dry run first.

    Cancel takes the initial focus so that a stray Enter cannot deploy production.
    """

    # The paging keys are bound on the screen rather than left to the focused widget.
    # Cancel holds the focus so a stray Enter cannot deploy, which means nothing focusable
    # scrolls the commit list, and reaching it by Tab takes three presses past Push.
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("pageup", "scroll_commits_up", "Scroll commits up"),
        ("pagedown", "scroll_commits_down", "Scroll commits down"),
        ("home", "scroll_commits_home", "First commit"),
        ("end", "scroll_commits_end", "Last commit"),
    ]

    def __init__(
        self,
        *,
        project: Project,
        env: Env,
        action: DeployAction,
        status: DeployStatus,
        dry_run: DryRun,
        show_commit: ShowCommit,
    ) -> None:
        super().__init__()
        self._project = project
        self._env = env
        self._action = action
        self._status = status
        self._dry_run = dry_run
        self._show_commit = show_commit

    def compose(self) -> ComposeResult:
        classes = "confirm prod" if self._env is Env.PROD else "confirm"
        with Vertical(classes=classes):
            yield Static(f"Deploy {self._project.name} to {self._env.value.upper()}", classes="confirm-title")

            if self._action.warning:
                yield Static(f"⚠  {self._action.warning}", classes="confirm-warning")

            # Only the commit list scrolls. The command about to run stays outside the
            # scrolling region because it is the line being audited: inside it, a long
            # commit list pushed it out of view on a short terminal, and Cancel holds the
            # focus, so there was no key that would bring it back.
            with VerticalScroll(classes="confirm-body"):
                yield Static(self._facts(), classes="confirm-facts")
                yield from self._commit_summary()
                yield RichLog(id="dry-output", classes="confirm-dry", wrap=True, highlight=False, auto_scroll=True)

            # One docked footer rather than three docked siblings: `max-height: 1fr` on the
            # body reserves space for docked siblings only, so anything that must survive a
            # long commit list has to be inside this container.
            with Vertical(classes="confirm-footer"):
                # What will ship and what will run are the two things to check, and they
                # read as one wall of text without a divider between them.
                yield Rule()
                yield Static(self._commands(), classes="confirm-commands")
                with Horizontal(classes="confirm-buttons"):
                    yield Button("Cancel", id="cancel", variant="primary")
                    yield Button("Dry run", id="dry-run")
                    yield Button("Push", id="push", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def _facts(self) -> str:
        settings = self._project.settings
        lines = [
            f"repo      {settings.path}",
            f"remote    {settings.remote} → {self._action.remote_url or 'unknown'}",
            f"push      {self._action.description}",
            f"status    {self._status.label}",
        ]
        return "\n".join(lines)

    def _commit_summary(self) -> ComposeResult:
        """The commit list, one widget per commit so each sha can be its own link.

        The sha is interpolated into the click action, which is safe only because
        `split_commit` has already established that it is hex. The subject goes through a
        markup variable instead, since a subject like "[skip ci] fix" would otherwise be
        read as a tag.
        """
        if not self._action.commits:
            yield Static("\nno commit list available", classes="confirm-commits")
            return

        total = len(self._action.commits) + self._action.truncated
        yield Static(f"\n{total} commits will be deployed:", classes="confirm-commits")
        for entry in self._action.commits:
            sha, subject = split_commit(entry)
            if not sha:
                yield Static(f"  {entry}", classes="confirm-commit", markup=False)
                continue
            yield Static(
                Content.from_markup(f"  [@click=screen.show_commit('{sha}')]{sha}[/] $subject", subject=subject),
                classes="confirm-commit",
            )
        if self._action.truncated:
            yield Static(f"  … and {self._action.truncated} more", classes="confirm-commit")

    def _commands(self) -> str:
        # No leading blank line: the Rule above this already carries a row of margin.
        lines = ["will run", f"  {' '.join(self._action.argv)}"]
        if self._action.documented_equivalent:
            lines += ["documented equivalent", f"  {self._action.documented_equivalent}"]
        return "\n".join(lines)

    @on(Button.Pressed, "#dry-run")
    def _on_dry_run(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_worker(self._perform_dry_run(), exclusive=True, exit_on_error=False)

    async def _perform_dry_run(self) -> None:
        log = self.query_one("#dry-output", RichLog)
        log.write("$ " + " ".join(self._action.argv) + " --dry-run")
        try:
            result = await self._dry_run()
        except Exception as exc:  # surfaced in the dialog rather than killing the app
            log.write(f"dry run failed: {exc}")
            return
        log.write(result.output)
        log.write("dry run finished; nothing was pushed" if result.ok else "dry run reported a problem")

    @on(Button.Pressed, "#push")
    def _on_push(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(Decision.PUSH)

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(Decision.CANCEL)

    def action_cancel(self) -> None:
        self.dismiss(Decision.CANCEL)

    def _body(self) -> VerticalScroll:
        return self.query_one(".confirm-body", VerticalScroll)

    def action_scroll_commits_up(self) -> None:
        self._body().scroll_page_up()

    def action_scroll_commits_down(self) -> None:
        self._body().scroll_page_down()

    def action_scroll_commits_home(self) -> None:
        self._body().scroll_home()

    def action_scroll_commits_end(self) -> None:
        self._body().scroll_end()

    def action_show_commit(self, sha: str) -> None:
        """Open one commit's details. Pushed on top, so closing it returns here."""
        self.app.push_screen(CommitDetailScreen(sha=sha, subject=self._subject_for(sha), show=self._show_commit))

    def _subject_for(self, sha: str) -> str:
        for entry in self._action.commits:
            found, subject = split_commit(entry)
            if found == sha:
                return subject
        return ""
