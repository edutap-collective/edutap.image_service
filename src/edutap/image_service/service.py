"""The upload use case: everything between a file arriving and a version existing.

Deliberately free of HTTP. What a router will do is unpack a request and call this;
what this does is the part worth testing, and it is testable without a server.

The order is not interchangeable. Sanitising comes before the other service sees
the bytes, because a bomb must not be forwarded. Storage comes before the row,
because a row pointing at objects that are not there is worse than objects nobody
references — the second is rubbish a retention run clears, the first is a reference
that resolves to nothing.
"""

import io
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from PIL import Image

from .clients.image_api import ValidationReport
from .ingest import Limits, rights_metadata, sanitise
from .manifest import Manifest, Variant
from .objectstore import raw_key, variant_key


class NoFaceToCrop(Exception):
    """The image is readable but contains no single face to centre on.

    Not a rejection in the review sense: nothing is stored, nobody is asked to look
    at it, and the person is told straight away. Storing it would put a version in
    the queue that no reviewer could act on -- there is no picture to approve.
    """

    def __init__(self, report: ValidationReport) -> None:
        """Carry the report, so the caller can tell the person which check failed."""
        super().__init__("no face-centred crop could be produced")
        self.report = report


class _ImageApi(Protocol):
    async def validate_and_crop(self, image: bytes, *, size: int = 512) -> ValidationReport: ...

    async def crop(
        self,
        image: bytes,
        *,
        mask: str = "none",
        aspect_ratio: str = "square",
        height: int = 512,
        width: int | str = "auto",
    ) -> bytes: ...


class _Store(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...


@dataclass(frozen=True)
class Submission:
    """What the caller needs to answer the person who uploaded."""

    version: str
    report: ValidationReport
    stored_objects: int


class PhotoService:
    """The use cases, over a repository, an object store and the image API."""

    def __init__(
        self,
        *,
        repository: Any,
        store: _Store,
        image_api: _ImageApi,
        manifest: Manifest,
        limits: Limits,
    ) -> None:
        """Hold the collaborators; the caller owns the transaction the repository uses."""
        self._repository = repository
        self._store = store
        self._image_api = image_api
        self._manifest = manifest
        self._limits = limits

    async def submit(
        self, *, person_uid: str, upload: bytes, actor: str, rights_declared: bool
    ) -> Submission:
        """Accept an uploaded file and record it as a pending version.

        `rights_declared` is not a courtesy flag. It is the declaration that carries
        the legal weight -- copyright metadata found in the file is recorded for a
        reviewer to read and never evaluated -- so a submission without it is
        refused here rather than defaulted.
        """
        if not rights_declared:
            raise ValueError("a submission needs the uploader's rights declaration")

        image = sanitise(upload, limits=self._limits)
        claims = rights_metadata(upload)
        report = await self._image_api.validate_and_crop(image.data)
        if report.crop is None:
            raise NoFaceToCrop(report)

        version = str(uuid4())
        stored = await self._store_version(person_uid, version, image.data, report.crop)

        await self._repository.add_pending(
            person_uid=person_uid,
            version=version,
            sha256=image.sha256,
            recipe=self._manifest.name,
            actor=actor,
            details={
                "validation": {
                    "passed": report.passed,
                    "warnings": report.warnings,
                    "checks": {check.name: check.passed for check in report.checks},
                },
                "rights_claims": claims,
                "dimensions": {"width": image.width, "height": image.height},
            },
        )
        return Submission(version=version, report=report, stored_objects=stored)

    async def _store_version(self, person_uid: str, version: str, raw: bytes, crop: bytes) -> int:
        """Put the sanitised original and every rendering of the manifest."""
        await self._store.put(raw_key(person_uid, version), raw, "image/jpeg")
        stored = 1
        for variant in self._manifest.variants:
            rendered = await self._render(crop, variant)
            await self._store.put(
                variant_key(person_uid, version, self._manifest.name, variant.name),
                rendered,
                variant.content_type,
            )
            stored += 1
        return stored

    async def _render(self, crop: bytes, variant: Variant) -> bytes:
        """Ask the image API for one rendering, re-encoding where the manifest says so."""
        rendered = await self._image_api.crop(
            crop,
            mask=variant.mask,
            aspect_ratio=variant.aspect_ratio,
            height=variant.height,
            width=variant.width,
        )
        return _to_jpeg(rendered) if variant.to_jpeg else rendered


def _to_jpeg(png: bytes) -> bytes:
    """Re-encode a rendering that needs no alpha channel.

    `/crop/` has no format parameter and always answers PNG. For an unmasked
    portrait that is roughly six times the bytes of the same picture as JPEG, per
    version, per person -- and the alpha channel it pays for is unused. The clean
    fix is a format parameter in `edutap.image_api`; until then this is the cheaper
    of the two evils, and it is one decode of an image we produced ourselves.
    """
    buffer = io.BytesIO()
    image = Image.open(io.BytesIO(png)).convert("RGB")
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()
