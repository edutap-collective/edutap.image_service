"""Acceptance of an uploaded file, before anything else touches it.

Two jobs, and the order between them is the point: refuse what should not be here,
then strip what should not be kept. A decompression bomb has to be caught by a
check on the way in, not survived by a decoder — which is why the byte limit is
tested before the file is opened at all, and the pixel limit before it is loaded.

`edutap.image_api` analyses and transforms; it does not sanitise, and it never sees
the file until it has passed through here.
"""

import hashlib
import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

#: Formats a person may upload. HEIC/HEIF is on the list because it is what an
#: iPhone produces by default; leaving it off would fail a large share of uploads
#: for no reason a person could act on. Everything else is refused without
#: negotiation rather than quietly converted -- the set a decoder accepts is far
#: wider than the set anybody meant to send.
ACCEPTED_FORMATS = frozenset({"JPEG", "PNG", "MPO", "HEIF", "HEIC"})

#: EXIF tags that carry a rights claim. Recorded for a reviewer, never evaluated.
_COPYRIGHT = 0x8298
_ARTIST = 0x013B


class UploadRefused(Exception):
    """Base of every refusal, so a caller may answer with one status."""


class FileTooLarge(UploadRefused):
    """More bytes than the deployment accepts."""


class ImageTooLarge(UploadRefused):
    """More pixels than the deployment accepts, whatever the file size."""


class UnsupportedFormat(UploadRefused):
    """Not an image, or not one of the accepted formats."""


@dataclass(frozen=True)
class Limits:
    """What a deployment accepts. Both are needed; neither implies the other."""

    max_bytes: int = 10 * 1024 * 1024
    max_edge: int = 8000


@dataclass(frozen=True)
class Sanitised:
    """The image as the system will keep it, and the hash that identifies it."""

    data: bytes
    width: int
    height: int
    sha256: str


def sanitise(upload: bytes, *, limits: Limits) -> Sanitised:
    """Refuse what does not belong, and return the image without its metadata.

    The re-encoding is the sanitisation: nothing is copied across from the original
    file, so EXIF, XMP, colour-profile comments and anything else a camera wrote
    simply do not exist in the result. Stripping tag by tag would leave whatever the
    next phone model invents.

    The hash is taken over the *result*. Two uploads that differ only in metadata
    are the same photograph, and hashing the file would make the review trail claim
    two distinct images had been checked.
    """
    if len(upload) > limits.max_bytes:
        raise FileTooLarge(f"{len(upload)} bytes exceeds the limit of {limits.max_bytes}")

    try:
        # `open` reads the header only; the pixels are not decoded until `load`,
        # which is what lets the edge check run before any allocation happens.
        probe = Image.open(io.BytesIO(upload))
    except UnidentifiedImageError as exc:
        raise UnsupportedFormat("the upload is not a readable image") from exc

    if probe.format not in ACCEPTED_FORMATS:
        raise UnsupportedFormat(f"{probe.format} is not an accepted upload format")
    if max(probe.size) > limits.max_edge:
        raise ImageTooLarge(f"{probe.size} exceeds the edge limit of {limits.max_edge}")

    image = probe.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    data = buffer.getvalue()
    return Sanitised(
        data=data,
        width=image.width,
        height=image.height,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def rights_metadata(upload: bytes) -> dict[str, str]:
    """Report a rights claim found in the upload, for a reviewer to read.

    Deliberately *reported*, not evaluated. Its absence proves nothing — most
    uploads carry none, and any phone tool strips it — and its presence says only
    that somebody asserts a right, not whether a licence was granted. Blocking on it
    would refuse constantly and wrongly; ignoring it would throw away the one hint a
    reviewer can act on.

    What carries legal weight is the declaration the uploader makes, which the
    caller records against the version.
    """
    try:
        exif = Image.open(io.BytesIO(upload)).getexif()
    except (UnidentifiedImageError, OSError):
        return {}
    claims = {
        "copyright": exif.get(_COPYRIGHT),
        "artist": exif.get(_ARTIST),
    }
    return {key: str(value).strip() for key, value in claims.items() if value}
