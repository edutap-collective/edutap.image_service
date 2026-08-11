"""The object store, and the key layout that is a contract rather than a detail.

The keys are pinned by tests because somebody else reads them. At one deployment a
vendor writes the connector that fetches these objects, and it will follow whatever
we hand it; changing the layout afterwards is not a refactoring but a coordinated
release with a third party.

    <person_uid>/photo/<version>/raw
    <person_uid>/photo/<version>/<recipe>/<variant>
"""

import re

import aioboto3
from botocore.exceptions import ClientError

_BUCKET_ALREADY_PROVISIONED = {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}

#: What a path component may contain. Deliberately narrow: an identifier is never
#: interpreted by this service, but it *is* concatenated into a key, and one
#: carrying a slash or a `..` would write outside the person's prefix.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._@-]+$")


def _checked(component: str, *, what: str) -> str:
    """Refuse a path component that could leave the prefix it belongs to."""
    if not _SAFE_COMPONENT.match(component) or component in {".", ".."}:
        raise ValueError(f"{what} is not usable in an object key: {component!r}")
    return component


def version_prefix(person_uid: str, version: str) -> str:
    """Everything belonging to one version, and nothing else.

    The trailing separator carries the whole safety of the purge path: without it,
    deleting by the prefix of version `abc` would also match `abcd`.
    """
    return f"{_checked(person_uid, what='person_uid')}/photo/{_checked(version, what='version')}/"


def raw_key(person_uid: str, version: str) -> str:
    """Return the key of the sanitised original.

    The only object that must never be delivered. The rule is enforced by the
    service, not by the layout -- it lives in the same prefix as the rest so that
    one purge clears a version completely.
    """
    return f"{version_prefix(person_uid, version)}raw"


def variant_key(person_uid: str, version: str, recipe: str, variant: str) -> str:
    """One rendering of one version.

    `recipe` is its own level so that a changed manifest renders beside the old one
    instead of over it.
    """
    return (
        f"{version_prefix(person_uid, version)}"
        f"{_checked(recipe, what='recipe')}/{_checked(variant, what='variant')}"
    )


class ObjectStore:
    """An S3-compatible bucket, addressed only through the key helpers above."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        """Configure the client for a single bucket."""
        self._endpoint_url = endpoint_url
        self._bucket = bucket
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Store one object."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    async def get(self, key: str) -> bytes:
        """Retrieve one object."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            response = await s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = await response["Body"].read()
            return body

    async def purge_version(self, person_uid: str, version: str) -> int:
        """Delete every object of one version and report how many there were.

        The count is returned rather than discarded because the retention run writes
        it into the review trail: "purged, 6 objects" is auditable, "purged" is not.

        Paginated: a version with many renderings exceeds the 1000 keys one `list`
        response carries, and the missing ones would be silently left behind.
        """
        prefix = version_prefix(person_uid, version)
        deleted = 0
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if not keys:
                    continue
                await s3.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})
                deleted += len(keys)
        return deleted

    async def ensure_bucket(self) -> None:
        """Create the bucket if it is not there. Idempotent."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            try:
                await s3.create_bucket(Bucket=self._bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code", "") not in _BUCKET_ALREADY_PROVISIONED:
                    raise

    async def ping(self) -> None:
        """Raise if the bucket is not reachable."""
        async with self._session.client("s3", endpoint_url=self._endpoint_url) as s3:
            await s3.head_bucket(Bucket=self._bucket)
