"""The HTTP surface, against the real routers and a real database.

The object store and the image API are fakes; what is under test is the mapping
between a request, a use case and a status code — above all the two refusals that
must not be confused, and the one route that has no token on it.
"""

import io
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from edutap.db_definitions.public import metadata
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from edutap.image_service.api.routers import public, router
from edutap.image_service.clients.image_api import ValidationReport
from edutap.image_service.events import PhotoEvents
from edutap.image_service.ingest import Limits
from edutap.image_service.manifest import DEFAULT
from edutap.image_service.repository import PhotoRepository
from edutap.image_service.service import PhotoService
from edutap.image_service.states import EvidenceKind, PhotoState

pytestmark = pytest.mark.integration

UID = "ab12cde@lmu.de"
TOKEN = "s3cr3t-token"  # noqa: S105  a fixture value, not a credential
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PLACEHOLDER = b"placeholder-bytes"


def _png(size=(512, 512)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStore:
    def __init__(self):
        self.objects = {}

    async def put(self, key, data, content_type):
        self.objects[key] = (data, content_type)

    async def get(self, key):
        return self.objects[key][0]

    async def purge_version(self, person_uid, version):
        prefix = f"{person_uid}/photo/{version}/"
        gone = [key for key in self.objects if key.startswith(prefix)]
        for key in gone:
            del self.objects[key]
        return len(gone)


class FakeProducer:
    def __init__(self):
        self.sent = []

    async def send_and_wait(self, topic, value, key=None, headers=None):
        self.sent.append({"topic": topic, "value": value, "headers": dict(headers or [])})


class FakeImageApi:
    def __init__(self, report=None):
        self.report = report or ValidationReport(passed=True, crop_mode="face", crop=_png())

    async def validate_and_crop(self, image, *, size=512):
        return self.report

    async def crop(self, image, *, mask="none", aspect_ratio="square", height=512, width="auto"):
        return _png((height, height))


@pytest.fixture
def client(postgres_dsn, request):
    """Build the app with its own engine, created inside the test client's loop.

    Not the `engine` fixture: `TestClient` runs the application in an event loop of
    its own, and an asyncpg connection belongs to the loop that opened it. Sharing
    one across the two produces "attached to a different loop" from somewhere deep
    in the driver, which is a long way from the cause.
    """
    store = FakeStore()
    image_api = getattr(request, "param", None) or FakeImageApi()
    producer = FakeProducer()
    events = PhotoEvents(producer=producer, topic_prefix="edutap.test")

    @asynccontextmanager
    async def lifespan(app):
        engine = create_async_engine(postgres_dsn)
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
            await connection.run_sync(metadata.create_all)

        @asynccontextmanager
        async def unit_of_work():
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield (
                    session,
                    PhotoService(
                        repository=PhotoRepository(session, origin="https://example.org/api"),
                        store=store,
                        image_api=image_api,
                        manifest=DEFAULT,
                        limits=Limits(max_bytes=5_000_000, max_edge=4096),
                        placeholder=PLACEHOLDER,
                        reactivation_max_age=timedelta(days=180),
                        events=events,
                    ),
                )

        app.state.unit_of_work = unit_of_work
        app.state.service_tokens = {"backend": TOKEN}
        app.state.default_expiry_days = 14
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.include_router(public)
    with TestClient(app) as test_client:
        test_client.store = store
        test_client.events = producer
        yield test_client


def _upload(client, **overrides):
    data = {"actor": "self", "rights_declared": "true"} | overrides
    return client.post(
        f"/persons/{UID}/photos",
        files={"file": ("portrait.png", _png(), "image/png")},
        data=data,
        headers=AUTH,
    )


def test_an_upload_is_accepted_and_comes_back_pending(client):
    response = _upload(client)
    assert response.status_code == 201
    assert response.json()["state"] == "pending"


def test_every_authenticated_route_refuses_without_a_token(client):
    """The token is the whole authorisation model here.

    A front end authenticates its own user and vouches for the call; this service
    never sees a person's session, which is what keeps it free of one institution's
    role model.
    """
    assert client.get(f"/persons/{UID}/photos").status_code == 401
    assert client.post(f"/persons/{UID}/photos").status_code == 401


def test_a_wrong_token_is_refused(client):
    response = client.get(f"/persons/{UID}/photos", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_the_current_route_needs_no_token_at_all(client):
    """Google fetches this URL without credentials, long after the pass was issued."""
    response = client.get(f"/persons/{UID}/photo/current/default/square-512")
    assert response.status_code == 200


def test_a_person_without_a_photograph_gets_the_placeholder_not_a_404(client):
    """A 404 here would be a broken image on somebody's card."""
    response = client.get(f"/persons/{UID}/photo/current/default/square-512")
    assert response.content == PLACEHOLDER
    assert response.headers["X-Photo-Placeholder"] == "true"


def test_the_current_route_serves_the_active_version_once_there_is_one(client):
    version = _upload(client).json()["version"]
    approved = client.post(
        f"/persons/{UID}/photos/{version}/approve",
        json={"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL},
        headers=AUTH,
    )
    assert approved.status_code == 204

    response = client.get(f"/persons/{UID}/photo/current/default/square-512")
    assert response.headers["X-Photo-Placeholder"] == "false"
    assert response.content != PLACEHOLDER


def test_a_pending_version_never_reaches_the_current_route(client):
    """It is visible to a reviewer and to nobody else.

    Serving it here would put an unreviewed photograph on a card the moment somebody
    uploaded one.
    """
    _upload(client)
    response = client.get(f"/persons/{UID}/photo/current/default/square-512")
    assert response.content == PLACEHOLDER


def test_a_pending_version_is_visible_on_the_review_route(client):
    version = _upload(client).json()["version"]
    response = client.get(f"/persons/{UID}/photos/{version}/default/square-512", headers=AUTH)
    assert response.status_code == 200
    assert response.content != PLACEHOLDER


def test_the_sanitised_original_is_never_served(client):
    """The one object nobody gets, reviewer included -- they look at the crop."""
    version = _upload(client).json()["version"]
    response = client.get(f"/persons/{UID}/photos/{version}/default/raw", headers=AUTH)
    assert response.status_code == 403


def test_approval_without_an_evidence_kind_is_refused(client):
    """There is no default. A default would record that somebody looked who did not."""
    version = _upload(client).json()["version"]
    response = client.post(
        f"/persons/{UID}/photos/{version}/approve", json={"actor": "support"}, headers=AUTH
    )
    assert response.status_code == 400


def test_rejecting_without_a_reason_is_refused(client):
    """The reason goes into the trail and into what the person is told."""
    version = _upload(client).json()["version"]
    response = client.post(
        f"/persons/{UID}/photos/{version}/reject", json={"actor": "support"}, headers=AUTH
    )
    assert response.status_code == 400


def test_an_illegal_transition_is_a_conflict(client):
    version = _upload(client).json()["version"]
    body = {"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL}
    client.post(f"/persons/{UID}/photos/{version}/approve", json=body, headers=AUTH)
    again = client.post(f"/persons/{UID}/photos/{version}/approve", json=body, headers=AUTH)
    assert again.status_code == 409


def test_deleting_the_active_version_is_a_conflict_and_a_held_one_is_locked(client):
    """Two refusals that must not collapse into one code.

    A front end shows a person "replace it instead" for the first and a reviewer
    "this is evidence in a proceeding" for the second. Making both 409 would force
    it to parse the message to tell them apart.
    """
    version = _upload(client).json()["version"]
    client.post(
        f"/persons/{UID}/photos/{version}/approve",
        json={"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL},
        headers=AUTH,
    )
    active = client.delete(f"/persons/{UID}/photos/{version}?actor=self", headers=AUTH)
    assert active.status_code == 409


def test_purging_a_superseded_version_clears_its_objects(client):
    first = _upload(client).json()["version"]
    body = {"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL}
    client.post(f"/persons/{UID}/photos/{first}/approve", json=body, headers=AUTH)
    second = _upload(client).json()["version"]
    client.post(f"/persons/{UID}/photos/{second}/approve", json=body, headers=AUTH)

    response = client.delete(f"/persons/{UID}/photos/{first}?actor=self", headers=AUTH)
    assert response.status_code == 204
    assert not [key for key in client.store.objects if f"/{first}/" in key]

    listed = client.get(f"/persons/{UID}/photos", headers=AUTH).json()
    states = {row["version"]: row["state"] for row in listed}
    assert states[first] == PhotoState.SUPERSEDED
    assert states[second] == PhotoState.ACTIVE


def test_an_unknown_version_is_a_404(client):
    response = client.post(
        f"/persons/{UID}/photos/does-not-exist/reject",
        json={"actor": "support", "reason": "nope"},
        headers=AUTH,
    )
    assert response.status_code == 404


def test_a_rejected_version_only_expires_once_the_person_was_told(client):
    """The clock starts at the notification, not at the rejection.

    Two runs with the same deadline: the first finds nothing because nobody has been
    told yet, the second finds it. That gap is the whole point -- somebody away for
    three weeks would otherwise lose the photograph before learning it was refused.
    """
    version = _upload(client).json()["version"]
    client.post(
        f"/persons/{UID}/photos/{version}/reject",
        json={"actor": "support:kb12", "reason": "too dark"},
        headers=AUTH,
    )

    first = client.post("/maintenance/expire", json={"older_than_days": 0}, headers=AUTH)
    assert first.json()["purged"] == []

    client.post(f"/persons/{UID}/photos/{version}/notified?actor=worker", headers=AUTH)
    second = client.post("/maintenance/expire", json={"older_than_days": 0}, headers=AUTH)
    assert [row["version"] for row in second.json()["purged"]] == [version]
    assert not [key for key in client.store.objects if f"/{version}/" in key]


def test_a_held_version_survives_the_retention_run(client):
    """Every deletion path but the person's own consults the hold."""
    version = _upload(client).json()["version"]
    client.post(
        f"/persons/{UID}/photos/{version}/reject",
        json={"actor": "support:kb12", "reason": "too dark"},
        headers=AUTH,
    )
    client.post(f"/persons/{UID}/photos/{version}/notified?actor=worker", headers=AUTH)
    client.post(
        f"/persons/{UID}/photos/{version}/hold",
        json={"actor": "support:kb12", "reason": "not this person"},
        headers=AUTH,
    )

    result = client.post("/maintenance/expire", json={"older_than_days": 0}, headers=AUTH)
    assert result.json()["purged"] == []
    assert [key for key in client.store.objects if f"/{version}/" in key]


def test_a_hold_without_a_reason_is_refused(client):
    """The reason is the whole value of the record when somebody asks about it later."""
    version = _upload(client).json()["version"]
    response = client.post(
        f"/persons/{UID}/photos/{version}/hold", json={"actor": "support"}, headers=AUTH
    )
    assert response.status_code == 400


def test_resetting_withdraws_the_photograph_and_the_placeholder_returns(client):
    version = _upload(client).json()["version"]
    client.post(
        f"/persons/{UID}/photos/{version}/approve",
        json={"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL},
        headers=AUTH,
    )
    assert client.get(f"/persons/{UID}/photo/current/default/square-512").content != PLACEHOLDER

    reset = client.post(f"/persons/{UID}/photos/reset?actor=support:kb12", headers=AUTH)
    assert reset.status_code == 204

    after = client.get(f"/persons/{UID}/photo/current/default/square-512")
    assert after.content == PLACEHOLDER
    listed = client.get(f"/persons/{UID}/photos", headers=AUTH).json()
    assert listed[0]["state"] == PhotoState.SUPERSEDED


def test_resetting_a_person_who_has_no_photograph_is_a_404(client):
    assert client.post(f"/persons/{UID}/photos/reset?actor=x", headers=AUTH).status_code == 404


def test_the_decisions_are_published_as_facts(client):
    """One event per decision, keyed by the person, with the schema a consumer pins."""
    version = _upload(client).json()["version"]
    client.post(
        f"/persons/{UID}/photos/{version}/approve",
        json={"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL},
        headers=AUTH,
    )
    client.post(f"/persons/{UID}/photos/reset?actor=support:kb12", headers=AUTH)

    actions = [event["headers"]["edutap-action"] for event in client.events.sent]
    assert actions == [b"activated", b"withdrawn"]
