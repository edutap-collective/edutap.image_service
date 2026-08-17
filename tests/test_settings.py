"""Configuration reads from the environment and keeps its secrets to itself."""

from datetime import timedelta

from sqlalchemy.dialects.postgresql.asyncpg import dialect as asyncpg_dialect
from sqlalchemy.engine import make_url

from edutap.image_service.settings import DatabaseSettings, Settings


def test_values_come_from_the_prefixed_environment(monkeypatch):
    monkeypatch.setenv("IMAGE_SERVICE_S3_BUCKET", "photos")
    monkeypatch.setenv("IMAGE_SERVICE_IMAGE_API_URL", "http://image-api:9500")
    settings = Settings(_env_file=None, _secrets_dir=None)
    assert settings.s3_bucket == "photos"
    assert settings.image_api_url == "http://image-api:9500"


def test_an_unprefixed_variable_is_ignored(monkeypatch):
    """`S3_BUCKET` is set in every one of these compose environments.

    Reading it would make this service follow whichever bucket the pass builder or
    the object browser happened to be pointed at.
    """
    monkeypatch.setenv("S3_BUCKET", "somebody-elses")
    assert Settings(_env_file=None, _secrets_dir=None).s3_bucket == "edutap"


def test_the_secret_does_not_appear_in_a_repr(monkeypatch):
    """A settings object reaches a traceback, and a traceback reaches an error tracker."""
    monkeypatch.setenv("IMAGE_SERVICE_S3_SECRET_KEY", "hunter2")
    settings = Settings(_env_file=None, _secrets_dir=None)
    assert "hunter2" not in repr(settings)
    assert settings.s3_secret_key.get_secret_value() == "hunter2"


def test_the_reactivation_age_is_six_months_by_default():
    """Long enough that switching back is ordinary, short enough that a face changes."""
    assert Settings(_env_file=None, _secrets_dir=None).reactivation_max_age == timedelta(days=180)


def test_the_reactivation_age_is_configured_in_whole_days(monkeypatch):
    """Every value here arrives from the environment as a string.

    A `timedelta` field would take only an ISO-8601 duration, so the obvious
    `…=86400` in an env file takes the process down at startup with `invalid
    character in hour`. Days are what an operator would write anyway.
    """
    monkeypatch.setenv("IMAGE_SERVICE_REACTIVATION_MAX_AGE_DAYS", "30")
    assert Settings(_env_file=None, _secrets_dir=None).reactivation_max_age == timedelta(days=30)


def test_the_database_settings_read_their_own_prefix(monkeypatch):
    """`IMAGE_SERVICE_DB_`, not `IMAGE_SERVICE_`.

    A prefix of its own keeps `user` and `database` from colliding with fields of
    the service settings that mean something else entirely.
    """
    monkeypatch.setenv("IMAGE_SERVICE_DB_HOSTS", "pg-a,pg-b")
    monkeypatch.setenv("IMAGE_SERVICE_DB_DATABASE", "edutap_production")
    monkeypatch.setenv("IMAGE_SERVICE_DB_USER", "edutap_production")
    monkeypatch.setenv("IMAGE_SERVICE_DB_PASSWORD", "hunter2")
    settings = DatabaseSettings(_env_file=None, _secrets_dir=None)
    assert settings.hosts == "pg-a,pg-b"
    assert settings.database == "edutap_production"


def test_the_async_url_names_every_node_with_its_port(monkeypatch):
    """Every node reaches the driver, and each entry carries an explicit port.

    The obvious `@a,b,c/db` form reaches asyncpg as ONE hostname -- the connection
    then fails with a name lookup error for a host that does not exist. Only the
    repeated `host=` query parameter expresses a cluster.
    """
    monkeypatch.setenv("IMAGE_SERVICE_DB_HOSTS", "pg-a,pg-b:5433,pg-c")
    monkeypatch.setenv("IMAGE_SERVICE_DB_DATABASE", "edutap")
    monkeypatch.setenv("IMAGE_SERVICE_DB_USER", "edutap")
    monkeypatch.setenv("IMAGE_SERVICE_DB_PASSWORD", "hunter2")
    url = DatabaseSettings(_env_file=None, _secrets_dir=None).async_url

    # Asserted on what the driver is handed, not on how the string is spelled:
    # `render_as_string` percent-encodes the colon, and a test that pinned the
    # spelling would fail on a change that harms nobody while still passing on the
    # `@a,b,c/database` form that breaks everything.
    _, connect_kwargs = asyncpg_dialect().create_connect_args(make_url(url))
    assert connect_kwargs["host"] == ["pg-a", "pg-b", "pg-c"]
    assert connect_kwargs["port"] == [5432, 5433, 5432]
    assert connect_kwargs["target_session_attrs"] == "read-write"


def test_the_database_password_arrives_as_a_mounted_file(tmp_path, monkeypatch):
    """A password in a file is in no `docker inspect` and in no frame local.

    The file name carries the prefix. A secret mounted under the bare field name is
    silently ignored -- measured, not assumed.
    """
    monkeypatch.setenv("IMAGE_SERVICE_DB_HOSTS", "pg-a")
    monkeypatch.setenv("IMAGE_SERVICE_DB_DATABASE", "edutap")
    monkeypatch.setenv("IMAGE_SERVICE_DB_USER", "edutap")
    (tmp_path / "IMAGE_SERVICE_DB_password").write_text("from-a-file")
    settings = DatabaseSettings(_env_file=None, _secrets_dir=tmp_path)
    assert settings.password.get_secret_value() == "from-a-file"
    assert "from-a-file" not in repr(settings)


def test_the_s3_secret_arrives_as_a_mounted_file(tmp_path):
    """Same mechanism for the object store, so nothing secret sits in the environment."""
    (tmp_path / "IMAGE_SERVICE_s3_secret_key").write_text("from-a-file")
    settings = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert settings.s3_secret_key.get_secret_value() == "from-a-file"


def test_the_service_tokens_arrive_as_a_mounted_file(tmp_path):
    """The whole mapping as one JSON document, out of the environment."""
    (tmp_path / "IMAGE_SERVICE_service_tokens").write_text('{"backend": "t1", "worker": "t2"}')
    settings = Settings(_env_file=None, _secrets_dir=tmp_path)
    assert settings.service_tokens == {"backend": "t1", "worker": "t2"}


def test_an_empty_service_tokens_value_is_an_empty_mapping(monkeypatch):
    """A set-but-empty variable must refuse callers, not take the process down.

    `${VAR}` without a `:-` default interpolates to the empty string and is still
    *set*, so the field default never applies and the value reaches the JSON parser.
    That killed the deployed service in a restart loop every nine seconds. An empty
    mapping is the documented safe direction: it refuses every authenticated route.
    """
    monkeypatch.setenv("IMAGE_SERVICE_SERVICE_TOKENS", "")
    assert Settings(_env_file=None, _secrets_dir=None).service_tokens == {}
