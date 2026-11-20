# ADR 0004 - Guard rails: TTL, quotas, memory budget and cost estimates

- Status: accepted
- Date: 2026-11-20

## Context

Self-service without limits turns into forgotten environments filling the host.
Limits must be predictable, explained to the user, and visible to whoever pays.

## Decision

- Every environment has a TTL (default 24h, between `MIN_TTL_SECONDS` and
  `MAX_TTL_HOURS`) and a hard deadline of `created_at + MAX_TOTAL_LIFETIME_HOURS`
  (72h) that extensions cannot exceed; capped extensions say so in the response.
- Quotas, checked atomically at reservation: `MAX_ENVS_PER_OWNER=3`,
  `MAX_ENVS_TOTAL=10`, `MAX_TOTAL_MEMORY_MB=1024` (sum of live env caps).
  Rejections are `429` problem+json with a reason and `Retry-After` when the
  earliest expiry is known; the CLI exits with code 3.
- Fixed sizes per environment (app 64 MB, Postgres 128 MB, enforced as
  memory = memory+swap) make the budget exact rather than estimated.
- Cost estimate = memory GB × hours × `COST_PER_GB_HOUR`, shown on each
  environment, summed in `/v1/finops/summary` and accumulated in
  `ephemeral_cost_units_total`.
- Prometheus metrics never use per-slug labels to keep cardinality bounded.

## Consequences

- Users learn the limits from the error, not from an admin.
- Unit prices are illustrative; real chargeback would map them to actual
  infrastructure costs.
- Disk usage of database volumes is not budgeted (documented residual risk).
