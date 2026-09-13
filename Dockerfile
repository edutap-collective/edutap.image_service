# Two stages: build installs the package, the runtime image carries only the result.
# Plain `pip install` on purpose -- `uv` belongs in the development environment, not
# in a container image.
# ONE PLACE FOR THE PYTHON VERSION. Both stages and the copied path below are
# derived from this argument, because they have to agree: `pip install` puts the
# package under the interpreter's own directory, and the runtime stage copies
# from exactly there.
#
# Keeping them in separate literals is what broke the build of
# edutap.webhook_heidi on 2026-09-13: Renovate raised the `FROM python:` lines
# -- correctly, that is its job -- while the hard-coded
# `/usr/local/lib/python3.13/site-packages` in the COPY stayed behind, and the
# build failed with `failed to compute cache key: ... not found`. It failed at
# the next build, not at the merge.
#
# This file had the same shape and would have broken the same way at the next
# version bump. Nothing was wrong with it today -- that is exactly what made it
# worth changing.
#
# renovate: datasource=docker depName=python versioning=docker
ARG PYTHON_VERSION=3.14

FROM python:${PYTHON_VERSION}-slim AS build
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

FROM python:${PYTHON_VERSION}-slim
# An ARG declared before the first FROM is outside every build stage; naming
# it again here brings it into this one. Without this line the substitution
# below would silently expand to an empty string.
ARG PYTHON_VERSION
# The interpreter of the base image is 3.14, so this is where `pip install` put the
# package in the build stage. Changing the base image tag means changing this path.
COPY --from=build /usr/local/lib/python${PYTHON_VERSION}/site-packages /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=build /usr/local/bin /usr/local/bin
RUN useradd --create-home --uid 10001 app
WORKDIR /app
USER app
EXPOSE 8000
# --factory: the application is built by create_app(), not exposed as a module-level
# object, so that settings are read when the process starts rather than on import.
CMD ["uvicorn", "edutap.image_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
