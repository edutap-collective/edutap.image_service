# CLAUDE.md — edutap.image_service

Repository-specific rules. They take precedence over the global defaults.

## Language

**English only.** This repository belongs to eduTAP proper, not to any single
institution: README, changelog, documentation, docstrings, code comments, commit
messages, pull request titles and bodies, and replies to review comments.

The language follows the repository, not the conversation. A discussion held in
German still produces English artefacts here.

## What this package is

The service that stores, reviews and delivers the photograph of a person. It is the
stateful counterpart to `edutap.image_api`, which analyses and transforms images and
holds nothing.

The design is recorded in
`superpowers/specs/2026-08-11-person-photo-service-design.md`.

## Guard rails

**It is the only writer.** No other service touches the bucket prefix or
`public.photo`, `public.photo_review` and the reference in `public.person_view`.
Front ends call this service; they do not reach past it. Three writers on one row
with no transactional bracket is the failure this arrangement exists to prevent.

**It knows nothing about passes, institutions or roles.** Not what a student is, not
which card type insists on a real photograph, not who is allowed to approve, not
what a rejection mail says. It answers with facts — `is_placeholder`,
`evidence_kind`, a photo's assurance — and the deployment decides. A generic service
that grows one institution's policy stops being adoptable by the next one.

**No activation without named evidence.** Every path to `active` carries
`support_visual`, `id_document` or `eudi_pid`. There is no default, because a
default would record that a human looked at a photograph nobody looked at.

**A legal hold defeats every deletion path** except the deletion of the person. That
one exception is deliberate and is written down in the design record, together with
what it costs.

**It has no clock.** Retention runs when a caller asks it to, and the caller supplies
the deadline. A deadline that lives here would be this package deciding an
operator's policy.

**Assurance describes the photograph, never the person.** A person proofed with an
identity document at enrolment holds their assurance regardless of how their later
photo was checked. Whoever issues a credential combines the two; this service must
never be read as reporting the person's.

## Working practice

Branch first, never commit on `main`. Push only when asked. Test-first: the state
machine has no I/O precisely so its rules can be pinned before anything calls them.
`make lint` and `make test-local` green before opening a pull request; CI calls the
same targets rather than repeating their commands.

Design records and plans under `superpowers/` record a decision at a point in time —
do not rewrite them to match a later state; write a new one. They live at the top
level rather than under `docs/`, which is collected into the central eduTAP
documentation build.

## Sources and confidentiality

**No vendor internals — from any vendor, not just the ones currently in play.**
Neither in files nor in commit messages.

The standard is academic: a statement counts as reliable only where it can be
evidenced from public information, with a link. Everything else is one of three
other things, and they are not interchangeable:

* **Documented** — public source, linked. May be written as fact.
* **Verified, not citable** — obtained by a person from an access-protected area and
  checked there; the reference is recorded internally but must not be published; and
  the statement has been reduced to what is not confidential. May be written as
  fact, carrying this label.
* **Measured** — established by our own tests. May be written down, but always marked
  as such: it describes what a platform did on the day we looked, not what it
  guarantees.
* **Insider knowledge** — is not written down at all.

Contract and regulatory material is wanted and citable: eduPersonAssurance, REFEDS
assurance profiles, GÉANT and eduGAIN terms, published wallet programme obligations.
