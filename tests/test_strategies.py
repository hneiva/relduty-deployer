# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""The strategy registry."""

from pathlib import Path

import pytest
from fakes import FakeGitClient

from relduty_deployer.projects import BALROG, BRANCH_PUSH, PROJECT_SPECS, SPECS_BY_NAME, Project, ProjectSettings, ProjectSpec
from relduty_deployer.strategies import UnknownStrategyError, build_strategies, resolve


def make_registry():
    return build_strategies(git=FakeGitClient(), github=object(), versions=object())


def test_the_registry_holds_exactly_the_declared_strategies():
    assert set(make_registry()) == {BRANCH_PUSH, BALROG}


def test_every_project_resolves_to_a_strategy():
    strategies = make_registry()
    for spec in PROJECT_SPECS:
        project = Project(spec=spec, settings=ProjectSettings(path=Path("/repo")))
        assert resolve(strategies, project).name == spec.strategy


def test_an_unknown_strategy_names_the_ones_that_exist():
    project = Project(
        spec=ProjectSpec(name="ghost", github_repo="mozilla-releng/ghost", strategy="carrier-pigeon", source_branch="main"),
        settings=ProjectSettings(path=Path("/repo")),
    )

    with pytest.raises(UnknownStrategyError, match="carrier-pigeon"):
        resolve(make_registry(), project)

    with pytest.raises(UnknownStrategyError, match=BRANCH_PUSH):
        resolve(make_registry(), project)


def test_the_registry_cannot_be_mutated_after_construction():
    # Built once in the composition root and passed down, so nothing can register into it
    # later and make import order load-bearing.
    with pytest.raises(TypeError):
        make_registry()["extra"] = object()


def test_two_registries_are_independent():
    first, second = make_registry(), make_registry()

    assert first[BRANCH_PUSH] is not second[BRANCH_PUSH]


def test_balrog_is_the_only_project_not_using_branch_push():
    strategies = make_registry()
    balrog = Project(spec=SPECS_BY_NAME["balrog"], settings=ProjectSettings(path=Path("/repo")))

    assert resolve(strategies, balrog).name == BALROG
