"""What this service tells the rest of the world about a photograph.

It reports **facts**, never instructions: a version became active, the active one
was withdrawn, a version was refused, a hold was placed. That a mail follows, or a
pass gets rebuilt, is somebody else's conclusion — this package sits in the
collective and knows nothing about passes or mail templates, and writing to a
command topic would be exactly that knowledge.

The message shape follows the eduTAP topic schema: dots in the name, the producing
service in a header rather than in the topic, and one consumer per topic.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

#: The contract this producer writes, carried in `edutap-schema`. Consumers pin it,
#: so a change in the payload is a change of this string and not a silent edit.
SCHEMA = "person-photo/v1"

PRODUCER = "image_service"


@dataclass(frozen=True)
class PhotoEvent:
    """One fact about one version."""

    action: str
    person_uid: str
    version: str | None
    payload: dict[str, Any]


class Producer(Protocol):
    """The half of a Kafka producer this service uses.

    Narrow on purpose: it is what a test double provides, and what somebody reads to
    know whether a different transport could go underneath.

    Keyword-only after the value, because `aiokafka` carries a `partition` argument
    between `key` and `headers`. Declaring them positionally would make the real
    producer fail to satisfy this protocol -- correct at runtime, since every call
    here names its arguments, and rejected by the type checker for a reason worth
    keeping: a positional call would silently pass `headers` as `partition`.
    """

    async def send_and_wait(
        self,
        topic: str,
        value: bytes,
        *,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> Any:
        """Send one message and wait for the broker to acknowledge it."""
        ...


class PhotoEvents:
    """Publishes to `edutap.<env>.person.photo`."""

    def __init__(
        self, *, producer: Producer, topic_prefix: str, producer_version: str = ""
    ) -> None:
        """Bind to a started producer and to the environment's topic prefix."""
        self._producer = producer
        self._topic = f"{topic_prefix}.person.photo"
        self._producer_version = producer_version

    async def publish(self, event: PhotoEvent, *, occurred_at: datetime | None = None) -> None:
        """Send one fact.

        `send_and_wait` rather than fire-and-forget: the caller publishes *before* it
        commits, and that arrangement only means anything if a failed send is
        actually observed.

        The key is the person, not the version. Every fact about one person then
        lands on one partition and is consumed in order — otherwise a withdrawal
        could be processed before the activation it follows.
        """
        stamp = occurred_at or datetime.now(tz=UTC)
        headers = [
            ("edutap-producer", PRODUCER.encode()),
            ("edutap-schema", SCHEMA.encode()),
            ("edutap-event-id", str(uuid4()).encode()),
            ("edutap-occurred-at", stamp.isoformat().encode()),
            ("edutap-action", event.action.encode()),
        ]
        if self._producer_version:
            headers.append(("edutap-producer-version", self._producer_version.encode()))

        body = {
            "person_uid": event.person_uid,
            "version": event.version,
            **event.payload,
        }
        await self._producer.send_and_wait(
            self._topic,
            value=json.dumps(body, separators=(",", ":")).encode(),
            key=event.person_uid.encode(),
            headers=headers,
        )


def activated(
    person_uid: str, version: str, *, evidence_kind: str, assurance: str | None
) -> PhotoEvent:
    """Build the fact that a version became the person's photograph."""
    return PhotoEvent(
        action="activated",
        person_uid=person_uid,
        version=version,
        payload={"evidence_kind": evidence_kind, "photo_assurance": assurance},
    )


def rejected(person_uid: str, version: str, *, reason: str) -> PhotoEvent:
    """Build the fact that a version was refused.

    The reason travels because the consumer writes it into the mail, and the mail is
    what starts the retention clock -- a rejection nobody was told about never
    expires.
    """
    return PhotoEvent(
        action="rejected",
        person_uid=person_uid,
        version=version,
        payload={"reason": reason},
    )


def withdrawn(person_uid: str) -> PhotoEvent:
    """Build the fact that the person has no active photograph any more.

    No version: what a consumer needs to know is that the card now shows a
    placeholder, and which version stopped being active does not change that.
    """
    return PhotoEvent(action="withdrawn", person_uid=person_uid, version=None, payload={})


def held(person_uid: str, version: str, *, reason: str, by: str) -> PhotoEvent:
    """Build the fact that a legal hold was placed.

    Published because somebody has to be told *now*: the deletion of the person
    removes held versions too, so the handover has to happen while the person is
    still on file.
    """
    return PhotoEvent(
        action="held",
        person_uid=person_uid,
        version=version,
        payload={"reason": reason, "by": by},
    )
