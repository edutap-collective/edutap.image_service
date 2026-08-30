"""What the service reports, and when it stops paying for reporting nobody collects.

The wiring itself lives in `edutap.observability_settings` and is tested there. What
these tests hold is this package's half of the contract: that the call happens at all,
that it happens before anything else resolves settings, and that instrumentation is
gated on there actually being a receiver.
"""

from importlib.metadata import version

from edutap.observability_settings import ObservabilitySettings

from edutap.image_service.api import app as app_module


def test_service_names_itself_by_its_distribution():
    """One spelling across a span, a log line and the installed distribution.

    In Loki `service_name` is the only indexed label; a name that disagrees with the
    distribution is one nobody thinks to select on.
    """
    assert app_module.SERVICE_NAME == "edutap.image_service"
    assert version(app_module.SERVICE_NAME)


def test_observability_is_installed_at_import():
    """The settings object exists before any application is built, not on first use.

    `create_app` reads `Settings` and `DatabaseSettings`, either of which can raise on
    a malformed value. Installing reporting first is what makes that failure visible
    rather than the one failure nobody ever sees.
    """
    assert app_module.observability is not None


def test_observability_reads_the_shared_edutap_prefix(monkeypatch):
    """`EDUTAP_`, not this package's own prefix.

    A deployment sets `EDUTAP_ENVIRONMENT` once for a whole stack and means the same
    thing in every eduTAP service. Reading it under a private name would make this
    service the one that quietly reports into the wrong environment.
    """
    monkeypatch.setenv("EDUTAP_ENVIRONMENT", "staging")
    assert ObservabilitySettings().environment == "staging"


def test_instrumentation_is_gated_on_there_being_a_receiver(monkeypatch):
    """Instrumentation costs something on every request; it is not installed blind.

    It patches the application whether or not anything collects what it produces, so
    the gate is what keeps an unmonitored deployment from paying for spans nobody
    reads.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert not app_module.exports_to_a_collector()

    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4318")
    assert app_module.exports_to_a_collector()

    monkeypatch.setenv("EDUTAP_TELEMETRY_ENABLED", "false")
    app_module.observability = ObservabilitySettings()
    assert not app_module.exports_to_a_collector()
