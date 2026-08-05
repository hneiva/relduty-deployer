# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Widgets for the deploy dashboard."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Button, Label

from relduty_deployer.models import DeployStatus, Env
from relduty_deployer.projects import Project

SAVED_LABEL = "Saved ☑️"
UNSAVED_LABEL = "Save"
DOCS_LABEL = "📚"


def slug(text: str) -> str:
    """A string usable as a Textual widget id."""
    return text.replace(".", "-").replace("/", "-")


def button_id(project_name: str, env: Env) -> str:
    return f"btn-{slug(project_name)}-{env.value}"


def docs_id(project_name: str) -> str:
    return f"docs-{slug(project_name)}"


def row_id(project_name: str) -> str:
    return f"row-{slug(project_name)}"


class SaveButton(Button):
    """Reflects whether the settings in memory differ from the ones on disk."""

    dirty: reactive[bool] = reactive(False)

    def __init__(self, **kwargs) -> None:
        super().__init__(SAVED_LABEL, variant="success", **kwargs)

    def watch_dirty(self, dirty: bool) -> None:
        self.label = UNSAVED_LABEL if dirty else SAVED_LABEL
        self.variant = "error" if dirty else "success"
        self.tooltip = "Write the changed settings to disk" if dirty else "Settings match the config file"


class DeployButton(Button):
    """One environment of one project.

    The label and colour come straight from the status, and the button is only enabled when
    pressing it would actually do something.
    """

    def __init__(self, project: Project, env: Env, **kwargs) -> None:
        self.project = project
        self.env = env
        self.status = DeployStatus.fetching()
        super().__init__(self.status.label, classes="deploy", **kwargs)
        self.disabled = True

    def apply(self, status: DeployStatus) -> None:
        """Show `status`."""
        self.status = status
        self.label = status.label
        self.variant = status.variant
        self.disabled = not status.clickable
        self.tooltip = status.tooltip or status.label


class DocsButton(Button):
    """Opens a project's deploy documentation.

    Disabled when the project has no documented procedure, rather than hidden, so that the
    rows stay the same shape.
    """

    def __init__(self, project: Project, **kwargs) -> None:
        self.project = project
        super().__init__(DOCS_LABEL, classes="docs", **kwargs)
        url = project.spec.docs_url
        self.disabled = not url
        self.tooltip = f"Deploy documentation: {url}" if url else f"No deploy documentation recorded for {project.name}"


class ProjectRow(Horizontal):
    """A project's name, one button per environment, then its documentation."""

    def __init__(self, project: Project, **kwargs) -> None:
        self.project = project
        super().__init__(id=row_id(project.name), classes="project-row", **kwargs)

    def compose(self) -> ComposeResult:
        yield Label(self.project.name, classes="project-name")
        for env in self.project.spec.environments:
            yield DeployButton(self.project, env, id=button_id(self.project.name, env))
        yield DocsButton(self.project, id=docs_id(self.project.name))

    def button(self, env: Env) -> DeployButton:
        return self.query_one(f"#{button_id(self.project.name, env)}", DeployButton)

    def set_status(self, env: Env, status: DeployStatus) -> None:
        self.button(env).apply(status)

    def set_all(self, status: DeployStatus) -> None:
        for env in self.project.spec.environments:
            self.set_status(env, status)
