"""Status classification, button labels, and the deployable guard.

Every fixture here is deliberately asymmetric in ahead vs behind. A symmetric case like
ahead=3, behind=3 would pass even if the two counts were swapped everywhere, which is
precisely the bug these tests exist to catch.
"""

import pytest

from relduty_deployer.models import ActionKind, AheadBehind, DeployStatus, StatusKind


@pytest.mark.parametrize(
    ("ahead", "behind", "kind"),
    [
        (0, 0, StatusKind.UP_TO_DATE),
        (0, 3, StatusKind.BEHIND),
        (2, 3, StatusKind.DIVERGED),
        (2, 0, StatusKind.DIVERGED),
    ],
)
def test_from_counts_classifies(ahead, behind, kind):
    assert DeployStatus.from_counts(AheadBehind(ahead=ahead, behind=behind)).kind is kind


@pytest.mark.parametrize(
    ("ahead", "behind", "label"),
    [
        (0, 0, "Up to date"),
        (0, 1, "1 commit behind"),
        (0, 15, "15 commits behind"),
        (2, 3, "3 behind, 2 ahead"),
        (2, 0, "2 ahead"),
    ],
)
def test_labels_match_the_specified_wording(ahead, behind, label):
    assert DeployStatus.from_counts(AheadBehind(ahead=ahead, behind=behind)).label == label


@pytest.mark.parametrize(
    ("ahead", "behind", "variant"),
    [
        (0, 0, "success"),
        (0, 4, "warning"),
        (1, 4, "error"),
        (1, 0, "error"),
    ],
)
def test_variants_colour_by_severity(ahead, behind, variant):
    assert DeployStatus.from_counts(AheadBehind(ahead=ahead, behind=behind)).variant == variant


@pytest.mark.parametrize(
    ("ahead", "behind", "deployable"),
    [
        (0, 0, False),  # nothing to ship
        (0, 5, True),  # the only pushable case
        (1, 5, False),  # diverged
        (1, 0, False),  # deploy branch has commits the source branch lacks
    ],
)
def test_only_a_pure_fast_forward_is_deployable(ahead, behind, deployable):
    status = DeployStatus.from_counts(AheadBehind(ahead=ahead, behind=behind))
    assert status.deployable is deployable
    assert (status.action is ActionKind.PUSH) is deployable


def test_a_single_commit_ahead_is_never_deployable():
    """The user's rule: any commits ahead means the user resolves it by hand."""
    status = DeployStatus.from_counts(AheadBehind(ahead=1, behind=99))
    assert status.deployable is False
    assert status.variant == "error"


def test_action_can_be_overridden_while_keeping_real_counts():
    # How balrog reports honest commit counts for an environment it does not automate.
    status = DeployStatus.from_counts(AheadBehind(ahead=0, behind=4), action=ActionKind.NONE)
    assert status.kind is StatusKind.BEHIND
    assert status.behind == 4
    assert status.deployable is False
    assert status.clickable is False


def test_detail_prefixes_the_terse_summary():
    status = DeployStatus.from_counts(AheadBehind(ahead=0, behind=2), detail="v3.121 unreleased")
    assert status.label == "v3.121 unreleased · 2 behind"


def test_up_to_date_detail_names_the_live_version():
    status = DeployStatus.from_counts(AheadBehind(ahead=0, behind=0), detail="3.120")
    assert status.label == "3.120 · Up to date"


def test_fetching_and_failure_are_never_deployable():
    assert DeployStatus.fetching().deployable is False
    assert DeployStatus.fetching().label == "fetching…"

    failed = DeployStatus.failed("no such remote")
    assert failed.deployable is False
    assert failed.clickable is False
    assert failed.error == "no such remote"
    assert failed.variant == "error"


def test_unimplemented_reports_not_available():
    status = DeployStatus.unimplemented()
    assert status.label == "n/a"
    assert status.deployable is False


def test_open_url_is_clickable_but_not_deployable():
    status = DeployStatus(kind=StatusKind.BEHIND, behind=1, action=ActionKind.OPEN_URL, url="https://example.invalid")
    assert status.clickable is True
    assert status.deployable is False


def test_every_status_kind_has_a_colour():
    for kind in StatusKind:
        assert DeployStatus(kind=kind).variant in ("default", "primary", "success", "warning", "error")
