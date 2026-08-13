"""The repository against a real PostgreSQL.

These need the database because what they assert is what the *database* does: the
partial unique index, the cascade, the single transaction that keeps the reference
from drifting away from the row it describes.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from sqlalchemy.exc import IntegrityError

from edutap.image_service.repository import PhotoRepository
from edutap.image_service.states import EvidenceKind, PhotoState, approve, confirm

pytestmark = pytest.mark.integration

ORIGIN = "https://wallet.example.org/api/images"
UID = "ab12cde@lmu.de"
NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]


def repo(session):
    return PhotoRepository(session, origin=ORIGIN)


async def _add_person_view(session, *, view_type="full_view"):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type=view_type, data={})
    )


async def _submit(r, *, version="v1"):
    """Upload and confirm -- how a version reaches `pending` now.

    Two steps rather than one, because that is the real path: a candidate exists
    first, and only its owner's confirmation turns it into a submission.
    """
    await r.add_draft(
        person_uid=UID, version=version, sha256="a" * 64, recipe="default", details={}
    )
    await r.apply(
        person_uid=UID,
        version=version,
        outcome=confirm(PhotoState.DRAFT),
        actor="self",
        action="submit",
    )


async def test_a_submitted_version_starts_pending_and_leaves_a_trail(session):
    await _submit(repo(session))
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.PENDING
    assert row["evidence_kind"] is None

    trail = (await session.execute(sa.select(REVIEW))).mappings().all()
    assert [entry["action"] for entry in trail] == ["submit"]
    assert trail[0]["sha256"] == "a" * 64


async def test_approving_writes_the_reference_into_every_view_of_the_person(session):
    """The photograph belongs to the person, not to one view of them.

    `person_view` is keyed by `(person_uid, view_type)`, so a person legitimately has
    several rows -- a full view and a speaking slice. A reference written into only
    one of them would leave the others reporting a placeholder for someone who has a
    photograph.
    """
    await _add_person_view(session, view_type="full_view")
    await _add_person_view(session, view_type="mensapass")
    r = repo(session)
    await _submit(r)
    await r.apply(
        person_uid=UID,
        version="v1",
        outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL),
        actor="support:kb12",
        action="approve",
    )
    await session.commit()

    references = (await session.execute(sa.select(PERSON_VIEW.c.photo))).scalars().all()
    assert len(references) == 2
    for reference in references:
        assert reference["version"] == "v1"
        assert reference["is_placeholder"] is False
        assert reference["photo_assurance"] == "https://refeds.org/assurance/IAP/medium"


async def test_a_person_without_a_view_row_is_not_an_error(session):
    """The photo tables are the truth; `person_view` is a projection for readers.

    A photograph can legitimately arrive before the person spooler has written its
    row, and failing here would make the upload depend on an unrelated pipeline.
    """
    r = repo(session)
    await _submit(r)
    await r.apply(
        person_uid=UID,
        version="v1",
        outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.EUDI_PID),
        actor="support:kb12",
        action="approve",
    )
    await session.commit()
    assert (await r.active_for(UID))["version"] == "v1"


async def test_activating_demotes_the_previous_active_version(session):
    r = repo(session)
    for version in ("v1", "v2"):
        await _submit(r, version=version)
    for version in ("v1", "v2"):
        await r.apply(
            person_uid=UID,
            version=version,
            outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL),
            actor="support:kb12",
            action="approve",
        )
    await session.commit()

    states = dict(
        (await session.execute(sa.select(PHOTO.c.version, PHOTO.c.state))).all()  # noqa: RUF015
    )
    assert states == {"v1": PhotoState.SUPERSEDED, "v2": PhotoState.ACTIVE}


async def test_the_database_refuses_a_second_active_version(session):
    """The invariant is the index, not the service.

    Written straight past the repository on purpose: this asserts that the *schema*
    refuses it, which is what makes two reviewers clicking in the same second safe.

    The refusal lands on the second INSERT, not on the commit -- a unique index is
    checked per statement unless it is explicitly deferred, and this one is not.
    That is why `PhotoRepository.apply` demotes the previous active version *before*
    activating the new one: the other order would show two active rows for the
    length of one statement and be refused here.
    """
    await session.execute(
        sa.insert(PHOTO).values(
            person_uid=UID,
            version="v1",
            state=PhotoState.ACTIVE,
            sha256="a" * 64,
            recipe="default",
        )
    )
    with pytest.raises(IntegrityError):
        await session.execute(
            sa.insert(PHOTO).values(
                person_uid=UID,
                version="v2",
                state=PhotoState.ACTIVE,
                sha256="b" * 64,
                recipe="default",
            )
        )


async def test_purging_keeps_the_row_and_the_trail(session):
    """The bytes go, the evidence that there was a photograph stays."""
    r = repo(session)
    await _submit(r)
    await r.mark_purged(person_uid=UID, version="v1", actor="self", objects_deleted=4)
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["purged_at"] is not None
    trail = (await session.execute(sa.select(REVIEW).order_by(REVIEW.c.occurred_at))).mappings()
    actions = [entry["action"] for entry in trail]
    assert actions == ["submit", "purge"]


async def test_deleting_the_person_takes_the_trail_with_it(session):
    """The one deletion that removes evidence too -- by cascade, not by a second query."""
    r = repo(session)
    await _submit(r)
    await session.commit()

    await r.delete_person(UID)
    await session.commit()

    assert (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar() == 0
    assert (await session.execute(sa.select(sa.func.count()).select_from(REVIEW))).scalar() == 0


async def test_a_held_version_is_not_offered_for_expiry(session):
    """Every deletion path consults the hold. This is the retention run's half."""
    r = repo(session)
    for version in ("v1", "v2"):
        await _submit(r, version=version)
    await session.execute(
        sa.update(PHOTO)
        .where(PHOTO.c.version.in_(["v1", "v2"]))
        .values(state=PhotoState.REJECTED, notified_at=NOW - timedelta(days=30))
    )
    await r.set_legal_hold(person_uid=UID, version="v2", actor="support:kb12", reason="suspected")
    await session.commit()

    due = await r.due_for_expiry(older_than=timedelta(days=14), now=NOW)
    assert [row["version"] for row in due] == ["v1"]


async def test_a_rejection_that_was_never_notified_does_not_expire(session):
    """The clock starts at the notification, not at the rejection.

    Someone away for three weeks would otherwise lose the photograph before ever
    learning it was refused.
    """
    r = repo(session)
    await _submit(r)
    await session.execute(sa.update(PHOTO).values(state=PhotoState.REJECTED, notified_at=None))
    await session.commit()

    assert await r.due_for_expiry(older_than=timedelta(days=14), now=NOW) == []


async def test_the_row_and_its_reference_move_together_or_not_at_all(session):
    """The single transaction is the whole reason the reference cannot drift.

    Rolled back after the write: if the reference were written outside the
    transaction, `person_view` would keep pointing at a version that no longer
    exists.
    """
    await _add_person_view(session)
    r = repo(session)
    await _submit(r)
    await r.apply(
        person_uid=UID,
        version="v1",
        outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL),
        actor="support:kb12",
        action="approve",
    )
    await session.rollback()

    assert (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar() == 0
    assert (await session.execute(sa.select(PERSON_VIEW.c.photo))).scalar() is None


async def test_a_candidate_writes_no_review_entry(session):
    """The trail is the register of claims, and a candidate claims nothing."""
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={"v": 1}
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.DRAFT
    assert row["draft_details"] == {"v": 1}
    assert row["rights_declared_at"] is None
    count = (await session.execute(sa.select(sa.func.count()).select_from(REVIEW))).scalar()
    assert count == 0


async def test_two_candidates_for_one_person_are_refused_by_the_database(session):
    """The service clears the old one first; this is what happens when it cannot."""
    await repo(session).add_draft(
        person_uid=UID, version="one", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    # The insert raises, not the commit: a unique index is checked per statement.
    with pytest.raises(IntegrityError):
        await repo(session).add_draft(
            person_uid=UID, version="two", sha256="b" * 64, recipe="default", details={}
        )


async def test_discarding_returns_the_version_it_removed(session):
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    removed = await repo(session).discard_draft(UID)
    await session.commit()

    assert removed == "cand"
    assert await repo(session).discard_draft(UID) is None


async def test_confirming_moves_the_candidate_to_pending_and_dates_the_declaration(session):
    await _add_person_view(session)
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=confirm(PhotoState.DRAFT),
        actor="user:ab12cde@lmu.de",
        action="submit",
        details={"declaration": {"tag": "v1.0", "sha": "a" * 40}},
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.PENDING
    assert row["rights_declared_at"] is not None
    assert row["draft_details"] is None

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["action"] == "submit"
    assert entry["details"]["declaration"]["tag"] == "v1.0"


async def test_a_pending_version_is_not_dated_twice(session):
    """`rights_declared_at` records one moment. A later transition must not move it."""
    await _add_person_view(session)
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()
    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=confirm(PhotoState.DRAFT),
        actor="self",
        action="submit",
    )
    await session.commit()
    declared_at = (await session.execute(sa.select(PHOTO.c.rights_declared_at))).scalar()

    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL),
        actor="desk:someone",
        action="approve",
    )
    await session.commit()

    assert (await session.execute(sa.select(PHOTO.c.rights_declared_at))).scalar() == declared_at
