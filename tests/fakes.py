# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Test doubles for the injected collaborators.

None of these inherit from anything in the package. That is the point of defining the
interfaces as Protocols: a fake only has to have the right methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from relduty_deployer.gitcmd import FetchSpec, GitError
from relduty_deployer.models import AheadBehind, DeployResult

CANONICAL_URL = "git@github.com:mozilla-releng/{repo}.git"


@dataclass
class FakeGitClient:
    """An in-memory GitClient that records what it was asked to do."""

    counts: dict[tuple[str, str], AheadBehind] = field(default_factory=dict)
    shas: dict[str, str] = field(default_factory=dict)
    files: dict[tuple[str, str], str] = field(default_factory=dict)
    commits: dict[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)
    known_objects: set[str] = field(default_factory=set)
    remote_urls: dict[str, str] = field(default_factory=dict)
    default_remote_url: str = ""
    push_ok: bool = True
    fetch_error: str = ""

    fetched: list[tuple[Path, str, FetchSpec]] = field(default_factory=list)
    pushed: list[tuple[str, str, bool]] = field(default_factory=list)

    async def fetch(self, path: Path, remote: str, *, spec: FetchSpec) -> None:
        if self.fetch_error:
            raise GitError(self.fetch_error)
        self.fetched.append((path, remote, spec))

    async def ahead_behind(self, path: Path, *, target_ref: str, source_ref: str) -> AheadBehind:
        try:
            return self.counts[(target_ref, source_ref)]
        except KeyError:
            raise GitError(f"unknown revision range {target_ref}...{source_ref}") from None

    async def rev_parse(self, path: Path, rev: str) -> str:
        try:
            return self.shas[rev]
        except KeyError:
            raise GitError(f"unknown revision {rev}") from None

    async def has_commit(self, path: Path, rev: str) -> bool:
        return rev in self.known_objects

    async def show_file(self, path: Path, ref: str, file: str) -> str:
        try:
            return self.files[(ref, file)]
        except KeyError:
            raise GitError(f"{ref}:{file} does not exist") from None

    async def commit_list(self, path: Path, *, target_ref: str, source_ref: str, limit: int) -> tuple[str, ...]:
        return self.commits.get((target_ref, source_ref), ())[:limit]

    async def remote_url(self, path: Path, remote: str) -> str:
        url = self.remote_urls.get(remote, self.default_remote_url)
        if not url:
            raise GitError(f"no such remote {remote!r}")
        return url

    async def push(self, path: Path, remote: str, *, sha: str, target_branch: str, dry_run: bool) -> DeployResult:
        self.pushed.append((sha, target_branch, dry_run))
        return DeployResult(
            ok=self.push_ok,
            output="rejected" if not self.push_ok else f"{sha} -> {target_branch}",
            dry_run=dry_run,
        )
