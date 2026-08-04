"""Reading the version a service actually has deployed.

Mozilla services expose a Dockerflow `/__version__` endpoint reporting the version and
the exact commit that was built. For balrog this is the only honest source of what is
live, since its rollout happens in ArgoCD rather than through a git push.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

DEFAULT_TIMEOUT = 10.0


class ProbeError(RuntimeError):
    """A deployed version could not be read."""


@dataclass(frozen=True)
class DeployedVersion:
    """What a `/__version__` endpoint reported."""

    version: str
    commit: str
    url: str


class VersionProbe(Protocol):
    """Reads a Dockerflow version endpoint."""

    async def probe(self, url: str) -> DeployedVersion: ...


class HttpxVersionProbe:
    """Fetches `/__version__` over HTTP.

    `transport` exists so tests can serve responses in-process; leaving it unset uses the
    real network.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._timeout = timeout
        self._transport = transport

    async def probe(self, url: str) -> DeployedVersion:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True, transport=self._transport) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            raise ProbeError(f"could not read {url}: {exc}") from exc
        except ValueError as exc:
            raise ProbeError(f"{url} did not return JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ProbeError(f"{url} returned {type(payload).__name__}, expected an object")
        version = payload.get("version")
        if not isinstance(version, str) or not version:
            raise ProbeError(f"{url} reported no version: {payload!r}")
        return DeployedVersion(version=version, commit=str(payload.get("commit") or ""), url=url)
