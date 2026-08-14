"""The paths a reviewer takes: notified, held, released, reset."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from test_service_submit import UID, _png, build

from edutap.image_service.events import NoEvents
from edutap.image_service.states import EvidenceKind, IllegalTransition, PhotoState

pytestmark = pytest.mark.integration

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


async def _pending(session, service):
    """Leave a version submitted and awaiting review, where a rejection starts."""
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await session.commit()
    return result.version


async def _active(session, service):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await service.approve(
        person_uid=UID,
        version=result.version,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        actor="desk:kb12",
    )
    await session.commit()
    return result.version


async def test_a_notification_starts_the_retention_clock(session):
    """Without it a rejected version never expires -- nothing else writes the field."""
    service, _, _ = build(session)
    version = await _pending(session, service)
    await service.reject(person_uid=UID, version=version, actor="desk", reason="unscharf")
    await session.commit()

    assert (await session.execute(sa.select(PHOTO.c.notified_at))).scalar() is None

    await service.mark_notified(person_uid=UID, version=version, when=NOW - timedelta(days=30))
    await session.commit()

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)
    assert len(result.purged) == 1


async def test_a_hold_is_announced_at_once(session):
    """Deleting the person removes held versions too, so the handover cannot wait."""
    events = NoEvents()
    service, _, _ = build(session, events=events)
    version = await _active(session, service)

    await service.set_hold(person_uid=UID, version=version, actor="desk:kb12", reason="Betrug")
    await session.commit()

    assert [e.event for e in events.published][-1] == "photo.held"
    assert events.published[-1].facts == {"reason": "Betrug", "by": "desk:kb12"}
    assert (await session.execute(sa.select(PHOTO.c.legal_hold_since))).scalar() is not None


async def test_a_held_version_survives_the_retention_run(session):
    service, store, _ = build(session)
    version = await _pending(session, service)
    await service.reject(person_uid=UID, version=version, actor="desk", reason="x")
    await service.mark_notified(person_uid=UID, version=version, when=NOW - timedelta(days=30))
    await service.set_hold(person_uid=UID, version=version, actor="desk", reason="Betrug")
    await session.commit()

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)

    assert result.purged == []
    assert [row["version"] for row in result.skipped_legal_hold] == [version]
    assert store.objects != {}


async def test_releasing_a_hold_lets_the_run_take_it(session):
    service, _, _ = build(session)
    version = await _pending(session, service)
    await service.reject(person_uid=UID, version=version, actor="desk", reason="x")
    await service.mark_notified(person_uid=UID, version=version, when=NOW - timedelta(days=30))
    await service.set_hold(person_uid=UID, version=version, actor="desk", reason="Betrug")
    await session.commit()
    await service.release_hold(person_uid=UID, version=version, actor="legal:head")
    await session.commit()

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)

    assert len(result.purged) == 1


async def test_a_reset_takes_the_photograph_off_the_card_without_deleting_it(session):
    events = NoEvents()
    service, store, _ = build(session, events=events)
    await _active(session, service)

    withdrew = await service.reset_to_placeholder(person_uid=UID, actor="desk:kb12")
    await session.commit()

    assert withdrew is True
    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.SUPERSEDED
    assert row["purged_at"] is None
    assert store.objects != {}
    assert [e.event for e in events.published][-1] == "photo.withdrawn"
    assert events.published[-1].version is None


async def test_the_reference_falls_back_to_the_placeholder(session):
    """What a card shows must follow, or it would keep pointing at a withdrawn image."""
    service, _, _ = build(session)
    await _active(session, service)

    await service.reset_to_placeholder(person_uid=UID, actor="desk:kb12")
    await session.commit()

    reference = (await session.execute(sa.select(PERSON_VIEW.c.photo))).scalar()
    assert reference["is_placeholder"] is True


async def test_a_reset_leaves_a_trail_entry(session):
    service, _, _ = build(session)
    await _active(session, service)

    await service.reset_to_placeholder(person_uid=UID, actor="desk:kb12")
    await session.commit()

    actions = [
        row["action"]
        for row in (
            await session.execute(sa.select(REVIEW).order_by(REVIEW.c.occurred_at))
        ).mappings()
    ]
    assert actions[-1] == "reset"


async def test_a_reset_without_an_active_photograph_says_so(session):
    """So a caller can tell "done" from "there was nothing" without a second query."""
    service, _, _ = build(session)

    assert await service.reset_to_placeholder(person_uid=UID, actor="desk") is False


async def test_the_person_cannot_reach_reset_through_reactivate(session):
    """Withdrawing is a reviewer's path; the state machine refuses the other one."""
    service, _, _ = build(session)
    version = await _active(session, service)

    with pytest.raises(IllegalTransition):
        await service.reactivate(person_uid=UID, version=version, actor="user:someone", now=NOW)
