"""What this service announces, and what it deliberately does not."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from test_service_submit import UID, _png, build

from edutap.image_service.events import NoEvents, PhotoEvent, activated, rejected
from edutap.image_service.states import EvidenceKind

pytestmark = pytest.mark.integration

PERSON_VIEW = metadata.tables["public.person_view"]


def test_an_event_serialises_as_the_consumer_reads_it():
    event = PhotoEvent(
        event="photo.activated",
        person_uid="abc@lmu.de",
        version="0198f3",
        occurred_at=datetime(2026, 8, 13, 12, 0, tzinfo=UTC),
        facts={"evidence_kind": "support_visual"},
    )

    body = event.payload()

    assert b'"event": "photo.activated"' in body
    assert b'"person_uid": "abc@lmu.de"' in body
    assert b'"evidence_kind": "support_visual"' in body


def test_the_facts_are_flat_and_not_nested():
    """A consumer reads one object, not an envelope wrapped around a payload."""
    import json

    body = json.loads(rejected("abc@lmu.de", "v", reason="unscharf").payload())

    assert body["reason"] == "unscharf"
    assert "facts" not in body


def test_an_activation_carries_the_assurance_of_the_photograph():
    """Not of the person -- whoever issues a credential combines the two."""
    import json

    body = json.loads(
        activated(
            "abc@lmu.de",
            "v",
            evidence_kind="support_visual",
            assurance="https://refeds.org/assurance/IAP/medium",
        ).payload()
    )

    assert body["photo_assurance"] == "https://refeds.org/assurance/IAP/medium"


async def _submitted(session, service):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    return result.version


async def test_an_approval_announces_that_the_photograph_became_active(session):
    events = NoEvents()
    service, _, _ = build(session, events=events)
    version = await _submitted(session, service)

    await service.approve(
        person_uid=UID,
        version=version,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        actor="desk:kb12",
    )

    assert [e.event for e in events.published] == ["photo.activated"]
    assert events.published[0].person_uid == UID


async def test_a_rejection_announces_its_reason(session):
    """A consumer writes the mail; it needs the reason without a second query."""
    events = NoEvents()
    service, _, _ = build(session, events=events)
    version = await _submitted(session, service)

    await service.reject(person_uid=UID, version=version, actor="desk", reason="unscharf")

    assert [e.event for e in events.published] == ["photo.rejected"]
    assert events.published[0].facts["reason"] == "unscharf"


async def test_an_upload_announces_nothing(session):
    """Nobody has stood behind it yet, and a candidate is nobody's business."""
    events = NoEvents()
    service, _, _ = build(session, events=events)
    await service.submit(person_uid=UID, upload=_png())

    assert events.published == []


async def test_a_confirmation_announces_nothing(session):
    """It reaches a queue, not a card. The worker is told when a decision lands."""
    events = NoEvents()
    service, _, _ = build(session, events=events)
    await _submitted(session, service)

    assert events.published == []
