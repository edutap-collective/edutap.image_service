# Plan: a candidate state, a declaration reference, and an answer about limits

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to work through this plan task by
> task. Steps use checkbox (`- [ ]`) syntax for tracking.

Date: 2026-08-13
Design: [`../specs/2026-08-11-person-photo-service-design.md`](../specs/2026-08-11-person-photo-service-design.md)
Consumer this unblocks: `lmu_edutap_backend`, plan
`docs/superpowers/plans/2026-08-13-lichtbild-selbstverwaltung.md`

**Goal:** Let a person see what was made of their upload before it reaches a
reviewer, record which version of a rights-declaration text they agreed to, and
tell a front end what this service accepts.

**Architecture:** Three changes, three pull requests. The first adds a state
before `pending` and therefore touches the state machine, where every invariant
of this service lives; it goes alone. The second adds two fields to the
transition the first introduced. The third is a route over limits that already
exist.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy Core over
`edutap.db_definitions`, pytest against a real PostgreSQL via `testcontainers`.

## Why a candidate state at all

Today an upload lands in `pending` and is queued for review immediately. The
person never sees what the service made of their photograph — the crop is
face-centred and produced by `edutap.image_api` — and they find out what it looks
like once it is on a card.

A candidate closes that gap without giving anybody a crop control: upload, look,
confirm. It also moves the rights declaration to the moment somebody commits, so
a discarded candidate never carried one.

## Global constraints

They apply implicitly to every task:

- **English only** — code, comments, docstrings, commit messages, PR text.
- **Test-first.** The state machine has no I/O precisely so its rules can be
  pinned before anything calls them.
- **This service knows nothing about passes, institutions or roles.** A
  declaration reference is an opaque pair of strings here; what the text says and
  who wrote it is the deployment's business.
- **It authenticates services, not people.** No task adds a notion of who a
  caller is beyond the configured token name.
- **No activation without named evidence.** Nothing here creates a path to
  `active`.
- **Tests follow the existing files.** Integration tests carry
  `pytestmark = pytest.mark.integration` and take the `session` fixture; they are
  plain `async def` without an extra marker.
- **`make lint` and `make test-local` green before every commit;**
  `make test-integration` before every pull request. CI calls the same targets.
- **Branch first, never commit on `main`. Push only when asked.**

## Prerequisite: `edutap.db_definitions` 0.2.1

`public.photo` is declared there, not here. Two additions, one release, and it
must land **before** task 2.

**The candidate is unique per person**, held by the database for the same reason
"at most one active version" already is. Next to the existing index in
`Photo.__table_args__`:

```python
        # At most one candidate per person. The service clears the previous one
        # before inserting, but a person with two tabs open is a race, and the
        # same argument that put `uq_photo_one_active_per_person` here applies.
        sa.Index(
            "uq_photo_one_draft_per_person",
            "person_uid",
            unique=True,
            postgresql_where=sa.text("state = 'draft'"),
        ),
```

**The validation report needs somewhere to wait.** It is produced at upload and
belongs in the review entry, which is written at confirmation. A new column on
`Photo`:

```python
    draft_details: dict[str, Any] | None = Field(
        default=None,
        sa_column=sa.Column(JSONB, nullable=True),
        description=(
            "The validation report and any rights claims found in the upload, "
            "held only while the version is a candidate. Moved into the review "
            "entry on confirmation and cleared there: the trail is where a "
            "verdict belongs once somebody has stood behind the image."
        ),
    )
```

The alternative — re-running `validate_and_crop` at confirmation — costs a second
call to `edutap.image_api` per upload and can produce a verdict different from the
one the person was shown. A verdict recorded at the moment it was produced is the
one the trail should carry.

> [!IMPORTANT]
> How these reach a running database is unresolved. `edutap.db_definitions` is not
> registered in `lmu_db_migrate` at all — `registry.UNITS` holds only
> `lmu_edutap_full_view` — and a `SqlModelUnit` runs `metadata.create_all()`,
> which creates missing tables and does **not** add a column or an index to a
> table that already exists. On a deployment that already has `public.photo`,
> both have to be applied by hand or by a real migration. Raise this with whoever
> owns the deployment before task 2, not after.

## File structure

| File | Change |
| --- | --- |
| `src/edutap/image_service/states.py` | `PhotoState.DRAFT`, `confirm()` |
| `src/edutap/image_service/repository.py` | `add_draft()` replaces `add_pending()`; `discard_draft()` |
| `src/edutap/image_service/service.py` | `submit()` keeps a candidate; new `confirm()` use case |
| `src/edutap/image_service/api/routers.py` | `POST …/confirm`; `submit` reports `draft`; `GET /limits` |
| `src/edutap/image_service/api/app.py` | `app.state.limits` |
| `tests/test_states.py` | the new rules |
| `tests/test_repository.py` | the new rows |
| `tests/test_service_submit.py` | the changed use case |
| `tests/test_service_confirm.py` | the new use case |
| `tests/test_api.py` | the routes |

---

## Pull request A — the candidate state

### Task 1: `draft` in the state machine

**Files:**

- Modify: `src/edutap/image_service/states.py`
- Test: `tests/test_states.py`

**Interfaces:**

- Produces: `PhotoState.DRAFT`; `confirm(current: PhotoState) -> Outcome`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_states.py`, and add `confirm` to the import block at its
top:

```python
def test_confirming_a_candidate_queues_it_for_review():
    outcome = confirm(PhotoState.DRAFT)
    assert outcome.new_state is PhotoState.PENDING
    assert outcome.supersede_active is False
    assert outcome.evidence_kind is None


@pytest.mark.parametrize(
    "state",
    [PhotoState.PENDING, PhotoState.ACTIVE, PhotoState.REJECTED, PhotoState.SUPERSEDED],
)
def test_only_a_candidate_can_be_confirmed(state):
    with pytest.raises(IllegalTransition):
        confirm(state)


def test_a_candidate_cannot_be_approved():
    """A reviewer never sees a candidate; reaching them is what confirming is for."""
    with pytest.raises(IllegalTransition):
        approve(PhotoState.DRAFT, evidence_kind=EvidenceKind.SUPPORT_VISUAL)


def test_a_candidate_cannot_be_rejected():
    with pytest.raises(IllegalTransition):
        reject(PhotoState.DRAFT)


def test_a_candidate_cannot_be_reactivated():
    with pytest.raises(IllegalTransition):
        reactivate(
            PhotoState.DRAFT,
            reviewed_at=NOW,
            now=NOW,
            max_age=SIX_MONTHS,
            evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        )


def test_a_candidate_may_be_purged():
    """Discarding what one just uploaded must not need a reviewer."""
    assert purge(PhotoState.DRAFT, legal_hold_since=None) is None


def test_a_held_candidate_is_not_purgeable_either():
    """A hold can strike any state -- a reviewer may recognise a stranger's face."""
    with pytest.raises(UnderLegalHold):
        purge(PhotoState.DRAFT, legal_hold_since=NOW)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-local`
Expected: FAIL with `ImportError: cannot import name 'confirm'`

- [ ] **Step 3: Add the state and the transition**

In `src/edutap/image_service/states.py`, extend the enum with `DRAFT` as its
first member:

```python
class PhotoState(StrEnum):
    """Where a version stands.

    Stored as text rather than as a native enum, so a new state is not a migration.
    """

    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
```

and add the transition next to the others:

```python
def confirm(current: PhotoState) -> Outcome:
    """Turn a candidate the person has looked at into a submission.

    The only way out of `draft` other than deletion, and it leads to `pending` and
    nowhere else. A candidate is not a weaker submission: nobody has seen it, no
    review entry mentions it, and it carries no declaration until this step. That
    is why `approve` refuses it -- approving something its owner never stood behind
    would record a decision about an image nobody submitted.
    """
    if current is not PhotoState.DRAFT:
        raise IllegalTransition(f"a {current} version cannot be confirmed")
    return Outcome(PhotoState.PENDING)
```

`approve`, `reject` and `reactivate` need no change: each already lists the states
it accepts and `draft` is in none of them. `purge` needs none either — it refuses
only `active` and a held version, and a candidate is neither.

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-local`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/edutap/image_service/states.py tests/test_states.py
git commit -m "feat(states): a candidate state before pending

An upload becomes a draft its owner has to confirm. Until then nobody has
seen it, no review entry mentions it, and it carries no rights
declaration -- which is why approve() refuses it outright."
```

### Task 2: the repository writes candidates

Needs `edutap.db_definitions` 0.2.1 installed — see the prerequisite.

**Files:**

- Modify: `src/edutap/image_service/repository.py`
- Test: `tests/test_repository.py`

**Interfaces:**

- Consumes: `PhotoState.DRAFT`, `confirm()` from task 1.
- Produces: `add_draft(*, person_uid, version, sha256, recipe, details) -> None`;
  `discard_draft(person_uid) -> str | None`
- Removes: `add_pending()` — nothing keeps it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repository.py`, adding `confirm` to its import from
`edutap.image_service.states`:

```python
async def test_a_candidate_writes_no_review_entry(session):
    """The trail is the register of claims, and a candidate claims nothing."""
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={"v": 1}
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.DRAFT
    assert row["draft_details"] == {"v": 1}
    assert row["rights_declared_at"] is None
    count = (await session.execute(sa.select(sa.func.count()).select_from(REVIEW))).scalar()
    assert count == 0


async def test_two_candidates_for_one_person_are_refused_by_the_database(session):
    """The service clears the old one first; this is what happens when it cannot."""
    await repo(session).add_draft(
        person_uid=UID, version="one", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    await repo(session).add_draft(
        person_uid=UID, version="two", sha256="b" * 64, recipe="default", details={}
    )
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_discarding_returns_the_version_it_removed(session):
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    removed = await repo(session).discard_draft(UID)
    await session.commit()

    assert removed == "cand"
    assert await repo(session).discard_draft(UID) is None


async def test_confirming_moves_the_candidate_to_pending_and_dates_the_declaration(session):
    await _add_person_view(session)
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()

    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=confirm(PhotoState.DRAFT),
        actor="user:ab12cde@lmu.de",
        action="submit",
        details={"declaration": {"tag": "v1.0", "sha": "a" * 40}},
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.PENDING
    assert row["rights_declared_at"] is not None
    assert row["draft_details"] is None

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["action"] == "submit"
    assert entry["details"]["declaration"]["tag"] == "v1.0"


async def test_a_pending_version_is_not_dated_twice(session):
    """`rights_declared_at` records one moment. A later transition must not move it."""
    await _add_person_view(session)
    await repo(session).add_draft(
        person_uid=UID, version="cand", sha256="a" * 64, recipe="default", details={}
    )
    await session.commit()
    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=confirm(PhotoState.DRAFT),
        actor="self",
        action="submit",
    )
    await session.commit()
    declared_at = (await session.execute(sa.select(PHOTO.c.rights_declared_at))).scalar()

    await repo(session).apply(
        person_uid=UID,
        version="cand",
        outcome=approve(PhotoState.PENDING, evidence_kind=EvidenceKind.SUPPORT_VISUAL),
        actor="desk:someone",
        action="approve",
    )
    await session.commit()

    assert (await session.execute(sa.select(PHOTO.c.rights_declared_at))).scalar() == declared_at
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-integration`
Expected: FAIL with `AttributeError: 'PhotoRepository' object has no attribute 'add_draft'`

- [ ] **Step 3: Replace `add_pending` with `add_draft`**

In `src/edutap/image_service/repository.py`, replace `add_pending` entirely:

```python
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
        is written by `apply()` on that confirmation, where the rights declaration
        finally exists.

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
```

In `apply()`, just before the `sa.update(PHOTO)` that writes `values`:

```python
        if action == "submit":
            # The declaration is made at confirmation, not at upload: a candidate
            # somebody discards never carried one. The verdict moves into the
            # trail with this entry, so the column that held it is cleared -- two
            # places holding one report is how they come apart.
            values["rights_declared_at"] = _now()
            values["draft_details"] = None
```

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-integration`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/edutap/image_service/repository.py tests/test_repository.py
git commit -m "feat(repository): candidates as rows without a trail entry

add_draft replaces add_pending. rights_declared_at is set on the
confirming transition rather than on the insert, and the held validation
report moves into the trail there and is cleared from the row."
```

### Task 3: the upload use case keeps a candidate

**Files:**

- Modify: `src/edutap/image_service/service.py`
- Modify: `tests/test_service_submit.py`
- Create: `tests/test_service_confirm.py`

**Interfaces:**

- Consumes: `add_draft()`, `discard_draft()` from task 2.
- Produces: `PhotoService.submit(*, person_uid, upload) -> Submission` —
  **`actor` and `rights_declared` are gone from this signature**;
  `PhotoService.confirm(*, person_uid, version, actor, rights_declared) -> None`
- Changes: `PhotoService.purge()` removes a candidate's row instead of marking it
  purged. A kept row would still read `draft` and block the next upload.

- [ ] **Step 1: Adjust the existing tests and write the new ones**

In `tests/test_service_submit.py`, every `service.submit(...)` call loses its
`actor` and `rights_declared` arguments, and
`test_a_submission_without_the_rights_declaration_is_refused` moves to the new
file below — an upload no longer carries one. The assertions on `state ==
"pending"` become `"draft"`. Then append:

```python
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


async def test_the_validation_report_waits_on_the_candidate_row(session):
    """It is produced now and belongs in a trail entry written later."""
    service, _, _ = build(session)
    await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["draft_details"]["validation"]["passed"] is True
    assert "dimensions" in row["draft_details"]
```

And a new file `tests/test_service_confirm.py`:

```python
"""Confirming a candidate: the step that turns an upload into a submission.

Same shape as `test_service_submit.py` -- a real repository against PostgreSQL,
fakes for the object store and the image API -- because what is under test is what
ends up recorded.
"""

import pytest
import sqlalchemy as sa
from edutap.db_definitions.public import metadata

from edutap.image_service.service import VersionNotFound
from edutap.image_service.states import IllegalTransition, PhotoState

from .test_service_submit import UID, _png, build

pytestmark = pytest.mark.integration

PHOTO = metadata.tables["public.photo"]
REVIEW = metadata.tables["public.photo_review"]
PERSON_VIEW = metadata.tables["public.person_view"]


async def _person_view(session):
    await session.execute(
        sa.insert(PERSON_VIEW).values(person_uid=UID, view_type="full_view", data={})
    )


async def test_confirming_queues_the_candidate(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="user:ab12cde@lmu.de",
        rights_declared=True,
    )
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["state"] == PhotoState.PENDING


async def test_the_trail_entry_carries_the_verdict_and_the_actor(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="user:ab12cde@lmu.de",
        rights_declared=True,
    )
    await session.commit()

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["action"] == "submit"
    assert entry["actor"] == "user:ab12cde@lmu.de"
    assert entry["details"]["validation"]["passed"] is True


async def test_confirming_without_the_declaration_is_refused(session):
    """Not a weaker confirmation. None."""
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    with pytest.raises(ValueError):
        await service.confirm(
            person_uid=UID,
            version=result.version,
            actor="user:ab12cde@lmu.de",
            rights_declared=False,
        )


async def test_confirming_an_unknown_version_is_not_found(session):
    service, _, _ = build(session)

    with pytest.raises(VersionNotFound):
        await service.confirm(
            person_uid=UID, version="nope", actor="self", rights_declared=True
        )


async def test_discarding_a_candidate_removes_its_row_entirely(session):
    """Not `purged_at` on a row that stays -- the row would keep the state `draft`,
    and the partial unique index would then refuse the next upload. A candidate has
    no trail to keep the row readable for, which is exactly why it may just go."""
    service, store, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.purge(person_uid=UID, version=result.version, actor="self")
    await session.commit()

    count = (await session.execute(sa.select(sa.func.count()).select_from(PHOTO))).scalar()
    assert count == 0
    assert store.objects == {}


async def test_a_discarded_candidate_does_not_block_the_next_upload(session):
    service, _, _ = build(session)
    first = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.purge(person_uid=UID, version=first.version, actor="self")
    await session.commit()

    second = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    row = (await session.execute(sa.select(PHOTO))).mappings().one()
    assert row["version"] == second.version


async def test_confirming_twice_is_refused(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()
    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await session.commit()

    with pytest.raises(IllegalTransition):
        await service.confirm(
            person_uid=UID, version=result.version, actor="self", rights_declared=True
        )
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-integration`
Expected: FAIL — `submit()` still demands `actor` and `rights_declared`

- [ ] **Step 3: Change `submit` and add `confirm`**

In `src/edutap/image_service/service.py`, add `confirm` to the imports from
`.states`, then replace `submit`:

```python
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
```

In `purge`, a candidate takes a different exit. Find the point where the state
machine's `purge()` has permitted the deletion and the objects are cleared, and
branch before `mark_purged`:

```python
        if PhotoState(current["state"]) is PhotoState.DRAFT:
            # A candidate leaves no row behind. `mark_purged` keeps the row so the
            # trail stays readable after the bytes are gone -- but a candidate has
            # no trail, and a kept row would still read `draft` and make the
            # partial unique index refuse this person's next upload.
            await self._repository.discard_draft(person_uid)
            return
```

and add the new use case next to `approve`:

```python
    async def confirm(
        self, *, person_uid: str, version: str, actor: str, rights_declared: bool
    ) -> None:
        """Turn a candidate into a submission, carrying the rights declaration.

        `rights_declared` is not a courtesy flag. It is the declaration that
        carries the legal weight -- copyright metadata found in the file is
        recorded for a reviewer to read and never evaluated -- so a confirmation
        without it is refused rather than defaulted.
        """
        if not rights_declared:
            raise ValueError("a submission needs the uploader's rights declaration")
        current = await self._require(person_uid, version)
        outcome = confirm(PhotoState(current["state"]))
        await self._repository.apply(
            person_uid=person_uid,
            version=version,
            outcome=outcome,
            actor=actor,
            action="submit",
            details=current.get("draft_details") or {},
        )
```

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-integration && make lint`
Expected: PASS, lint green

- [ ] **Step 5: Commit**

```bash
git add src/edutap/image_service/service.py tests/
git commit -m "feat(service): upload keeps a candidate, confirmation submits it

The rights declaration moves from submit to confirm: a candidate the
person discards never carried one, and a declaration collected twice is
one nobody can point at."
```

### Task 4: the routes

**Files:**

- Modify: `src/edutap/image_service/api/routers.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: `PhotoService.submit()`, `PhotoService.confirm()` from task 3.
- Produces: `POST /persons/{person_uid}/photos` → `201`, `state: "draft"`;
  `POST /persons/{person_uid}/photos/{version}/confirm` → `204`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_an_upload_answers_with_a_candidate(client):
    response = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    )

    assert response.status_code == 201
    assert response.json()["state"] == "draft"


def test_confirming_queues_the_candidate(client):
    version = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    ).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "user:ab12cde@lmu.de", "rights_declared": True},
        headers=AUTH,
    )

    assert response.status_code == 204
    listed = client.get(f"/persons/{UID}/photos", headers=AUTH).json()
    assert [row["state"] for row in listed] == ["pending"]


def test_confirming_without_the_declaration_is_a_bad_request(client):
    version = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    ).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "user:ab12cde@lmu.de", "rights_declared": False},
        headers=AUTH,
    )

    assert response.status_code == 400


def test_confirming_the_wrong_version_is_a_conflict(client):
    """A person confirms what they saw. A stale version is not it."""
    client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    )
    second = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    ).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/not-{second}/confirm",
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
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-integration`
Expected: FAIL with `404` on the confirm route

- [ ] **Step 3: Change the upload route and add the confirming one**

In `src/edutap/image_service/api/routers.py`, adjust `SubmissionAccepted` and
`submit`:

```python
class SubmissionAccepted(BaseModel):
    """What a front end tells the person who just uploaded.

    `state` is `draft` and not `pending`: nobody has been asked to look at this
    yet, and a front end saying "in review" here would be lying by one step.
    """

    version: str
    state: str = "draft"
    passed: bool
    warnings: list[str] = []


@router.post("/persons/{person_uid}/photos", status_code=status.HTTP_201_CREATED)
async def submit(
    request: Request,
    person_uid: str,
    caller: Caller,
    file: Annotated[bytes, File()],
) -> SubmissionAccepted:
    """Accept an upload and keep it as a candidate."""
    async with request.app.state.unit_of_work() as (session, service):
        try:
            result = await service.submit(person_uid=person_uid, upload=file)
        except (FileTooLarge, ImageTooLarge, UnsupportedFormat) as exc:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
        except NoFaceToCrop as exc:
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
```

and add the confirming route next to `approve_version`:

```python
class Confirmation(BaseModel):
    """What a person sends to stand behind the candidate they looked at."""

    actor: str
    rights_declared: bool = False


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
                )
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-integration && make lint`
Expected: PASS, lint green

- [ ] **Step 5: Commit and open the pull request**

```bash
git add src/edutap/image_service/api/routers.py tests/test_api.py
git commit -m "feat(api): POST .../confirm, and uploads answer with a candidate"
gh pr create --title "A candidate state before pending" --body "$(cat <<'EOF'
## Summary

An upload becomes a `draft` its owner has to confirm before it reaches a
reviewer. Until then nobody has seen it, no review entry mentions it, and it
carries no rights declaration.

The declaration moves from the upload to the confirmation, which is where it is
actually made: a candidate somebody discards never carried one.

## Tests

`make test-local` and `make test-integration` green. `tests/test_states.py` covers
every transition into and out of `draft`; `tests/test_service_confirm.py` is new.

## Risks

- Requires `edutap.db_definitions` 0.2.1: the partial unique index
  `uq_photo_one_draft_per_person` and the `draft_details` column. How both reach
  an existing database is unresolved — see the plan.
- `submit()` loses two parameters. Its only caller today is the route in this
  repository.
- A candidate is listed by `GET /persons/{uid}/photos` and served by the version
  route. This service authenticates services, not roles, so it cannot tell a
  person's front end from a review client — that a service desk hides candidates
  is a promise of the review interface.
EOF
)"
```

---

## Pull request B — the declaration reference

### Task 5: which text version was agreed to

**Files:**

- Modify: `src/edutap/image_service/service.py`
- Modify: `src/edutap/image_service/api/routers.py`
- Test: `tests/test_service_confirm.py`, `tests/test_api.py`

**Interfaces:**

- Consumes: `PhotoService.confirm()` from task 3.
- Produces: `confirm(..., declaration_tag: str | None = None, declaration_sha: str | None = None)`;
  the pair lands in `public.photo_review.details["declaration"]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_service_confirm.py`:

```python
async def test_the_declaration_reference_lands_in_the_trail(session):
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="self",
        rights_declared=True,
        declaration_tag="v1.0",
        declaration_sha="a" * 40,
    )
    await session.commit()

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["details"]["declaration"] == {"tag": "v1.0", "sha": "a" * 40}


async def test_the_reference_is_optional(session):
    """A deployment without a versioned text is not forced to invent one."""
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID, version=result.version, actor="self", rights_declared=True
    )
    await session.commit()

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert "declaration" not in entry["details"]


async def test_a_half_given_reference_is_refused(session):
    """A tag without its hash records a version nobody can verify later."""
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    with pytest.raises(ValueError):
        await service.confirm(
            person_uid=UID,
            version=result.version,
            actor="self",
            rights_declared=True,
            declaration_tag="v1.0",
        )


async def test_the_reference_is_recorded_not_interpreted(session):
    """This service does not know what the text says and must not pretend to."""
    await _person_view(session)
    service, _, _ = build(session)
    result = await service.submit(person_uid=UID, upload=_png())
    await session.commit()

    await service.confirm(
        person_uid=UID,
        version=result.version,
        actor="self",
        rights_declared=True,
        declaration_tag="anything-at-all",
        declaration_sha="0" * 40,
    )
    await session.commit()

    entry = (await session.execute(sa.select(REVIEW))).mappings().one()
    assert entry["details"]["declaration"]["tag"] == "anything-at-all"
```

And to `tests/test_api.py`:

```python
def test_the_declaration_reference_travels_through_the_route(client):
    version = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    ).json()["version"]

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
    version = client.post(
        f"/persons/{UID}/photos",
        files={"file": ("p.png", _png(), "image/png")},
        headers=AUTH,
    ).json()["version"]

    response = client.post(
        f"/persons/{UID}/photos/{version}/confirm",
        json={"actor": "self", "rights_declared": True, "declaration_tag": "v1.0"},
        headers=AUTH,
    )

    assert response.status_code == 400
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-integration`
Expected: FAIL — `confirm()` got an unexpected keyword argument `declaration_tag`

- [ ] **Step 3: Carry the reference into the trail**

In `src/edutap/image_service/service.py`, replace `confirm`:

```python
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

        `declaration_tag` and `declaration_sha` identify the wording the person
        agreed to. They are **recorded, not interpreted**: what the text says, who
        wrote it and where it lives is the deployment's business, and a service
        that grew an opinion about it would stop being adoptable elsewhere.

        Both or neither. A tag without its hash records a version nobody can
        verify later -- a tag can be moved and a hash cannot -- and a declaration
        that cannot be checked is the failure this pair exists to prevent.
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
```

In `routers.py`, widen the request model and pass both through:

```python
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
```

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-integration && make lint`
Expected: PASS, lint green

- [ ] **Step 5: Commit and open the pull request**

```bash
git add src/edutap/image_service/service.py src/edutap/image_service/api/routers.py tests/
git commit -m "feat(service): record which declaration text a person agreed to

Tag and commit hash together, or neither: a tag can be moved and a hash
cannot, and a declaration nobody can verify later is the failure this
pair exists to prevent. Recorded, never interpreted."
gh pr create --title "Record the version of the rights declaration text" --body "$(cat <<'EOF'
## Summary

`POST .../confirm` accepts an optional `declaration_tag` / `declaration_sha` pair,
which lands in `public.photo_review.details["declaration"]`.

Without it a stored `rights_declared` proves only that some box was ticked, not
what it said. With it, the declaration and the wording it refers to sit in one
append-only trail that outlives the image.

The pair is opaque to this service. What the text says and where it lives is the
deployment's business.

## Tests

`make test-integration` green. Covered: both given, neither given, half given
(refused), and that the value is passed through unread.

## Risks

Optional by design, so no existing caller breaks.
EOF
)"
```

---

## Pull request C — an answer about limits

### Task 6: `GET /limits`

**Files:**

- Modify: `src/edutap/image_service/api/routers.py`
- Modify: `src/edutap/image_service/api/app.py`
- Modify: `tests/test_api.py`

**Interfaces:**

- Consumes: `ingest.Limits` as already built in `app.py`.
- Produces: `app.state.limits`; `GET /limits` on the `public` router →
  `{max_file_bytes, max_image_edge, accepted_formats}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py`:

```python
def test_limits_are_readable_without_a_token(client):
    """A browser checks them before uploading, and it holds no service token."""
    response = client.get("/limits")

    assert response.status_code == 200


def test_limits_report_what_this_deployment_enforces(client):
    """One number in one place. A second copy drifts, and then a front end
    refuses what this service would have taken."""
    body = client.get("/limits").json()

    # The client fixture builds the service with Limits(max_bytes=5_000_000,
    # max_edge=4096); the route must report those and not a default.
    assert body["max_file_bytes"] == 5_000_000
    assert body["max_image_edge"] == 4096


def test_limits_are_reported_as_media_types(client):
    """A front end puts these in an `accept` attribute; PIL's names are no use there."""
    formats = client.get("/limits").json()["accepted_formats"]

    assert "image/jpeg" in formats
    assert "image/png" in formats
    assert "JPEG" not in formats
```

In the `client` fixture's `lifespan`, next to `app.state.service_tokens`, add the
limits — the same object the service is built with:

```python
        limits = Limits(max_bytes=5_000_000, max_edge=4096)
```

declared before `unit_of_work`, passed to `PhotoService(limits=limits, …)`, and
then:

```python
        app.state.limits = limits
```

- [ ] **Step 2: Run them and watch them fail**

Run: `make test-integration`
Expected: FAIL with `404` on `/limits`

- [ ] **Step 3: Add the route**

In `src/edutap/image_service/api/routers.py`:

```python
#: The accepted upload formats as media types. `ingest.ACCEPTED_FORMATS` holds
#: Pillow's format names, which are the right thing for a decoder and the wrong
#: thing for a browser's `accept` attribute. MPO is a multi-picture JPEG and has
#: no media type of its own.
ACCEPTED_MEDIA_TYPES = ["image/jpeg", "image/png", "image/heic", "image/heif"]


class ServiceLimits(BaseModel):
    """What this service accepts, so a front end can say so before uploading."""

    max_file_bytes: int
    max_image_edge: int
    accepted_formats: list[str]


@public.get("/limits")
async def limits(request: Request) -> ServiceLimits:
    """The acceptance limits this deployment enforces.

    Tokenless, like the `current` route and for a related reason: the caller is a
    browser about to upload and holds no service token. Nothing in the answer is
    about a person -- it is a property of the deployment, and anyone could
    discover the same numbers by uploading something too large.

    Read from the very `Limits` object the ingest check uses. A second copy of
    these numbers anywhere drifts, and then a front end refuses what this service
    would have taken.
    """
    enforced = request.app.state.limits
    return ServiceLimits(
        max_file_bytes=enforced.max_bytes,
        max_image_edge=enforced.max_edge,
        accepted_formats=ACCEPTED_MEDIA_TYPES,
    )
```

In `src/edutap/image_service/api/app.py`, inside the `lifespan`, build the limits
once and publish them:

```python
        enforced = Limits(
            max_bytes=settings.max_upload_bytes,
            max_edge=settings.max_image_edge,
        )
        app.state.limits = enforced
```

and pass `limits=enforced` to `PhotoService(...)` instead of constructing a second
`Limits` there — one object, one truth.

- [ ] **Step 4: Run them and watch them pass**

Run: `make test-integration && make lint`
Expected: PASS, lint green

- [ ] **Step 5: Commit and open the pull request**

```bash
git add src/edutap/image_service/api/routers.py src/edutap/image_service/api/app.py tests/test_api.py
git commit -m "feat(api): GET /limits, tokenless

Read from the same Limits object the ingest check uses. A front end that
keeps its own copy of these numbers drifts, and then it refuses what this
service would have taken."
gh pr create --title "Report the acceptance limits" --body "$(cat <<'EOF'
## Summary

`GET /limits` reports maximum file size, maximum image edge and the accepted media
types, read from the same `Limits` object the ingest check enforces.

Tokenless, because the caller is a browser about to upload and nothing in the
answer is about a person.

## Tests

`make test-integration` green, including that the reported numbers equal the
enforced ones.

## Risks

A second tokenless route. It exposes two configured numbers and a fixed list --
nothing about any person, and nothing that was not already discoverable by
uploading something too large.
EOF
)"
```

---

## What this plan deliberately leaves out

- **A health route.** `create_app()` mounts only the two functional routers, and
  the consuming service now probes `GET /limits` instead. That works and is honest
  about what it proves — the process answers, not that the database and bucket are
  reachable. A real health route deserves its own pull request and should not
  block this work.
- **Retention of candidates.** Slice 8 (`POST /maintenance/expire`) is where a
  candidate nobody came back for gets cleared. Until then "at most one per person"
  carries the normal case on its own: the next upload replaces the candidate. It
  does not cover the person who never returns.
- **Delivery rules for `draft`.** A candidate is listed and served to any token
  holder. This service authenticates services and not roles, so it cannot tell a
  person's front end from a review client. That a service desk hides candidates is
  a promise of the review interface, not of this package — worth adding to the
  design record rather than pretending otherwise.
