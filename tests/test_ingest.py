"""Acceptance of an uploaded file: limits, sanitisation, refusals.

Everything here happens before any renderer sees the bytes. A decompression bomb
has to be refused by a check, not survived by a decoder.
"""

import io

import pytest
from PIL import Image

from edutap.image_service.ingest import (
    FileTooLarge,
    ImageTooLarge,
    Limits,
    UnsupportedFormat,
    rights_metadata,
    sanitise,
)

LIMITS = Limits(max_bytes=1_000_000, max_edge=2048)


def _encode(fmt: str = "JPEG", size: tuple[int, int] = (600, 800), **save) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (120, 90, 60)).save(buffer, format=fmt, **save)
    return buffer.getvalue()


def test_a_jpeg_is_accepted_and_comes_back_as_a_decoded_re_encoded_image():
    result = sanitise(_encode("JPEG"), limits=LIMITS)
    assert result.width == 600
    assert result.height == 800
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


def test_a_png_upload_is_accepted():
    assert sanitise(_encode("PNG"), limits=LIMITS).height == 800


def test_a_file_that_is_not_an_image_is_refused():
    with pytest.raises(UnsupportedFormat):
        sanitise(b"this is not an image", limits=LIMITS)


def test_a_format_outside_the_accepted_set_is_refused():
    """Refused without negotiation, rather than converted quietly.

    A GIF or a TIFF that silently becomes a portrait is a surprise for whoever
    uploaded it, and the set of things a decoder will accept is much wider than the
    set anybody meant to send.
    """
    with pytest.raises(UnsupportedFormat):
        sanitise(_encode("GIF"), limits=LIMITS)


def test_a_file_over_the_byte_limit_is_refused_before_decoding():
    """The size check comes first, so a bomb never reaches a decoder."""
    with pytest.raises(FileTooLarge):
        sanitise(b"x" * (LIMITS.max_bytes + 1), limits=LIMITS)


def test_an_image_beyond_the_edge_limit_is_refused():
    """A 40000x40000 PNG compresses to very little and expands to gigabytes.

    The byte limit alone does not catch it -- this is the check that does.
    """
    with pytest.raises(ImageTooLarge):
        sanitise(
            _encode("PNG", size=(4000, 100)),
            limits=Limits(max_bytes=10_000_000, max_edge=512),
        )


def test_camera_metadata_does_not_survive():
    """Including in what is stored as `raw`.

    A phone photograph carries GPS coordinates, a device identifier and a capture
    time. None of that belongs in a bucket of portraits, and the sha256 recorded
    against the review is therefore taken over the sanitised image: the claim is
    "this image was reviewed", not "this file was uploaded".
    """
    original = io.BytesIO()
    image = Image.new("RGB", (100, 100), (10, 20, 30))
    exif = image.getexif()
    exif[0x010F] = "SomePhoneMaker"
    image.save(original, format="JPEG", exif=exif)
    assert b"SomePhoneMaker" in original.getvalue()

    result = sanitise(original.getvalue(), limits=LIMITS)
    assert b"SomePhoneMaker" not in result.data
    assert not Image.open(io.BytesIO(result.data)).getexif()


def test_a_copyright_claim_is_reported_rather_than_evaluated():
    """It is shown to a reviewer, never used to block.

    Its absence proves nothing -- most uploads carry none and any phone tool strips
    it -- and its presence says only that somebody asserts a right, not whether a
    licence was granted. What carries legal weight is the uploader's declaration.
    """
    original = io.BytesIO()
    image = Image.new("RGB", (100, 100), (10, 20, 30))
    exif = image.getexif()
    exif[0x8298] = "(c) Fotostudio Mueller"
    exif[0x013B] = "A Photographer"
    image.save(original, format="JPEG", exif=exif)

    claims = rights_metadata(original.getvalue())
    assert claims["copyright"] == "(c) Fotostudio Mueller"
    assert claims["artist"] == "A Photographer"


def test_no_claim_is_reported_as_an_empty_mapping():
    """So a caller writes `{}` into the trail rather than a null it has to interpret."""
    assert rights_metadata(_encode("JPEG")) == {}


def test_the_hash_is_taken_over_the_sanitised_image():
    """Two uploads differing only in metadata are the same photograph.

    Hashing the uploaded file would make them different, and the review trail would
    claim two distinct images had been checked.
    """
    plain = io.BytesIO()
    tagged = io.BytesIO()
    image = Image.new("RGB", (100, 100), (10, 20, 30))
    image.save(plain, format="JPEG", quality=90)
    exif = image.getexif()
    exif[0x010F] = "SomePhoneMaker"
    image.save(tagged, format="JPEG", quality=90, exif=exif)

    assert plain.getvalue() != tagged.getvalue()
    assert sanitise(plain.getvalue(), limits=LIMITS).sha256 == (
        sanitise(tagged.getvalue(), limits=LIMITS).sha256
    )
