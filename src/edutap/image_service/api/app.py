"""The application, and the one place the collaborators are wired together.

A single `unit_of_work` rather than a dependency per collaborator: a request that
writes has to hold one transaction across the photo tables and the reference in
`person_view`, and handing a router four independently-scoped objects is how that
one transaction quietly becomes several.
"""

import importlib.resources
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ..clients.image_api import ImageApiClient
from ..events import PhotoEvents
from ..ingest import Limits
from ..manifest import manifest
from ..objectstore import ObjectStore
from ..service import PhotoService
from ..settings import Settings, get_settings
from .routers import public, router


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


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance, so a test can build one against
    its own settings without the import having already connected to something.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_dsn)
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

        events = None
        producer = None
        if settings.kafka_bootstrap_servers:
            from aiokafka import AIOKafkaProducer

            producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap_servers)
            await producer.start()
            events = PhotoEvents(
                producer=producer,
                topic_prefix=settings.kafka_topic_prefix,
                producer_version=settings.producer_version,
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
                        limits=Limits(
                            max_bytes=settings.max_upload_bytes,
                            max_edge=settings.max_image_edge,
                        ),
                        placeholder=placeholder,
                        reactivation_max_age=settings.reactivation_max_age,
                        events=events,
                    ),
                )

        app.state.unit_of_work = unit_of_work
        app.state.service_tokens = settings.service_tokens
        app.state.default_expiry_days = settings.default_expiry_days
        try:
            yield
        finally:
            if producer is not None:
                await producer.stop()
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
