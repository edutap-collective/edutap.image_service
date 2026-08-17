# edutap.image_service

Stores, reviews and delivers the photograph of a person — for identity cards and for
the wallet passes that carry them.

A person uploads a photo. The service has it validated by
[`edutap.image_api`](https://github.com/edutap-collective/edutap.image_api), keeps it
as an immutable version, and lets a reviewer approve or reject it. Exactly one
version per person is active at a time, and that is what cards and passes show.
Where there is none, the same URL delivers a recognisable placeholder — which
matters more than it sounds, because a wallet provider bakes that URL into an issued
pass and will fetch it long after.

**Status: in construction.** The design is settled and the contract tables are
merged; the service is being built slice by slice. See
[the plan](superpowers/plans/2026-08-11-person-photo-service.md) for the order and
what has landed.

## Delimitation

* `edutap.image_service` (this package) — storage, review and delivery.
* `edutap.image_api` — analysis and transformation: biometric checks, face-centred
  crops, masks. Stateless.

The two are separate on purpose: transformation is a computation, while delivery
needs storage, access control and cache-friendly URLs.

## What it is deliberately ignorant of

Wallet passes, card types, institutional roles, mail templates. It reports facts —
whether a person has a real photograph, how it was verified, what that says about
its provenance — and whoever issues a card decides what to do with them. A generic
service that grows one institution's policy stops being adoptable by the next one.

## Running it

```console
docker build -t edutap-image-service .
docker run --rm -p 8000:8000 \
  -e IMAGE_SERVICE_DB_HOSTS=pg-a,pg-b,pg-c \
  -e IMAGE_SERVICE_DB_DATABASE=edutap \
  -e IMAGE_SERVICE_DB_USER=edutap \
  -e IMAGE_SERVICE_DB_SSLMODE=verify-full \
  -e PGSSLROOTCERT=/run/certs/ca.pem \
  -e IMAGE_SERVICE_S3_ENDPOINT=http://... \
  -v /path/to/secrets:/run/secrets:ro \
  edutap-image-service
```

Two stages; the runtime image carries the installed package and nothing that built
it. The `kafka` extra is installed even though publishing events is optional --
otherwise the deployments that publish would need a second image, and the flag
already decides at runtime.

### Secrets are files, not variables

Three values are credentials and are read from `/run/secrets` rather than from the
environment, so that they appear in no `docker inspect` and in no frame local an
error tracker collects. **The file name carries the prefix of the settings class it
belongs to:**

| File | What it holds |
| --- | --- |
| `/run/secrets/IMAGE_SERVICE_DB_password` | the database password |
| `/run/secrets/IMAGE_SERVICE_s3_secret_key` | the object store secret |
| `/run/secrets/IMAGE_SERVICE_service_tokens` | the caller → token mapping, as JSON |

A secret mounted under the bare field name is silently ignored. Each may still be
given as an environment variable, which is what a development machine does; a
missing `/run/secrets` is harmless and falls back to the environment.

### Reaching a cluster

`IMAGE_SERVICE_DB_HOSTS` takes every node, comma separated, each optionally with its
own port. All of them reach the driver, and `target_session_attrs` (default
`read-write`) is what finds the one that accepts writes — naming a single node is
what breaks at the next failover, and a connection to a replica *succeeds* and fails
only at the first write.

TLS is spelled `IMAGE_SERVICE_DB_SSLMODE`. The CA that verifies the server reaches
asyncpg **only** through the environment variable `PGSSLROOTCERT`, never as a
connect argument — so a deployment that requires `verify-full` sets both.

## Development

```console
make venv          # create .venv and install with the dev extra
make test-local    # unit tests; no database, no bucket
make lint          # ruff check, ruff format --check, ty
```

CI calls the same targets rather than repeating their commands, so a run that is
green here is green there.

## Documentation

* [Design record](superpowers/specs/2026-08-11-person-photo-service-design.md) — the
  domain model, the storage layout, the tables, the API surface, and the alternative
  that was rejected at each fork.
* [Implementation plan](superpowers/plans/2026-08-11-person-photo-service.md) — the
  slices and their order.
