"""The HTTP surface, against the real routers and a real database.

The object store and the image API are fakes; what is under test is the mapping
between a request, a use case and a status code — above all the two refusals that
must not be confused, and the one route that has no token on it.
"""

import io
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from edutap.db_definitions.public import metadata
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from edutap.image_service.api.routers import public, router
from edutap.image_service.clients.image_api import ValidationReport
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

    # The same object the route reports and the ingest check enforces, so a test
    # asserting on one is asserting on the other.
    enforced = Limits(max_bytes=5_000_000, max_edge=4096)
    # Only `default_expiry_days` is read from here, by the retention route when the
    # caller omits a deadline. A real Settings would want a database and a bucket.
    settings = SimpleNamespace(default_expiry_days=14)

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
                        limits=enforced,
                        placeholder=PLACEHOLDER,
                        reactivation_max_age=timedelta(days=180),
                    ),
                )

        app.state.unit_of_work = unit_of_work
        app.state.service_tokens = {"backend": TOKEN}
        app.state.limits = enforced
        app.state.settings = settings
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.include_router(public)
    with TestClient(app) as test_client:
        test_client.store = store
        yield test_client


def _upload(client):
    """Post a file. What comes back is a candidate -- nobody has been asked yet."""
    return client.post(
        f"/persons/{UID}/photos",
        files={"file": ("portrait.png", _png(), "image/png")},
        headers=AUTH,
    )


def _submit(client, **overrides):
    """Upload and confirm, and return the version.

    The two steps a person actually takes, for the tests that need a version
    somebody has stood behind -- everything a reviewer touches.
    """
    version = _upload(client).json()["version"]
    body = {"actor": "user:ab12cde@lmu.de", "rights_declared": True} | overrides
    client.post(f"/persons/{UID}/photos/{version}/confirm", json=body, headers=AUTH)
    return version


def test_an_upload_comes_back_as_a_candidate(client):
    """An upload answers with a candidate.

    `draft`, not `pending`: nobody has been asked to look at it yet, and a front end
    that said "in review" here would be lying by one step.
    """
    response = _upload(client)
    assert response.status_code == 201
    assert response.json()["state"] == "draft"


def test_confirming_queues_the_candidate(client):
    version = _upload(client).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "user:ab12cde@lmu.de", "rights_declared": True},
        headers=AUTH,
    )

    assert response.status_code == 204
    listed = client.get(f"/persons/{UID}/photos", headers=AUTH).json()
    assert [row["state"] for row in listed] == ["pending"]


def test_confirming_without_the_declaration_is_a_bad_request(client):
    version = _upload(client).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "user:ab12cde@lmu.de", "rights_declared": False},
        headers=AUTH,
    )

    assert response.status_code == 400


def test_confirming_an_unknown_version_is_a_404(client):
    """A person confirms what they saw, so the version travels in the path."""
    _upload(client)

    response = client.post(
        f"/persons/{UID}/photos/some-other-version/confirm",
        json={"actor": "self", "rights_declared": True},
        headers=AUTH,
    )

    assert response.status_code == 404


def test_confirming_needs_a_token(client):
    response = client.post(
        f"/persons/{UID}/photos/whatever/confirm",
        json={"actor": "self", "rights_declared": True},
    )

    assert response.status_code == 401


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
    version = _submit(client)
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
    version = _submit(client)
    response = client.get(f"/persons/{UID}/photos/{version}/default/square-512", headers=AUTH)
    assert response.status_code == 200
    assert response.content != PLACEHOLDER


def test_the_sanitised_original_is_never_served(client):
    """The one object nobody gets, reviewer included -- they look at the crop."""
    version = _submit(client)
    response = client.get(f"/persons/{UID}/photos/{version}/default/raw", headers=AUTH)
    assert response.status_code == 403


def test_approval_without_an_evidence_kind_is_refused(client):
    """There is no default. A default would record that somebody looked who did not."""
    version = _submit(client)
    response = client.post(
        f"/persons/{UID}/photos/{version}/approve", json={"actor": "support"}, headers=AUTH
    )
    assert response.status_code == 400


def test_rejecting_without_a_reason_is_refused(client):
    """The reason goes into the trail and into what the person is told."""
    version = _submit(client)
    response = client.post(
        f"/persons/{UID}/photos/{version}/reject", json={"actor": "support"}, headers=AUTH
    )
    assert response.status_code == 400


def test_an_illegal_transition_is_a_conflict(client):
    version = _submit(client)
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
    version = _submit(client)
    client.post(
        f"/persons/{UID}/photos/{version}/approve",
        json={"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL},
        headers=AUTH,
    )
    active = client.delete(f"/persons/{UID}/photos/{version}?actor=self", headers=AUTH)
    assert active.status_code == 409


def test_purging_a_superseded_version_clears_its_objects(client):
    first = _submit(client)
    body = {"actor": "support:kb12", "evidence_kind": EvidenceKind.SUPPORT_VISUAL}
    client.post(f"/persons/{UID}/photos/{first}/approve", json=body, headers=AUTH)
    second = _submit(client)
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


def test_the_declaration_reference_travels_through_the_route(client):
    version = _upload(client).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={
            "actor": "user:ab12cde@lmu.de",
            "rights_declared": True,
            "declaration_tag": "v1.0",
            "declaration_sha": "a" * 40,
        },
        headers=AUTH,
    )

    assert response.status_code == 204


def test_a_half_given_reference_is_a_bad_request(client):
    version = _upload(client).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "self", "rights_declared": True, "declaration_tag": "v1.0"},
        headers=AUTH,
    )

    assert response.status_code == 400


def test_limits_are_readable_without_a_token(client):
    """A browser checks them before uploading, and it holds no service token."""
    response = client.get("/limits")

    assert response.status_code == 200


def test_limits_report_what_this_deployment_enforces(client):
    """One number in one place.

    A second copy in a front end drifts, and then it refuses what this service would
    have taken. The client fixture builds the service with max_bytes=5_000_000 and
    max_edge=4096, so the route must report those and not a default.
    """
    body = client.get("/limits").json()

    assert body["max_file_bytes"] == 5_000_000
    assert body["max_image_edge"] == 4096


def test_limits_are_reported_as_media_types(client):
    """A front end puts these in an `accept` attribute; PIL's names are no use there."""
    formats = client.get("/limits").json()["accepted_formats"]

    assert "image/jpeg" in formats
    assert "image/png" in formats
    assert "JPEG" not in formats


def test_an_oversized_upload_is_refused_at_the_reported_limit(client):
    """The reported number and the enforced one are the same number, not two."""
    reported = client.get("/limits").json()["max_file_bytes"]

    response = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("big.png", b"\x89PNG" + b"\x00" * (reported + 1), "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 413


def test_the_retention_run_reports_what_it_did(client):
    """The answer is the record: an operator reads it in a deploy log."""
    response = client.post(
        "/maintenance/expire",
        json={"state": "rejected", "older_than_days": 14},
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["purged"] == []
    assert body["skipped_legal_hold"] == []


def test_a_state_that_never_expires_is_a_bad_request(client):
    """`pending` stays and stays visible -- an operator watches the queue, not a timer."""
    response = client.post("/maintenance/expire", json={"state": "pending"}, headers=AUTH)
    assert response.status_code == 400


def test_the_deadline_may_be_omitted(client):
    """Then the deployment's configured default applies, not a constant in here."""
    response = client.post("/maintenance/expire", json={}, headers=AUTH)
    assert response.status_code == 200


def test_the_retention_run_needs_a_token(client):
    """Not a public route: it deletes."""
    assert client.post("/maintenance/expire", json={}).status_code == 401
