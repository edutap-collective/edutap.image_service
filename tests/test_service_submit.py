"""The upload use case, end to end except for HTTP.

The repository runs against a real PostgreSQL — what it promises is what the
database does. The object store and the image API are fakes, because what is under
test here is the *order* of the steps and what ends up recorded, not their wire
protocols; those have their own tests.
"""

import io
from datetime import timedelta

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from PIL import Image

from edutap.image_service.clients.image_api import ValidationReport
from edutap.image_service.ingest import Limits, UnsupportedFormat
from edutap.image_service.manifest import DEFAULT
from edutap.image_service.repository import PhotoRepository
from edutap.image_service.service import NoFaceToCrop, PhotoService

pytestmark = pytest.mark.integration

UID = "ab12cde@lmu.de"
PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]


def _png(size=(512, 512)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStore:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key, data, content_type):
        self.objects[key] = (data, content_type)

    async def purge_version(self, person_uid, version):
        prefix = f"{person_uid}/photo/{version}/"
        gone = [key for key in self.objects if key.startswith(prefix)]
        for key in gone:
            del self.objects[key]
        return len(gone)


class FakeImageApi:
    def __init__(self, *, report=None):
        self.report = report or ValidationReport(passed=True, crop_mode="face", crop=_png())
        self.crops = []

    async def validate_and_crop(self, image, *, size=512):
        return self.report

    async def crop(self, image, *, mask="none", aspect_ratio="square", height=512, width="auto"):
        self.crops.append({"mask": mask, "height": height})
        return _png((height, height))


def build(session, image_api=None):
    store = FakeStore()
    api = image_api or FakeImageApi()
    service = PhotoService(
        repository=PhotoRepository(session, origin="https://example.org/api"),
        store=store,
        image_api=api,
        manifest=DEFAULT,
        limits=Limits(max_bytes=5_000_000, max_edge=4096),
        placeholder=b"placeholder-bytes",
        reactivation_max_age=timedelta(days=180),
    )
    return service, store, api


async def test_a_submission_stores_the_raw_and_every_variant_of_the_manifest(session):
    service, store, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    keys = sorted(store.objects)
    assert f"{UID}/photo/{result.version}/raw" in keys
    for variant in DEFAULT.variants:
        assert f"{UID}/photo/{result.version}/default/{variant.name}" in keys
    assert result.stored_objects == 1 + len(DEFAULT.variants)


async def test_the_unmasked_variants_are_stored_as_jpeg_and_the_masked_one_as_png(session):
    """The mask needs an alpha channel; the portrait does not, and pays for it in bytes."""
    service, store, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    types = {
        key.rsplit("/", 1)[-1]: value[1]
        for key, value in store.objects.items()
        if f"/{result.version}/default/" in key
    }
    assert types == {
        "square-512": "image/jpeg",
        "square-1024": "image/jpeg",
        "circle-512": "image/png",
    }


async def test_the_row_lands_as_a_candidate_with_the_validation_waiting_on_it(session):
    """The verdict is produced now and belongs in a trail entry written later.

    It waits on the row rather than in memory, because the request that produced it
    and the request that records it are different requests.
    """
    service, _, _ = build(session)
    await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == "draft"
    assert row["recipe"] == "default"
    assert row["draft_details"]["validation"]["passed"] is True
    assert "dimensions" in row["draft_details"]

    count = (await session.execute(sa.select(sa.func.count()).select_from(REVIEW))).scalar()
    assert count == 0


async def test_a_second_upload_replaces_the_first_candidate(session):
    """At most one candidate per person. The service clears, the index guarantees."""
    service, store, _ = build(session)
    first = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    second = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    rows = (await session.execute(sa.select(PHOTO))).mappings().all()
    assert [row["version"] for row in rows] == [second.version]
    assert not any(key.startswith(f"{UID}/photo/{first.version}/") for key in store.objects)


async def test_a_photograph_that_fails_a_hard_check_is_still_queued(session):
    """Failing is what a reviewer is for. Only an unusable *file* is refused outright.

    A person whose photograph is badly lit uploads something a human can still judge,
    and the design puts every upload in front of a reviewer anyway.
    """
    api = FakeImageApi(
        report=ValidationReport(
            passed=False, crop_mode="face", warnings=["no_headwear"], crop=_png()
        )
    )
    service, _, _ = build(session, image_api=api)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    assert not result.report.passed
    assert (await session.execute(sa.select(PHOTO.c.state))).scalar() == "draft"


async def test_an_image_without_a_face_stores_nothing_at_all(session):
    """There would be no picture for a reviewer to approve.

    Queueing it would fill the review list with entries nobody can act on, so the
    person is told immediately instead.
    """
    api = FakeImageApi(report=ValidationReport(passed=False, crop_mode=None, crop=None))
    service, store, _ = build(session, image_api=api)

    with pytest.raises(NoFaceToCrop):
        await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    assert store.objects == {}
    assert (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar() == 0


async def test_an_upload_needs_no_rights_declaration(session):
    """It is made at confirmation. Demanding it here would collect it twice.

    The refusal itself has not gone away -- it moved to `test_service_confirm.py`,
    which is where the declaration is now made.
    """
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    assert result.version


async def test_an_unusable_file_never_reaches_the_image_api(session):
    """Sanitising comes first so a bomb is not forwarded to the other service."""
    service, store, api = build(session)
    with pytest.raises(UnsupportedFormat):
        await service.submit(person_uid=UID, upload=b"not an image")
    assert api.crops == []
    assert store.objects == {}
