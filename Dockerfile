# Two stages: build installs the package, the runtime image carries only the result.
# Plain `pip install` on purpose -- `uv` belongs in the development environment, not
# in a container image.
FROM python:3.14-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# git, because two runtime dependencies are PEP 508 direct references to a git
# repository. No cleanup afterwards: this stage is discarded, only site-packages and
# the console scripts are copied out of it, so nothing installed here reaches the
# runtime image.
#
# The `kafka` extra comes along: publishing events is optional per deployment, but a
# published image that cannot do it would force a second image for the deployments
# that can, and the flag alone already decides at runtime.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git; \
    pip install --no-cache-dir ".[kafka]"

FROM python:3.14-slim
# The interpreter of the base image is 3.14, so this is where `pip install` put the
# package in the build stage. Changing the base image tag means changing this path.
COPY --from=build /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
RUN useradd --create-home --uid 10001 app
WORKDIR /app
USER app
EXPOSE 8000
# --factory: the application is built by create_app(), not exposed as a module-level
# object, so that settings are read when the process starts rather than on import.
CMD ["uvicorn", "edutap.image_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
