"""Client for `edutap.image_api`, the stateless half of the pair.

That service analyses and transforms; this one stores and delivers. The split is
deliberate — transformation is a computation, delivery needs storage, access
control and cache-friendly URLs — and this module is the whole of the seam
between them.

`httpx2` rather than `httpx`: it is the house standard for REST clients across
these packages, and starlette prefers it too.
"""

import base64
from typing import Any

import httpx2
from pydantic import BaseModel


class ImageApiUnavailable(Exception):
    """The other service could not be reached, or failed on its own account.

    Named rather than letting an `httpx2` type escape, so a caller can answer 502
    without importing the transport library to catch it.
    """


class CheckResult(BaseModel):
    """One biometric check and what it measured."""

    name: str
    passed: bool
    best_effort: bool
    detail: str = ""
    measured: dict[str, Any] = {}


class ValidationReport(BaseModel):
    """The verdict on a portrait, plus the face-centred crop where there is one.

    `passed` is decided by the hard checks alone. The best-effort ones — sunglasses,
    headwear — surface in `warnings` and never refuse anybody, because the heuristic
    cannot reliably tell a headscarf from a hat.
    """

    passed: bool
    crop_mode: str | None
    checks: list[CheckResult] = []
    warnings: list[str] = []
    crop: bytes | None = None


class ImageApiClient:
    """Calls `edutap.image_api` over a shared, injected HTTP client."""

    def __init__(self, *, base_url: str, timeout: float, client: httpx2.AsyncClient) -> None:
        """Hold the connection settings and the client the application shares."""
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client

    async def validate_and_crop(self, image: bytes, *, size: int = 512) -> ValidationReport:
        """Run the biometric checks and take a face-centred square crop.

        A failed check is not an error here. The endpoint answers 200 whenever the
        image was readable, and a client that raised on `passed: false` would make
        the ordinary case — somebody uploads an unusable picture — look like an
        outage.
        """
        response = await self._post(
            "/validate_and_crop/",
            files={"file": ("upload", image, "application/octet-stream")},
            data={"size": str(size)},
        )
        payload = response.json()
        output = payload.get("output") or {}
        encoded = output.get("image_base64")
        return ValidationReport(
            passed=payload["passed"],
            crop_mode=payload.get("crop_mode"),
            checks=payload.get("checks", []),
            warnings=payload.get("warnings", []),
            crop=base64.b64decode(encoded) if encoded else None,
        )

    async def crop(
        self,
        image: bytes,
        *,
        mask: str = "none",
        aspect_ratio: str = "square",
        height: int = 512,
        width: int | str = "auto",
    ) -> bytes:
        """Render one variant and return it.

        Always a PNG: the endpoint has no format parameter. Where a deployment wants
        a smaller unmasked variant, the caller re-encodes — masked ones have to stay
        PNG for the alpha channel either way.
        """
        response = await self._post(
            "/crop/",
            files={"file": ("upload", image, "application/octet-stream")},
            data={
                "mask": mask,
                "aspect_ratio": aspect_ratio,
                "height": str(height),
                "width": str(width),
            },
        )
        return response.content

    async def _post(self, path: str, **kwargs: Any) -> httpx2.Response:
        """Post to the service, mapping its failures onto ours.

        422 is the other service saying the file is unusable — that belongs to
        whoever sent it, so it surfaces as a `ValueError` rather than as an
        unavailability. Everything else that goes wrong is an outage from our side.
        """
        try:
            response = await self._client.post(
                f"{self._base_url}{path}", timeout=self._timeout, **kwargs
            )
        except httpx2.HTTPError as exc:
            raise ImageApiUnavailable(f"image_api did not answer: {exc}") from exc
        if response.status_code == 422:
            raise ValueError("image_api refused the upload as unreadable")
        if response.status_code >= 400:
            raise ImageApiUnavailable(f"image_api answered {response.status_code}")
        return response
