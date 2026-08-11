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
| 3 | Object store | `objectstore.py` — key layout, put/get/purge — **done** |
| 4 | Repository | `repository.py` — the two tables plus the `person_view` reference, in one transaction; the temporary exit-5 guard in the `integration` CI job is gone with it — **done** |
| 5 | Ingest, `edutap.image_api` client, upload use case | `ingest.py`, `clients/image_api.py`, `manifest.py`, `service.py` — **done** |
| 6 | HTTP API | upload, review, delivery, placeholder — the routers over `service.py` — **done** |
| 7 | Events | `person.photo` producer |
| 8 | Retention | `POST /maintenance/expire` |
| 9 | Container and compose | `Dockerfile`, `compose.yml`, docs |

Slices 1 and 2 landed together, because a skeleton with no behaviour in it is not
reviewable — there is nothing to disagree with. Slices 3 and 4 landed together for
the opposite reason: the object store has no observable behaviour of its own until
something records what it did, and slice 4 is what removed the temporary CI guard
that slice 1 had to introduce.

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


## What slice 5 added that the plan did not foresee

**Sanitisation is ours, not the image API's.** `edutap.image_api` analyses and
transforms; nothing there strips metadata or guards against a decompression bomb.
Both have to happen before a file is forwarded anywhere, which is what `ingest.py`
is, and it is why `pillow` joined the runtime dependencies.

**`/crop/` has no format parameter and always answers PNG.** The design says format
follows purpose — PNG where a mask needs alpha, JPEG where nothing does — and an
unmasked portrait as PNG is roughly six times the bytes, per version, per person.
Until the other service can emit a format, the manifest marks which renderings get
re-encoded here. A format parameter in `edutap.image_api` is the clean fix and
belongs in that repository.

**An upload with no face is refused rather than queued.** It was not obvious from
the design, which says every upload reaches a reviewer. But `crop_mode: null` means
there is no picture to approve, so queueing it would fill the review list with
entries nobody can act on. A photograph that merely *fails* a check is still
queued — failing is what a reviewer is for.


## What slice 6 settled

**Authentication is of services, not of people.** A front end authenticates its own
user — a person through their session, a reviewer through the institution's role
model — and vouches for the call. That is what keeps this package free of
Shibboleth, of one university's role names and of any opinion about who may approve
a photograph. Tokens are configured as a mapping, keyed by the calling service, so
the trail can record which one acted and one can be rotated without invalidating
the others.

**`423 Locked` and `409 Conflict` are kept apart.** Both would be true of a held
active version, but a front end shows a person "replace it instead" for one and a
reviewer "this is evidence in a proceeding" for the other. Collapsing them would
force it to parse a message to tell them apart.

**The `current` route answers with a placeholder, never a 404.** A 404 there is a
broken image on somebody's card, and the URL is already baked into every pass
issued before the photograph existed.

Still open before this is deployable: the events of slice 7, the retention endpoint
of slice 8, and the container of slice 9. `ObjectStore` still has no test against a
live bucket — that belongs with slice 9, where a MinIO or RustFS container joins the
integration job.
