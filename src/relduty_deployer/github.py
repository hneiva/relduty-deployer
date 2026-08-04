# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Reading GitHub release state.

Balrog deploys by publishing a GitHub release, so knowing whether a version has been
released is what tells us whether it has shipped. This shells out to `gh`, which is
already authenticated on a RelEng machine, rather than handling a token here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from relduty_deployer.process import CommandError, run

DEFAULT_TIMEOUT = 20.0


class GitHubError(RuntimeError):
    """A GitHub query could not be answered."""


class GitHubUnavailableError(GitHubError):
    """The `gh` CLI is missing or not authenticated."""


@dataclass(frozen=True)
class Release:
    """A published GitHub release."""

    tag: str
    published_at: str


class GitHubClient(Protocol):
    """The GitHub queries this tool needs."""

    async def published_release(self, repo: str, tag: str) -> Release | None:
        """The published release for `tag`, or None if there is no published release."""
        ...

    async def latest_release(self, repo: str) -> Release | None:
        """The most recent published, non-prerelease release."""
        ...


class GhCliGitHubClient:
    """Answers release questions by invoking `gh api`."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def _api(self, path: str) -> dict | None:
        """GET a REST path, returning None for a 404.

        A 404 is a real answer here — "no release for that tag" — so it is distinguished
        from a genuine failure such as `gh` being absent or unauthenticated.
        """
        argv = ("gh", "api", path)
        try:
            result = await run(argv, timeout=self._timeout)
        except CommandError as exc:
            raise GitHubUnavailableError(f"could not run gh: {exc}") from exc

        if not result.ok:
            if "not found" in result.combined.lower():
                return None
            raise GitHubError(f"gh api {path} failed: {result.combined.strip()}")

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh api {path} returned unparseable JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise GitHubError(f"gh api {path} returned {type(payload).__name__}, expected an object")
        return payload

    @staticmethod
    def _to_release(payload: dict) -> Release | None:
        """Convert a release payload, treating a draft as not released."""
        if payload.get("draft"):
            return None
        tag = payload.get("tag_name")
        if not isinstance(tag, str) or not tag:
            raise GitHubError(f"release payload has no tag_name: {payload!r}")
        return Release(tag=tag, published_at=str(payload.get("published_at") or ""))

    async def published_release(self, repo: str, tag: str) -> Release | None:
        payload = await self._api(f"repos/{repo}/releases/tags/{tag}")
        return None if payload is None else self._to_release(payload)

    async def latest_release(self, repo: str) -> Release | None:
        # This endpoint already excludes drafts and prereleases.
        payload = await self._api(f"repos/{repo}/releases/latest")
        return None if payload is None else self._to_release(payload)
