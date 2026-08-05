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
from textual.screen import ModalScreen
from textual.widgets import Button, RichLog, Static

from relduty_deployer.models import DeployAction, DeployResult, DeployStatus, Env
from relduty_deployer.projects import Project

DryRun = Callable[[], Awaitable[DeployResult]]


class Decision(StrEnum):
    """What the user chose in the confirmation dialog."""

    PUSH = "push"
    CANCEL = "cancel"


class ConfirmDeployScreen(ModalScreen[Decision]):
    """Shows exactly what a deploy will run, and offers a dry run first.

    Cancel takes the initial focus so that a stray Enter cannot deploy production.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, project: Project, env: Env, action: DeployAction, status: DeployStatus, dry_run: DryRun) -> None:
        super().__init__()
        self._project = project
        self._env = env
        self._action = action
        self._status = status
        self._dry_run = dry_run

    def compose(self) -> ComposeResult:
        classes = "confirm prod" if self._env is Env.PROD else "confirm"
        with Vertical(classes=classes):
            yield Static(f"Deploy {self._project.name} to {self._env.value.upper()}", classes="confirm-title")

            if self._action.warning:
                yield Static(f"⚠  {self._action.warning}", classes="confirm-warning")

            with VerticalScroll(classes="confirm-body"):
                yield Static(self._facts(), classes="confirm-facts")
                yield Static(self._commit_summary(), classes="confirm-commits")
                yield Static(self._commands(), classes="confirm-commands")
                yield RichLog(id="dry-output", classes="confirm-dry", wrap=True, highlight=False, auto_scroll=True)

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

    def _commit_summary(self) -> str:
        if not self._action.commits:
            return "\nno commit list available"
        lines = [f"\n{len(self._action.commits) + self._action.truncated} commits will be deployed:"]
        lines += [f"  {commit}" for commit in self._action.commits]
        if self._action.truncated:
            lines.append(f"  … and {self._action.truncated} more")
        return "\n".join(lines)

    def _commands(self) -> str:
        lines = ["\nwill run", f"  {' '.join(self._action.argv)}"]
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
