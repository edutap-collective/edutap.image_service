"""The client for edutap.image_api, against a mock transport.

No network in a unit test. What is asserted here is the shape of the requests we
send and how we read what comes back — the two things that break when the other
service changes and nobody tells us.
"""

import httpx2
import pytest

from edutap.image_service.clients.image_api import ImageApiClient, ImageApiUnavailable

REPORT = {
    "passed": True,
    "crop_mode": "face",
    "checks": [
        {
            "name": "exactly_one_face",
            "passed": True,
            "best_effort": False,
            "detail": "one face",
            "measured": {"faces": 1},
        },
        {
            "name": "no_sunglasses",
            "passed": False,
            "best_effort": True,
            "detail": "iris landmarks missing",
            "measured": {},
        },
    ],
    "warnings": ["no_sunglasses"],
    "output": {"width": 512, "height": 512, "format": "png", "image_base64": "aGk="},
}


def client_for(handler) -> ImageApiClient:
    transport = httpx2.MockTransport(handler)
    return ImageApiClient(
        base_url="http://image-api:9500",
        timeout=5.0,
        client=httpx2.AsyncClient(transport=transport),
    )


async def test_validation_sends_a_multipart_upload_to_the_documented_path():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type", "")
        return httpx2.Response(200, json=REPORT)

    await client_for(handler).validate_and_crop(b"bytes", size=512)
    assert seen["url"] == "http://image-api:9500/validate_and_crop/"
    assert seen["content_type"].startswith("multipart/form-data")


async def test_the_report_is_parsed_including_the_decoded_crop():
    """The endpoint answers 200 even when the photograph fails.

    A client that raised on a failed check would make the ordinary case -- somebody
    uploads a picture that is not usable -- look like an outage.
    """
    report = await client_for(lambda request: httpx2.Response(200, json=REPORT)).validate_and_crop(
        b"bytes"
    )
    assert report.passed
    assert report.crop_mode == "face"
    assert report.warnings == ["no_sunglasses"]
    assert report.crop == b"hi"


async def test_a_report_without_a_crop_is_not_an_error():
    """Zero or several faces: `crop_mode` is null and no image comes back."""
    body = REPORT | {
        "passed": False,
        "crop_mode": None,
        "output": {"width": 512, "height": 512, "format": "png", "image_base64": None},
    }
    report = await client_for(lambda request: httpx2.Response(200, json=body)).validate_and_crop(
        b"bytes"
    )
    assert not report.passed
    assert report.crop is None


async def test_the_hard_and_best_effort_checks_stay_apart():
    """`passed` is decided by the hard checks; the heuristics surface as warnings.

    Treating a best-effort failure as a refusal would reject people for wearing a
    headscarf, which the check cannot reliably tell from a hat.
    """
    report = await client_for(lambda request: httpx2.Response(200, json=REPORT)).validate_and_crop(
        b"bytes"
    )
    assert [check.name for check in report.checks if check.best_effort] == ["no_sunglasses"]
    assert report.passed


async def test_cropping_asks_for_the_documented_form_fields_and_returns_the_png():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx2.Response(200, content=b"\x89PNG-payload")

    result = await client_for(handler).crop(b"bytes", mask="circle", height=512, width=512)
    assert seen["url"] == "http://image-api:9500/crop/"
    for field in (b"mask", b"circle", b"aspect_ratio", b"height", b"width"):
        assert field in seen["body"]
    assert result == b"\x89PNG-payload"


async def test_an_unreadable_upload_is_reported_as_a_refusal_not_an_outage():
    """422 is the other service saying the file is unusable, which is our caller's problem."""
    with pytest.raises(ValueError):
        await client_for(lambda request: httpx2.Response(422, json={})).validate_and_crop(b"x")


async def test_a_transport_error_becomes_one_named_exception():
    """So the caller answers 502 rather than leaking an httpx type through its API."""

    def handler(request):
        raise httpx2.ConnectError("refused")

    with pytest.raises(ImageApiUnavailable):
        await client_for(handler).validate_and_crop(b"x")


async def test_a_server_error_is_also_an_unavailability():
    with pytest.raises(ImageApiUnavailable):
        await client_for(lambda request: httpx2.Response(503)).validate_and_crop(b"x")
