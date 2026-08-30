"""The application, and the one place the collaborators are wired together.

A single `unit_of_work` rather than a dependency per collaborator: a request that
writes has to hold one transaction across the photo tables and the reference in
`person_view`, and handing a router four independently-scoped objects is how that
one transaction quietly becomes several.
"""

import importlib.resources
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import version

import httpx2
from edutap.observability_settings import (
    OTLP_ENDPOINT_VARIABLE,
    ObservabilitySettings,
    install_observability,
    instrument_fastapi_safely,
)
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ..clients.image_api import ImageApiClient
from ..events import KafkaPhotoEvents, NoEvents
from ..ingest import Limits
from ..manifest import manifest
from ..objectstore import ObjectStore
from ..service import PhotoService
from ..settings import DatabaseSettings, Settings, get_settings
from .routers import public, router

#: The name telemetry travels under: the distribution name, which is what `pip show`
#: prints and what the sibling services use. In Loki `service_name` is the only
#: indexed label, so one spelling across a span, a log line and the installed
#: distribution is what makes it selectable at all.
SERVICE_NAME = "edutap.image_service"

#: Error reporting, tracing and structured logging, resolved at import.
#:
#: At import and not in `create_app`, deliberately: `install_observability` exists to
#: be called before a service resolves the settings it needs to run, so that a process
#: refusing to start is reported rather than silently absent. `create_app` reads
#: `Settings` and `DatabaseSettings`, either of which can raise on a malformed value.
#:
#: Reading this can never fail for want of a value: no field of `ObservabilitySettings`
#: is required, which is precisely what makes the ordering possible.
#:
#: The prefix is `EDUTAP_`, not this package's own: these fields are defined by an
#: eduTAP package and mean the same thing in every eduTAP service.
observability = ObservabilitySettings()
install_observability(
    observability,
    service_name=SERVICE_NAME,
    service_version=version(SERVICE_NAME),
)


def exports_to_a_collector() -> bool:
    """Whether an exporter will actually carry a span off this process.

    Both conditions are needed: `telemetry_enabled` is the deliberate off switch, and
    the endpoint decides whether anything is listening. The endpoint is read from the
    environment rather than from a field because `OTEL_EXPORTER_OTLP_ENDPOINT` is the
    variable every OpenTelemetry SDK reads by itself -- giving it a second name here
    would ask an operator to set the same address twice.
    """
    return observability.telemetry_enabled and bool(os.environ.get(OTLP_ENDPOINT_VARIABLE))


def _placeholder_bytes(settings: Settings) -> bytes:
    """Load the image served where a person has no active version.

    Read once at startup rather than per request: it is served on the hottest route
    in the service, and it never changes while the process runs.
    """
    if settings.placeholder_path:
        with open(settings.placeholder_path, "rb") as handle:
            return handle.read()
    asset = importlib.resources.files("edutap.image_service") / "assets" / "placeholder.png"
    return asset.read_bytes()


def create_app(
    settings: Settings | None = None,
    database: DatabaseSettings | None = None,
) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance, so a test can build one against
    its own settings without the import having already connected to something.

    `database` is separate from `settings` because the two are read from different
    prefixes and, in a deployment, from different places: the cluster coordinates
    arrive as environment variables, the password as a mounted file. Both default to
    reading their own environment, which is what the entry point wants; a test that
    has a container to point at passes its own.
    """
    settings = settings or get_settings()
    database = database or DatabaseSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # THE HTTP SIDE OF OBSERVABILITY. The call at import configures the *process*
        # -- error reporting, the exporter, structured logging -- and knows nothing
        # about this application, so it produces no request spans by itself.
        # Instrumenting needs the finished route table, which exists by the time a
        # lifespan runs but not while `create_app` is still building the object.
        #
        # `instrument_fastapi_safely` rather than `logfire.instrument_fastapi`: the
        # bare instrumentation writes the raw request path into five span attributes.
        # This service serves person images and its paths carry the identifier, so the
        # house helper substitutes the route template and thereby honours
        # `person_uid_mode` instead of leaving it to be discovered later.
        #
        # ONLY WHEN SOMETHING EXPORTS. Instrumentation patches the application whether
        # or not a receiver exists, and a span nobody collects is work done on every
        # request.
        if exports_to_a_collector():
            instrument_fastapi_safely(app, observability)

        engine = create_async_engine(database.async_url)
        http = httpx2.AsyncClient()
        store = ObjectStore(
            endpoint_url=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
            region=settings.s3_region,
        )
        await store.ensure_bucket()
        image_api = ImageApiClient(
            base_url=settings.image_api_url,
            timeout=settings.image_api_timeout,
            client=http,
        )
        placeholder = _placeholder_bytes(settings)
        chosen = manifest(settings.recipe)

        # One object, not one per unit of work and another for the route that
        # reports them: the numbers a front end is told and the numbers the ingest
        # check enforces have to be the same numbers, not two copies of them.
        events = (
            KafkaPhotoEvents(
                bootstrap_servers=settings.kafka_bootstrap_servers,
                topic_prefix=settings.kafka_topic_prefix,
            )
            if settings.kafka_enabled
            else NoEvents()
        )
        if isinstance(events, KafkaPhotoEvents):
            await events.start()

        enforced = Limits(
            max_bytes=settings.max_upload_bytes,
            max_edge=settings.max_image_edge,
        )

        @asynccontextmanager
        async def unit_of_work() -> AsyncIterator[tuple[AsyncSession, PhotoService]]:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                yield (
                    session,
                    PhotoService(
                        repository=_repository(session, settings),
                        store=store,
                        image_api=image_api,
                        manifest=chosen,
                        limits=enforced,
                        placeholder=placeholder,
                        reactivation_max_age=settings.reactivation_max_age,
                        events=events,
                    ),
                )

        app.state.unit_of_work = unit_of_work
        app.state.service_tokens = settings.service_tokens
        app.state.limits = enforced
        # The retention route reads `default_expiry_days` from here: the caller
        # may omit the deadline, and then the deployment's own number applies.
        app.state.settings = settings
        try:
            yield
        finally:
            if isinstance(events, KafkaPhotoEvents):
                # Flush before the loop goes: a shutdown must not drop a fact
                # the database has already recorded.
                await events.stop()
            await http.aclose()
            await engine.dispose()

    app = FastAPI(
        title="eduTAP Image Service",
        description="Stores, reviews and delivers the photograph of a person.",
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(public)
    return app


def _repository(session: AsyncSession, settings: Settings):  # noqa: ANN202
    """Build the repository. Split out so a test can substitute it without a lifespan."""
    from ..repository import PhotoRepository

    return PhotoRepository(session, origin=settings.public_origin)
