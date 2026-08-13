"""Runtime configuration, all of it in one place.

Every value a deployment can differ on is here rather than scattered through
`os.getenv` calls, which is what makes the set of things an operator has to decide
readable in one file.
"""

from datetime import timedelta
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration of the person photo service, prefix `IMAGE_SERVICE_`."""

    model_config = SettingsConfigDict(env_prefix="IMAGE_SERVICE_", env_file=".env")

    database_dsn: str = "postgresql+asyncpg://edutap@localhost/edutap"

    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "edutap"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")

    image_api_url: str = "http://localhost:9500"
    image_api_timeout: float = 30.0

    kafka_topic_prefix: str = "edutap.dev"

    #: Off by default, and `aiokafka` lives in the optional `kafka` extra: a
    #: deployment that does not publish events must not have to install a broker
    #: client to run the service.
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = ""

    public_origin: str = "http://localhost:8000"
    """External origin of this service.

    The `current` URL is written into `person_view.photo` and from there into issued
    passes, so it has to be the address a wallet provider can reach -- not the one
    this process happens to bind.
    """

    recipe: str = "default"
    """Which manifest renders a version's derivatives.

    A name rather than the sizes themselves: changing sizes means adding a manifest
    beside the old one, so renderings of both can coexist while passes catch up.
    """

    reactivation_max_age_days: int = 180
    """How long an approval keeps a version reactivable without a fresh review.

    Six months by default. Anchored to how long a photograph stays recognisable
    rather than to anything a database enforces, so a deployment with different card
    validity moves it.

    Days rather than a `timedelta`: every value here arrives as a string from the
    environment, and pydantic parses a `timedelta` only from an ISO-8601 duration.
    An operator writing the obvious `…_MAX_AGE=86400` in an env file would take the
    process down at startup with `invalid character in hour`.
    """

    @property
    def reactivation_max_age(self) -> timedelta:
        """The configured age as the state machine wants it."""
        return timedelta(days=self.reactivation_max_age_days)

    default_expiry_days: int = 14
    """Fallback for a retention run that does not supply its own deadline.

    The number is the operator's policy, and the caller normally passes it. The
    default exists so a deployment without a scheduler still forgets rejected
    photographs eventually -- a photo service that never forgets anything on its own
    is not one anybody else should adopt.
    """

    service_tokens: dict[str, str] = {}
    """Tokens this service accepts, keyed by the name of the calling service.

    Keyed rather than a bare list so the review trail can record *which* service
    acted, and so one of them can be rotated without invalidating the others. An
    empty mapping refuses every authenticated route -- the safe direction for a
    deployment where nobody set them.
    """

    max_upload_bytes: int = 10 * 1024 * 1024
    """Refused before the file is opened, so a bomb never reaches a decoder."""

    max_image_edge: int = 8000
    """Refused before the pixels are loaded. The byte limit alone does not catch it."""

    placeholder_path: str = ""
    """Image served where a person has no active version.

    Empty means the bundled one. Whatever a deployment substitutes has to *look*
    like a placeholder: a card without a verified photograph must not read like a
    card with one.
    """


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()
