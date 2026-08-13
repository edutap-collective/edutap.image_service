"""The reference other services read out of `person_view.photo`.

This is the contract surface of the whole service: a data provider hands it to a
pass builder, a vendor connector reads it by SQL, and an issuing service decides
from it whether a card may be produced. Everything it says has to be answerable
without a second call.
"""

from edutap.image_service.reference import PLACEHOLDER_ASSURANCE, build_reference, placeholder
from edutap.image_service.states import EvidenceKind

ORIGIN = "https://wallet.example.org/public-api/images/v1"
UID = "ab12cde@lmu.de"
VERSION = "0198f3c2-7b41-7000-8000-0242ac120002"


def test_the_url_is_person_scoped_and_says_current():
    """Google bakes this URL into an issued pass and fetches it without credentials.

    Version-pinned it would go stale the moment the photograph is replaced, and the
    pass would keep showing the old one -- or nothing.
    """
    reference = build_reference(
        origin=ORIGIN,
        person_uid=UID,
        version=VERSION,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        sha256="a" * 64,
    )
    assert reference["url"] == f"{ORIGIN}/persons/{UID}/photo/current"


def test_the_concrete_version_travels_beside_the_current_url():
    """So a pass builder can record which version it embedded, without asking again.

    Apple bakes bytes rather than a URL, and the audit question afterwards is which
    photograph went into which pass.
    """
    reference = build_reference(
        origin=ORIGIN,
        person_uid=UID,
        version=VERSION,
        evidence_kind=EvidenceKind.ID_DOCUMENT,
        sha256="b" * 64,
    )
    assert reference["version"] == VERSION
    assert reference["sha256"] == "b" * 64


def test_evidence_maps_to_the_assurance_of_the_photograph():
    """Medium for a human's look, high for a document or a wallet-held credential."""
    visual = build_reference(
        origin=ORIGIN,
        person_uid=UID,
        version=VERSION,
        evidence_kind=EvidenceKind.SUPPORT_VISUAL,
        sha256="c" * 64,
    )
    document = build_reference(
        origin=ORIGIN,
        person_uid=UID,
        version=VERSION,
        evidence_kind=EvidenceKind.ID_DOCUMENT,
        sha256="c" * 64,
    )
    assert visual["photo_assurance"] == "https://refeds.org/assurance/IAP/medium"
    assert document["photo_assurance"] == "https://refeds.org/assurance/IAP/high"
    assert not visual["is_placeholder"]


def test_the_placeholder_reference_has_the_same_url():
    """A person who never uploaded anything still needs a URL that resolves.

    Any design where the URL appears only once a photo exists leaves every pass
    issued before that with a dead image link.
    """
    reference = placeholder(origin=ORIGIN, person_uid=UID)
    assert reference["url"] == f"{ORIGIN}/persons/{UID}/photo/current"
    assert reference["is_placeholder"]
    assert reference["version"] is None
    assert reference["evidence_kind"] is None


def test_the_placeholder_claims_no_assurance():
    """`IAP/low` is not it: this says nothing was verified, not that little was.

    An issuing service reads this to decide whether a card may be produced at all,
    and a value that sorts alongside the real ones would let it through.
    """
    assert placeholder(origin=ORIGIN, person_uid=UID)["photo_assurance"] is PLACEHOLDER_ASSURANCE
    assert PLACEHOLDER_ASSURANCE is None


def test_a_trailing_slash_on_the_origin_does_not_double_up():
    """Operators write both forms in an env file, and one of them would break the URL."""
    reference = placeholder(origin=ORIGIN + "/", person_uid=UID)
    assert "//persons" not in reference["url"]
    assert reference["url"] == f"{ORIGIN}/persons/{UID}/photo/current"
