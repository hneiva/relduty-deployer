"""Status classification, button labels, and the deployable guard."""

import pytest

from relduty_deployer.models import DeployStatus, StatusKind


@pytest.mark.parametrize(
    ("behind", "ahead", "kind"),
    [
        (0, 0, StatusKind.UP_TO_DATE),
        (3, 0, StatusKind.BEHIND),
        (3, 2, StatusKind.DIVERGED),
        (0, 2, StatusKind.DIVERGED),
    ],
)
def test_from_counts_classifies(behind, ahead, kind):
    assert DeployStatus.from_counts(behind=behind, ahead=ahead).kind is kind


@pytest.mark.parametrize(
    ("behind", "ahead", "label"),
    [
        (0, 0, "Up to date"),
        (1, 0, "1 commit behind"),
        (15, 0, "15 commits behind"),
        (3, 2, "3 behind, 2 ahead"),
        (0, 2, "2 ahead"),
    ],
)
def test_labels_match_the_specified_wording(behind, ahead, label):
    assert DeployStatus.from_counts(behind=behind, ahead=ahead).label == label


@pytest.mark.parametrize(
    ("behind", "ahead", "variant"),
    [
        (0, 0, "success"),
        (4, 0, "warning"),
        (4, 1, "error"),
        (0, 1, "error"),
    ],
)
def test_variants_colour_by_severity(behind, ahead, variant):
    assert DeployStatus.from_counts(behind=behind, ahead=ahead).variant == variant


@pytest.mark.parametrize(
    ("behind", "ahead", "deployable"),
    [
        (0, 0, False),  # nothing to ship
        (5, 0, True),  # the only pushable case
        (5, 1, False),  # diverged
        (0, 1, False),  # deploy branch has commits the source branch lacks
    ],
)
def test_only_a_pure_fast_forward_is_deployable(behind, ahead, deployable):
    assert DeployStatus.from_counts(behind=behind, ahead=ahead).deployable is deployable


def test_detail_prefixes_the_terse_summary():
    status = DeployStatus.from_counts(behind=2, ahead=0, detail="v3.121 unreleased")
    assert status.label == "v3.121 unreleased · 2 behind"


def test_fetching_and_failure_are_never_deployable():
    assert DeployStatus.fetching().deployable is False
    assert DeployStatus.fetching().label == "fetching…"

    failed = DeployStatus.failed("no such remote")
    assert failed.deployable is False
    assert failed.error == "no such remote"
    assert failed.variant == "error"


def test_unimplemented_reports_not_available():
    status = DeployStatus.unimplemented()
    assert status.label == "n/a"
    assert status.deployable is False
