"""Types describing deploy environments, their status, and planned actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


class Env(StrEnum):
    """A deploy environment."""

    STAGING = "staging"
    PROD = "prod"


class AheadBehind(NamedTuple):
    """How a deploy branch diverges from the branch it deploys from.

    Returned as a named tuple rather than a bare pair so that unpacking it in the wrong
    order is not silently possible: call sites read `counts.behind`, not `counts[1]`.
    """

    ahead: int
    """Commits on the deploy branch that the source branch does not have."""

    behind: int
    """Commits on the source branch that the deploy branch does not have."""


class StatusKind(StrEnum):
    """How an environment compares to its source branch."""

    FETCHING = "fetching"
    UP_TO_DATE = "up_to_date"
    BEHIND = "behind"
    DIVERGED = "diverged"
    NOT_IMPLEMENTED = "not_implemented"
    ERROR = "error"


class ActionKind(StrEnum):
    """What pressing an environment's button does."""

    PUSH = "push"
    OPEN_URL = "open_url"
    NONE = "none"


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
    """The state of one environment of one project."""

    kind: StatusKind
    behind: int = 0
    ahead: int = 0
    action: ActionKind = ActionKind.NONE
    detail: str = ""
    tooltip: str = ""
    url: str = ""
    error: str = ""

    @classmethod
    def from_counts(
        cls,
        counts: AheadBehind,
        *,
        action: ActionKind | None = None,
        detail: str = "",
        tooltip: str = "",
    ) -> DeployStatus:
        """Classify divergence counts.

        This is the only place the two counts become a verdict, so callers cannot
        disagree about which direction blocks a deploy. Any `ahead` at all is unsafe: a
        push would have to discard those commits.

        `action` overrides the default affordance, which is how balrog reports real
        commit counts for an environment it deliberately does not automate.
        """
        if counts.ahead:
            kind, default_action = StatusKind.DIVERGED, ActionKind.NONE
        elif counts.behind:
            kind, default_action = StatusKind.BEHIND, ActionKind.PUSH
        else:
            kind, default_action = StatusKind.UP_TO_DATE, ActionKind.NONE
        return cls(
            kind=kind,
            behind=counts.behind,
            ahead=counts.ahead,
            action=default_action if action is None else action,
            detail=detail,
            tooltip=tooltip,
        )

    @classmethod
    def fetching(cls) -> DeployStatus:
        """Status shown while the project's remote is being fetched."""
        return cls(kind=StatusKind.FETCHING)

    @classmethod
    def failed(cls, message: str) -> DeployStatus:
        """Status shown when the comparison could not be made at all."""
        return cls(kind=StatusKind.ERROR, error=message, detail=message)

    @classmethod
    def unimplemented(cls, detail: str = "", tooltip: str = "") -> DeployStatus:
        """Status for an environment whose state this tool cannot determine."""
        return cls(kind=StatusKind.NOT_IMPLEMENTED, detail=detail, tooltip=tooltip or detail)

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
        if self.kind is StatusKind.UP_TO_DATE and self.detail:
            # Balrog's up-to-date states carry the version that is live.
            return f"{self.detail} · Up to date"
        if self.detail and self.kind in (StatusKind.BEHIND, StatusKind.DIVERGED):
            # A count of zero means it could not be measured, so show only the detail
            # rather than an unhelpful "0 behind".
            if self.kind is StatusKind.BEHIND and self.behind == 0:
                return self.detail
            return f"{self.detail} · {self.summary}"
        if self.kind is StatusKind.BEHIND:
            return f"{_plural(self.behind, 'commit')} behind"
        if self.kind is StatusKind.NOT_IMPLEMENTED and self.detail:
            return self.detail
        return self.summary

    @property
    def variant(self) -> str:
        """The Textual Button variant that colours this status."""
        return _VARIANTS[self.kind]

    @property
    def deployable(self) -> bool:
        """Whether a fast-forward deploy is possible right now."""
        return self.action is ActionKind.PUSH

    @property
    def clickable(self) -> bool:
        """Whether the button does anything at all when pressed."""
        return self.action is not ActionKind.NONE


@dataclass(frozen=True)
class DeployAction:
    """A fully resolved description of a deploy, built before anything is run.

    The confirmation dialog renders this rather than rebuilding the command, so what the
    user approves is exactly what executes.
    """

    kind: ActionKind
    description: str
    argv: tuple[str, ...] = ()
    url: str = ""
    sha: str = ""
    commits: tuple[str, ...] = ()
    truncated: int = 0
    warning: str = ""
    documented_equivalent: str = ""


@dataclass(frozen=True)
class DeployResult:
    """The outcome of running a `DeployAction`."""

    ok: bool
    output: str
    argv: tuple[str, ...] = ()
    dry_run: bool = False
