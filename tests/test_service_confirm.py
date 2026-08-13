"""Confirming a candidate: the step that turns an upload into a submission.

Same shape as `test_service_submit.py` -- a real repository against PostgreSQL,
fakes for the object store and the image API -- because what is under test is what
ends up recorded.
"""

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from test_service_submit import UID, _png, build

from edutap.image_service.service import VersionNotFound
from edutap.image_service.states import IllegalTransition, PhotoState

pytestmark = pytest.mark.integration

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]


async def _person_view(session):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )


async def test_confirming_queues_the_candidate(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="user:ab12cde@lmu.de",
        rights_declared=True,
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.PENDING


async def test_the_trail_entry_carries_the_verdict_and_the_actor(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="user:ab12cde@lmu.de",
        rights_declared=True,
    )
    await session.commit()

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["action"] == "submit"
    assert entry["actor"] == "user:ab12cde@lmu.de"
    assert entry["details"]["validation"]["passed"] is True


async def test_confirming_without_the_declaration_is_refused(session):
    """Not a weaker confirmation. None."""
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    with pytest.raises(ValueError):
        await service.confirm(
            person_uid=UID,
            version=result.version,
            actor="user:ab12cde@lmu.de",
            rights_declared=False,
        )


async def test_confirming_an_unknown_version_is_not_found(session):
    service, _, _ = build(session)

    with pytest.raises(VersionNotFound):
        await service.confirm(person_uid=UID, version="nope", actor="self", rights_declared=True)


async def test_confirming_twice_is_refused(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await session.commit()

    with pytest.raises(IllegalTransition):
        await service.confirm(
            person_uid=UID, version=result.version, actor="self", rights_declared=True
        )


async def test_discarding_a_candidate_removes_its_row_entirely(session):
    """Not `purged_at` on a row that stays.

    `mark_purged` keeps the row so the trail stays readable after the bytes are
    gone -- but a candidate has no trail, and a kept row would still read `draft`
    and make the partial unique index refuse this person's next upload.
    """
    service, store, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.purge(person_uid=UID, version=result.version, actor="self")
    await session.commit()

    count = (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar()
    assert count == 0
    assert store.objects == {}


async def test_a_discarded_candidate_does_not_block_the_next_upload(session):
    service, _, _ = build(session)
    first = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.purge(person_uid=UID, version=first.version, actor="self")
    await session.commit()

    second = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["version"] == second.version
