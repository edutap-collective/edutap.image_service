"""The routes, which are a thin unwrapping over `PhotoService`.

Nothing decides anything here. A router reads a request, calls one use case and
maps the refusal it gets back onto a status code — the refusals themselves live in
the state machine, where they can be tested without a server.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..clients.image_api import ImageApiUnavailable
from ..ingest import FileTooLarge, ImageTooLarge, UnsupportedFormat
from ..service import NoFaceToCrop, NotDeliverable, VersionNotFound
from ..states import EvidenceKind, TransitionRefused, UnderLegalHold
from .auth import require_service_token

router = APIRouter()
public = APIRouter()

Caller = Annotated[str, Depends(require_service_token)]


class SubmissionAccepted(BaseModel):
    """What a front end tells the person who just uploaded."""

    version: str
    state: str = "pending"
    passed: bool
    warnings: list[str] = []


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
    actor: Annotated[str, Form()],
    rights_declared: Annotated[bool, Form()],
) -> SubmissionAccepted:
    """Accept an upload and queue it for review."""
    async with request.app.state.unit_of_work() as (session, service):
        try:
            result = await service.submit(
                person_uid=person_uid,
                upload=file,
                actor=actor,
                rights_declared=rights_declared,
            )
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
