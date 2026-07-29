# edutap.image_service

Stores and serves person-related images (for example ID photos) to web wallets.

**Status: planned.** To be designed generically; deployments that currently run a
site-specific image backend switch over afterwards.

## Delimitation

* `edutap.image_service` (this package) — storage and delivery of the images a
  wallet pass references.
* `edutap.image_api` — analysis and transformation of images (biometric checks,
  auto-crop for ID photos).

The two are separate on purpose: transformation is a stateless computation,
delivery needs storage, access control and cache-friendly URLs.
