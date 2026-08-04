# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Modal screens: confirming a deploy, and editing settings."""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from enum import StrEnum
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, RichLog, Static

from relduty_deployer.config import Config
from relduty_deployer.models import DeployAction, DeployResult, DeployStatus, Env
from relduty_deployer.projects import PROJECT_SPECS, Project

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


class SettingsScreen(ModalScreen[None]):
    """Edits the machine-local settings: checkout path, remote, and whether to show a project.

    Which branch deploys which environment is deliberately not editable — that is a fact
    about the repository, and a wrong value would push to a branch nothing watches.
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, config: Config, on_change: Callable[[], None]) -> None:
        super().__init__()
        self._config = config
        self._on_change = on_change

    def compose(self) -> ComposeResult:
        with Vertical(classes="settings"):
            yield Static("Settings", classes="confirm-title")
            yield Static(
                "Deploy branches are not configurable: they mirror each repository's Taskcluster branch gate.",
                classes="settings-note",
            )
            with VerticalScroll(classes="settings-body"):
                for spec in PROJECT_SPECS:
                    settings = self._config.projects.get(spec.name)
                    if settings is None:
                        continue
                    yield Static(f"{spec.name}  ({spec.source_branch} → {self._targets(spec)})", classes="settings-project")
                    with Horizontal(classes="settings-fields"):
                        yield Label("path", classes="settings-label")
                        yield Input(value=str(settings.path), id=f"path-{spec.name}", classes="settings-input")
                    with Horizontal(classes="settings-fields"):
                        yield Label("remote", classes="settings-label")
                        yield Input(value=settings.remote, id=f"remote-{spec.name}", classes="settings-remote")
                        yield Checkbox("shown", value=settings.enabled, id=f"enabled-{spec.name}")
            with Horizontal(classes="confirm-buttons"):
                yield Button("Close", id="close", variant="primary")

    @staticmethod
    def _targets(spec) -> str:
        if not spec.targets:
            return "no deploy branches"
        return ", ".join(f"{env.value}: {branch}" for env, branch in spec.targets.items())

    @on(Input.Changed)
    def _on_input(self, event: Input.Changed) -> None:
        assert event.input.id is not None
        field, _, name = event.input.id.partition("-")
        current = self._config.projects.get(name)
        if current is None:
            return
        if field == "path":
            updated = dataclasses.replace(current, path=Path(event.value).expanduser())
        else:
            updated = dataclasses.replace(current, remote=event.value)
        self._config.projects[name] = updated
        self._on_change()

    @on(Checkbox.Changed)
    def _on_checkbox(self, event: Checkbox.Changed) -> None:
        assert event.checkbox.id is not None
        _, _, name = event.checkbox.id.partition("-")
        current = self._config.projects.get(name)
        if current is None:
            return
        self._config.projects[name] = dataclasses.replace(current, enabled=event.value)
        self._on_change()

    @on(Button.Pressed, "#close")
    def _on_close(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
