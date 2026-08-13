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
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from PIL import Image

from .clients.image_api import ValidationReport
from .ingest import Limits, rights_metadata, sanitise
from .manifest import MANIFESTS, Manifest, Variant
from .objectstore import raw_key, variant_key
from .states import (
    EvidenceKind,
    IllegalTransition,
    PhotoState,
    approve,
    confirm,
    purge,
    reactivate,
    reject,
)


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
    """What this service needs from an object store, and nothing more.

    Narrow on purpose: it is what a test double has to provide, and it is the list
    somebody reads to know whether a different store could be put underneath.
    """

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def purge_version(self, person_uid: str, version: str) -> int: ...


@dataclass(frozen=True)
class Delivered:
    """Bytes plus what they are, so a router does not have to guess a content type."""

    data: bytes
    content_type: str
    is_placeholder: bool


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
        placeholder: bytes,
        reactivation_max_age: timedelta,
    ) -> None:
        """Hold the collaborators; the caller owns the transaction the repository uses."""
        self._repository = repository
        self._store = store
        self._image_api = image_api
        self._manifest = manifest
        self._limits = limits
        self._placeholder = placeholder
        self._reactivation_max_age = reactivation_max_age

    async def submit(self, *, person_uid: str, upload: bytes) -> Submission:
        """Accept an uploaded file and keep it as a candidate.

        No rights declaration here. It is made when the person confirms what they
        see, so a candidate they discard never carried one -- and a declaration
        collected twice is one nobody can point at.

        A previous candidate is cleared first. The database allows at most one per
        person; doing it here rather than letting the insert fail turns "upload
        another one" into what it obviously means.
        """
        image = sanitise(upload, limits=self._limits)
        claims = rights_metadata(upload)
        report = await self._image_api.validate_and_crop(image.data)
        if report.crop is None:
            raise NoFaceToCrop(report)

        previous = await self._repository.discard_draft(person_uid)
        if previous is not None:
            await self._store.purge_version(person_uid, previous)

        version = str(uuid4())
        stored = await self._store_version(person_uid, version, image.data, report.crop)

        await self._repository.add_draft(
            person_uid=person_uid,
            version=version,
            sha256=image.sha256,
            recipe=self._manifest.name,
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

    async def confirm(
        self,
        *,
        person_uid: str,
        version: str,
        actor: str,
        rights_declared: bool,
        declaration_tag: str | None = None,
        declaration_sha: str | None = None,
    ) -> None:
        """Turn a candidate into a submission, carrying the rights declaration.

        `rights_declared` is not a courtesy flag. It is the declaration that carries
        the legal weight -- copyright metadata found in the file is recorded for a
        reviewer to read and never evaluated -- so a confirmation without it is
        refused here rather than defaulted.

        `declaration_tag` and `declaration_sha` identify the wording the person
        agreed to. They are **recorded, not interpreted**: what the text says, who
        wrote it and where it lives is the deployment's business, and a service that
        grew an opinion about it would stop being adoptable elsewhere.

        Both or neither. A tag without its hash records a version nobody can verify
        later -- a tag can be moved and a hash cannot -- and a declaration that
        cannot be checked is the failure this pair exists to prevent.

        The verdict the upload produced travels from the candidate row into the
        review entry, which is why it waited there: the request that produced it and
        this one are different requests.
        """
        if not rights_declared:
            raise ValueError("a submission needs the uploader's rights declaration")
        if bool(declaration_tag) != bool(declaration_sha):
            raise ValueError("a declaration reference needs both a tag and a hash")

        current = await self._require(person_uid, version)
        outcome = confirm(PhotoState(current["state"]))
        details = dict(current.get("draft_details") or {})
        if declaration_tag and declaration_sha:
            details["declaration"] = {"tag": declaration_tag, "sha": declaration_sha}
        await self._repository.apply(
            person_uid=person_uid,
            version=version,
            outcome=outcome,
            actor=actor,
            action="submit",
            details=details,
        )

    async def approve(
        self, *, person_uid: str, version: str, evidence_kind: EvidenceKind, actor: str
    ) -> None:
        """Activate a version on a reviewer's decision."""
        current = await self._require(person_uid, version)
        outcome = approve(PhotoState(current["state"]), evidence_kind=evidence_kind)
        await self._repository.apply(
            person_uid=person_uid,
            version=version,
            outcome=outcome,
            actor=actor,
            action="approve",
        )

    async def reject(self, *, person_uid: str, version: str, actor: str, reason: str) -> None:
        """Refuse a version awaiting review.

        `notified_at` is deliberately **not** set here. The retention clock starts
        when the person was told, and telling them is the worker's job on the event
        this raises -- setting it now would start the clock on a message nobody has
        sent yet.
        """
        current = await self._require(person_uid, version)
        outcome = reject(PhotoState(current["state"]))
        await self._repository.apply(
            person_uid=person_uid,
            version=version,
            outcome=outcome,
            actor=actor,
            action="reject",
            reason=reason,
        )

    async def reactivate(self, *, person_uid: str, version: str, actor: str, now: datetime) -> None:
        """Let the person switch back to a version they kept.

        How old the approval is comes from the trail. A version that was never
        approved cannot be reactivated by its owner at all, and passing the epoch
        rather than raising would silently send it back through review -- correct by
        accident, and wrong the day the rule changes.
        """
        current = await self._require(person_uid, version)
        reviewed_at = await self._repository.last_approval_at(person_uid, version)
        if reviewed_at is None:
            raise IllegalTransition("this version was never approved")
        outcome = reactivate(
            PhotoState(current["state"]),
            reviewed_at=reviewed_at,
            now=now,
            max_age=self._reactivation_max_age,
            evidence_kind=EvidenceKind(current["evidence_kind"])
            if current["evidence_kind"]
            else None,
        )
        await self._repository.apply(
            person_uid=person_uid,
            version=version,
            outcome=outcome,
            actor=actor,
            action="reactivate",
        )

    async def purge(self, *, person_uid: str, version: str, actor: str) -> int:
        """Clear the bytes of a version the person no longer wants.

        The state machine is asked first, so a held or active version never reaches
        the bucket. Objects go before the row is marked: the other order would leave
        a row claiming its bytes are gone while they are still there, which is the
        one inconsistency nothing later would notice.
        """
        current = await self._require(person_uid, version)
        purge(PhotoState(current["state"]), legal_hold_since=current["legal_hold_since"])
        deleted = await self._store.purge_version(person_uid, version)

        if PhotoState(current["state"]) is PhotoState.DRAFT:
            # A candidate leaves no row behind. `mark_purged` keeps the row so the
            # trail stays readable after the bytes are gone -- but a candidate has
            # no trail, and a kept row would still read `draft` and make the partial
            # unique index refuse this person's next upload.
            await self._repository.discard_draft(person_uid)
            return deleted

        await self._repository.mark_purged(
            person_uid=person_uid, version=version, actor=actor, objects_deleted=deleted
        )
        return deleted

    async def deliver_current(self, *, person_uid: str, recipe: str, variant: str) -> Delivered:
        """Serve the active version, or the placeholder.

        The one route with no token on it, because a wallet provider fetches this
        URL without credentials long after a pass was issued. It therefore serves
        exactly one thing: the active version. Never `pending`, never `rejected`,
        never `raw`.
        """
        active = await self._repository.active_for(person_uid)
        if active is None:
            return Delivered(data=self._placeholder, content_type="image/png", is_placeholder=True)
        key = variant_key(person_uid, active["version"], active["recipe"], variant)
        return Delivered(
            data=await self._store.get(key),
            content_type=_content_type_for(recipe, variant),
            is_placeholder=False,
        )

    async def deliver_version(
        self, *, person_uid: str, version: str, recipe: str, variant: str
    ) -> Delivered:
        """Serve one specific version, for a review client.

        This is the route `pending` is visible on -- a reviewer has to see what they
        are deciding about. `raw` is still refused: it is the only object that is
        never delivered to anybody, and a reviewer looks at the crop.
        """
        if variant == "raw":
            raise NotDeliverable("the sanitised original is never served")
        await self._require(person_uid, version)
        key = variant_key(person_uid, version, recipe, variant)
        return Delivered(
            data=await self._store.get(key),
            content_type=_content_type_for(recipe, variant),
            is_placeholder=False,
        )

    async def list_versions(self, person_uid: str) -> list[dict[str, Any]]:
        """Every version of one person, newest first, for a review client."""
        return await self._repository.list_for(person_uid)

    async def _require(self, person_uid: str, version: str) -> dict[str, Any]:
        row = await self._repository.get(person_uid, version)
        if row is None:
            raise VersionNotFound(f"no version {version!r} for {person_uid!r}")
        return row

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


class VersionNotFound(Exception):
    """No such version for this person."""


class NotDeliverable(Exception):
    """The version exists but must not be served on the route that asked.

    Two cases, and they are not the same refusal: `raw` is never served to anybody,
    and a version that is not active is never served on the public `current` route.
    """


def _content_type_for(recipe: str, variant: str) -> str:
    """Return the media type a stored rendering is served as.

    Read from the manifest the object was rendered with, falling back to PNG. The
    fallback is the safe direction: a JPEG mislabelled as PNG still displays in
    every browser and wallet, while a PNG with an alpha channel labelled as JPEG
    loses its transparency in some of them.
    """
    known = MANIFESTS.get(recipe)
    if known is not None:
        for candidate in known.variants:
            if candidate.name == variant:
                return candidate.content_type
    return "image/png"
