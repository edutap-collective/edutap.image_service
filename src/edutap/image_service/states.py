"""The state machine of a photograph, as pure functions.

Every invariant of this service lives here, and nothing else does: no row, no
object, no caller. That is deliberate. These rules are the part where a mistake is
expensive -- a wrong transition discovered after the API exists has to be corrected
in two places -- and keeping them free of I/O is what lets them be tested
exhaustively without a fixture.

Each function raises rather than returning a boolean. A boolean would force every
caller to invent its own wording for the same refusal, and the refusals are exactly
what a person or a reviewer ends up reading.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class PhotoState(StrEnum):
    """Where a version stands.

    Stored as text rather than as a native enum, so a new state is not a migration.
    """

    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EvidenceKind(StrEnum):
    """How a photograph was verified.

    There is no `automatic`: an identity document and a wallet-held credential *are*
    the automatic paths, and a value meaning "somehow" would record nothing anyone
    could later rely on.
    """

    SUPPORT_VISUAL = "support_visual"
    ID_DOCUMENT = "id_document"
    EUDI_PID = "eudi_pid"


class TransitionRefused(Exception):
    """Base of every refusal, so a caller may catch the category."""


class IllegalTransition(TransitionRefused):
    """The state does not allow this step."""


class EvidenceRequired(TransitionRefused):
    """Activation was attempted without naming how the photograph was verified."""


class ActiveVersionIsNotDeletable(TransitionRefused):
    """The active version may be replaced, never deleted."""


class UnderLegalHold(TransitionRefused):
    """The version is held as evidence and no deletion path may touch it."""


@dataclass(frozen=True)
class Outcome:
    """What a transition changes.

    `supersede_active` is a separate answer rather than something the caller infers
    from `new_state`, because the two come apart: a reactivation whose review has
    aged out reaches `pending` and demotes nothing.
    """

    new_state: PhotoState
    supersede_active: bool = False
    evidence_kind: EvidenceKind | None = None


def confirm(current: PhotoState) -> Outcome:
    """Turn a candidate its owner has looked at into a submission.

    The only way out of `draft` other than deletion, and it leads to `pending` and
    nowhere else. A candidate is not a weaker submission: nobody has seen it, no
    review entry mentions it, and it carries no rights declaration until this step.
    That is why :func:`approve` refuses it -- approving something its owner never
    stood behind would record a decision about an image nobody submitted.
    """
    if current is not PhotoState.DRAFT:
        raise IllegalTransition(f"a {current} version cannot be confirmed")
    return Outcome(PhotoState.PENDING)


def approve(current: PhotoState, *, evidence_kind: EvidenceKind | None) -> Outcome:
    """Activate a version on a reviewer's decision.

    `rejected` is a legitimate starting point: a reviewer refused it by mistake, and
    the alternative to correcting that is asking someone to upload the same
    photograph a second time. The person cannot take this path -- see
    :func:`reactivate`.
    """
    if current not in (PhotoState.PENDING, PhotoState.REJECTED):
        raise IllegalTransition(f"a {current} version cannot be approved")
    if evidence_kind is None:
        raise EvidenceRequired("a version becomes active only with named evidence")
    return Outcome(PhotoState.ACTIVE, supersede_active=True, evidence_kind=evidence_kind)


def reject(current: PhotoState) -> Outcome:
    """Refuse a version awaiting review.

    Only from `pending`. Withdrawing a photograph already in use is a reset to the
    placeholder, which differs in what the person is told and in what happens to
    their card -- letting this stand in for it would report the wrong thing.
    """
    if current is not PhotoState.PENDING:
        raise IllegalTransition(f"a {current} version cannot be rejected")
    return Outcome(PhotoState.REJECTED)


def reactivate(
    current: PhotoState,
    *,
    reviewed_at: datetime,
    now: datetime,
    max_age: timedelta,
    evidence_kind: EvidenceKind | None,
) -> Outcome:
    """Let the person switch back to a version they kept.

    Only from `superseded`. Reaching `active` from `rejected` is a reviewer's
    decision, and if the person could do it here the refusal would be a suggestion.

    An old approval no longer says what it said: the photograph a card carries is
    there to be recognised, and one approved years ago no longer shows the person
    standing at the counter. Beyond `max_age` the version therefore returns to the
    queue and loses the evidence that expired with it -- it is not refused, the
    person still owns it.
    """
    if current is not PhotoState.SUPERSEDED:
        raise IllegalTransition(f"a {current} version cannot be reactivated by its owner")
    if now - reviewed_at > max_age:
        return Outcome(PhotoState.PENDING)
    return Outcome(PhotoState.ACTIVE, supersede_active=True, evidence_kind=evidence_kind)


def withdraw(current: PhotoState) -> Outcome:
    """Take the active photograph off the card without deleting it.

    Only from `active`, and it lands on `superseded` rather than nowhere: the person
    may still switch back to it, and the review trail keeps saying what was once on
    the card. Deleting instead would answer a different question -- "remove this
    image" rather than "stop showing it" -- and only a reviewer may ask the first.

    A transition rather than an `Outcome` a caller builds itself. Every invariant of
    this service lives here, and one assembled at a call site is one this module
    cannot refuse.
    """
    if current is not PhotoState.ACTIVE:
        raise IllegalTransition(f"a {current} version is not on any card to withdraw")
    return Outcome(PhotoState.SUPERSEDED)


def purge(current: PhotoState, *, legal_hold_since: datetime | None) -> None:
    """Check that the bytes of this version may be cleared.

    Returns nothing: purging changes no state. The row stays with `purged_at` set,
    which is what keeps the review trail readable after the image is gone.

    The hold is checked before the state on purpose. Both refusals would be correct
    for a held active version, but they mean different things to whoever reads them:
    one says "replace it instead", the other says "this is evidence in a
    proceeding".
    """
    if legal_hold_since is not None:
        raise UnderLegalHold(f"held since {legal_hold_since.isoformat()}")
    if current is PhotoState.ACTIVE:
        raise ActiveVersionIsNotDeletable("the active version can be replaced, not deleted")
    return None
