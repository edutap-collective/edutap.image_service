"""Configuration reads from the environment and keeps its secrets to itself."""

from datetime import timedelta

from edutap.image_service.settings import Settings


def test_values_come_from_the_prefixed_environment(monkeypatch):
    monkeypatch.setenv("IMAGE_SERVICE_S3_BUCKET", "photos")
    monkeypatch.setenv("IMAGE_SERVICE_IMAGE_API_URL", "http://image-api:9500")
    settings = Settings(_env_file=None)
    assert settings.s3_bucket == "photos"
    assert settings.image_api_url == "http://image-api:9500"


def test_an_unprefixed_variable_is_ignored(monkeypatch):
    """`S3_BUCKET` is set in every one of these compose environments.

    Reading it would make this service follow whichever bucket the pass builder or
    the object browser happened to be pointed at.
    """
    monkeypatch.setenv("S3_BUCKET", "somebody-elses")
    assert Settings(_env_file=None).s3_bucket == "edutap"


def test_the_secret_does_not_appear_in_a_repr(monkeypatch):
    """A settings object reaches a traceback, and a traceback reaches an error tracker."""
    monkeypatch.setenv("IMAGE_SERVICE_S3_SECRET_KEY", "hunter2")
    settings = Settings(_env_file=None)
    assert "hunter2" not in repr(settings)
    assert settings.s3_secret_key.get_secret_value() == "hunter2"


def test_the_reactivation_age_is_six_months_by_default():
    """Long enough that switching back is ordinary, short enough that a face changes."""
    assert Settings(_env_file=None).reactivation_max_age == timedelta(days=180)


def test_the_reactivation_age_is_configured_in_whole_days(monkeypatch):
    """Every value here arrives from the environment as a string.

    A `timedelta` field would take only an ISO-8601 duration, so the obvious
    `…=86400` in an env file takes the process down at startup with `invalid
    character in hour`. Days are what an operator would write anyway.
    """
    monkeypatch.setenv("IMAGE_SERVICE_REACTIVATION_MAX_AGE_DAYS", "30")
    assert Settings(_env_file=None).reactivation_max_age == timedelta(days=30)
