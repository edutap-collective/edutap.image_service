"""The retention run: what it clears, what it refuses to, and what it reports."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from test_service_submit import UID, _png, build

from edutap.image_service.states import PhotoState

pytestmark = pytest.mark.integration

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


async def _person_view(session):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )


async def _rejected(session, service, *, notified_days_ago):
    await _person_view(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await service.reject(person_uid=UID, version=result.version, actor="desk", reason="unscharf")
    await session.execute(
        sa.update(PHOTO).values(notified_at=NOW - timedelta(days=notified_days_ago))
    )
    await session.commit()
    return result.version


async def test_a_rejection_past_its_deadline_is_cleared(session):
    service, store, _ = build(session)
    version = await _rejected(session, service, notified_days_ago=30)

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)
    await session.commit()

    assert [row["version"] for row in result.purged] == [version]
    assert store.objects == {}
    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["purged_at"] is not None


async def test_the_clock_runs_from_the_notification_not_the_rejection(session):
    """Somebody away for three weeks must not lose the image before hearing of it."""
    service, _, _ = build(session)
    await _rejected(session, service, notified_days_ago=2)

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)

    assert result.purged == []


async def test_the_trail_says_expire_and_not_purge(session):
    service, _, _ = build(session)
    await _rejected(session, service, notified_days_ago=30)

    await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)
    await session.commit()

    actions = [
        row["action"]
        for row in (
            await session.execute(sa.select(REVIEW).order_by(REVIEW.c.occurred_at))
        ).mappings()
    ]
    assert actions == ["submit", "reject", "expire"]


async def test_a_held_version_is_skipped_and_reported(session):
    service, store, _ = build(session)
    version = await _rejected(session, service, notified_days_ago=30)
    await session.execute(sa.update(PHOTO).values(legal_hold_since=NOW))
    await session.commit()

    result = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)

    assert result.purged == []
    assert [row["version"] for row in result.skipped_legal_hold] == [version]
    assert store.objects != {}


async def test_a_stale_candidate_is_cleared_row_and_all(session):
    """Nobody came back for it, and it leaves no row: it has no trail to keep one."""
    service, store, _ = build(session)
    await service.submit(person_uid=UID, upload=_png())
    await session.execute(sa.update(PHOTO).values(created_at=NOW - timedelta(days=3)))
    await session.commit()

    result = await service.expire(state=PhotoState.DRAFT, older_than=timedelta(days=1), now=NOW)
    await session.commit()

    assert len(result.purged) == 1
    assert (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar() == 0
    assert store.objects == {}


async def test_running_twice_changes_nothing_the_second_time(session):
    """Idempotent, so an operator may call it as often as they like."""
    service, _, _ = build(session)
    await _rejected(session, service, notified_days_ago=30)

    first = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)
    await session.commit()
    second = await service.expire(state=PhotoState.REJECTED, older_than=timedelta(days=14), now=NOW)

    assert len(first.purged) == 1
    assert second.purged == []


@pytest.mark.parametrize("state", [PhotoState.PENDING, PhotoState.ACTIVE, PhotoState.SUPERSEDED])
async def test_the_other_states_never_expire(session, state):
    """`pending` stays and stays visible; an operator watches the queue, not a timer."""
    service, _, _ = build(session)

    with pytest.raises(ValueError):
        await service.expire(state=state, older_than=timedelta(days=1), now=NOW)
