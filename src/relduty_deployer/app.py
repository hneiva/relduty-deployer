# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The deploy dashboard."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable, Mapping

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Label, RichLog
from textual.worker import Worker, WorkerState

from relduty_deployer.config import Config, ConfigStore, build_projects
from relduty_deployer.gitcmd import GitClient
from relduty_deployer.models import ActionKind, DeployStatus, Env, StatusKind
from relduty_deployer.projects import Project
from relduty_deployer.refresh import EXPECTED_FAILURES, refresh, strategy_for
from relduty_deployer.screens import ConfirmDeployScreen, Decision, SettingsScreen
from relduty_deployer.strategies import Strategy, resolve
from relduty_deployer.widgets import DeployButton, DocsButton, ProjectRow, SaveButton, row_id


class RelDutyApp(App[None]):
    """Shows how far each environment is behind, and performs fast-forward deploys."""

    CSS_PATH = "app.tcss"
    TITLE = "relduty-deployer"
    SUB_TITLE = "RelEng staging and production deploys"

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("s", "save", "Save"),
        Binding("c", "settings", "Settings"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        store: ConfigStore,
        config: Config,
        strategies: Mapping[str, Strategy],
        git: GitClient,
        opener: Callable[[str], object] | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._config = config
        self._strategies = strategies
        self._git = git
        self._opener = opener if opener is not None else webbrowser.open
        self._projects = build_projects(config)

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            with Horizontal(id="toolbar"):
                yield Label(str(self._store.path), id="config-path")
                yield SaveButton(id="save")
            with Horizontal(id="column-headings"):
                yield Label("project", classes="heading-name")
                yield Label("staging", classes="heading")
                yield Label("prod", classes="heading")
            with VerticalScroll(id="rows"):
                for project in self._projects:
                    yield ProjectRow(project)
            yield RichLog(id="output", wrap=True, highlight=False, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        for note in self._store.notes:
            self._log(f"note: {note}")
        self._sync_dirty()
        self._apply_visibility()
        self.action_refresh()

    # Refreshing ---------------------------------------------------------------

    def action_refresh(self) -> None:
        """Fetch every enabled project and re-read its environments."""
        for project in self._projects:
            if project.settings.enabled:
                self._refresh_project(project)

    def _refresh_project(self, project: Project) -> None:
        # A group per project, so re-refreshing one never cancels another's fetch.
        self.run_worker(
            self._do_refresh(project),
            name=f"refresh:{project.name}",
            group=f"refresh:{project.name}",
            exclusive=True,
            exit_on_error=False,
        )

    async def _do_refresh(self, project: Project) -> None:
        row = self._row(project.name)
        row.set_all(DeployStatus.fetching())
        strategy = strategy_for(project, self._strategies)
        if strategy is None:
            message = f"unknown strategy {project.spec.strategy!r}"
            row.set_all(DeployStatus.failed(message))
            self._log(f"{project.name}: {message}")
            return
        result = await refresh(project, strategy, self._git)
        if result.failed:
            row.set_all(DeployStatus.failed(result.error))
            self._log(f"{project.name}: fetch failed: {result.error}")
            return
        for env, status in result.statuses.items():
            row.set_status(env, status)
            if status.kind is StatusKind.ERROR:
                self._log(f"{project.name} {env}: {status.error}")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Backstop for anything that escaped a worker's own error handling."""
        if event.state is WorkerState.ERROR:
            self._log(f"worker {event.worker.name or event.worker.group} failed: {event.worker.error}")

    # Deploying ----------------------------------------------------------------

    @on(Button.Pressed, ".docs")
    def _on_docs_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button
        if not isinstance(button, DocsButton):
            return
        url = button.project.spec.docs_url
        if not url:
            return
        self._log(f"{button.project.name}: opening {url}")
        self._opener(url)

    @on(Button.Pressed, ".deploy")
    def _on_deploy_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        button = event.button
        if not isinstance(button, DeployButton):
            return
        status = button.status
        if status.action is ActionKind.OPEN_URL:
            self._log(f"{button.project.name} {button.env}: opening {status.url}")
            self._opener(status.url)
            return
        # PUSH and CREATE_PR both go through confirmation before anything happens.
        if status.actionable:
            self._start_deploy(button.project, button.env)

    def _start_deploy(self, project: Project, env: Env) -> None:
        # push_screen_wait can only be awaited from a worker, so the whole flow runs in one.
        self.run_worker(
            self._do_deploy(project, env),
            name=f"deploy:{project.name}:{env}",
            group=f"deploy:{project.name}:{env}",
            exclusive=True,
            exit_on_error=False,
        )

    async def _do_deploy(self, project: Project, env: Env) -> None:
        strategy = resolve(self._strategies, project)
        row = self._row(project.name)

        try:
            # The button label may be minutes old; re-check before offering to push.
            status = await strategy.status(project, env)
            row.set_status(env, status)
            if not status.actionable:
                self._log(f"{project.name} {env}: not deploying, it is now {status.label}")
                self.notify(f"{project.name} {env} is {status.label}", severity="warning")
                return
            action = await strategy.plan(project, env)
        except EXPECTED_FAILURES as exc:
            self._log(f"{project.name} {env}: {exc}")
            self.notify(str(exc), severity="error")
            return

        async def dry_run():
            return await strategy.execute(project, env, action, dry_run=True)

        async def show_commit(sha: str) -> str:
            return await self._git.show_commit(project.settings.path, sha)

        decision = await self.push_screen_wait(
            ConfirmDeployScreen(project=project, env=env, action=action, status=status, dry_run=dry_run, show_commit=show_commit)
        )
        if decision is not Decision.PUSH:
            self._log(f"{project.name} {env}: cancelled")
            return

        self._log(f"$ {' '.join(action.argv)}")
        try:
            result = await strategy.execute(project, env, action, dry_run=False)
        except EXPECTED_FAILURES as exc:
            self._log(f"{project.name} {env}: refused: {exc}")
            self.notify(str(exc), severity="error")
            return

        self._log(result.output)
        if result.ok:
            self.notify(f"deployed {project.name} to {env}")
        else:
            self.notify(f"{project.name} {env}: push failed", severity="error")
        self._refresh_project(project)

    # Settings -----------------------------------------------------------------

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self._config, self._on_settings_changed), self._after_settings)

    def _on_settings_changed(self) -> None:
        self._sync_dirty()

    def _after_settings(self, _result: None) -> None:
        self._apply_visibility()

    @on(Button.Pressed, "#save")
    def _on_save_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_save()

    def action_save(self) -> None:
        """Write the settings to disk and pick up any changed paths or remotes."""
        if not self._store.is_dirty(self._config):
            self.notify("nothing to save")
            return
        try:
            self._store.save(self._config)
        except OSError as exc:
            self._log(f"could not save settings: {exc}")
            self.notify(f"could not save: {exc}", severity="error")
            return
        self._sync_dirty()
        self._rebuild_projects()
        self._log(f"settings saved to {self._store.path}")
        self.action_refresh()

    def _sync_dirty(self) -> None:
        self.query_one("#save", SaveButton).dirty = self._store.is_dirty(self._config)

    def _rebuild_projects(self) -> None:
        """Re-derive the projects after a settings change, and hand them to the rows."""
        self._projects = build_projects(self._config)
        for project in self._projects:
            row = self._row(project.name)
            row.project = project
            for env in project.spec.environments:
                row.button(env).project = project
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        for project in self._projects:
            self._row(project.name).display = project.settings.enabled

    # Helpers ------------------------------------------------------------------

    def _row(self, project_name: str) -> ProjectRow:
        return self.query_one(f"#{row_id(project_name)}", ProjectRow)

    def _log(self, message: str) -> None:
        self.query_one("#output", RichLog).write(message)
