"""The rules of the state machine, one refusal at a time.

These tests need no database, no bucket and no HTTP client, which is the point of
keeping the rules in a module that knows about none of them.
"""

from datetime import UTC, datetime, timedelta

import pytest

from edutap.image_service.states import (
    ActiveVersionIsNotDeletable,
    EvidenceKind,
    EvidenceRequired,
    IllegalTransition,
    PhotoState,
    UnderLegalHold,
    approve,
    confirm,
    purge,
    reactivate,
    reject,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
SIX_MONTHS = timedelta(days=180)


def test_approving_a_pending_version_activates_it():
    outcome = approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL)
    assert outcome.new_state is PhotoState.ACTIVE
    assert outcome.evidence_kind is EvidenceKind.SUPPORT_VISUAL


def test_approving_supersedes_whatever_was_active():
    """The caller is told, rather than discovering it when the unique index fires.

    Exactly one active version per person is enforced by a partial unique index in
    `public.photo`. A caller that activates without demoting the previous one hits
    that index as an integrity error at the worst possible moment -- mid-write, with
    the object already in the bucket.
    """
    assert approve(PhotoState.PENDING, evidence_kind=EvidenceKind.EUDI_PID).supersede_active


def test_approving_without_evidence_is_refused():
    """There is no activation without named evidence anywhere in this service.

    Not a default of `support_visual`, which would silently record that a human
    looked at a photograph nobody looked at.
    """
    with pytest.raises(EvidenceRequired):
        approve(PhotoState.PENDING, evidence_kind=None)


def test_a_rejected_version_can_be_approved_after_all():
    """A reviewer refused it by mistake; a further decision corrects that.

    The person cannot do this -- see the reactivation tests -- but the reviewer path
    has to stay open, because the alternative is telling someone to upload the same
    photograph again.
    """
    assert approve(PhotoState.REJECTED, evidence_kind=EvidenceKind.ID_DOCUMENT).new_state is (
        PhotoState.ACTIVE
    )


def test_approving_an_already_active_version_is_refused():
    with pytest.raises(IllegalTransition):
        approve(PhotoState.ACTIVE, evidence_kind=EvidenceKind.SUPPORT_VISUAL)


def test_rejecting_a_pending_version():
    assert reject(PhotoState.PENDING).new_state is PhotoState.REJECTED


def test_rejecting_the_active_version_is_refused():
    """Withdrawing a photograph in use is a reset to the placeholder, not a review.

    They differ in what the person is told and in what happens to the card, so
    letting one stand in for the other would report the wrong thing.
    """
    with pytest.raises(IllegalTransition):
        reject(PhotoState.ACTIVE)


def test_a_recent_superseded_version_comes_back_without_a_new_review():
    """The ordinary "I liked the old one better" case must not queue."""
    reviewed = NOW - timedelta(days=30)
    outcome = reactivate(
        PhotoState.SUPERSEDED,
        reviewed_at=reviewed,
        now=NOW,
        max_age=SIX_MONTHS,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
    )
    assert outcome.new_state is PhotoState.ACTIVE
    assert outcome.supersede_active


def test_an_old_superseded_version_goes_back_through_review():
    """A photograph approved long ago no longer shows the person the card identifies.

    It is not refused -- the person still owns it -- but it re-enters the queue and
    loses the evidence that has expired with it.
    """
    reviewed = NOW - timedelta(days=200)
    outcome = reactivate(
        PhotoState.SUPERSEDED,
        reviewed_at=reviewed,
        now=NOW,
        max_age=SIX_MONTHS,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
    )
    assert outcome.new_state is PhotoState.PENDING
    assert not outcome.supersede_active
    assert outcome.evidence_kind is None


def test_the_person_cannot_reactivate_a_rejected_version():
    """Otherwise the reviewer's refusal is a suggestion.

    This is the one transition where the same verb means different things depending
    on who says it: a reviewer reaches `active` from `rejected` through `approve`,
    and this path exists so the person cannot.
    """
    with pytest.raises(IllegalTransition):
        reactivate(
            PhotoState.REJECTED,
            reviewed_at=NOW,
            now=NOW,
            max_age=SIX_MONTHS,
            evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        )


def test_purging_a_superseded_version():
    assert purge(PhotoState.SUPERSEDED, legal_hold_since=None) is None


def test_purging_the_active_version_is_refused():
    """It can be replaced, never deleted -- a card would lose its photograph."""
    with pytest.raises(ActiveVersionIsNotDeletable):
        purge(PhotoState.ACTIVE, legal_hold_since=None)


def test_a_legal_hold_defeats_the_purge():
    """Every deletion path except the deletion of the person consults this."""
    with pytest.raises(UnderLegalHold):
        purge(PhotoState.REJECTED, legal_hold_since=NOW)


def test_the_hold_is_checked_before_the_state():
    """A held active version reports the hold, not the state.

    Both refusals are correct, but they mean different things to whoever reads the
    message: one says "replace it instead", the other says "this is evidence".
    """
    with pytest.raises(UnderLegalHold):
        purge(PhotoState.ACTIVE, legal_hold_since=NOW)


def test_confirming_a_candidate_queues_it_for_review():
    outcome = confirm(PhotoState.DRAFT)
    assert outcome.new_state is PhotoState.PENDING
    assert outcome.supersede_active is False
    assert outcome.evidence_kind is None


@pytest.mark.parametrize(
    "state",
    [PhotoState.PENDING, PhotoState.ACTIVE, PhotoState.REJECTED, PhotoState.SUPERSEDED],
)
def test_only_a_candidate_can_be_confirmed(state):
    with pytest.raises(IllegalTransition):
        confirm(state)


def test_a_candidate_cannot_be_approved():
    """A reviewer never sees a candidate; reaching them is what confirming is for."""
    with pytest.raises(IllegalTransition):
        approve(PhotoState.DRAFT, evidence_kind=EvidenceKind.SUPPORT_VISUAL)


def test_a_candidate_cannot_be_rejected():
    with pytest.raises(IllegalTransition):
        reject(PhotoState.DRAFT)


def test_a_candidate_cannot_be_reactivated():
    with pytest.raises(IllegalTransition):
        reactivate(
            PhotoState.DRAFT,
            reviewed_at=NOW,
            now=NOW,
            max_age=SIX_MONTHS,
            evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        )


def test_a_candidate_may_be_purged():
    """Discarding what one just uploaded must not need a reviewer."""
    assert purge(PhotoState.DRAFT, legal_hold_since=None) is None


def test_a_held_candidate_is_not_purgeable_either():
    """A hold can strike any state -- a reviewer may recognise a stranger's face."""
    with pytest.raises(UnderLegalHold):
        purge(PhotoState.DRAFT, legal_hold_since=NOW)
