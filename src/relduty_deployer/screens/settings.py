# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The settings screen: checkout path, remote, and whether to show a project."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from relduty_deployer.config import Config
from relduty_deployer.projects import PROJECT_SPECS


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
