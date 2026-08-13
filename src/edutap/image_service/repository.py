"""Database access for the photo tables and the reference other services read.

**Nothing here commits.** The caller owns the transaction, and that is the whole
mechanism behind the design's central promise: `public.photo` and the reference in
`public.person_view.photo` move together or not at all. A repository that committed
per method would turn one promise into two writes with a window between them, and
the window is exactly where a reference to a version that does not exist comes from.

The photo tables are the truth; `person_view` is a projection maintained for
readers who want one row rather than a join — a data provider, a pass builder, and
at some deployments a vendor connector reading SQL directly.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from edutap.db_definitions.public import metadata
from sqlalchemy.ext.asyncio import AsyncSession

from .reference import PHOTO_ASSURANCE, build_reference, placeholder
from .states import EvidenceKind, Outcome, PhotoState

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]


class PhotoRepository:
    """The two photo tables plus the reference, over one session."""

    def __init__(self, session: AsyncSession, *, origin: str) -> None:
        """Bind to a session the caller commits, and to the externally visible origin."""
        self._session = session
        self._origin = origin

    async def add_draft(
        self,
        *,
        person_uid: str,
        version: str,
        sha256: str,
        recipe: str,
        details: dict[str, Any],
    ) -> None:
        """Record an uploaded candidate.

        No review entry and no actor: the trail is the register of claims, and a
        candidate claims nothing until its owner confirms it. The submission entry
        is written by :meth:`apply` on that confirmation, where the rights
        declaration finally exists.

        `details` -- the validation report and any rights claims found in the file
        -- waits on the row rather than in memory, because the request that
        produced it and the request that records it are different requests.
        """
        await self._session.execute(
            sa.insert(PHOTO).values(
                person_uid=person_uid,
                version=version,
                state=PhotoState.DRAFT,
                sha256=sha256,
                recipe=recipe,
                draft_details=details,
            )
        )

    async def discard_draft(self, person_uid: str) -> str | None:
        """Remove this person's candidate row, if there is one.

        Returns the version so the caller can clear its objects: the row and the
        objects are removed by two different collaborators, and this is the one
        that knows the version.
        """
        return await self._session.scalar(
            sa.delete(PHOTO)
            .where(PHOTO.c.person_uid == person_uid, PHOTO.c.state == PhotoState.DRAFT)
            .returning(PHOTO.c.version)
        )

    async def apply(
        self,
        *,
        person_uid: str,
        version: str,
        outcome: Outcome,
        actor: str,
        action: str,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Carry out a decision the state machine already permitted.

        The order matters and is not incidental: the previously active version is
        demoted **before** this one is activated. The other way round the partial
        unique index sees two active rows for the length of one statement and
        refuses the write — the constraint is checked per statement, not per
        transaction.
        """
        if outcome.supersede_active:
            await self._session.execute(
                sa.update(PHOTO)
                .where(
                    PHOTO.c.person_uid == person_uid,
                    PHOTO.c.state == PhotoState.ACTIVE,
                    PHOTO.c.version != version,
                )
                .values(state=PhotoState.SUPERSEDED, updated_at=_now())
            )

        values: dict[str, Any] = {"state": outcome.new_state, "updated_at": _now()}
        if outcome.evidence_kind is not None:
            values["evidence_kind"] = outcome.evidence_kind
            values["photo_assurance"] = PHOTO_ASSURANCE[outcome.evidence_kind]
        elif outcome.new_state is PhotoState.PENDING:
            # A reactivation whose review aged out returns to the queue, and the
            # evidence that expired with it must not travel along.
            values["evidence_kind"] = None
            values["photo_assurance"] = None

        if action == "submit":
            # The declaration is made at confirmation, not at upload: a candidate
            # its owner discards never carried one. The verdict that waited on the
            # row moves into the trail with this entry and is cleared here -- one
            # report in two places is how the two come apart.
            values["rights_declared_at"] = _now()
            values["draft_details"] = None

        sha256 = await self._session.scalar(
            sa.update(PHOTO)
            .where(PHOTO.c.person_uid == person_uid, PHOTO.c.version == version)
            .values(**values)
            .returning(PHOTO.c.sha256)
        )
        if sha256 is None:
            raise LookupError(f"no version {version!r} for {person_uid!r}")

        await self._append_review(
            person_uid=person_uid,
            version=version,
            action=action,
            actor=actor,
            sha256=sha256,
            evidence_kind=outcome.evidence_kind,
            reason=reason,
            details=details or {},
        )
        await self._refresh_reference(person_uid)

    async def mark_purged(
        self,
        *,
        person_uid: str,
        version: str,
        actor: str,
        objects_deleted: int,
        action: str = "purge",
    ) -> None:
        """Record that the bytes are gone while the row and its trail stay.

        `objects_deleted` goes into the trail rather than being discarded: "purged,
        6 objects" is auditable, "purged" is not.

        `action` separates the two ways bytes go: `purge` is somebody deciding,
        `expire` is a deadline passing. The row looks identical afterwards, and
        only the trail can say which of the two happened.
        """
        sha256 = await self._session.scalar(
            sa.update(PHOTO)
            .where(PHOTO.c.person_uid == person_uid, PHOTO.c.version == version)
            .values(purged_at=_now(), updated_at=_now())
            .returning(PHOTO.c.sha256)
        )
        if sha256 is None:
            raise LookupError(f"no version {version!r} for {person_uid!r}")
        await self._append_review(
            person_uid=person_uid,
            version=version,
            action=action,
            actor=actor,
            sha256=sha256,
            details={"objects_deleted": objects_deleted},
        )

    async def set_legal_hold(
        self, *, person_uid: str, version: str, actor: str, reason: str
    ) -> None:
        """Place a hold. Every deletion path but the person's own then skips this row."""
        await self._hold(
            person_uid=person_uid,
            version=version,
            actor=actor,
            reason=reason,
            since=_now(),
            action="hold_set",
        )

    async def release_legal_hold(self, *, person_uid: str, version: str, actor: str) -> None:
        """Lift a hold. A narrower right than placing one, enforced by the caller."""
        await self._hold(
            person_uid=person_uid,
            version=version,
            actor=actor,
            reason=None,
            since=None,
            action="hold_release",
        )

    async def due_for_expiry(
        self, *, older_than: timedelta, now: datetime, state: str = PhotoState.REJECTED
    ) -> list[dict[str, Any]]:
        """Versions the retention run may clear.

        Three conditions, and each of them is a decision rather than a filter: the
        clock runs from `notified_at`, so a rejection nobody was told about never
        expires; a held version is never offered; and one already purged is not
        offered twice.
        """
        result = await self._session.execute(
            sa.select(PHOTO.c.person_uid, PHOTO.c.version, PHOTO.c.sha256).where(
                PHOTO.c.state == state,
                PHOTO.c.legal_hold_since.is_(None),
                PHOTO.c.purged_at.is_(None),
                PHOTO.c.notified_at.is_not(None),
                PHOTO.c.notified_at < now - older_than,
            )
        )
        return [dict(row) for row in result.mappings()]

    async def stale_drafts(self, *, older_than: timedelta, now: datetime) -> list[dict[str, Any]]:
        """Candidates nobody came back to confirm.

        Their clock runs from `created_at`, not from `notified_at` as a rejection's
        does: nobody is told about a candidate, so there is no notification to start
        it. What this catches is the person who uploaded and closed the tab -- the
        "at most one per person" rule covers the person who returns, and only this
        covers the one who does not.

        A hold is respected here as everywhere: it defeats every deletion path.
        """
        result = await self._session.execute(
            sa.select(PHOTO.c.person_uid, PHOTO.c.version, PHOTO.c.sha256).where(
                PHOTO.c.state == PhotoState.DRAFT,
                PHOTO.c.legal_hold_since.is_(None),
                PHOTO.c.created_at < now - older_than,
            )
        )
        return [dict(row) for row in result.mappings()]

    async def held_and_due(
        self,
        *,
        older_than: timedelta,
        now: datetime,
        state: str = PhotoState.REJECTED,
    ) -> list[dict[str, Any]]:
        """Report what a hold alone keeps back, so the run can name it.

        Not a mirror of :meth:`due_for_expiry` with one condition flipped for
        symmetry's sake: an operator reading a deploy log has to be able to tell
        "nothing was due" from "something was due and is evidence in a proceeding".
        """
        clock = PHOTO.c.created_at if state == PhotoState.DRAFT else PHOTO.c.notified_at
        result = await self._session.execute(
            sa.select(PHOTO.c.person_uid, PHOTO.c.version, PHOTO.c.sha256).where(
                PHOTO.c.state == state,
                PHOTO.c.legal_hold_since.is_not(None),
                PHOTO.c.purged_at.is_(None),
                clock.is_not(None),
                clock < now - older_than,
            )
        )
        return [dict(row) for row in result.mappings()]

    async def last_approval_at(self, person_uid: str, version: str) -> datetime | None:
        """When this version was last approved, or nothing if it never was.

        Read from the trail rather than kept as a column: the trail is the record,
        and a second copy on the row would be one more thing that can disagree with
        it. The reactivation rule asks how old an approval is, and this is where the
        answer lives.
        """
        return await self._session.scalar(
            sa.select(sa.func.max(REVIEW.c.occurred_at)).where(
                REVIEW.c.person_uid == person_uid,
                REVIEW.c.version == version,
                REVIEW.c.action == "approve",
            )
        )

    async def active_for(self, person_uid: str) -> dict[str, Any] | None:
        """Return the one active version, or nothing."""
        result = await self._session.execute(
            sa.select(PHOTO).where(
                PHOTO.c.person_uid == person_uid, PHOTO.c.state == PhotoState.ACTIVE
            )
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def get(self, person_uid: str, version: str) -> dict[str, Any] | None:
        """One version by key."""
        result = await self._session.execute(
            sa.select(PHOTO).where(PHOTO.c.person_uid == person_uid, PHOTO.c.version == version)
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def list_for(self, person_uid: str) -> list[dict[str, Any]]:
        """Every version of one person, newest first."""
        result = await self._session.execute(
            sa.select(PHOTO)
            .where(PHOTO.c.person_uid == person_uid)
            .order_by(PHOTO.c.created_at.desc())
        )
        return [dict(row) for row in result.mappings()]

    async def delete_person(self, person_uid: str) -> list[str]:
        """Remove every version of a person, held or not, and the trail with them.

        The one deletion path that ignores a legal hold. The review entries go by
        cascade rather than by a second statement — one less thing to forget, and no
        window in which the trail outlives its rows.
        """
        result = await self._session.execute(
            sa.delete(PHOTO).where(PHOTO.c.person_uid == person_uid).returning(PHOTO.c.version)
        )
        versions = list(result.scalars())
        await self._session.execute(
            sa.update(PERSON_VIEW)
            .where(PERSON_VIEW.c.person_uid == person_uid)
            .values(photo=sa.null())
        )
        return versions

    async def _hold(
        self,
        *,
        person_uid: str,
        version: str,
        actor: str,
        reason: str | None,
        since: datetime | None,
        action: str,
    ) -> None:
        sha256 = await self._session.scalar(
            sa.update(PHOTO)
            .where(PHOTO.c.person_uid == person_uid, PHOTO.c.version == version)
            .values(
                legal_hold_since=since,
                legal_hold_by=actor if since else None,
                legal_hold_reason=reason,
                updated_at=_now(),
            )
            .returning(PHOTO.c.sha256)
        )
        if sha256 is None:
            raise LookupError(f"no version {version!r} for {person_uid!r}")
        await self._append_review(
            person_uid=person_uid,
            version=version,
            action=action,
            actor=actor,
            sha256=sha256,
            reason=reason,
            details={},
        )

    async def _append_review(
        self,
        *,
        person_uid: str,
        version: str,
        action: str,
        actor: str,
        sha256: str,
        evidence_kind: EvidenceKind | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        await self._session.execute(
            sa.insert(REVIEW).values(
                review_id=uuid4(),
                person_uid=person_uid,
                version=version,
                occurred_at=_now(),
                actor=actor,
                action=action,
                evidence_kind=evidence_kind,
                reason=reason,
                sha256=sha256,
                details=details or {},
            )
        )

    async def _refresh_reference(self, person_uid: str) -> None:
        """Project the active version into every view row of the person.

        Every row, not one: `person_view` is keyed by `(person_uid, view_type)`, so a
        person legitimately has several — a full view and a speaking slice. Writing
        one of them would leave the others reporting a placeholder for somebody who
        has a photograph.

        Updating zero rows is not an error. A photograph can arrive before the person
        spooler has written anything, and failing here would make an upload depend on
        an unrelated pipeline.
        """
        active = await self.active_for(person_uid)
        if active is None:
            reference = placeholder(origin=self._origin, person_uid=person_uid)
        else:
            reference = build_reference(
                origin=self._origin,
                person_uid=person_uid,
                version=active["version"],
                evidence_kind=EvidenceKind(active["evidence_kind"]),
                sha256=active["sha256"],
            )
        await self._session.execute(
            sa.update(PERSON_VIEW)
            .where(PERSON_VIEW.c.person_uid == person_uid)
            .values(photo=reference)
        )


def _now() -> datetime:
    """Timezone-aware now. Naive would be read against the writer's server time zone."""
    return datetime.now(tz=UTC)
