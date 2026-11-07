# Alerts and activity history

Open a server's **Activity** tab for its audit timeline. **Settings > Instance
activity** is the cross-server timeline; **Settings > Alerts & notifications**
manages generic JSON webhooks and Discord, test notifications, recent alerts and
delivery status. No destination is configured automatically.

## Permissions and attribution

OIDC administrators receive the new permissions through the built-in `admin`
role. Custom roles must opt in:

- `activity.read` plus `server.view` reads activity/alerts for a granted server.
- Global `host.activity.read` reads the whole installation's history, including
  deleted servers and instance-wide changes. It intentionally grants cross-server
  audit visibility; do not grant it to a server-only auditor.
- Global `notifications.manage` manages destinations and reads instance-wide
  operational alerts/delivery status. It can disclose operational metadata to
  external recipients and is not grantable by server-scoped roles.

For example, a server-assigned role can contain `["server.view","activity.read"]`.
`viewer` and `operator` do not gain audit access implicitly. All new APIs are
unavailable to MCP bearer tokens, even manage tokens. Existing token-authorized
mod/server operations still record the token ID and, under OIDC, its owner.

Audit records include timestamp, actor, server (or instance scope), fixed action,
outcome and optional opaque job reference. OIDC records the issuer and exact
subject; a shared password can identify only "panel", not an individual.
Authentication-disabled operations are explicitly unattributed. Automatic runs
identify the scheduler; unexpected exits identify the process monitor.

Panel operations record mod/plugin install/delete/toggle, modpack install,
configuration and file writes/uploads/deletion, player ops/whitelist/bans,
password/token access changes, backup configuration/deletion, client-pack/sharing
changes, map/DNS/proxy configuration, power operations and server creation/deletion.
Backup, restore, world import/copy, fork, loader/upgrade, Java install and command
schedule completion/failure are recorded independently of the initiating HTTP
request. `accepted` means queued, not completed. Transfers record terminal outcomes
in both source and destination timelines without exposing the other server's ID.

No whole config, changed value, filename, console command, IP ban value, bearer
credential, webhook URL or raw error/console output is stored in the timeline.
Use the authorized operation view and job reference for detailed diagnosis.
Changes made outside Fabricator (filesystem edits, deployment-owned OIDC grants,
commands issued directly to a JVM) are not auditable by the panel.

## Destinations and secrets

Create a generic webhook or paste a Discord `/api/webhooks/...` URL, select event
types, save, then **Send test**. Tests use the same persistent queue and are
limited to one per destination per minute. "Queued" is not confirmation of
delivery: inspect **Delivery status**. A 2xx response confirms acceptance by the
remote HTTP service, not that a person read it. Discord mentions are disabled.

The full URL, including its credential-bearing path/query, is encrypted with
Fernet in `events/events.sqlite3` next to `servers.json`. The separate
`events/webhooks.key` is generated locally. Both files are created mode `0600`
and the events directory mode `0700` on POSIX; use OS account/ACL protection on
Windows. Back up the database **and key together**, but keep backups private.
This protects against disclosure of the database alone, not compromise of the
Fabricator OS account, which can read both. URLs are write-only in the API/UI;
editing with no `url` preserves the old secret. Replacing/pausing/deleting a
destination cancels pending deliveries. An already claimed delivery can finish
against its original URL. Do not put credentials in destination display names.

Public destinations require HTTPS on port 443 with ordinary TLS certificate and
hostname verification. HTTP proxies from the environment are not used.
Every attempt resolves DNS anew, rejects *all* answers if any is nonpublic, and
connects only to the checked numeric address while preserving the original TLS
hostname. No HTTP redirect is followed. Response bodies are never read/logged.
DNS waits are limited to five seconds with at most two outstanding resolver
threads; connection IO has a five-second timeout and a ten-second total deadline.

Private destinations are an **operator-only deployment decision**, never a UI
"allow private" switch. Map an exact normalized origin (explicit port required)
to exact literal IP addresses:

```dotenv
FABRICATOR_WEBHOOK_PRIVATE_TARGETS='{"https://hooks.internal:8443":["10.20.30.40"]}'
```

Only that scheme, hostname and port is permitted, and *every* resolved address
must match a configured IP. This is not a CIDR allowlist or permission to probe
other private hosts. Link-local/metadata, unspecified and multicast addresses
are always blocked. Loopback and plain HTTP are supported only with an explicit
exact-origin/IP entry (useful for an isolated local receiver). Such an entry
authorizes paths on that service; grant only a dedicated notification receiver,
not an internal administration API. HTTPS private targets still need a trusted
certificate; certificate verification cannot be disabled.

Set `FABRICATOR_PUBLIC_URL=https://panel.example` to include absolute panel links.
Without it payloads contain relative paths. Links lead to the server's authorized
Activity page, or instance notification settings for host alerts; they carry no
login token and never grant access to the recipient.

Generic webhook payload example (no actor or raw diagnostic content):

```json
{
  "id": 123,
  "at": 1794000000.0,
  "server_id": "srv_example",
  "action": "worlds.import",
  "outcome": "failed",
  "alert": "job.failed",
  "ref": "bji_example",
  "path": "/server/srv_example/activity?event=123",
  "url": "https://panel.example/server/srv_example/activity?event=123"
}
```

## Alert policy and persistence

- **Unexpected exits:** any managed JVM exit without a Fabricator stop/restart
  or exact console `stop`, `minecraft:stop` or `shutdown` command. Exit code zero
  alone is not evidence of intent. Backup shutdowns and scheduled power actions
  use the same lifecycle and do not produce crash alerts. External process
  termination may be indistinguishable from a crash. Crash events have a
  five-minute per-server cooldown.
- **Backups and jobs:** terminal failure alerts once per run; a panel restart
  marks unfinished tracked jobs `interrupted`, not successful. This reports
  uncertainty; it does not resume a backup/install or replace transfer recovery.
  Post-backup/restore/import restart failures produce a separate failed-job
  record. A caught pending-modpack failure is reported even when the bare loader
  install succeeds. Java cancellation and skipped schedules are not failures.
- **Disk pressure:** panel database volume, every configured server data volume
  and every configured local backup destination volume, sampled each minute.
  Start at **90% used OR less than 2 GiB free**; clear only at **85% used or less
  AND at least 3 GiB free**. State persists across restarts. The action identifies
  panel/server/backup scope; no host filesystem path is disclosed. A nonexistent
  future path is measured on its nearest existing parent volume. Several servers
  sharing a volume can each alert. S3 bucket capacity, quotas and inode pressure
  are not measured. Re-entry alerts have a one-hour cooldown per storage scope.

Notification delivery never runs on or rolls back a server operation. The
SQLite journal is best-effort on disk/permission/locking errors and logs an
explicit subsystem failure without sensitive inputs. Delivery is **at least
once**, not exactly once: use the stable event `id` for receiver idempotency.
A restart after a remote POST but before its acknowledgement is committed can
produce a duplicate. At most five attempts are consumed, including interrupted
attempts. An interrupted final attempt has an unknown remote outcome and is
reported as `delivery_interrupted`.

Transient network/408/429/5xx failures retry after 30s, 2m, 10m and 30m.
Other non-2xx HTTP responses, blocked targets and invalid destinations fail
permanently. Fixed failure codes and attempt counts remain visible; URLs and
upstream response bodies never appear. There is no automatic infinite retry or
manual retry loop; fix the destination and send a new rate-limited test.

Operational notifications have a five-minute cooldown per destination, alert
type and server (or instance scope). Every job outcome remains in audit history;
suppressed deliveries show **Cooldown**. Retention is 30 days / 10,000 events,
up to 2,000 queued/in-flight deliveries, and up to 5,000 recent terminal delivery
records in addition to the live queue. Undelivered entries expire after seven
days. Queue overflow records `queue_capacity` instead of silently succeeding.
Completed job attribution is kept for seven days / 10,000 recent entries.
The history is an operational audit trail, not a tamper-proof compliance ledger.

Use the existing single-process deployment model (one process owns the process
registry and schedulers); multiple independently booting WSGI workers are not
supported. `FABRICATOR_DISABLE_NOTIFICATIONS=1` pauses disk sampling and delivery
without disabling event recording. `FABRICATOR_DISABLE_SCHEDULER=1` also pauses
this worker for tests/operator maintenance. The queue resumes on the next normal
boot. Self-updates performed by an external service manager and jobs in external
systems are not monitored here.

## Reusable event interfaces

`backend.events.store.emit(action, server_id=..., outcome=..., alert=..., who=...,
ref=..., cooldown=..., key=...)` records an event and optionally enqueues an
operational alert. Use fixed action names, fixed alert types (`server.crashed`,
`backup.failed`, `job.failed`, `disk.pressure`, `disk.recovered`) and sanitized
opaque references only. Never pass exception/request/console payloads.

`track_job(key, server_id, action, phase, ref=...)` captures the current actor
before a worker is started, then preserves it for terminal `done`/`failed`/
`error`/`cancelled` calls outside the request context. Give each run a unique key.
Existing `install:` and `modpack:` progress adapters deliberately reuse their
per-server key on a new `starting` transition. Call sites must not emit a terminal
phase before the actual source operation has finished.
