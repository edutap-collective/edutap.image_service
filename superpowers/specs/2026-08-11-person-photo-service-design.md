# Design: person photo service

Date: 2026-08-11
Status: design agreed, pending implementation plan
Branch: `docs/person-photo-service-design`

## Overview

`edutap.image_service` stores, reviews and delivers the photograph of a person.
It is the counterpart to `edutap.image_api`, which analyses and transforms images
but holds no state: analysis is a stateless computation, delivery needs storage,
access control and cache-friendly URLs.

A person uploads a photo. The service has it validated by `edutap.image_api`,
keeps it as an immutable version, and lets a reviewer approve or reject it.
Exactly one version per person is active at a time. The active version is what
identity cards and wallet passes show; when there is none, the service delivers a
placeholder under the very same URL.

The service is deliberately generic. It knows nothing about wallet passes,
students, mail templates or any institution's role model. What it knows is
versions, states, evidence, storage and delivery. Everything that is policy —
who may approve, which pass types insist on a real photo, what a rejection mail
says — belongs to the deployment that operates it.

## Goals

- One writer for person photographs: no other service touches the bucket or the
  photo tables.
- An immutable version per upload, with a state machine and an append-only
  review trail.
- Exactly one active version per person, enforced by the database rather than by
  application logic.
- A stable, person-scoped delivery URL that resolves before the person ever
  uploaded anything.
- Every activation carries named evidence of how the photo was verified.
- Deletion paths that a deployment can drive from outside, so the service needs
  no clock of its own.

## Non-goals

- Image analysis and transformation. That is `edutap.image_api`; this service
  calls it.
- A review user interface. The service exposes the review as an API; the
  operator builds or buys the front end.
- Sending mail. The service publishes events; whoever consumes them notifies
  people.
- Knowledge of pass types, credentials or wallet providers. The service reports
  facts; the issuing service decides.
- Identity assurance of the *person*. The service records how the *photograph*
  was verified, which is a different statement (see [Assurance](#assurance)).

## Decisions

Taken during design, 2026-08-11. Each of these was a fork with a live
alternative; the reasoning is kept because the alternatives will look attractive
again later.

| Decision | Alternative rejected because |
|---|---|
| New service rather than reworking a site-specific image backend | The existing site backend carries no substance worth migrating, and a rework would keep it site-specific. |
| Sole writer; front ends proxy through it | Letting each front end write the bucket puts three writers on one row with no transactional bracket. |
| Path `/<person_uid>/photo/<version>/…` | A content-addressed flat layout loses the ability to browse per person, which operations needs. |
| `version` is the upload generation; the variant recipe is a separate level | Folding the recipe into the version forces a full re-render of every stored photo when a target size changes. |
| Person-scoped `current` URL for delivery | Version-pinned URLs bake into issued passes and go stale the moment a photo is replaced. |
| Placeholder under the same URL when no active version exists | Any design where the URL only appears once a photo exists leaves every previously issued pass with a dead image URL. |
| `legal_hold` orthogonal to the state | As a state it would need the previous state stored somewhere to restore, and it would lose the fact that the held photo was active at the time. |
| Both photo tables in `public` | More than one service reads them, and `public` is the schema for exactly that case. |

## Domain model

### Version

A version is one photograph as the system keeps it: the sanitised raw image plus
its crop. Its content never changes. A new upload is a new version; a corrected
crop of the same original is a new *recipe rendering*, not a new version.

`version` is an opaque, sortable identifier (UUIDv7). It appears in the object
path and in the reference the service publishes, and it is never interpreted by
anyone but this service.

### State

```
                 ┌──────────► rejected ──(support)──┐
                 │                                  │
   upload ──► pending ──(review)──► active ◄────────┘
                                      │  ▲
                          (replaced)  │  │ (user switches back)
                                      ▼  │
                                  superseded
```

| State | Meaning |
|---|---|
| `pending` | Uploaded, awaiting review. Never delivered towards a pass; delivered to a review client. |
| `active` | The photograph of this person. At most one per person. |
| `rejected` | Reviewed and refused. Reaches `active` only through a reviewer, never through the person. |
| `superseded` | Was active, has been replaced. The person may switch back to it. |

Two rules the database enforces, not the application:

- `CREATE UNIQUE INDEX … ON public.photo (person_uid) WHERE state = 'active'` —
  two reviewers clicking at the same moment cannot produce two active photos.
- A version reaches `active` only with `evidence_kind` set. There is no
  activation without named evidence.

Switching back to a `superseded` version does not create a version. It sets that
row to `active` and the previously active row to `superseded`. It is free while
the original review is younger than a configured age (default six months);
beyond that the version returns to `pending` and is reviewed again. The
reasoning: switching back is the ordinary "I liked the old one better" case and
must not queue, whereas a photograph approved years ago no longer shows the
person the card is supposed to identify.

### Review trail

`public.photo_review` is append-only. Every transition writes one row: the
submission, the approval, the rejection, the reactivation, setting and releasing
a legal hold, a deletion, an expiry. Nothing is ever updated in place, so a
mistaken rejection is corrected by a further entry rather than by rewriting
history.

The review entry outlives the image. When bytes are purged — by the person, by
the expiry run — the `photo` row remains with `purged_at` set and the review
trail stays intact. Only the deletion of the person removes both.

> This follows from "the review outlives the image" together with referential
> integrity, and was not separately decided: a review row pointing at a deleted
> photo row would either dangle or force the trail to be deleted with it.

### Legal hold

A suspected fraud is not a state, because it can strike a version in any state —
a `pending` photo the reviewer immediately recognises as someone else's face, an
`active` one where the suspicion arises months later, a `superseded` one in the
history.

`legal_hold_since` (a nullable timestamp; present means held), with
`legal_hold_by` and `legal_hold_reason`. While set, **every** deletion path skips
the version: the person cannot delete it, the expiry run ignores it. Setting it
is a reviewer's right; releasing it is reserved to a separate, narrower role.
Both write a review entry.

```{important}
The deletion of the person removes held versions too, and the notification goes
out when the hold is *set*, not when the deletion approaches. An operator should
know the consequence: where a fraud case is followed by the person leaving the
institution, the directory's deletion event can remove the evidence while the
handover to a prosecutor is still in progress. Handing over has to happen when
the hold is set.
```

## Storage layout

```
<bucket>/<person_uid>/photo/<version>/raw
<bucket>/<person_uid>/photo/<version>/<recipe>/<variant>
```

`person_uid` is the identifier the institution guarantees to be permanent — at a
university typically the ePPN. It is explicitly *not* the directory DN, which
changes.

`raw` is the sanitised original: decoded and re-encoded on the way in, so no
camera metadata survives. It is the only object that must never be delivered,
and it lives in the same prefix as the rest; the access rule is enforced by the
service, not by the layout.

The recipe level exists so that a changed target size produces a new rendering
*beside* the old one. Passes referring to the previous rendering keep working
until they are rebuilt.

Format follows purpose rather than uniformity: masked variants are PNG because
the mask needs an alpha channel; raw and unmasked crops are JPEG, where a
photograph is roughly a sixth of the size a lossless encoding would take.

## Database

Both tables live in `public` and are declared in `edutap.db_definitions`, because
more than one service reads them — a data provider, a pass builder, and at some
deployments a vendor connector reading SQL directly. `edutap.image_service` is
their only writer.

### `public.photo`

| Column | Type | Note |
|---|---|---|
| `person_uid` | `varchar(64)` collation `C` | PK part 1. Never interpreted. |
| `version` | `varchar(64)` collation `C` | PK part 2. UUIDv7, opaque. |
| `state` | `varchar(32)` | `pending` \| `active` \| `rejected` \| `superseded`. Text, not a native enum. |
| `sha256` | `char(64)` | Of the sanitised raw, not of the uploaded file. |
| `evidence_kind` | `varchar(32)` null | `support_visual` \| `id_document` \| `eudi_pid`. Set on activation. |
| `photo_assurance` | `varchar(128)` null | RAF IAP URI derived from `evidence_kind`. |
| `recipe` | `varchar(64)` | Which manifest rendered this version's variants. |
| `rights_declared_at` | `timestamptz` | When the uploader declared they hold the rights. |
| `legal_hold_since` | `timestamptz` null | Present means held. |
| `legal_hold_by` | `varchar(128)` null | |
| `legal_hold_reason` | `text` null | |
| `notified_at` | `timestamptz` null | When the person was told of a rejection. Starts the expiry clock. |
| `purged_at` | `timestamptz` null | Bytes gone, row and trail kept. |
| `created_at` | `timestamptz` | |
| `updated_at` | `timestamptz` | |

Indexes: the partial unique index on `(person_uid) WHERE state = 'active'`, plus
`(state, notified_at)` for the expiry run.

### `public.photo_review`

| Column | Type | Note |
|---|---|---|
| `review_id` | `uuid` | PK. |
| `person_uid`, `version` | | The version this concerns. |
| `occurred_at` | `timestamptz` | |
| `actor` | `varchar(128)` | Who acted. The service stores the string it is given. |
| `action` | `varchar(32)` | `submit` \| `approve` \| `reject` \| `reactivate` \| `hold_set` \| `hold_release` \| `purge` \| `expire`. |
| `evidence_kind` | `varchar(32)` null | On `approve`. |
| `reason` | `text` null | |
| `sha256` | `char(64)` | Repeated here on purpose, so the trail is readable after the bytes are gone. |
| `details` | `jsonb` | The validation report summary; any copyright claim found in the upload's metadata. |

### `public.person_view.photo`

The reference other services read stays a real column, written by
`edutap.image_service`.

```json
{
  "url": "https://…/<person_uid>/photo/current",
  "version": "0198f3…",
  "is_placeholder": false,
  "evidence_kind": "support_visual",
  "photo_assurance": "https://refeds.org/assurance/IAP/medium",
  "sha256": "…"
}
```

`url` is a base; a consumer appends `/<recipe>/<variant>`. Carrying the concrete
`version` alongside the `current` URL lets a pass builder record which version it
embedded without a second call.

```{note}
An earlier draft made this column a view over `public.photo`. That is not
buildable: `person_view` is a table a spooler upserts into, and a single column
of a written table cannot become a view. The alternative — renaming the base
table and laying a view over it — moves every writer to a new name for one
derived column. Since both tables live in the same database, the service instead
writes `public.photo` and `public.person_view.photo` in one transaction, which
makes drift impossible. The person spooler already names its columns
individually so as not to touch `photo`, which is exactly this arrangement.
```

## HTTP API

The service authenticates callers by service token only. It has no notion of a
person's session, a reviewer's role, or an institution's directory: the front end
authenticates its user and vouches for the call.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/persons/{person_uid}/photos` | Upload. Validates, stores as `pending`, returns the validation report. |
| `GET` | `/persons/{person_uid}/photos` | The versions with their states. |
| `GET` | `/persons/{person_uid}/photos/{version}/{recipe}/{variant}` | Bytes of one version. Serves `pending` too — this is the review path. |
| `POST` | `/persons/{person_uid}/photos/{version}/approve` | Requires `evidence_kind` and an actor. |
| `POST` | `/persons/{person_uid}/photos/{version}/reject` | Requires a reason. |
| `POST` | `/persons/{person_uid}/photos/{version}/activate` | Switch back to a `superseded` version. |
| `DELETE` | `/persons/{person_uid}/photos/{version}` | Purges bytes. Refuses on `active` and on a held version. |
| `POST` | `/persons/{person_uid}/photos/{version}/hold` | Set or release. Release needs the narrower role. |
| `POST` | `/persons/{person_uid}/photos/reset` | Back to the placeholder. |
| `GET` | `/persons/{person_uid}/photo/current/{recipe}/{variant}` | **The only unauthenticated route.** |
| `POST` | `/maintenance/expire` | Externally driven retention run. |
| `DELETE` | `/persons/{person_uid}` | Everything, including held versions. |

Approve, reject, activate, hold and reset exist from the first release even
though no review interface does. Without them nothing ever becomes `active` and
the service delivers placeholders forever; an operator drives them with a service
token until a front end arrives.

### The unauthenticated route

`GET /persons/{person_uid}/photo/current/{recipe}/{variant}` carries no token
because a wallet provider bakes this URL into an issued pass and fetches it
without credentials. It resolves `current` to the active version, or to the
placeholder when there is none. It never serves `pending`, `rejected` or
`superseded`, and never `raw`.

## Delivery and the placeholder

When a person has no active version the route serves a placeholder that is
*recognisably* a placeholder — an image that says no photograph is on file, not a
neutral silhouette that passes for one in a glance. A card without a verified
photograph must not read like a card with one.

The placeholder is one image for all purposes; it does not vary by what the photo
is used for.

Whether a given use *tolerates* the placeholder is not this service's decision.
The reference carries `is_placeholder` and `evidence_kind`, and the issuing
service decides — for pass types that insist on a real photograph, issuance fails
with a hard error.

## Assurance

`evidence_kind` maps to REFEDS Assurance Framework identity proofing:

| `evidence_kind` | Mapped to |
|---|---|
| `support_visual` | `https://refeds.org/assurance/IAP/medium` |
| `id_document` | `https://refeds.org/assurance/IAP/high` |
| `eudi_pid` | `https://refeds.org/assurance/IAP/high` |

`IAP/low` does not occur: there is no activation without evidence.

```{important}
This value is a statement about the *photograph*, not about the person. A person
proofed with an identity document at enrolment holds their assurance regardless
of how their later photo was checked. Where a credential carries assurance, the
value that goes out is the **minimum over every attribute delivered** — computed
by whichever service issues the credential and knows both numbers. This service
knows only the photograph's, and must not be read as reporting the person's.
```

## Events

The service publishes to `edutap.<env>.person.photo`, with
`edutap.<env>.person.photo.dlq` beside it, following the established topic
schema: dots only, the producing service in the header and never in the name, one
consumer per topic.

Published: a version became active; the active version was withdrawn; a version
was rejected; a legal hold was set. The messages state facts about photographs.
They do not ask for anything — translating "this person's photo changed" into
"rebuild and push their passes", or into a mail, is the consumer's business, and
keeps this service free of any knowledge of passes.

## Retention and deletion

The service has no clock. Retention runs when someone calls it:

```
POST /maintenance/expire  {"state": "rejected", "older_than_days": 14}
→ {"purged": [...], "skipped_legal_hold": [...]}
```

Idempotent, callable as often as an operator likes, and the response is the
record of what happened. The caller supplies the deadline — the number is the
operator's policy, not the service's — and the service falls back to a configured
default when none is given.

The clock starts at `notified_at`, not at the rejection: somebody on a three-week
holiday would otherwise have no chance to save their photo before it goes.

Only `rejected` expires automatically. `pending` never does — a photo nobody
reviewed stays and stays visible, and an operator watches the queue by monitoring
rather than by losing the backlog to a timer. `superseded` stays until the person
deletes it or leaves.

What the person may do: delete any version that is not active; replace the active
one. Not delete the active one — a card would silently lose its photograph. A
reviewer resets to the placeholder where that is genuinely wanted.

Deleting the person deletes everything: every version, every rendering, every
review entry, held or not.

## Upload handling

Limits are enforced on acceptance, before any renderer sees the file: a
decompression bomb has to be refused by the size check, not survived by the
decoder. A browser client checks the same limits first, so the common case fails
fast and locally.

Accepted: JPEG, PNG, HEIC/HEIF — the last because it is what an iPhone produces,
and refusing it would fail a large share of uploads for no reason. Everything
else is refused without negotiation.

Every image is decoded and re-encoded on the way in. Camera metadata does not
survive, including in `raw`; `sha256` is therefore taken over the sanitised
image, because the claim being recorded is "this image was reviewed", not "this
file was uploaded".

### Rights

The upload carries a mandatory declaration that the uploader holds the rights to
the image or has permission to use it. That declaration is what carries legal
weight, and it is stored with the version.

Copyright metadata is *recorded, not evaluated*. Its absence proves nothing —
most uploads carry none and any phone tool strips it — and its presence says only
that somebody asserts a right, not whether a licence was granted. Evaluating it
automatically would either block constantly and wrongly or achieve nothing.
Where `Copyright`, `Artist` or XMP rights fields are present, their value goes
into the review entry and is shown to the reviewer, who can ask.

## Configuration

`pydantic-settings`, prefix `IMAGE_SERVICE_`: object store endpoint, bucket and
credentials; the database DSN; the `edutap.image_api` base URL; the Kafka prefix;
the variant manifest; the reactivation age (default 180 days); the default
expiry deadline; the placeholder image.

The variant manifest is configuration rather than code. A different institution
needs different sizes, and a service that hard-codes one set is not a standard
anybody else can adopt.

## Testing

Test-first, `pytest` with `anyio`. `edutap.image_api` is exercised through
`httpx2.MockTransport` — no network in unit tests. The object store gets a fake
in unit tests and the real RustFS in the integration suite.

The tests that matter most are the ones about invariants rather than endpoints:
two concurrent approvals produce one active version; an activation without
evidence is refused by the database; a held version survives every deletion path
except the person's; the expiry clock starts at the notification; `current`
resolves to the placeholder for a person who never uploaded anything.

## Open questions for the plan

1. **The variant manifest.** Sizes and recipes are not yet fixed. Note that the
   wallet asset sizes `edutap.image_api` offers are pass artwork — logo, hero,
   strip — and contain no portrait slot beyond Apple's 90×90 thumbnail. A
   person's photograph is a `/crop/` product, not a wallet asset.
2. **`recipe` on the row or per rendering.** The column above assumes one recipe
   per version at a time; keeping several renderings side by side during a
   manifest change needs a second table.
3. **Actor identity.** The service stores the actor string it is handed. Whether
   that is enough for an audit, or whether the trail needs the calling service
   recorded separately, is a deployment question that should be settled before
   the first release rather than after.
