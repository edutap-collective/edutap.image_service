"""The messages this service publishes, and the headers the schema requires.

A consumer pins `edutap-schema`, so what is asserted here is a contract with
another service rather than an internal detail.
"""

import json
from datetime import UTC, datetime

from edutap.image_service.events import (
    PRODUCER,
    SCHEMA,
    PhotoEvents,
    activated,
    held,
    rejected,
    withdrawn,
)

WHEN = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
UID = "ab12cde@lmu.de"


class FakeProducer:
    def __init__(self):
        self.sent = []

    async def send_and_wait(self, topic, value, key=None, headers=None):
        self.sent.append(
            {"topic": topic, "value": value, "key": key, "headers": dict(headers or [])}
        )


def publisher() -> tuple[PhotoEvents, FakeProducer]:
    producer = FakeProducer()
    return PhotoEvents(
        producer=producer, topic_prefix="edutap.dev", producer_version="1.2.3"
    ), producer


async def test_the_topic_follows_the_agreed_naming():
    """`edutap.<env>.person.photo` — dots only, and the producer is not in the name."""
    events, producer = publisher()
    await events.publish(withdrawn(UID))
    assert producer.sent[0]["topic"] == "edutap.dev.person.photo"


async def test_every_mandatory_header_is_present():
    events, producer = publisher()
    await events.publish(activated(UID, "v1", evidence_kind="support_visual", assurance="x"))
    headers = producer.sent[0]["headers"]
    for name in ("edutap-producer", "edutap-schema", "edutap-event-id", "edutap-occurred-at"):
        assert name in headers
    assert headers["edutap-producer"] == PRODUCER.encode()
    assert headers["edutap-schema"] == SCHEMA.encode()


async def test_the_key_is_the_person_not_the_version():
    """Every fact about one person lands on one partition and is consumed in order.

    Keyed by version, a withdrawal could be processed before the activation it
    follows, and the consumer would rebuild a pass from a state that never existed.
    """
    events, producer = publisher()
    await events.publish(activated(UID, "v1", evidence_kind="support_visual", assurance="x"))
    assert producer.sent[0]["key"] == UID.encode()


async def test_the_timestamp_is_the_senders_and_stable_across_a_retry():
    """`edutap-occurred-at` is what a consumer compares against, so the caller may pin it."""
    events, producer = publisher()
    await events.publish(withdrawn(UID), occurred_at=WHEN)
    assert producer.sent[0]["headers"]["edutap-occurred-at"] == WHEN.isoformat().encode()


async def test_an_activation_carries_the_evidence_and_the_assurance():
    events, producer = publisher()
    await events.publish(
        activated(UID, "v1", evidence_kind="id_document", assurance="https://refeds.org/x")
    )
    body = json.loads(producer.sent[0]["value"])
    assert body == {
        "person_uid": UID,
        "version": "v1",
        "evidence_kind": "id_document",
        "photo_assurance": "https://refeds.org/x",
    }


async def test_a_rejection_carries_the_reason():
    """The consumer writes it into the mail, and the mail starts the retention clock."""
    events, producer = publisher()
    await events.publish(rejected(UID, "v1", reason="too dark"))
    assert json.loads(producer.sent[0]["value"])["reason"] == "too dark"
    assert producer.sent[0]["headers"]["edutap-action"] == b"rejected"


async def test_a_withdrawal_names_no_version():
    """What a consumer needs to know is that the card shows a placeholder now."""
    events, producer = publisher()
    await events.publish(withdrawn(UID))
    assert json.loads(producer.sent[0]["value"])["version"] is None


async def test_a_hold_says_who_placed_it_and_why():
    """Somebody has to act on this while the person is still on file."""
    events, producer = publisher()
    await events.publish(held(UID, "v1", reason="not this person", by="support:kb12"))
    body = json.loads(producer.sent[0]["value"])
    assert body["by"] == "support:kb12"
    assert producer.sent[0]["headers"]["edutap-action"] == b"held"


async def test_the_optional_producer_version_travels_when_configured():
    events, producer = publisher()
    await events.publish(withdrawn(UID))
    assert producer.sent[0]["headers"]["edutap-producer-version"] == b"1.2.3"
