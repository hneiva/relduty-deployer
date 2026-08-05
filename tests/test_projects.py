# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The deploy matrix is load-bearing, so it is asserted rather than trusted."""

from pathlib import Path

import pytest

from relduty_deployer.models import Env
from relduty_deployer.projects import (
    BALROG,
    BRANCH_PUSH,
    PROJECT_SPECS,
    SPECS_BY_NAME,
    Project,
    ProjectSettings,
    default_path,
)

# Confirmed against each repo's .taskcluster.yml branch gate, its README, and the GitHub
# API. A change here should mean a repo actually changed, not that someone guessed.
EXPECTED = {
    "scriptworker-scripts": ("master", "dev", "production"),
    "shipit": ("main", "dev", "production"),
    "k8s-autoscale": ("main", "dev", "production"),
    "tooltool": ("master", "staging", "production"),
}


@pytest.mark.parametrize(("name", "expected"), EXPECTED.items())
def test_branch_push_matrix(name, expected):
    spec = SPECS_BY_NAME[name]
    source, staging, prod = expected
    assert spec.strategy == BRANCH_PUSH
    assert spec.source_branch == source
    assert spec.targets[Env.STAGING] == staging
    assert spec.targets[Env.PROD] == prod


EXPECTED_DOCS = {
    "scriptworker-scripts": "scriptworker-scripts.readthedocs.io",
    "balrog": "mozilla-balrog.readthedocs.io",
    "shipit": "github.com/mozilla-releng/shipit",
    "k8s-autoscale": "github.com/mozilla-releng/k8s-autoscale",
    "tooltool": "github.com/mozilla-releng/tooltool",
}


@pytest.mark.parametrize(("name", "host"), EXPECTED_DOCS.items())
def test_every_project_documents_its_deploy(name, host):
    """A docs button with no URL is a dead button, so every project must carry one."""
    url = SPECS_BY_NAME[name].docs_url
    assert url.startswith("https://"), f"{name} docs URL is not https"
    assert host in url, f"{name} docs URL does not point at {host}"


def test_the_docs_urls_are_deep_links_not_front_pages():
    """The rotation wants the deploy section, which is the whole point of recording these."""
    for name, url in ((spec.name, spec.docs_url) for spec in PROJECT_SPECS):
        assert "#" in url or url.endswith(".html"), f"{name} docs URL is not deep-linked: {url}"


def test_tooltool_stages_from_staging_not_dev():
    # tooltool's `dev` branch is a 2020 leftover that is no longer in its branch gate,
    # so pushing it would look successful and deploy nothing.
    assert SPECS_BY_NAME["tooltool"].targets[Env.STAGING] == "staging"


def test_balrog_is_status_only():
    spec = SPECS_BY_NAME["balrog"]
    assert spec.strategy == BALROG
    assert spec.source_branch == "main"
    assert spec.targets == {}


def test_only_balrog_has_a_custom_strategy():
    custom = [spec.name for spec in PROJECT_SPECS if spec.strategy != BRANCH_PUSH]
    assert custom == ["balrog"]


def test_all_five_projects_are_registered():
    assert [spec.name for spec in PROJECT_SPECS] == [
        "scriptworker-scripts",
        "balrog",
        "shipit",
        "k8s-autoscale",
        "tooltool",
    ]


def test_scriptworker_staging_carries_the_policy_warning():
    spec = SPECS_BY_NAME["scriptworker-scripts"]
    assert Env.STAGING in spec.warnings
    assert Env.PROD not in spec.warnings


def test_refs_are_fully_qualified_with_the_configured_remote():
    project = Project(
        spec=SPECS_BY_NAME["tooltool"],
        settings=ProjectSettings(path=Path("/tmp/tooltool"), remote="upstream"),
    )
    # refs/remotes/... rather than the shorthand, so a same-named tag cannot shadow it.
    assert project.source_ref() == "refs/remotes/upstream/master"
    assert project.target_ref(Env.STAGING) == "refs/remotes/upstream/staging"
    assert project.target_ref(Env.PROD) == "refs/remotes/upstream/production"


def test_every_project_names_its_canonical_github_repo():
    # Needed to catch a remote that points at a personal fork, where a push deploys nothing.
    for spec in PROJECT_SPECS:
        assert spec.github_repo == f"mozilla-releng/{spec.name}"


def test_missing_target_fails_loudly():
    project = Project(spec=SPECS_BY_NAME["balrog"], settings=ProjectSettings(path=Path("/tmp/balrog")))
    with pytest.raises(KeyError, match="balrog has no staging branch target"):
        project.target(Env.STAGING)


def test_default_path_is_under_dev():
    assert default_path("shipit") == Path.home() / "dev" / "shipit"
