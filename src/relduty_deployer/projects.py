# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The projects this tool deploys, and how each one is deployed.

Everything here is a fact about the project rather than a preference, so it lives in
code. Only the machine-local bits — where the checkout is and which remote to push —
come from the config file.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from relduty_deployer.models import Env

BRANCH_PUSH = "branch_push"
BALROG = "balrog"
ISCRIPT = "iscript"

# iscript is deployed out of ronin_puppet, which pins the scriptworker-scripts revision the
# mac signers run. Bumping that pin is a pull request against ronin_puppet rather than a
# branch push, so it is the one project whose first column is not an environment.
ISCRIPT_PINNED_REPO = "mozilla-releng/scriptworker-scripts"
ISCRIPT_PINNED_BRANCH = "master"
ISCRIPT_REVISION_FILE = "data/common.yaml"
ISCRIPT_REVISION_KEY = "scriptworker_scripts_revision"
ISCRIPT_BUMP_TITLE = "chore(iscript): Bump revision"

SCRIPTWORKER_STAGING_WARNING = (
    "RelEng policy: scriptworker staging deploys are normally skipped, because nothing runs "
    "against them regularly and pushing may interfere with people testing their own changes "
    "to scriptworkers."
)

BALROG_DOCS_URL = "https://mozilla-balrog.readthedocs.io/en/latest/infrastructure.html"

# Each project documents its own deploy procedure somewhere different. These are the pages
# the rotation actually needs, deep-linked to the deploy section rather than the project's
# front page.
SCRIPTWORKER_DOCS_URL = "https://scriptworker-scripts.readthedocs.io/en/latest/scriptworkers-FAQ.html#how-do-i-deploy-changes-to-a-specific-scriptworker-script"
SHIPIT_DOCS_URL = "https://github.com/mozilla-releng/shipit#deployed-environments"
K8S_AUTOSCALE_DOCS_URL = "https://github.com/mozilla-releng/k8s-autoscale#deployment"
TOOLTOOL_DOCS_URL = "https://github.com/mozilla-releng/tooltool#deployed-environments"
ISCRIPT_DOCS_URL = "https://github.com/mozilla-releng/scriptworker-scripts/wiki/Mac-Signers-Maintenance#deploying-scriptworker-updates"


@dataclass(frozen=True)
class ProjectSpec:
    """How a project is deployed."""

    name: str
    github_repo: str
    strategy: str
    source_branch: str
    targets: Mapping[Env, str] = field(default_factory=dict)
    warnings: Mapping[Env, str] = field(default_factory=dict)
    docs_url: str = ""
    checkout_dir: str = ""
    """Set when the checkout is not named after the project, as iscript lives in ronin_puppet."""

    @property
    def checkout_name(self) -> str:
        """The directory the clone is expected to be in, under the configured parent."""
        return self.checkout_dir or self.name

    @property
    def environments(self) -> tuple[Env, ...]:
        """The environments this project exposes, in display order."""
        return (Env.STAGING, Env.PROD)


@dataclass(frozen=True)
class ProjectSettings:
    """Where this machine keeps the project, and which remote deploys it."""

    path: Path
    remote: str = "origin"
    enabled: bool = True


@dataclass(frozen=True)
class Project:
    """A spec paired with the local settings a strategy needs to act on it."""

    spec: ProjectSpec
    settings: ProjectSettings

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def github_repo(self) -> str:
        return self.spec.github_repo

    def target(self, env: Env) -> str:
        """The branch that deploys `env`."""
        try:
            return self.spec.targets[env]
        except KeyError:
            raise KeyError(f"{self.name} has no {env} branch target; its strategy is {self.spec.strategy!r}") from None

    def source_ref(self) -> str:
        """The remote-tracking ref the deploy ships from.

        Fully qualified, so a local branch or tag of the same name cannot shadow it.
        """
        return f"refs/remotes/{self.settings.remote}/{self.spec.source_branch}"

    def target_ref(self, env: Env) -> str:
        """The remote-tracking ref for `env`'s deploy branch."""
        return f"refs/remotes/{self.settings.remote}/{self.target(env)}"


# Source branches genuinely differ between these repos, and a local clone's
# refs/remotes/origin/HEAD cannot be trusted to name the live one: k8s-autoscale's still
# points at `master`, which is over a hundred commits behind `main` and is not in that
# repo's Taskcluster branch gate at all. Every branch below was confirmed against the
# repo's .taskcluster.yml gate, its README, and the GitHub API.
PROJECT_SPECS: tuple[ProjectSpec, ...] = (
    ProjectSpec(
        name="scriptworker-scripts",
        github_repo="mozilla-releng/scriptworker-scripts",
        strategy=BRANCH_PUSH,
        source_branch="master",
        targets={Env.STAGING: "dev", Env.PROD: "production"},
        warnings={Env.STAGING: SCRIPTWORKER_STAGING_WARNING},
        docs_url=SCRIPTWORKER_DOCS_URL,
    ),
    # Balrog has no deploy branches. It ships by publishing a GitHub release, and its
    # production promotion happens by hand in ArgoCD, so this tool reports status only.
    ProjectSpec(
        name="balrog",
        github_repo="mozilla-releng/balrog",
        strategy=BALROG,
        source_branch="main",
        docs_url=BALROG_DOCS_URL,
    ),
    ProjectSpec(
        name="shipit",
        github_repo="mozilla-releng/shipit",
        strategy=BRANCH_PUSH,
        source_branch="main",
        targets={Env.STAGING: "dev", Env.PROD: "production"},
        docs_url=SHIPIT_DOCS_URL,
    ),
    ProjectSpec(
        name="k8s-autoscale",
        github_repo="mozilla-releng/k8s-autoscale",
        strategy=BRANCH_PUSH,
        source_branch="main",
        targets={Env.STAGING: "dev", Env.PROD: "production"},
        docs_url=K8S_AUTOSCALE_DOCS_URL,
    ),
    # iscript has no staging: only production is ever updated. Its first column bumps the
    # scriptworker-scripts revision that ronin_puppet pins, which is a pull request, and its
    # production branch is `macos-signer-latest` rather than `production`.
    ProjectSpec(
        name="iscript",
        github_repo="mozilla-platform-ops/ronin_puppet",
        strategy=ISCRIPT,
        source_branch="master",
        targets={Env.PROD: "macos-signer-latest"},
        docs_url=ISCRIPT_DOCS_URL,
        checkout_dir="ronin_puppet",
    ),
    # tooltool stages from `staging`, not `dev`. Its `dev` branch was last touched in 2020
    # and no longer appears in its branch gate, so pushing it deploys nothing.
    ProjectSpec(
        name="tooltool",
        github_repo="mozilla-releng/tooltool",
        strategy=BRANCH_PUSH,
        source_branch="master",
        targets={Env.STAGING: "staging", Env.PROD: "production"},
        docs_url=TOOLTOOL_DOCS_URL,
    ),
)

SPECS_BY_NAME: Mapping[str, ProjectSpec] = {spec.name: spec for spec in PROJECT_SPECS}


def default_path(name: str) -> Path:
    """Where a checkout is assumed to live before the user says otherwise."""
    return Path.home() / "dev" / name
