"""Types describing deploy environments, their status, and planned actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Env(StrEnum):
    """A deploy environment."""

    STAGING = "staging"
    PROD = "prod"


class StatusKind(StrEnum):
    """How an environment compares to its source branch."""

    FETCHING = "fetching"
    UP_TO_DATE = "up_to_date"
    BEHIND = "behind"
    DIVERGED = "diverged"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"


# Textual Button variants, which map to the -success/-warning/-error CSS classes.
_VARIANTS = {
    StatusKind.FETCHING: "default",
    StatusKind.UP_TO_DATE: "success",
    StatusKind.BEHIND: "warning",
    StatusKind.DIVERGED: "error",
    StatusKind.NOT_IMPLEMENTED: "default",
    StatusKind.ERROR: "error",
}


def _plural(count: int, word: str) -> str:
    """Render `count` with `word` pluralised by an appended s."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


@dataclass(frozen=True)
class DeployStatus:
    """The state of one environment of one project.

    `behind` counts commits on the source branch that the deploy branch is missing —
    the work a deploy would ship. `ahead` counts commits on the deploy branch that the
    source branch is missing, which means the branch has diverged and must be resolved
    by hand.
    """

    kind: StatusKind
    behind: int = 0
    ahead: int = 0
    detail: str = ""
    error: str = ""

    @classmethod
    def from_counts(cls, *, behind: int, ahead: int, detail: str = "") -> DeployStatus:
        """Classify a raw ahead/behind pair.

        This is the only place the two counts are turned into a verdict, so callers
        cannot disagree about which direction blocks a deploy.
        """
        if ahead:
            kind = StatusKind.DIVERGED
        elif behind:
            kind = StatusKind.BEHIND
        else:
            kind = StatusKind.UP_TO_DATE
        return cls(kind=kind, behind=behind, ahead=ahead, detail=detail)

    @classmethod
    def fetching(cls) -> DeployStatus:
        """Status shown while the project's remote is being fetched."""
        return cls(kind=StatusKind.FETCHING)

    @classmethod
    def failed(cls, message: str) -> DeployStatus:
        """Status shown when the comparison could not be made at all."""
        return cls(kind=StatusKind.ERROR, error=message)

    @classmethod
    def unimplemented(cls, detail: str = "") -> DeployStatus:
        """Status for an environment this tool deliberately does not automate."""
        return cls(kind=StatusKind.NOT_IMPLEMENTED, detail=detail)

    @property
    def summary(self) -> str:
        """Terse status, used when `detail` already occupies most of the button."""
        match self.kind:
            case StatusKind.FETCHING:
                return "fetching…"
            case StatusKind.UP_TO_DATE:
                return "Up to date"
            case StatusKind.BEHIND:
                return f"{self.behind} behind"
            case StatusKind.DIVERGED:
                if self.behind and self.ahead:
                    return f"{self.behind} behind, {self.ahead} ahead"
                return f"{self.ahead} ahead" if self.ahead else f"{self.behind} behind"
            case StatusKind.NOT_IMPLEMENTED:
                return "n/a"
            case StatusKind.ERROR:
                return "error"
            case _:
                return "unknown"

    @property
    def label(self) -> str:
        """The text shown on the environment's button."""
        if self.detail:
            return f"{self.detail} · {self.summary}"
        if self.kind is StatusKind.BEHIND:
            return f"{_plural(self.behind, 'commit')} behind"
        return self.summary

    @property
    def variant(self) -> str:
        """The Textual Button variant that colours this status."""
        return _VARIANTS[self.kind]

    @property
    def deployable(self) -> bool:
        """Whether a fast-forward deploy is possible right now."""
        return self.kind is StatusKind.BEHIND and self.ahead == 0


class ActionKind(StrEnum):
    """What pressing an environment's button will do."""

    PUSH = "push"
    OPEN_URL = "open_url"
    NONE = "none"


@dataclass(frozen=True)
class DeployAction:
    """A fully resolved description of a deploy, built before anything is run.

    The confirmation dialog renders this rather than rebuilding the command, so what
    the user approves is exactly what executes.
    """

    kind: ActionKind
    description: str
    argv: tuple[str, ...] = ()
    url: str = ""
    sha: str = ""
    commits: tuple[str, ...] = ()
    warning: str = ""
    blocked_reason: str = ""


@dataclass(frozen=True)
class DeployResult:
    """The outcome of running a `DeployAction`."""

    ok: bool
    output: str
    argv: tuple[str, ...] = ()
    dry_run: bool = False
