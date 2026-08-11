# Plan: build the person photo service

Date: 2026-08-11
Design: `superpowers/specs/2026-08-11-person-photo-service-design.md`

The design is agreed and the contract tables are merged
(`edutap.db_definitions` 0.1.4: `public.photo`, `public.photo_review`). This plan
slices the service into pieces that each land as their own pull request, green on
their own.

## Order, and why this one

The slices go inside-out: the rules first, the I/O around them, the wiring last.
That order is not a preference. The state machine is where every invariant of this
service lives, it needs no database, no bucket and no HTTP, and it is the only part
where a mistake is expensive later — a wrong transition rule discovered after the
API exists has to be corrected in two places.

| # | Slice | Lands |
|---|---|---|
| 1 | Package skeleton and settings | tooling, CI, `settings.py` |
| 2 | State machine | `states.py` — pure, no I/O |
| 3 | Object store | `objectstore.py` — key layout, put/get/purge |
| 4 | Repository | `repository.py` — the two tables plus the `person_view` reference, in one transaction |
| 5 | `edutap.image_api` client | `clients/image_api.py` |
| 6 | HTTP API | upload, review, delivery, placeholder |
| 7 | Events | `person.photo` producer |
| 8 | Retention | `POST /maintenance/expire` |
| 9 | Container and compose | `Dockerfile`, `compose.yml`, docs |

Slices 1 and 2 are this pull request. They are together because a skeleton with no
behaviour in it is not reviewable — there is nothing to disagree with.

## Slice 1 — skeleton

Modelled on `edutap.data_provider`, the reference package for these conventions:
`hatchling`, `src/edutap/…` layout, `[project.optional-dependencies] dev`, a
`Makefile` whose targets CI calls rather than repeating, `ruff` and `ty`, no
lockfile.

Settings carry the prefix `IMAGE_SERVICE_`. What they hold is fixed by the design:
the object store, the database, the `edutap.image_api` base URL, the Kafka prefix,
the variant manifest, the reactivation age, the default expiry deadline and the
placeholder.

The manifest is configuration and not code — a different institution needs
different sizes, and a service that hard-codes one set is not a standard anybody
else can adopt.

## Slice 2 — the state machine

One module of pure functions. Each answers "may this transition happen, and what
does it change", and each raises rather than returning a boolean, because every
caller of a boolean would have to invent its own error message for the same
refusal.

The rules it holds:

- A version reaches `active` only with an `evidence_kind`. There is no path that
  activates without one.
- `rejected` reaches `active` only through a reviewer. The person cannot reactivate
  something that was refused.
- Activating a version supersedes whichever version was active. The caller is told
  to do so rather than discovering it.
- The active version cannot be purged, only replaced. A card would otherwise lose
  its photograph silently.
- A legal hold defeats every deletion path — the person's, the retention run's.
  Only the deletion of the person ignores it.
- Reactivating a `superseded` version is free while its review is younger than the
  configured age, and returns to `pending` beyond it.

What it deliberately does not know: whether a row exists, what the bucket holds,
who is calling. Those belong to slices 3, 4 and 6, and keeping them out is what
makes this module testable without a fixture.

## Open, carried from the design

- The variant manifest — sizes and recipes. The service reads it from
  configuration, so the values can land after this.
- Whether `state`, `evidence_kind` and `action` should get a home in
  `edutap.data_models.vocabulary`, as the other contract vocabulary has. Declared
  here for now; moving them later is a rename, not a redesign.
- Whether the actor string a caller hands over is enough for an audit.
