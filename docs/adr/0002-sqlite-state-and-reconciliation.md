# ADR 0002 - SQLite as source of truth, reconciled against Docker

- Status: accepted
- Date: 2026-11-20

## Context

The control plane must know which environments exist, enforce quotas without
races, survive restarts, and never leak containers when something fails
half-way. Running an extra database server for the platform would cost memory
(the control plane is capped at 96 MB) and setup steps.

## Decision

- Store state in SQLite (WAL mode) on a named volume. Reservations run in a
  `BEGIN IMMEDIATE` transaction that checks the slug and all quotas and inserts
  the row atomically.
- A partial unique index on `slug WHERE status NOT IN ('DROPPED','FAILED')`
  guarantees one live environment per slug while keeping history.
- The audit table is append-only, enforced by triggers.
- Docker is the observed state; a periodic reconciler (level-triggered, not
  event-driven) converges it to the desired state: expiry, stuck provisioning,
  drift, teardown retries with bounded exponential backoff, orphan removal after
  a grace period. All Docker operations are idempotent (404 tolerated).
- Every resource carries labels (`managed-by=ephemeral-idp`, `ephemeral.env-id`,
  owner, slug, expiry, role) so orphans can be found without the database.

## Consequences

- Restarts are safe: the first pass runs immediately and fixes whatever was in flight.
- Single writer process; multi-replica would need Postgres and leader election
  (roadmap).
- Polling adds up to `RECONCILE_INTERVAL_SECONDS` of latency to expiry.
