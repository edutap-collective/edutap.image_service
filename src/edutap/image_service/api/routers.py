"""The routes, which are a thin unwrapping over `PhotoService`.

Nothing decides anything here. A router reads a request, calls one use case and
maps the refusal it gets back onto a status code — the refusals themselves live in
the state machine, where they can be tested without a server.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..clients.image_api import ImageApiUnavailable
from ..ingest import FileTooLarge, ImageTooLarge, UnsupportedFormat
from ..service import NoFaceToCrop, NotDeliverable, VersionNotFound
from ..states import EvidenceKind, TransitionRefused, UnderLegalHold
from .auth import require_service_token

router = APIRouter()
public = APIRouter()

Caller = Annotated[str, Depends(require_service_token)]

#: The accepted upload formats as media types. `ingest.ACCEPTED_FORMATS` holds
#: Pillow's format names, which are the right thing for a decoder and the wrong
#: thing for a browser's `accept` attribute. MPO is a multi-picture JPEG and has no
#: media type of its own.
ACCEPTED_MEDIA_TYPES = ["image/jpeg", "image/png", "image/heic", "image/heif"]


class ServiceLimits(BaseModel):
    """What this service accepts, so a front end can say so before uploading."""

    max_file_bytes: int
    max_image_edge: int
    accepted_formats: list[str]


class SubmissionAccepted(BaseModel):
    """What a front end tells the person who just uploaded.

    `state` is `draft` and not `pending`: nobody has been asked to look at this yet,
    and a front end saying "in review" here would be lying by one step.
    """

    version: str
    state: str = "draft"
    passed: bool
    warnings: list[str] = []


class ExpiryRequest(BaseModel):
    """What a retention run is told to do.

    `older_than_days` may be omitted, and then the deployment's configured default
    applies. It is not a constant here: the number is the operator's retention
    policy, and one living in this package would be one institution's rule baked
    into a standard.
    """

    state: str = "rejected"
    older_than_days: int | None = None


class ExpiryReport(BaseModel):
    """What the run did, and what it deliberately left alone."""

    purged: list[dict[str, Any]]
    skipped_legal_hold: list[dict[str, Any]]


class Notification(BaseModel):
    """What whoever sent the message reports back."""

    when: datetime | None = None


class Hold(BaseModel):
    """A reviewer placing or lifting a legal hold."""

    actor: str
    reason: str | None = None


class Reset(BaseModel):
    """A reviewer taking the active photograph off the card."""

    actor: str


class Confirmation(BaseModel):
    """What a person sends to stand behind the candidate they looked at.

    The declaration reference is opaque here. A deployment that versions its
    rights-declaration text in a repository sends the tag and the commit hash; one
    that does not sends neither.
    """

    actor: str
    rights_declared: bool = False
    declaration_tag: str | None = None
    declaration_sha: str | None = None


class Decision(BaseModel):
    """A reviewer's decision, as a front end passes it on."""

    actor: str
    evidence_kind: EvidenceKind | None = None
    reason: str | None = None


@router.post("/persons/{person_uid}/photos", status_code=status.HTTP_201_CREATED)
async def submit(
    request: Request,
    person_uid: str,
    caller: Caller,
    file: Annotated[bytes, File()],
) -> SubmissionAccepted:
    """Accept an upload and keep it as a candidate.

    Neither an actor nor a rights declaration: both belong to the confirming call,
    which is where the person actually stands behind the image.
    """
    async with request.app.state.unit_of_work() as (session, service):
        try:
            result = await service.submit(person_uid=person_uid, upload=file)
        except (FileTooLarge, ImageTooLarge, UnsupportedFormat) as exc:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
        except NoFaceToCrop as exc:
            # 422 rather than 400: the request was well formed and the file was a
            # readable image. What failed is the picture, and the person needs the
            # report to know which check to act on.
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "detail": "no face-centred crop could be produced",
                    "warnings": exc.report.warnings,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except ImageApiUnavailable as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "image analysis is unavailable"
            ) from exc
        await session.commit()
        return SubmissionAccepted(
            version=result.version,
            passed=result.report.passed,
            warnings=result.report.warnings,
        )


@router.get("/persons/{person_uid}/photos")
async def list_versions(request: Request, person_uid: str, caller: Caller) -> list[dict[str, Any]]:
    """Every version of one person, for a review client."""
    async with request.app.state.unit_of_work() as (_, service):
        return await service.list_versions(person_uid)


@router.post("/persons/{person_uid}/photos/{version}/confirm")
async def confirm_version(
    request: Request, person_uid: str, version: str, body: Confirmation, caller: Caller
) -> Response:
    """Turn a candidate into a submission.

    The version travels in the path although a person has at most one candidate:
    they confirm what they *saw*. A call without it would confirm whatever is
    current, which stops being the same thing the moment a second upload happens.
    """
    async with request.app.state.unit_of_work() as (session, service):
        try:
            await _guarded(
                service.confirm(
                    person_uid=person_uid,
                    version=version,
                    actor=body.actor,
                    rights_declared=body.rights_declared,
                    declaration_tag=body.declaration_tag,
                    declaration_sha=body.declaration_sha,
                )
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/persons/{person_uid}/photos/{version}/approve")
async def approve_version(
    request: Request, person_uid: str, version: str, decision: Decision, caller: Caller
) -> Response:
    """Activate a version. Requires the evidence, which is never defaulted."""
    if decision.evidence_kind is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "approval requires an evidence kind")
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(
            service.approve(
                person_uid=person_uid,
                version=version,
                evidence_kind=decision.evidence_kind,
                actor=decision.actor,
            )
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/persons/{person_uid}/photos/{version}/reject")
async def reject_version(
    request: Request, person_uid: str, version: str, decision: Decision, caller: Caller
) -> Response:
    """Refuse a version. The reason travels into the trail and into the person's mail."""
    if not decision.reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a rejection needs a reason")
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(
            service.reject(
                person_uid=person_uid,
                version=version,
                actor=decision.actor,
                reason=decision.reason,
            )
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/persons/{person_uid}/photos/{version}/reactivate")
async def reactivate_version(
    request: Request, person_uid: str, version: str, decision: Decision, caller: Caller
) -> Response:
    """Switch back to a version the person kept."""
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(
            service.reactivate(
                person_uid=person_uid,
                version=version,
                actor=decision.actor,
                now=datetime.now(tz=UTC),
            )
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/persons/{person_uid}/photos/{version}")
async def purge_version(
    request: Request, person_uid: str, version: str, actor: str, caller: Caller
) -> Response:
    """Clear the bytes of a version the person no longer wants."""
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(service.purge(person_uid=person_uid, version=version, actor=actor))
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/persons/{person_uid}/photos/{version}/{recipe}/{variant}")
async def deliver_version(
    request: Request, person_uid: str, version: str, recipe: str, variant: str, caller: Caller
) -> Response:
    """Serve one version to a review client. This is where `pending` is visible."""
    async with request.app.state.unit_of_work() as (_, service):
        try:
            delivered = await service.deliver_version(
                person_uid=person_uid, version=version, recipe=recipe, variant=variant
            )
        except NotDeliverable as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except VersionNotFound as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return Response(content=delivered.data, media_type=delivered.content_type)


@router.post("/persons/{person_uid}/photos/{version}/notified")
async def mark_notified(
    request: Request, person_uid: str, version: str, body: Notification, caller: Caller
) -> Response:
    """Report that the person was told about a decision.

    The feedback loop that starts the retention clock. This service refuses a
    photograph; it does not send the mail, so it cannot know when the person heard
    -- and a clock started at the rejection would run against the wrong moment.
    """
    when = body.when or datetime.now(tz=UTC)
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(service.mark_notified(person_uid=person_uid, version=version, when=when))
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/persons/{person_uid}/photos/{version}/hold")
async def set_hold(
    request: Request, person_uid: str, version: str, body: Hold, caller: Caller
) -> Response:
    """Place a legal hold. Every deletion path skips the version from here on."""
    if not body.reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a hold needs a reason")
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(
            service.set_hold(
                person_uid=person_uid, version=version, actor=body.actor, reason=body.reason
            )
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/persons/{person_uid}/photos/{version}/hold")
async def release_hold(
    request: Request, person_uid: str, version: str, actor: str, caller: Caller
) -> Response:
    """Lift a legal hold. A narrower right than placing one."""
    async with request.app.state.unit_of_work() as (session, service):
        await _guarded(service.release_hold(person_uid=person_uid, version=version, actor=actor))
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/persons/{person_uid}/photos/reset")
async def reset_to_placeholder(
    request: Request, person_uid: str, body: Reset, caller: Caller
) -> Response:
    """Take the active photograph off the card, back to the placeholder.

    `204` when something was withdrawn, `404` when there was nothing on the card --
    so a caller can tell the two apart without a second query.
    """
    async with request.app.state.unit_of_work() as (session, service):
        withdrew = await _guarded(
            service.reset_to_placeholder(person_uid=person_uid, actor=body.actor)
        )
        await session.commit()
    if not withdrew:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no active photograph to withdraw")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/maintenance/expire")
async def expire(request: Request, body: ExpiryRequest, caller: Caller) -> ExpiryReport:
    """Clear what has been due long enough. Externally driven, on purpose.

    This service has no clock. Whoever operates it decides when this runs and how
    long the deadline is; a scheduler in here would be this package deciding an
    operator's policy.

    Idempotent, so it may be called as often as anyone likes, and the answer is the
    record of what happened -- including what a legal hold kept back, which is the
    half a count could not report.
    """
    settings = request.app.state.settings
    days = (
        body.older_than_days if body.older_than_days is not None else settings.default_expiry_days
    )
    async with request.app.state.unit_of_work() as (session, service):
        try:
            result = await service.expire(
                state=body.state,
                older_than=timedelta(days=days),
                now=datetime.now(tz=UTC),
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
    return ExpiryReport(purged=result.purged, skipped_legal_hold=result.skipped_legal_hold)


@public.get("/limits")
async def limits(request: Request) -> ServiceLimits:
    """Report the acceptance limits this deployment enforces.

    Tokenless, like the `current` route and for a related reason: the caller is a
    browser about to upload and holds no service token. Nothing in the answer is
    about a person -- it is a property of the deployment, and anyone could discover
    the same numbers by uploading something too large.

    Read from the very `Limits` object the ingest check uses. A second copy of these
    numbers anywhere drifts, and then a front end refuses what this service would
    have taken.
    """
    enforced = request.app.state.limits
    return ServiceLimits(
        max_file_bytes=enforced.max_bytes,
        max_image_edge=enforced.max_edge,
        accepted_formats=ACCEPTED_MEDIA_TYPES,
    )


@public.get("/persons/{person_uid}/photo/current/{recipe}/{variant}")
async def deliver_current(request: Request, person_uid: str, recipe: str, variant: str) -> Response:
    """Serve the active version, or the placeholder.

    The only route without a token. A wallet provider bakes this URL into an issued
    pass and fetches it without credentials, long after issuance — which is also why
    it answers with a placeholder rather than 404 for a person who has never
    uploaded anything. A 404 here would be a broken image on a card.
    """
    async with request.app.state.unit_of_work() as (_, service):
        delivered = await service.deliver_current(
            person_uid=person_uid, recipe=recipe, variant=variant
        )
    return Response(
        content=delivered.data,
        media_type=delivered.content_type,
        headers={
            # Short, because a withdrawal has to reach the card. Not zero, because
            # a wallet provider refetches often enough for it to matter.
            "Cache-Control": "public, max-age=300",
            "X-Photo-Placeholder": "true" if delivered.is_placeholder else "false",
        },
    )


async def _guarded(awaitable: Any) -> Any:
    """Map a refusal from the state machine onto the status it deserves.

    A legal hold gets its own code. `409 Conflict` would be true of both, but a
    front end shows a person "replace it instead" for one and a reviewer "this is
    evidence in a proceeding" for the other, and it should not have to parse a
    message to tell them apart.
    """
    try:
        return await awaitable
    except UnderLegalHold as exc:
        raise HTTPException(status.HTTP_423_LOCKED, str(exc)) from exc
    except TransitionRefused as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except VersionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
