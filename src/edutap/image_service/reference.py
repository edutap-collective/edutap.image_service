"""What `public.person_view.photo` carries, and what other services read.

This is the contract surface of the service. A data provider hands it to a pass
builder, a vendor connector reads it straight out of the column by SQL, and an
issuing service decides from it whether a card may be produced at all. Everything
those three need has to be answerable from here without a second call — which is
why the concrete `version`, the evidence and the assurance travel beside the URL
rather than behind another request.
"""

from typing import Any

from .states import EvidenceKind

#: How the provenance of a photograph maps onto REFEDS identity proofing.
#:
#: A statement about the *image*, never about the person: someone proofed with a
#: document at enrolment holds their assurance regardless of how their later photo
#: was checked. Whoever issues a credential combines the two — the value that goes
#: out is the minimum over every attribute delivered — and this service knows only
#: one of them.
PHOTO_ASSURANCE: dict[EvidenceKind, str] = {
    EvidenceKind.SUPPORT_VISUAL: "https://refeds.org/assurance/IAP/medium",
    EvidenceKind.ID_DOCUMENT: "https://refeds.org/assurance/IAP/high",
    EvidenceKind.EUDI_PID: "https://refeds.org/assurance/IAP/high",
}

#: What a placeholder claims, which is nothing.
#:
#: Not `IAP/low`. That would say a little was verified, where the truth is that
#: nothing was — and an issuing service reading it as the bottom of a scale would
#: let the card through instead of refusing it.
PLACEHOLDER_ASSURANCE: None = None


def _current_url(origin: str, person_uid: str) -> str:
    """Build the person-scoped delivery URL.

    Person-scoped and stable because Google bakes it into an issued pass and fetches
    it without credentials, long after issuance. A version-pinned URL would go stale
    the moment the photograph is replaced.

    The origin is stripped of a trailing slash: operators write it both ways in an
    env file, and one of them would produce `…//persons/…`.
    """
    return f"{origin.rstrip('/')}/persons/{person_uid}/photo/current"


def build_reference(
    *,
    origin: str,
    person_uid: str,
    version: str,
    evidence_kind: EvidenceKind,
    sha256: str,
) -> dict[str, Any]:
    """Build the reference for a person who has an active photograph."""
    return {
        "url": _current_url(origin, person_uid),
        "version": version,
        "is_placeholder": False,
        "evidence_kind": str(evidence_kind),
        "photo_assurance": PHOTO_ASSURANCE[evidence_kind],
        "sha256": sha256,
    }


def placeholder(*, origin: str, person_uid: str) -> dict[str, Any]:
    """Build the reference for a person who has none.

    Same shape and same URL as the real one. A consumer therefore needs no special
    case, and `is_placeholder` is the one field it has to read to know the
    difference — which is what an issuing service checks before producing a card
    that is supposed to carry a verified likeness.
    """
    return {
        "url": _current_url(origin, person_uid),
        "version": None,
        "is_placeholder": True,
        "evidence_kind": None,
        "photo_assurance": PLACEHOLDER_ASSURANCE,
        "sha256": None,
    }
