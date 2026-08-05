# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The commit detail screen: one commit in full, as `git show` renders it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static

ShowCommit = Callable[[str], Awaitable[str]]

LOADING = "loading…"


class CommitDetailScreen(ModalScreen[None]):
    """Shows one commit's message, changed files, and diff.

    The lookup is injected as a callable rather than taken as a git client, which is how
    the confirmation screen receives its dry run: the screen stays unaware of git.
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, *, sha: str, subject: str, show: ShowCommit) -> None:
        super().__init__()
        self._sha = sha
        self._subject = subject
        self._show = show

    def compose(self) -> ComposeResult:
        with Vertical(classes="commit-detail"):
            yield Static(f"{self._sha}  {self._subject}", classes="confirm-title", markup=False)
            with VerticalScroll(classes="commit-detail-body"):
                # markup=False because a diff is arbitrary text, and a line like `[bold]`
                # in someone's patch would otherwise be parsed as a tag.
                yield Static(LOADING, id="commit-body", markup=False)
            with Horizontal(classes="confirm-buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()
        self.run_worker(self._load(), exclusive=True, exit_on_error=False)

    async def _load(self) -> None:
        body = self.query_one("#commit-body", Static)
        try:
            details = await self._show(self._sha)
        except Exception as exc:  # surfaced in the screen rather than killing the app
            body.update(f"could not read {self._sha}: {exc}")
            return
        body.update(details.rstrip("\n") or f"{self._sha} produced no output")

    @on(Button.Pressed, "#close")
    def _on_close(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
