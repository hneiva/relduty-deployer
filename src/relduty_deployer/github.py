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
from collections.abc import Sequence
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


@dataclass(frozen=True)
class PullRequest:
    """An open pull request."""

    number: int
    url: str
    title: str


class GitHubClient(Protocol):
    """The GitHub queries this tool needs."""

    async def published_release(self, repo: str, tag: str) -> Release | None:
        """The published release for `tag`, or None if there is no published release."""
        ...

    async def latest_release(self, repo: str) -> Release | None:
        """The most recent published, non-prerelease release."""
        ...

    async def branch_head(self, repo: str, branch: str) -> str:
        """The sha at the tip of `branch`, read from GitHub rather than a local clone."""
        ...

    async def open_pull_request(self, repo: str, title: str) -> PullRequest | None:
        """The open pull request with exactly this title, if there is one."""
        ...

    async def create_pull_request(self, repo: str, *, head: str, base: str, title: str) -> PullRequest:
        """Open a pull request from `head` into `base`."""
        ...


class GhCliGitHubClient:
    """Answers release questions by invoking `gh api`."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout

    async def _call(self, argv: Sequence[str], *, what: str) -> object | None:
        """Run a `gh` invocation and parse its JSON, returning None for a 404."""
        try:
            result = await run(argv, timeout=self._timeout)
        except CommandError as exc:
            raise GitHubUnavailableError(f"could not run gh: {exc}") from exc

        if not result.ok:
            if "not found" in result.combined.lower():
                return None
            raise GitHubError(f"{what} failed: {result.combined.strip()}")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubError(f"{what} returned unparseable JSON: {exc}") from exc

    async def _api(self, path: str) -> dict | None:
        """GET a REST path, returning None for a 404.

        A 404 is a real answer here — "no release for that tag" — so it is distinguished
        from a genuine failure such as `gh` being absent or unauthenticated.
        """
        payload = await self._call(("gh", "api", path), what=f"gh api {path}")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GitHubError(f"gh api {path} returned {type(payload).__name__}, expected an object")
        return payload

    async def _api_list(self, path: str) -> list:
        """GET a REST path that answers with an array. A 404 is an empty result."""
        payload = await self._call(("gh", "api", path), what=f"gh api {path}")
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise GitHubError(f"gh api {path} returned {type(payload).__name__}, expected an array")
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

    @staticmethod
    def _to_pull_request(payload: dict) -> PullRequest:
        number = payload.get("number")
        if not isinstance(number, int):
            raise GitHubError(f"pull request payload has no number: {payload!r}")
        return PullRequest(number=number, url=str(payload.get("html_url") or ""), title=str(payload.get("title") or ""))

    async def branch_head(self, repo: str, branch: str) -> str:
        payload = await self._api(f"repos/{repo}/commits/{branch}")
        if payload is None:
            raise GitHubError(f"{repo} has no branch {branch!r}")
        sha = payload.get("sha")
        if not isinstance(sha, str) or not sha:
            raise GitHubError(f"commit payload for {repo}@{branch} has no sha: {payload!r}")
        return sha

    async def open_pull_request(self, repo: str, title: str) -> PullRequest | None:
        """Matched on the exact title, because that is all the bump PRs are identified by."""
        for payload in await self._api_list(f"repos/{repo}/pulls?state=open&per_page=100"):
            if isinstance(payload, dict) and payload.get("title") == title:
                return self._to_pull_request(payload)
        return None

    async def create_pull_request(self, repo: str, *, head: str, base: str, title: str) -> PullRequest:
        argv = (
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repo}/pulls",
            "-f",
            f"title={title}",
            "-f",
            f"head={head}",
            "-f",
            f"base={base}",
        )
        payload = await self._call(argv, what=f"gh api POST repos/{repo}/pulls")
        if not isinstance(payload, dict):
            raise GitHubError(f"opening a pull request on {repo} returned {type(payload).__name__}, expected an object")
        return self._to_pull_request(payload)
