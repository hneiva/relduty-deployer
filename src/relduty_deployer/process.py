"""Running external commands.

Everything here takes an argv sequence and never a shell string: repository paths come
from user config, so a shell would make them injectable. `shlex.join` appears only to
render a command for a human to read.
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class CommandError(RuntimeError):
    """A command could not be run, or did not finish in time."""


@dataclass(frozen=True)
class CommandOutput:
    """The result of one command. A non-zero exit is a result, not an error."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def combined(self) -> str:
        """Both streams, for showing the user what the command said."""
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)


async def run(argv: Sequence[str], *, timeout: float, env: Mapping[str, str] | None = None) -> CommandOutput:
    """Run `argv` to completion, capturing both streams.

    stdin is closed so that a command which decides to prompt gets EOF immediately
    instead of blocking forever, and the timeout kills anything that still hangs. Both
    matter because a stalled subprocess behind a full-screen TUI shows no symptom at all.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=None if env is None else dict(env),
        )
    except OSError as exc:
        raise CommandError(f"could not run {shlex.join(argv)}: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        process.kill()
        await process.wait()
        raise CommandError(f"timed out after {timeout:g}s: {shlex.join(argv)}") from None

    return CommandOutput(
        returncode=process.returncode if process.returncode is not None else -1,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
    )
