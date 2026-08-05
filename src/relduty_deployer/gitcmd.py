# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Running git.

The argv builders are pure functions so that the exact command shape can be asserted in
tests without running anything, which is what protects the ahead/behind comparison from
being inverted.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from relduty_deployer.models import AheadBehind, DeployResult
from relduty_deployer.process import CommandError, CommandOutput, run

DEFAULT_TIMEOUT = 20.0
DEFAULT_FETCH_TIMEOUT = 120.0
DEFAULT_MAX_CONCURRENT_FETCHES = 4


class GitError(RuntimeError):
    """A git invocation failed. The message carries the command and git's stderr."""


@dataclass(frozen=True)
class FetchSpec:
    """What a strategy needs fetched before its status can be trusted."""

    tags: bool = False
    prune: bool = False


def rev_list_count_argv(path: Path, *, target_ref: str, source_ref: str) -> tuple[str, ...]:
    """Build the divergence command.

    Emits::

        git -C <path> rev-list --left-right --count <TARGET_REF>...<SOURCE_REF>

    Output is one line of two tab-separated integers, ``"<LEFT>\\t<RIGHT>"``:

    * ``LEFT``  — commits reachable from TARGET_REF but not SOURCE_REF, so `ahead`
    * ``RIGHT`` — commits reachable from SOURCE_REF but not TARGET_REF, so `behind`

    Three dots, not two: ``..`` is an asymmetric range and would print a single number
    that looks entirely plausible. The mnemonic is that the Target goes on the Left, and
    the Left count is Ahead.

    Verified against a real repository::

        $ git -C ~/dev/scriptworker-scripts rev-list --left-right --count \\
              refs/remotes/origin/dev...refs/remotes/origin/master
        0	14

    `dev` needed 14 commits from `master`, so ahead=0 and behind=14.
    """
    return ("git", "-C", str(path), "rev-list", "--left-right", "--count", f"{target_ref}...{source_ref}")


def commit_list_argv(path: Path, *, target_ref: str, source_ref: str, limit: int) -> tuple[str, ...]:
    """Build the command listing the commits a deploy would ship.

    Emits ``git log --oneline <TARGET_REF>..<SOURCE_REF>`` — two dots here, an asymmetric
    range, unlike the three dots in `rev_list_count_argv`. One extra commit is requested
    so the caller can tell that the list was truncated.
    """
    return (
        "git",
        "-C",
        str(path),
        "log",
        "--oneline",
        "--no-decorate",
        f"--max-count={limit + 1}",
        f"{target_ref}..{source_ref}",
    )


def fetch_argv(path: Path, remote: str, *, spec: FetchSpec) -> tuple[str, ...]:
    """Build the fetch command for a single named remote.

    Never `--all`: a clone may also have a personal `fork` remote, and fetching it wastes
    time and can prompt for separate credentials.
    """
    args = ["git", "-C", str(path), "fetch", "--quiet", "--no-write-fetch-head"]
    if spec.tags:
        args.append("--tags")
    if spec.prune:
        args.append("--prune")
    args.append(remote)
    return tuple(args)


def push_argv(path: Path, remote: str, *, sha: str, target_branch: str, dry_run: bool) -> tuple[str, ...]:
    """Build the deploy push.

    Pushes a resolved commit sha rather than a branch name, so what is pushed is exactly
    what the status was measured from — a local branch can be far behind its
    remote-tracking ref. The destination is fully qualified so a same-named tag cannot
    make it ambiguous. There is no code path here that adds `--force`.
    """
    args = ["git", "-C", str(path), "push"]
    if dry_run:
        args.append("--dry-run")
    args += [remote, f"{sha}:refs/heads/{target_branch}"]
    return tuple(args)


# Four guards against git blocking forever on an interactive prompt. A subprocess waiting
# on a terminal from inside a full-screen TUI hangs invisibly: no error, no output.
_ENV_GUARDS: Mapping[str, str] = MappingProxyType(
    {
        "GIT_TERMINAL_PROMPT": "0",
        "SSH_ASKPASS_REQUIRE": "never",
        "GIT_OPTIONAL_LOCKS": "0",
    }
)


def _child_env() -> dict[str, str]:
    """The environment git runs in."""
    env = dict(os.environ)
    env.update(_ENV_GUARDS)
    # Respect an existing GIT_SSH_COMMAND, but default to one that refuses to prompt.
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


class GitClient(Protocol):
    """The git operations this tool needs.

    The ref parameters are keyword-only throughout so that a target and a source cannot
    be transposed at a call site.
    """

    async def fetch(self, path: Path, remote: str, *, spec: FetchSpec) -> None: ...

    async def ahead_behind(self, path: Path, *, target_ref: str, source_ref: str) -> AheadBehind: ...

    async def rev_parse(self, path: Path, rev: str) -> str: ...

    async def has_commit(self, path: Path, rev: str) -> bool: ...

    async def show_file(self, path: Path, ref: str, file: str) -> str: ...

    async def commit_list(self, path: Path, *, target_ref: str, source_ref: str, limit: int) -> tuple[str, ...]: ...

    async def show_commit(self, path: Path, sha: str) -> str: ...

    async def commit_replacing_file(self, path: Path, *, base_ref: str, file: str, content: str, message: str) -> str: ...

    async def remote_url(self, path: Path, remote: str) -> str: ...

    async def push(self, path: Path, remote: str, *, sha: str, target_branch: str, dry_run: bool) -> DeployResult: ...


class SubprocessGitClient:
    """Runs git as a subprocess, with prompts disabled and every call bounded by a timeout."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        max_concurrent_fetches: int = DEFAULT_MAX_CONCURRENT_FETCHES,
    ) -> None:
        self._timeout = timeout
        self._fetch_timeout = fetch_timeout
        self._fetch_semaphore = asyncio.Semaphore(max_concurrent_fetches)

    async def _run(self, argv: Sequence[str], *, timeout: float) -> CommandOutput:
        """Run git and capture both streams. Does not raise on a non-zero exit."""
        try:
            return await run(argv, timeout=timeout, env=_child_env())
        except CommandError as exc:
            raise GitError(str(exc)) from exc

    async def _checked(self, argv: Sequence[str], *, timeout: float | None = None) -> str:
        """Run git and return stdout, raising `GitError` if it failed."""
        result = await self._run(argv, timeout=self._timeout if timeout is None else timeout)
        if not result.ok:
            raise GitError(f"exit {result.returncode}: {shlex.join(argv)}\n{result.combined}")
        return result.stdout

    async def _checked_with_env(self, argv: Sequence[str], *, env: Mapping[str, str]) -> str:
        """As `_checked`, but for the plumbing steps that need GIT_INDEX_FILE set."""
        try:
            result = await run(argv, timeout=self._timeout, env=env)
        except CommandError as exc:
            raise GitError(str(exc)) from exc
        if not result.ok:
            raise GitError(f"exit {result.returncode}: {shlex.join(argv)}\n{result.combined}")
        return result.stdout

    async def fetch(self, path: Path, remote: str, *, spec: FetchSpec) -> None:
        """Update remote-tracking refs. Concurrency-limited to avoid a burst of auth prompts."""
        async with self._fetch_semaphore:
            await self._checked(fetch_argv(path, remote, spec=spec), timeout=self._fetch_timeout)

    async def ahead_behind(self, path: Path, *, target_ref: str, source_ref: str) -> AheadBehind:
        """How far `target_ref` diverges from `source_ref`."""
        argv = rev_list_count_argv(path, target_ref=target_ref, source_ref=source_ref)
        raw = await self._checked(argv)
        parts = raw.split()
        if len(parts) != 2:
            raise GitError(f"expected two counts from {shlex.join(argv)}, got {raw!r}")
        left, right = parts
        try:
            return AheadBehind(ahead=int(left), behind=int(right))
        except ValueError as exc:
            raise GitError(f"non-numeric counts {raw!r} from {shlex.join(argv)}") from exc

    async def rev_parse(self, path: Path, rev: str) -> str:
        """Resolve `rev` to a full commit sha."""
        return (await self._checked(("git", "-C", str(path), "rev-parse", rev))).strip()

    async def has_commit(self, path: Path, rev: str) -> bool:
        """Whether `rev` names a commit that exists in this clone."""
        result = await self._run(("git", "-C", str(path), "cat-file", "-e", f"{rev}^{{commit}}"), timeout=self._timeout)
        return result.ok

    async def show_file(self, path: Path, ref: str, file: str) -> str:
        """Read a file as it exists at `ref`, never from the working tree."""
        return await self._checked(("git", "-C", str(path), "show", f"{ref}:{file}"))

    async def commit_list(self, path: Path, *, target_ref: str, source_ref: str, limit: int) -> tuple[str, ...]:
        """The commits a deploy would ship, capped at `limit` entries."""
        raw = await self._checked(commit_list_argv(path, target_ref=target_ref, source_ref=source_ref, limit=limit))
        lines = tuple(line for line in raw.splitlines() if line.strip())
        return lines[:limit]

    async def show_commit(self, path: Path, sha: str) -> str:
        """One commit in full: message, changed files, and diff.

        `--no-color` because the output is rendered as plain text; git would omit colour
        anyway when stdout is a pipe, but not if the user has `color.ui = always` set.
        """
        return await self._checked(("git", "-C", str(path), "show", "--no-color", "--stat", "--patch", sha))

    async def commit_replacing_file(self, path: Path, *, base_ref: str, file: str, content: str, message: str) -> str:
        """Build a commit on top of `base_ref` with one file replaced, and return its sha.

        Plumbing only, so the working tree, the index and HEAD are all left alone. The
        checkout may be dirty or sitting on an unrelated branch and this still works, which
        matters because the repository being committed to is one the user works in.

        The tree is assembled through a temporary index named by GIT_INDEX_FILE rather than
        the repository's own. The new blob does land in the object database, unreferenced
        until something points at it; git collects it if the push never happens.
        """
        parent = (await self._checked(("git", "-C", str(path), "rev-parse", f"{base_ref}^{{commit}}"))).strip()

        with tempfile.TemporaryDirectory() as scratch:
            blob_source = Path(scratch) / "content"
            blob_source.write_text(content)
            blob = (await self._checked(("git", "-C", str(path), "hash-object", "-w", "--path", file, str(blob_source)))).strip()

            env = _child_env()
            env["GIT_INDEX_FILE"] = str(Path(scratch) / "index")
            await self._checked_with_env(("git", "-C", str(path), "read-tree", parent), env=env)
            await self._checked_with_env(("git", "-C", str(path), "update-index", "--add", "--cacheinfo", f"100644,{blob},{file}"), env=env)
            tree = (await self._checked_with_env(("git", "-C", str(path), "write-tree"), env=env)).strip()

        argv = ("git", "-C", str(path), "commit-tree", tree, "-p", parent, "-m", message)
        return (await self._checked(argv)).strip()

    async def remote_url(self, path: Path, remote: str) -> str:
        """The URL a named remote points at."""
        return (await self._checked(("git", "-C", str(path), "remote", "get-url", remote))).strip()

    async def push(self, path: Path, remote: str, *, sha: str, target_branch: str, dry_run: bool) -> DeployResult:
        """Push `sha` to `target_branch`, reporting failure rather than raising.

        Push output is shown to the user either way, so a rejected push is a result to
        display, not an exception to unwind.
        """
        argv = push_argv(path, remote, sha=sha, target_branch=target_branch, dry_run=dry_run)
        try:
            result = await self._run(argv, timeout=self._fetch_timeout)
        except GitError as exc:
            return DeployResult(ok=False, output=str(exc), argv=argv, dry_run=dry_run)
        return DeployResult(
            ok=result.ok,
            output=result.combined or "(git printed nothing)",
            argv=argv,
            dry_run=dry_run,
        )
