# Redis topologies for `docker-enterprise`

Systemu's `docker-enterprise` mode uses Redis for two independent things:

1. **Huey broker** — work queue between dashboard (producer) and workers
   (consumers).  Read/written by `huey_consumer` and `Huey.execute()`.
2. **Supervisor durable queue** — `RedisPriorityQueue` stores the
   priority-ordered submission rows, the running set, heartbeats, and the
   dead-letter list.  Read/written by `Supervisor` and `recover_orphans`.

Both share the same connection URL (`SYSTEMU_REDIS_URL`).  This page documents
the URL shapes the codebase actually supports.

---

## Standalone Redis (default in compose)

```env
SYSTEMU_REDIS_URL=redis://:password@redis:6379/0
```

This is what `install.py --mode docker-enterprise` writes.  Suitable for:

- Dev / staging / single-host production
- Cloud-managed Redis without TLS (rare — most managed offerings require TLS)

No special configuration; redis-py and Huey both pick up `redis://`.

---

## TLS-encrypted Redis (`rediss://`)

```env
SYSTEMU_REDIS_URL=rediss://:password@redis.example.com:6380/0
```

Trigger: scheme is `rediss://` (note the second **s**).  redis-py uses its
TLS connection class automatically.  Use this for:

- AWS ElastiCache with encryption-in-transit
- Upstash Redis (mandatory TLS)
- Azure Cache for Redis on the SSL port (6380)
- Any operator-deployed Redis fronted by stunnel / sidecar TLS

### Custom CA / client certs

If your TLS endpoint uses a self-signed CA or requires mutual TLS, set the
standard redis-py environment hooks before the dashboard / worker starts:

```bash
export REDIS_SSL_CA_CERTS=/etc/ssl/private/ca.pem
export REDIS_SSL_CERTFILE=/etc/ssl/private/client.pem
export REDIS_SSL_KEYFILE=/etc/ssl/private/client.key
```

Mounting these into the dashboard + worker containers via
`docker-compose.override.yml` is the recommended pattern — they're operator-
local and shouldn't be baked into the published image.

---

## Sentinel-fronted HA cluster

```env
SYSTEMU_REDIS_URL=redis+sentinel://sentinel-a:26379,sentinel-b:26379,sentinel-c:26379/mymaster/0?password=secret
```

Trigger: scheme is `redis+sentinel://`.  Systemu translates this into a
`SentinelConnectionPool` from `redis.sentinel` so failovers happen
transparently — neither the dashboard nor the worker has to be restarted
when the master changes.

URL grammar:

| Field | Meaning | Required? |
|---|---|---|
| `host:port,host:port,...` | Sentinel addresses (comma-separated) | yes |
| First path segment | Sentinel-monitored service name (e.g. `mymaster`) | yes |
| Second path segment | DB index (default `0`) | optional |
| `?password=…` | Password for both sentinels and master | optional |

Limitations of the current support:

- One password is shared between the sentinels and the masters they monitor.
  If your deployment uses different passwords (`sentinel-pass` vs
  `master-pass`), you'll need to extend `_build_sentinel_pool` in
  `systemu/queue/huey_app.py` to read both — file an issue.
- TLS to sentinels is not supported via the URL form.  Use a TLS-terminating
  proxy or wait for the explicit support to land.

---

## Redis Cluster (sharded — NOT supported)

Systemu does **not** support Redis Cluster (the open-source sharded variant)
today.  Reason: the `RedisPriorityQueue` keys (`systemu:queue`, `systemu:row:*`,
`systemu:running`) live on different slots and would split across nodes —
multi-key operations like `ZADD + HSET` in a pipeline would fail with
`CROSSSLOT`.

Workarounds if you must run on Cluster:

1. Wrap every Systemu key in `{systemu}:…` — Redis hash-tag syntax forces
   them all to the same slot.  Configure with
   `SYSTEMU_REDIS_PREFIX={systemu}` (note the curly braces).  All operations
   then live on a single shard, defeating sharding for Systemu but letting
   the Cluster host other workloads in parallel.
2. Run a small standalone Redis next to the Cluster for Systemu's queue —
   no sharding pretence at all.

---

## Sizing

Approximate footprint per million queued submissions:

| Item | Bytes/each | Notes |
|---|---|---|
| `systemu:queue` ZSET member | ~30 | submission_id + 8-byte float score |
| `systemu:row:<sub>` HASH | ~700 | full payload, JSON-encoded fields |
| `systemu:running` HASH entry | ~50 | submission_id → worker_id |
| `systemu:heartbeat:<sub>` STRING + TTL | ~80 | TTL key, churns continuously |

≈ 900 MB raw + overhead for 1M queued + running rows.  In practice the queue
drains on a horizon of seconds to minutes, so steady-state usage is far
lower; size for peak burst rather than total throughput.

---

## Health checks

The compose file's healthcheck issues `redis-cli ping`.  For TLS / Sentinel
deployments where that simple check doesn't apply, override in
`docker-compose.override.yml`:

```yaml
services:
  redis:
    healthcheck:
      test: ["CMD-SHELL", "redis-cli -h $$REDIS_HOST --tls --cacert /etc/ssl/ca.pem ping | grep PONG"]
```

For Sentinel you typically don't run the redis service inside Compose at all —
the sentinels and masters run outside, and the override removes the local
`redis` service entirely.
