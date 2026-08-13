"""What this service tells the world about a photograph.

Facts, not requests. "This person's photograph became active" is something that
happened; turning it into "rebuild and push their passes", or into a mail, is the
consumer's business -- and keeping it that way is what leaves this package free of
any knowledge of passes, mail templates or one institution's role model.

`aiokafka` lives in the optional `kafka` extra rather than the core dependencies,
so it is never imported at module import time -- only lazily, inside `start`, and
only when Kafka is actually enabled. A deployment that does not publish events must
not have to install a broker client to run the service.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from aiokafka import AIOKafkaProducer  # ty: ignore[unresolved-import]


@dataclass(frozen=True)
class PhotoEvent:
    """One fact about one version.

    `person_uid` is the message key, so everything about one person keeps its order
    on the topic. Two events about two people have no order worth preserving, and
    demanding one would cost a partition.
    """

    event: str
    person_uid: str
    version: str | None
    occurred_at: datetime
    facts: dict[str, Any]

    def payload(self) -> bytes:
        """Serialise as the consumer reads it."""
        return json.dumps(
            {
                "event": self.event,
                "person_uid": self.person_uid,
                **({"version": self.version} if self.version is not None else {}),
                "occurred_at": self.occurred_at.isoformat(),
                **self.facts,
            }
        ).encode()


class PhotoEvents(Protocol):
    """What the use cases need in order to announce something."""

    async def publish(self, event: PhotoEvent) -> None:
        """Announce one fact about one version."""
        ...


class NoEvents:
    """The default: a service that announces nothing.

    Not a stub for tests -- a deployment without a broker is a legitimate way to run
    this service, and it should not have to pretend to have one.
    """

    def __init__(self) -> None:
        """Record what was published, so a test can assert without a broker."""
        self.published: list[PhotoEvent] = []

    async def publish(self, event: PhotoEvent) -> None:
        """Keep the event and do nothing else."""
        self.published.append(event)


class KafkaPhotoEvents:
    """Publishes to `<prefix>.person.photo`.

    The topic carries no producer name: the convention across these topics puts the
    producing service in a header, because a name in the topic would have to change
    the day a second service publishes the same fact.
    """

    def __init__(self, *, bootstrap_servers: str, topic_prefix: str) -> None:
        """Store the connection settings; the producer is created in `start`."""
        self._bootstrap_servers = bootstrap_servers
        self._topic = f"{topic_prefix}.person.photo"
        self._producer: "AIOKafkaProducer | None" = None  # noqa: UP037

    async def start(self) -> None:
        """Create and start the producer.

        Raises:
            RuntimeError: if the optional `kafka` extra is not installed.

        """
        try:
            from aiokafka import AIOKafkaProducer  # ty: ignore[unresolved-import]
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise RuntimeError(
                "events are enabled but aiokafka is not installed; install the 'kafka' extra"
            ) from exc
        self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
        await self._producer.start()

    async def stop(self) -> None:
        """Flush and close, so a shutdown does not drop what was just recorded."""
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, event: PhotoEvent) -> None:
        """Send one fact, keyed by the person it concerns."""
        if self._producer is None:
            raise RuntimeError("publish before start")
        await self._producer.send_and_wait(
            self._topic,
            key=event.person_uid.encode(),
            value=event.payload(),
            headers=[("producer", b"edutap.image_service")],
        )


def activated(person_uid: str, version: str, *, evidence_kind: str, assurance: str) -> PhotoEvent:
    """Announce that a version became the photograph of this person."""
    return PhotoEvent(
        event="photo.activated",
        person_uid=person_uid,
        version=version,
        occurred_at=datetime.now(tz=UTC),
        facts={"evidence_kind": evidence_kind, "photo_assurance": assurance},
    )


def rejected(person_uid: str, version: str, *, reason: str) -> PhotoEvent:
    """Announce a refusal. The reason travels: a consumer writes the mail."""
    return PhotoEvent(
        event="photo.rejected",
        person_uid=person_uid,
        version=version,
        occurred_at=datetime.now(tz=UTC),
        facts={"reason": reason},
    )


def withdrawn(person_uid: str) -> PhotoEvent:
    """Announce that the person has no active photograph any more.

    No version: what a consumer needs to know is that the card now shows a
    placeholder, and which version stopped being active does not change that.
    """
    return PhotoEvent(
        event="photo.withdrawn",
        person_uid=person_uid,
        version=None,
        occurred_at=datetime.now(tz=UTC),
        facts={},
    )


def held(person_uid: str, version: str, *, reason: str, by: str) -> PhotoEvent:
    """Announce that a legal hold was placed.

    Published because somebody has to be told *now*: the deletion of the person
    removes held versions too, so the handover has to happen while the person is
    still on file. Nobody watches this table.
    """
    return PhotoEvent(
        event="photo.held",
        person_uid=person_uid,
        version=version,
        occurred_at=datetime.now(tz=UTC),
        facts={"reason": reason, "by": by},
    )
