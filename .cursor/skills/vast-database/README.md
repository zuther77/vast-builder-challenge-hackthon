# Vast Database (vss2)

Raw access to the team’s VastDB with the `vastdb` Python SDK — **not** the JWT retrieval API.

Connect to the **data VIP** from `S3_ENDPOINT` in `/config/<team>.config`. Reads may optionally use the Query Engine VIP from the backend K8s secret. Writes always use the data VIP.

## Skills

| Skill | Purpose |
|-------|---------|
| [vastdb-read](vastdb-read/SKILL.md) | Catalog + select (data VIP / optional QE) |
| [vastdb-write](vastdb-write/SKILL.md) | Create schema/table + insert (data VIP only) |
