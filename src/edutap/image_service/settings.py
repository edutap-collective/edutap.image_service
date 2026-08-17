"""Runtime configuration, all of it in one place.

Every value a deployment can differ on is here rather than scattered through
`os.getenv` calls, which is what makes the set of things an operator has to decide
readable in one file.
"""

import json
from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Any

from edutap.db_definitions.settings import ASYNC_DRIVER, ClusterSettings
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Where an orchestrator mounts secrets. pydantic-settings reads from here, so a
#: password can arrive as a file instead of an environment variable -- and then it is
#: never in the process environment at all, which means never in `docker inspect` and
#: never in a frame local an error tracker collects.
#:
#: Two things worth knowing before wiring this up, both measured against
#: pydantic-settings 2.15.0 rather than assumed:
#:
#: * **The file name carries the `env_prefix`.** The database password is read from
#:   `/run/secrets/IMAGE_SERVICE_DB_password`, not `.../password`. A secret mounted
#:   under the bare field name is silently ignored.
#: * **A missing directory is harmless.** pydantic-settings emits a `UserWarning` and
#:   falls back to the environment, so a developer without `/run/secrets` is not
#:   blocked -- which is why this can be the default rather than a deployment switch.
SECRETS_DIR = "/run/secrets"


class DatabaseSettings(ClusterSettings):
    """Where the photographs are recorded, prefix `IMAGE_SERVICE_DB_`.

    Everything about *reaching* a cluster -- naming every node, asking for the one
    that accepts writes, spelling TLS the way each driver wants it -- comes from
    :class:`edutap.db_definitions.settings.ClusterSettings`. This package already
    depended on `edutap.db_definitions` for the table definitions and built its own
    connection string beside them, which is how it ended up with a single `host` and
    a password in one string.

    **The four fields are re-declared without defaults on purpose.** The base gives
    them sensible ones for a development machine (`postgres`, `edutap`), and a
    default is exactly wrong here: a deployment that misspells the prefix would then
    start cleanly and write into *some* database rather than abort.

    A prefix of its own, and not the service's: `user` and `database` would otherwise
    collide with names that mean something else in :class:`Settings`.
    """

    model_config = SettingsConfigDict(
        env_prefix="IMAGE_SERVICE_DB_",
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=SECRETS_DIR,
        extra="ignore",
    )

    #: Every node of the cluster, comma separated -- see :class:`ClusterSettings`.
    hosts: str
    database: str
    user: str
    password: SecretStr

    @property
    def async_url(self) -> str:
        """Return the DSN for the async driver this service uses.

        Note what this spares the deployment: the hosts move into repeated `host=`
        query parameters with an explicit port each, which is the only form
        SQLAlchemy hands to asyncpg as a *list*. The obvious `@a,b,c/database`
        arrives as one hostname and fails with a name lookup error for a host that
        does not exist.
        """
        return self.url(ASYNC_DRIVER)


class Settings(BaseSettings):
    """Configuration of the person photo service, prefix `IMAGE_SERVICE_`."""

    model_config = SettingsConfigDict(
        env_prefix="IMAGE_SERVICE_",
        env_file=".env",
        secrets_dir=SECRETS_DIR,
    )

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

    service_tokens: Annotated[dict[str, str], NoDecode] = {}
    """Tokens this service accepts, keyed by the name of the calling service.

    Keyed rather than a bare list so the review trail can record *which* service
    acted, and so one of them can be rotated without invalidating the others. An
    empty mapping refuses every authenticated route -- the safe direction for a
    deployment where nobody set them.

    Best supplied as a mounted file, `/run/secrets/IMAGE_SERVICE_service_tokens`,
    holding the same JSON. Every value in it is a credential, and an environment
    variable is readable by anyone who can run `docker inspect`.

    `NoDecode` because the decoding has to happen *here* rather than in the settings
    source -- see :meth:`_tolerate_an_unset_value` for the failure that forces it.
    """

    @field_validator("service_tokens", mode="before")
    @classmethod
    def _tolerate_an_unset_value(cls, value: Any) -> Any:
        """Read the mapping, and treat a set-but-empty value as no mapping at all.

        A compose file interpolating `${IMAGE_SERVICE_SERVICE_TOKENS}` without a
        `:-` default produces the empty string, and an empty string is *set* -- so
        the field default never applies and the raw value reaches a JSON parser.
        Left to the settings source that is a `SettingsError` during construction,
        which is to say the process does not start; under a restart policy it is a
        loop, every nine seconds, with a traceback that names JSON rather than the
        variable that is missing.

        Refusing every authenticated route is the documented safe direction and the
        one an operator can diagnose: the service answers, and it answers 401.

        This is why the field carries `NoDecode`. The source decodes complex types
        *before* any validator runs, so a validator alone would never see the value.
        """
        if value is None:
            return {}
        if isinstance(value, str):
            if not value.strip():
                return {}
            return json.loads(value)
        return value

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
