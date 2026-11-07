# Fabricator — API Documentation

REST API reference for the Fabricator Minecraft server manager. Every endpoint lives under `/api`
and normally returns JSON. Snapshot/client-pack downloads stream binary archive
attachments; world/modpack imports take raw archive bytes as their request bodies. Unknown `/api/...` paths
return `404 {"error": "endpoint not found", "path": ...}` rather than the SPA's HTML.

Errors are consistently `{"error": "<message>"}`. Anything under `/api` not listed as public in
[Authentication](#authentication) requires a session.

## Contents

- [Authentication](#authentication)
- [Alerts and activity history](#alerts-and-activity-history)
- [Health](#health)
- [Servers](#servers)
- [Instance forks and world copies](#instance-forks-and-world-copies)
- [Staged modpack updates](#staged-modpack-updates)
- [DNS management](#dns-management)
- [Server files](#server-files)
- [Mods](#mods)
- [Client modpack exports](#client-modpack-exports)
- [World archive downloads](#world-archive-downloads)
- [Private BlueMap maps](#private-bluemap-maps)
- [Console & metrics](#console--metrics)
- [Loader versions](#loader-versions)
- [Java management](#java-management)
- [Players](#players)
- [Backups & snapshots](#backups--snapshots)
- [Scheduled commands](#scheduled-commands)
- [Modrinth integration](#modrinth-integration)
- [playit.gg tunnel](#playitgg-tunnel)
- [System updates](#system-updates)
- [Configuration](#configuration)
- [Loader registry (developers)](#loader-registry-developers)
- [Modrinth client (Python)](#modrinth-client-python)
- [Worked examples](#worked-examples)
- [Testing](#testing)
- [Security notes](#security-notes)

---

## Staged modpack updates

Session-only, owner-bound and `Cache-Control: no-store`; unavailable to MCP
tokens and managed deployments. See the [operator guide](deploy/modpack-updates/README.md)
for trust boundaries, exact scope and recovery. No endpoint starts production.

Base: `/api/servers/<server_id>/modpack-updates`. All endpoints require
`server.view`, `modpacks.install`, `files.read` and `server.settings.read` on
production, plus the operation-specific permissions below.

| Method | Suffix | Body / result |
|---|---|---|
| GET | (base) | `{plans:[...]}` for the current owner, newest first |
| POST | (base) | `{source:"project",project_id:"slug",replaceFiles:[]}` or `{source:"upload",upload_id:"...",replaceFiles:[]}`; `202` with plan ID/status |
| GET | `/<plan_id>` | Public review/status, no private source metadata or filesystem roots |
| DELETE | `/<plan_id>` | Delete inactive review and candidate storage; retain staging and safety backups |
| POST | `/<plan_id>/stage` | `{reviewToken,name,acknowledgeRisks:true}`; needs source `server.fork` and global `servers.create` |
| POST | `/<plan_id>/validate` | `{reviewToken,executePackCode:true}`; needs staging view/settings-read/files-read/start/stop |
| POST | `/<plan_id>/promote` | `{reviewToken,confirmName:"exact production name"}`; needs staging rights above, production `modpacks.promote` and `backups.create` |

Poll the plan or collection after any `202`. States: `preparing`, `review`,
`staging`, `staged`, `validating`, `validated`, `promoting`, `promoted`, `failed`.
Reviews include `changes` with path/action/category/conflict, before/after
SHA-256 hashes and bounded text config diffs; `preservedFiles`,
`loaderChanges`, `dependencyReview`, `clientImpact`, `warnings` and
`reviewToken`. Staging adds `stagingId`; validation adds `validatedAt`;
promotion adds `safetySnapshotId`. A post-commit failure explicitly reports
`productionChanged:true`; never blindly retry it. Failed/interrupted operations
require a fresh review. `recoveryRequired:true` means an update journal still
blocks production starts and review deletion until panel startup recovery.

Only reviewed mod/config/defaultconfig files are promoted. Game/loader/build
mismatches are visible but blocked. Reviewed local-conflict replacements
require a **new plan**; production/staging content or runtime changes invalidate
approval. Production worlds may evolve between review and promotion; the
mandatory safety backup captures their current state, and staging worlds are
never imported. Startup validation only verifies a fresh Minecraft readiness
signal and successful stop, not gameplay or complete dependency compatibility.

Malformed/unsupported operations return `400`; missing/foreign plans or
permissions return `403` (unknown plans may return `400`); stale review/input
bindings return `409`. Worker failures are explicit persisted `failed` states.
Uploaded pack handles use the existing owner-bound modpack upload endpoint.

## Alerts and activity history

Session-authenticated; **unavailable to MCP API tokens**. OIDC `admin` includes
these permissions; custom roles must opt in. Shared-password installations retain
their existing all-panel authority. See the [operator guide](deploy/notifications/README.md)
for safe destinations, event coverage, attribution, disk thresholds and retention.

| Method | Path (prefix `/api`) | Capability / response |
|---|---|---|
| GET | `/activity?before=<event_id>&alerts=1` | Global `host.activity.read`; installation-wide history (omit `alerts=1` for all events) |
| GET | `/servers/<server_id>/activity?before=<event_id>` | `server.view` + `activity.read`; this server only |
| GET | `/servers/<server_id>/alerts?before=<event_id>` | Same permissions; operational alerts only |
| GET | `/notifications` | Global `notifications.manage`; sanitized destinations, latest 200 deliveries and 50 alerts |
| POST | `/notifications/destinations` | Global `notifications.manage`; create, returns `201 {"id":"..."}` |
| PUT | `/notifications/destinations/<id>` | Same permission; replace configuration, omit `url` to preserve the saved secret |
| DELETE | `/notifications/destinations/<id>` | Same permission; delete and cancel pending deliveries |
| POST | `/notifications/destinations/<id>/test` | Same permission; rate-limited queued test, `202 {"event_id":123,"status":"queued"}` |

History returns `{ "items": [...], "next": 123 }`, at most 50 newest-first events.
Pass `next` as `before` for the next page; `null` means no older page.
Events contain `id`, Unix-seconds `at`, `actor` (`kind`, `id`, optional
OIDC `issuer`/token owner `subject`), `server_id` (null for installation scope),
fixed `action`, `outcome`, optional `alert`, and optional opaque job `ref`.
Possible outcomes are `accepted`, `succeeded`, `failed`, `interrupted`, `skipped`.
Responses are `Cache-Control: no-store`; no raw config, console or error payload
is present.

Destination create/update body (maximum 16 KiB):

```json
{
  "name": "Operations",
  "kind": "webhook",
  "url": "https://hooks.example/credential-bearing-path",
  "enabled": true,
  "alerts": ["server.crashed", "backup.failed", "job.failed", "disk.pressure", "disk.recovered"]
}
```

`kind` is `webhook` or `discord`. At most 20 destinations; names are 1-80
characters; URLs at most 2048 ASCII characters, encrypted at rest and write-only.
At least one supported alert must be selected. Returned destination fields are
`id`, `name`, `kind`, `enabled`, `alerts`, `has_url`; neither plaintext nor
ciphertext URLs are exposed. A type change requires a replacement URL.
Unconfigured private/plain-HTTP/nonstandard-port destinations are rejected;
DNS and IP policy are revalidated on every delivery attempt. URL syntax errors
return 400; nonexistent destination IDs 404. Queued test means **not yet delivered**.

Delivery records include `id`, `destination_id`, display `name`, `event_id`,
Unix-seconds `created`/`next_at`, `state`, `attempts`, and fixed safe `error` code.
States are `pending`, `sending`, `sent`, `failed`, `cancelled`, `suppressed`.
No upstream response body or destination URL is returned.

## World archive downloads

See the [operator guide](deploy/world-downloads/README.md) for exact archive
shape, exclusions, privacy, compatibility and deployment limits. Private
routes are session-only (all MCP API tokens are denied); all capabilities are
server-scoped and also require `server.view`.

| Method | Path (prefix `/api`) | Capability / response |
|---|---|---|
| POST | `/servers/<id>/world-exports` | `worlds.read` + `worlds.export`; `{"world_id":"<discovered group>"}`, stopped-only synchronous generation; `201` metadata |
| GET | `/servers/<id>/world-exports` | `worlds.download`; `{"exports":[...]}` retained metadata, active share count and pinned expiry |
| GET | `/servers/<id>/world-exports/<artifact>/download` | `worlds.download`; streamed tar.gz attachment, `Content-Length`, `X-Archive-SHA256` |
| GET | `/servers/<id>/world-export-shares` | `worlds.share` + `worlds.download`; own active links without secrets |
| POST | `/servers/<id>/world-exports/<artifact>/shares` | Same rights; `{"expiry_seconds":3600,"acknowledge_privacy":true}`; `201` one-time fragment URL |
| DELETE | `/servers/<id>/world-export-shares/<share>` | Same rights; revokes own link |
| DELETE | `/servers/<id>/world-exports/<artifact>` | Same rights; shared archives need `{"confirm_shared":true}`; invalidates all linked grants |
| POST | `/world-shares/download` | Public bearer redemption only; bounded `{"token":"..."}` body; streamed single immutable attachment |

Metadata includes `id`, `filename`, `created_at`, `world`, `loader`,
`loader_version`, `size`, `source_bytes`, `file_count` and `sha256`. Share
metadata includes `id`, `artifact_id`, `created_at`, `expires_at`; a secret
`url` appears only on creation. Public links expire/revoke/delete to `404`,
throttling returns `429` with `Retry-After`, busy downloads `503`, stopped/
content/storage-policy conflicts `409`, malformed private input `400`, and
storage I/O failure `500` (public storage failure is `503`). No bearer can
mutate a world or select another artifact. Already-downloaded/in-flight bytes
cannot be recalled. Public requests omit cookies and use no URL token.

---

## Instance forks and world copies

Experimental, session-authenticated, **unavailable to all MCP API tokens** and
disabled in managed mode. Both source and destination must already be stopped;
no endpoint stops the source. See the [operator guide](deploy/transfers/README.md)
for exact file scope, layout restrictions, trust model and crash recovery.

| Method | Path (prefix `/api`) | Capability / response |
|---|---|---|
| GET | `/servers/<source_id>/worlds` | `worlds.read` on source; `{ "worlds": [...] }` with active-group ID/name, directories, dimensions, layout, game version and DataVersion |
| POST | `/servers/<source_id>/transfers/fork/preview` | `server.fork` + `files.read` + `server.settings.read` on source and global `servers.create`; returns content-bound preview |
| POST | `/servers/<source_id>/transfers/fork` | Same rights; confirms preview and queues independent instance creation, `202 {"jobId":"..."}` |
| POST | `/servers/<source_id>/transfers/world-copy/preview` | `worlds.read` on source; `worlds.import` + `backups.create` on destination; returns compatibility/scope preview |
| POST | `/servers/<source_id>/transfers/world-copy` | Same rights, plus `server.start` on destination if `restart:true`; `202 {"jobId":"..."}` |
| GET | `/transfer-jobs/<job_id>` | Initiating OIDC owner only, with both instances' current operation grants; phase/byte progress and terminal result/error |

All per-instance capabilities also require `server.view`. A view-only
principal cannot discover source world metadata, preview or poll another
user's job. The generic `/backup-jobs/<id>` endpoint rejects transfer jobs.
Responses use `Cache-Control: no-store`; no roles, routing authority or
per-server grants are inherited by the new instance.

Fork preview body:

```json
{
  "name": "survival-sandbox",
  "includeWorlds": true,
  "includePlayerData": false
}
```

World-copy preview body:

```json
{
  "worldId": "island",
  "destinationId": "srv_destination",
  "includePlayerData": false,
  "restart": false
}
```

Booleans are explicit choices, not optional destructive defaults. The world
ID comes from discovery and identifies a complete dimension group.
Previews include `previewToken`, `worlds`, `bytes` and `warnings`. World-copy
previews also include `world`, `destinationWorld`, `replaces`,
`requiresSafetyBackup` and `layout`; fork previews include fresh-access and
autostart scope. Preview refusal is `400`, denied operation `403`, hidden or
foreign job `404`, stale submission token `409`.

Submit to the corresponding non-`/preview` path with the same choices plus:

```json
{
  "previewToken": "<current content-bound preview token>",
  "acknowledgeRisks": true,
  "confirmReplace": "island"
}
```

`confirmReplace` is required only when replacing an initialized destination
world, and must exactly match `destinationWorld`. The worker revalidates the
token and all prerequisites after obtaining deterministic instance locks.
Changes while queued fail rather than using a stale preview.

Poll `/transfer-jobs/<jobId>` until `phase` is `done` or `failed`. Phases:
`queued`, `safety_snapshot`, `copying` (`copied_bytes`, `total_bytes`),
`verifying`, `publishing`, optional `restarting`, `done`/`failed`. Fork results
contain `serverId`, `name`, `port`; world-copy results contain `destinationId`
and `safetySnapshotId`. Errors after successful publication include
`published:true`; inspect before retrying. Jobs are not durable across panel
restarts: missing progress is an unknown outcome, not success.

Supported worlds are Java nested-dimension groups or same-layout Bukkit
split-dimension groups. World copies require exact game/loader/build matches,
verifiable world formats, identical mod/plugin jar hashes and, for modded
loaders, matching `config`/`defaultconfigs`. Vanilla/Bukkit also require
identical launch-jar hashes. No force-load, version migration,
layout conversion, arbitrary plugin-world discovery or live-source snapshot
API is exposed. Source bytes are preserved. Destination non-world
configuration is unchanged, and a verified local safety backup is mandatory
before rename-based replacement.

---

## Private BlueMap maps

Experimental, exact-version-scoped integration; see the
[operator guide and support matrix](deploy/maps/README.md). Management uses the
normal session gate and is unavailable to MCP tokens. Each OIDC operation also
requires `server.view` on the target server.

| Method | Path (prefix `/api/servers/<server_id>/map`) | Capability | Request / response |
|---|---|---|---|
| GET | empty suffix | `map.read` | `enabled`, `configured`, `ready`, compatibility/dependency `preview`, readiness `message` and global-waystone count when ready |
| POST | `/install-bluemap` | `map.manage` + `mods.install` | Explicit pinned, integrity-verified BlueMap Fabric install; stopped compatible server only; `201` with `restart_required` |
| POST | `/setup` | `map.manage` | `{ "accept_resource_download": true }`; stopped server and exact dependencies required; preserve existing configs; `201` with `restart_required` |
| PUT | `/enabled` | `map.manage` | `{ "enabled": true/false }`; enable requires valid existing config/dependencies; disable immediately blocks map resources but does not stop rendering |
| POST | `/session` | `map.read` | Fresh process-bound adapter heartbeat required; returns relative viewer `url` and `expires_in: 300` |
| GET / HEAD | `/view/<ticket>/` and `/view/<ticket>/<path>` | Map-only capability | Read-only isolated viewer/assets/tiles/live JSON; no session cookies needed; owner/grants, server scope, enable state and heartbeat rechecked |

All responses are `no-store` / `no-referrer`. Resources use an opaque CSP/iframe
sandbox and non-credentialed `Origin: null` CORS, not an unrestricted admin-origin
iframe. The resource handler is the only narrow capability-auth exception;
it does not make map resources public. Invalid/expired/revoked capabilities
return `401`, inaccessible/disabled servers `404`, incompatible/not-ready/config
conflicts `409`, invalid enable payload `400`, invalid upstream JSON `502`.
Readiness GET returns `ready: false` with an actionable message for a temporarily
unavailable configured map. No endpoint restarts a server or accepts an upstream
URL. Live transport is BlueMap's existing HTTP polling, not a WebSocket.

## Client modpack exports

Experimental launcher-importable packs for Fabric, Quilt, Forge and NeoForge.
Management is session-authenticated and unavailable to MCP API tokens. Each OIDC capability
below also requires `server.view` on the target server. No endpoint installs
client-only selections onto the server or needs a restart.

| Method | Path (prefix `/api/servers/<server_id>`) | Capability | Response / request |
|---|---|---|---|
| GET | `/client-pack` | `clientpacks.read` | Current `config`, effective `entries`, pinned `files`, `dependencies`, `errors`, `warnings`, `fingerprint`, counts, override candidates and excluded imported paths |
| PUT | `/client-pack` | `clientpacks.manage` | Replace editor configuration using its current `revision`; returns new `config` |
| POST | `/client-pack/import` | `clientpacks.manage` | Raw `.mrpack` body, max 64 MiB; replace retained client metadata only |
| POST | `/client-pack/exports` | `clientpacks.export` | `{ "fingerprint": "<reviewed fingerprint>" }`; `201` immutable artifact metadata |
| GET | `/client-pack/exports` | `clientpacks.download` | `{ "exports": [...] }`; retained immutable snapshots |
| GET | `/client-pack/exports/<artifact_id>/download` | `clientpacks.download` | Attachment, MIME `application/x-modrinth-modpack+zip`, private/no-store |
| GET | `/client-pack/shares` | `clientpacks.manage` + `clientpacks.download` | `{ "shares": [...] }`; caller-owned active grants only |
| POST | `/client-pack/exports/<artifact_id>/shares` | `clientpacks.manage` + `clientpacks.download` | `{ "expiry_seconds": 86400 }`; `201` grant metadata and one-time relative `url` |
| DELETE | `/client-pack/shares/<share_id>` | `clientpacks.manage` + `clientpacks.download` | Revoke caller-owned grant; unknown/foreign `404` |
| DELETE | `/client-pack/exports/<artifact_id>` | `clientpacks.manage` + `clientpacks.download` | Delete archive and its grants; requires `{ "confirm_shared": true }` if shared, otherwise `409` |

Editor configuration example:

```json
{
  "revision": "<revision from GET>",
  "name": "Friends client pack",
  "loader_version": "0.16.9",
  "selections": [
    { "key": "client:AANobbMI", "version_id": "<exact Modrinth version ID>", "requirement": "optional" },
    { "key": "server:mods/local.jar", "requirement": "excluded" }
  ],
  "overrides": ["imported:config/sodium-options.json"]
}
```

Keys identify a server file, an imported file (`imported:mods/example.jar`), a
client project (`client:<project_id>`) or an excluded bundled override
(`imported-override:client-overrides/mods/custom.jar`). Requirements are
`required`, `optional`, `excluded`; client selections must name a matching
exact version. Server/shared versions cannot be replaced by client selections.
Use only advertised allowlisted override candidates. Saving selections does
not make them valid: GET exposes errors, and generation refuses any error.
Stale editor revisions and changed export sources return `409`; malformed
configuration/imports return `400`; oversized uploads `413`; expired/foreign
artifacts `404`. Modrinth errors preserve rate-limit/retry details.

Artifacts include `id`, `filename`, `created_at` (Unix seconds), `fingerprint`,
`size`, `file_count` and `override_count`. Up to five snapshots per server are
kept for 24 hours, swept on artifact access, except that active share grants pin
their archive through the latest grant expiry. Listing adds `active_shares` and
`pinned_until` (Unix seconds or null). Each archive is capped at 16 MiB.
Generation returns `409` with an actionable error if all five slots are pinned.
Compare their fingerprints with a
fresh GET review to indicate outdated sources; listing alone does not establish
freshness. A failed generation never publishes a partial pack.

See the [operator guide](deploy/client-packs/README.md) for dependency behavior,
old-install reconstruction, override privacy/redistribution limits and launcher
optional-file caveats.

### Anonymous immutable-archive downloads

Only `POST /api/client-pack-shares/download` is public (outside setup lockdown).
It takes `{ "token": "<fragment from the share URL>" }` in a JSON body of at most
512 bytes and returns only the named archive attachment, not server metadata.
No session or OIDC redirect is needed. It never exports on demand. Unknown,
expired, revoked, missing-artifact or deleted-server grants share the same
generic `404` response. A process-local token bucket allows burst 30/refill
30 per minute, then `429` with `Retry-After`. A busy server/storage returns
`503`; retry later. MCP bearer-token authentication remains disabled for this
route, as for all other client-pack endpoints.

Creation requires an explicit integer `expiry_seconds` from 3,600 to 604,800.
At most 20 active grants may exist per server. Grant metadata is
`id`, `artifact_id`, `created_at`, `expires_at` (Unix seconds), plus `url` **only
at creation**. List/revoke operations are creator-owned in OIDC mode and
server-scoped; the plaintext token and hash are never listed. Deleting an
archive invalidates all its grants, including other creators' grants, and
server deletion invalidates every associated link. Already-streaming bytes
cannot be recalled.

The browser URL is `/client-pack-share#<opaque token>`; it grants exactly one
fixed artifact and is not a direct launcher download URL. Its view clears the
fragment/history state, then POSTs without cookies, caching or referrers.
Tokens never occur in HTTP URLs or ordinary access logs. Use HTTPS and do not
capture request/response bodies, fragments or one-time URLs in reverse-proxy,
APM or analytics instrumentation. Download responses set private/no-store,
no-referrer, nosniff and noindex headers. The public view disallows external
scripts/connections. Review all selected pack settings before creating links;
they are intentionally disclosed to anyone holding the link.

## DNS management

DNS administration is session-only (not available to MCP API tokens) and
unavailable in managed-hosting mode. Provider credentials are configured in the
separate webhook service. DNS settings are disabled until explicitly configured.
In OIDC mode all DNS administration requires the global `dns.manage` permission,
including server bindings (which additionally require `server.view` on the target).
DNS administration is instance-wide because synchronization and ownership span
all bindings and manual records; a per-server role cannot grant it.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/dns` | Configuration, manual record sets, bindings, owner, cleanup inventory, sync status |
| PUT | `/api/dns/config` | Configure `{enabled, url, zone, domain}`; domain defaults to zone |
| PUT | `/api/dns/records` | Replace manual record sets with a validated JSON array |
| POST | `/api/dns/plan` | Read live provider state and return desired records and grouped changes |
| POST | `/api/dns/sync` | Reconcile all changes and return persisted status |
| GET | `/api/servers/<server_id>/dns` | Read the server's DNS binding |
| PUT | `/api/servers/<server_id>/dns` | Save an opt-in DNS binding |

Record shape:

```json
{"dnsName":"play.mc.example.com","recordType":"A","recordTTL":300,"targets":["192.0.2.10"]}
```

Supported manual types are A, AAAA, CNAME, SRV, TXT, MX, CAA, and NS.
Use full DNS names, one RRset per name/type, TTL 60–2147483647, and presentation
targets for SRV (`0 0 25565 play.mc.example.com.`), MX (`10 mail.example.com.`),
and CAA (`0 issue "letsencrypt.org"`). SOA and managed-apex NS are infrastructure
owned by the authoritative server. A CNAME cannot coexist with other data or be
an SRV target. The `_fabricator-` prefix is reserved for ownership metadata.

Server binding shape:

```json
{
  "enabled": true,
  "hostname": "survival.mc.example.com",
  "addresses": ["192.0.2.10", "2001:db8::10"],
  "target": "",
  "srv": true,
  "ttl": 300,
  "priority": 0,
  "weight": 0
}
```

Supply addresses or a CNAME target, not both. SRV generation requires explicit
addresses and follows the server's configured port. `{ "enabled": false }`
removes the binding's owned records on reconciliation; stopping the process
does not. Server deletion retains cleanup inventory until removal succeeds.

Saved changes are desired state, not publication acknowledgments. Status is
`pending`, `applied`, `failed`, or `disabled`; `syncedAt` denotes the last successful
provider verification, not public recursive propagation. Validation errors
return 400; provider failures return 502. A failed manual sync returns its
status and `error`; background failures persist the same diagnostics.

Existing unmanaged records and ownership conflicts are never adopted silently.
Only Fabricator-owned records are changed or deleted. Keep `dns.json` persistent
when moving/restarting installations. Changes between provider URLs retain the
old connection reference until cleanup succeeds. Clear manual records and
bindings before changing the managed domain.

See [deployment recipes](deploy/dns/README.md) for private webhook networking,
Cloudflare credentials, authoritative delegation, and Kubernetes ExternalDNS
usage. Fabricator does not create DNSEndpoint resources.

## Authentication

Fabricator supports single-operator password login or deployment-configured OIDC
multi-user login. The gate is default-deny: every `/api/`
route requires an authenticated session except the ones listed below.

**States**

- **Disabled** — `FABRICATOR_DISABLE_AUTH=1`. The gate passes everything through.
- **Setup mode** — auth is enabled but no credential exists yet. The app boots locked and only
  serves `POST /api/auth/setup`, `GET /api/auth/status` and `GET /api/health` until a password is set.
- **Configured** — normal login. Public routes are `POST /api/auth/login`, `GET /api/auth/status`
  and `GET /api/health`, plus the grant-validated `POST /api/client-pack-shares/download`
  and `POST /api/world-shares/download`;
  everything else needs the session cookie.
- **OIDC** — one configured provider replaces password login/setup. Public routes
  are `GET /api/auth/oidc/login`, `GET /api/auth/oidc/callback`,
  `GET /api/auth/status`, `GET /api/health` and the grant-validated
  `POST /api/client-pack-shares/download` and `POST /api/world-shares/download`.
  Password setup/login/enable/disable/change
  return `409`. Protected endpoints also require resolved action permissions.

Unauthenticated requests get `401 {"error": "authentication required"}` (or `{"error": "setup required"}`
in setup mode). The session cookie is HttpOnly, SameSite=Lax, and lives for 7 days.

#### `POST /api/auth/setup`

First-boot only: sets the operator password and logs the caller in. JSON body required.

**Body:** `{ "password": "at-least-8-chars" }`

**Response (200):** `{ "authenticated": true }`
**Errors:** `400` invalid/short password or non-JSON body · `409 {"error": "already configured"}`

#### `POST /api/auth/login`

**Body:** `{ "password": "..." }`

**Response (200):** `{ "authenticated": true }`
**Errors:** `400` password missing · `401 {"error": "invalid credentials"}` (delayed ~1s)

#### `POST /api/auth/change-password`

Requires an active session **and** the current password.

**Body:** `{ "current": "...", "new": "at-least-8-chars" }`

**Response (200):** `{ "changed": true }`
**Errors:** `400` missing fields or new password too short · `401` current password incorrect ·
`409` when the password is managed via `FABRICATOR_AUTH_PASSWORD_HASH`

#### `POST /api/auth/logout`

Clears the session. **Response (200):** `{ "authenticated": false }`

#### `GET /api/auth/status`

**Response (200):**
```json
{ "enabled": true, "authenticated": false, "needs_setup": false }
```

The response also includes `managed`; proven sessions/tokens receive
`panel_version`. OIDC adds `auth_mode: "oidc"` and `provider_name`. Only a proven
OIDC identity gets its own `user` object:

```json
{
  "sub": "user-sub",
  "roles": ["token-user"],
  "permissions": ["tokens.self.manage"],
  "servers": {
    "srv_1": {
      "roles": ["operator"],
      "permissions": ["server.view", "server.metrics.read", "server.logs.read", "server.start", "server.stop", "server.restart", "tokens.self.manage"]
    }
  }
}
```

`user.permissions` contains global grants; each server entry includes the union
of global and server grants. Global server permissions also apply to servers
not explicitly listed in `user.servers`. Role names are descriptive, never an
authorization shortcut. No other user's map or provider tokens are returned.

#### `GET /api/auth/oidc/login`

Starts Authorization Code + PKCE S256 login with state and nonce.
Optional `redirect` is a same-app absolute path; unsafe values fall back to `/`.
Returns a browser redirect to the configured provider, not a JSON token.
An unreachable provider redirects to `/login?oidc_error=unavailable`; transaction
capacity exhaustion uses `busy`. OIDC not configured returns `404` outside setup mode.

#### `GET /api/auth/oidc/callback`

Registered callback URI; consumes the browser-bound, single-use transaction
(expires after ten minutes). Validates the ID token signature, issuer, audience,
authorized party, expiry and nonce, and requires a configured exact subject.
Sets a seven-day HttpOnly session cookie, with no provider tokens inside it.
Success redirects to the safe stored destination. Failures redirect to login
with fixed error codes `invalid`, `denied` or `cancelled`; raw provider errors are
never reflected. Logout is local only.

#### OIDC permission catalog

Configure `FABRICATOR_OIDC_USERS` and optional `FABRICATOR_AUTH_ROLES` as
documented in [README](README.md#oidc-multiple-users-and-custom-roles) and
`.env.example`. `admin` defaults to the explicit full catalog; `viewer` to
`server.view`, `server.metrics.read`, `server.logs.read`; `operator` adds
`server.start`, `server.stop`, `server.restart`; `token-user` grants only
global `tokens.self.manage`.

| Scope | Permission identifiers |
| --- | --- |
| Server discovery/details | `server.view` |
| Server telemetry | `server.metrics.read`, `server.logs.read` |
| Power | `server.start`, `server.stop`, `server.restart` |
| Console/install/lifecycle | `server.console.execute`, `server.install`, `server.upgrade`, `server.delete` |
| Settings | `server.settings.read`, `server.settings.write`, `server.autostart.write`, `server.icon.write` |
| Files | `files.list`, `files.read`, `files.write`, `files.upload`, `files.delete`, `files.create_directory` |
| Add-ons | `mods.read`, `mods.install`, `mods.toggle`, `mods.delete`, `modpacks.install` |
| Backup archives | `backups.list`, `backups.create`, `backups.download`, `backups.restore`, `backups.delete` |
| Backup configuration | `backups.config.read`, `backups.config.write` |
| World import | `worlds.import` |
| Players | `players.read`, `players.kick`, `players.bans.manage`, `players.ops.manage`, `players.whitelist.manage`, `players.known.delete` |
| Schedules | `schedules.read`, `schedules.create`, `schedules.update`, `schedules.delete`, `schedules.run` |
| Global server operations | `servers.create`, `servers.import`, `servers.reorder` |
| Global host metrics | `host.metrics.read` |
| Global Java | `java.read`, `java.install`, `java.delete` |
| Global updates | `updates.read`, `updates.apply` |
| Global tunnels (shared agent) | `tunnels.read`, `tunnels.manage` |
| Global DNS administration | `dns.manage` |
| Global token/MCP controls | `tokens.self.manage`, `tokens.all.manage`, `mcp.configure` |

Every non-global action requires `server.view` on its target; configuration
rejects effective server assignments missing this prerequisite. Settings-read
controls whether full records (including sensitive configuration and paths)
are returned. Without it, list/detail responses use a minimal projection,
and metrics require their own grant. The properties form requires both
settings-read and settings-write to edit; write-only API clients can still send
explicit partial updates without reading current settings. File/backup access can expose secrets;
console, installation, schedules, launch configuration and restore permissions
can execute commands/code, including asynchronously. These permissions are not
a host sandbox. Schedules run as the host service even after their creator logs
out; review existing schedules when changing access.

GET `/api/servers` filters by `server.view`. A server outside the caller's grants
returns non-disclosing `404`; forbidden actions within an accessible server
return `403`; missing/invalid identity returns `401`. Unknown routes fail closed.
Existing managed-hosting locks remain additional restrictions.

Route/action mapping follows resource and HTTP operation: files GET listing/search
uses `files.list`, content GET/PUT uses `files.read`/`files.write`; uploads,
deletion and directory creation have independent grants. Mods GET/POST/PATCH/DELETE
use read/install/toggle/delete respectively. Backup download/restore/delete use
their matching permissions; configuration CRUD uses config read/write, and purge
also requires `backups.delete`. Backup/job progress requires the initiating
operation's permission on the job's server. Install progress requires
`server.install` or `server.upgrade`; Java install progress accepts `java.read`
or `java.install`. Modpack staging handles are owner-bound,
and installation checks the target server. Shared catalog reads require a
relevant server/install/catalog capability; they never authorize mutation.

#### User-owned MCP tokens

Session-only `GET /api/integrations/mcp` lists the caller's tokens with
`tokens.self.manage`, or all owners' tokens with `tokens.all.manage`.
`POST /api/integrations/mcp/tokens` requires `tokens.self.manage` and accepts
`{"name":"example","scope":"read"}` (or `"manage"`). The owner is always the
current session; client-supplied owners are ignored. The secret appears only
once, in the creation response. Metadata includes `owner: {"issuer":"...","sub":"..."}`.
`DELETE /api/integrations/mcp/tokens/<token_id>` requires ownership plus
self-management, or all-token management. Unavailable tokens return `404`.
`PUT /api/integrations/mcp` requires `mcp.configure` and `{"enabled":true}`.

Bearer access intersects the existing MCP read/manage/never table with the
owner's effective permissions and filtered server access. Bearer credentials
never manage tokens or the switch, even for full-access owners. Revocation is
immediate; user/role changes take effect after restart. Ownerless legacy tokens
are rejected in OIDC mode; OIDC-owned tokens are rejected in password mode or
with a different issuer. Existing password-only tokens retain their behavior.

---

## Health

#### `GET /api/health`

**Response (200):** `{ "healthy": true }`

---

## Servers

#### `GET /api/servers`

List all servers. Each record is augmented with a `runtime` block from the process registry
(live status, PID, RAM, mod count). Returned in saved order; the first server is the one the panel opens on.

#### `PUT /api/servers/order`

Reorder the server list. **Body:** `{ "ids": ["srv_b", "srv_a"] }`. Unknown ids are ignored; servers not listed keep their order at the end.

**Response (200):** the reordered list, same shape as `GET /api/servers`.
**Error (400):** `ids` not a list of strings

#### `POST /api/servers`

Create a server record. This only registers the server — call `/install` afterwards to lay down
the files.

**Required fields:** `name`, `version`, `loader`, `port`, `installPath`

**Response (201):** the created server, including a `javaRequirement` block describing the
required Java major, what was detected, and a `recommended_install` download hint.

**Errors:** `400` missing fields, the port is already used by another server, or invalid memory
fields (validated as in [settings](#put-apiserversserver_idsettings)) · `500` on write failure

#### `POST /api/servers/import/inspect`

Inspect a server folder Fabricator did not create. Read-only.

**Body:** `{ "path": "/abs/path/to/server" }`. The path must be absolute and must not be a drive root
or the home folder, and the folder must contain `server.properties`, `eula.txt` or a `*.jar`.

**Response (200):** `{ path, name, loader, version, loaderVersion, port, modsCount, launch, warnings }`.
Undetected values are `null`. `warnings` holds codes: `loader`, `version`, `launch`, `permissions`
(some files are read-only for the panel user; running in place may fail). For a Forge/NeoForge
folder holding several builds, the one named in `run.sh`/`run.bat` wins.

**Errors:** `400` invalid path, a folder that doesn't look like a server, or any file or folder in
it the panel user cannot read (the message names it)

#### `POST /api/servers/import`

Register that folder as a server. Disabled in managed mode (`403`).

**Body:** `path`, `mode` (`copy`, the default, duplicates the folder into the server root;
`inplace` runs it where it is and marks the record `external`), plus optional overrides of the
detected `name`, `version`, `loader`, `port` and of `memory`, `memoryMin`, `memoryUnit`, `javaPath`,
`jvmArgs`. The launch target is always re-detected on the server. `server.properties` values are
copied into the record.

**Response (202):** the created record (status `installing`) plus `progress`. Poll
[`/install/progress`](#get-apiserversserver_idinstallprogress) for phases `copying`, `linking_mods`,
`done`/`failed`. When done, the entry holds `mods_total`, `mods_linked` and `mods_unlinked`; mods
Modrinth recognises by hash are recorded in the content manifest. If Modrinth can't be reached,
`mods_link_error` is set and the import still succeeds.

**Errors:** `400` bad path or mode, nothing launchable found, missing name/version/loader, a port
already in use, an in-place folder already used by another server, or a folder that contains the server
root. A copy follows symlinks, so it never shares files with the original

#### `GET /api/servers/<server_id>`

Single server, augmented with runtime state. `404` if unknown.

While the server runs, `runtime.ram` holds the process's resident memory:

```json
{ "rssBytes": 5234491392 }
```

**Query:** `heap=1` adds JVM heap figures, read from the running server with `jcmd`:

```json
{
  "rssBytes": 5234491392,
  "heapUsedBytes": 2147483648,
  "heapCommittedBytes": 3221225472,
  "heapMaxBytes": 4294967296
}
```

The heap figures are left out when they can't be read (no `jcmd` next to the server's Java, or a
GC output format Fabricator doesn't recognise). Probing starts a short-lived JVM, so results are
cached for 5 seconds per server, and only this endpoint probes — the server list and other
endpoints never do.

#### `PUT /api/servers/<server_id>/settings`

Update server settings and rewrite `server.properties`. `id` and `createdAt` are ignored if sent.

These fields are launch tuning rather than `server.properties`, and take effect on the next start:

| Field | Meaning |
| --- | --- |
| `memory` | Maximum heap (`-Xmx`), in `memoryUnit`. Must be a finite number greater than zero. |
| `memoryMin` | Initial heap (`-Xms`), in `memoryUnit`. Same rules as `memory`, and must not exceed it. Records without it launch with `-Xms` equal to `-Xmx`. |
| `memoryUnit` | `GB` (default) or `MB`; applies to both `memory` and `memoryMin`. |
| `javaPath` | JVM to run this server on. A path is checked for existence and the executable bit; a bare command name (`java`, `java21`) is accepted and resolved on `PATH` at launch. Empty string clears the override, falling back to a managed runtime matching the MC version. |
| `jvmArgs` | Extra JVM flags, as the string the user typed (split shell-style at launch). Appended **after** the installer's own `launch.jvm_args`, so a repeated option resolves in the user's favour. Empty string clears them. |

`jvmArgs` refuses arguments that would contradict other settings or the installer: `-Xmx`/`-Xms`
and friends (use the `memory` / `memoryMin` fields, or the two would silently disagree), `-jar` / `-cp` /
`--class-path` / `--module-path`, and `@argfile`. Max 2000 characters and 64 arguments.

**Errors:** `409` if the server is running (stop it first) · `404` unknown server · `400` invalid
`javaPath` / `jvmArgs`, with a message naming the offending value · `400` invalid memory fields
(`Memory unit must be GB or MB`, `Memory values must be numbers`, `Memory values must be finite
numbers`, `Memory values must be greater than zero`, `Minimum memory cannot exceed maximum
memory`) · `500` if `server.properties` cannot be written

A partial update is checked against the stored values: sending only `memory` fails if it would drop
below the stored `memoryMin`.

In managed mode `javaPath`, `jvmArgs`, `command` and `launch` are all rejected with `400` — the
deployment owns the JVM. The memory fields are accepted but ignored at launch: the heap is pinned to
`FABRICATOR_MANAGED_MEMORY_GB` for both `-Xms` and `-Xmx`. `POST /api/servers` validates `javaPath`,
`jvmArgs` and the memory fields the same way.

#### `PUT /api/servers/<server_id>/autostart`

Set the boot auto-start mode. This is a Fabricator-level preference, so it can be changed while
the server is running and does not touch `server.properties`.

**Body:** `{ "mode": "always" | "never" | "last" }`

**Response (200):** `{ "success": true, "autoStart": "always", "server": { ... } }`

#### `DELETE /api/servers/<server_id>`

Stops the process, removes scheduled backups, backup records and scheduled commands, deletes the
server record, then
deletes the install directory from disk. Servers imported in place (`external`) keep their folder.

**Response (200):** `{ "success": true, "message": "Server deleted successfully" }`
**Response (500):** the record is gone but the files could not be removed — the body carries
`error` and `success: false`.

#### `POST /api/servers/<server_id>/install`

Starts the install asynchronously. Java guards run synchronously and fail fast; on success the
download/subprocess work happens in a background thread.

**Response (202):** the current progress entry, e.g. `{ "active": true, "phase": "starting", ... }`

**Errors:**
- `400` no loader/version configured, unsupported loader, or a Java guard failed. Java failures
  include `required_java`, `detected_java`, `java_missing`, `java_too_old`, `compatibility` and
  `recommended_install`.
- `409 {"error": "Another operation is in progress for this server"}`

#### `GET /api/servers/<server_id>/install/progress`

Poll install progress. `active` is `true` until `phase` becomes `done` or `failed`. A missing entry
also reports `active: false`, which is what you see if the backend restarted mid-install. The
frontend polls at roughly 750 ms intervals. Returns `404` for an unknown server.

```json
{
  "active": true,
  "phase": "downloading_server_jar",
  "bytes_done": 12058624,
  "bytes_total": 48234496,
  "server_id": "srv_a1b2c3d4",
  "loader": "fabric"
}
```

**Phase vocabulary** (set by the concrete loaders; treat as opaque strings elsewhere):

| Phase | Meaning |
|---|---|
| `starting` | worker thread spawned |
| `resolving_versions` | versions API call in flight |
| `downloading_installer` | installer JAR download (carries `bytes_done`/`bytes_total`) |
| `downloading_server_jar` | server JAR download (carries `bytes_done`/`bytes_total`) |
| `verifying` | SHA1 check |
| `running_installer` | subprocess execution; phase only, no bytes |
| `detecting_artifacts` | post-install file detection |
| `writing_eula` | final `eula.txt` write |
| `done` | completed successfully |
| `failed` | failed; the entry carries `error` |

#### `POST /api/servers/<server_id>/start`

**Response (200):** `{ "success": true, "message": "...", "server": { ... }, "compatibility": { ... } }`

**Errors:**
- `400` server still `pending` (install first), currently `installing`, or a Java check failed —
  the body carries `java_missing`, `java_too_old`, `required_java`, `detected_java` and
  `recommended_install`.
- `409` a stop is still draining.

#### `POST /api/servers/<server_id>/stop`

Always returns `200`. If the server is running the status flips to `stopping` and the actual stop
runs in a background thread; poll the server record for the final state.

#### `POST /api/servers/<server_id>/restart`

**Response (200):** `{ "success": true, "message": "...", "details": { ... }, "server": { ... } }`
**Error (400):** restart could not complete; `success` is `false`.

#### `GET /api/servers/<server_id>/logs`

**Query:** `limit` (int, default `200`) — applied to each stream separately.

```json
{
  "stdout": [{ "ts": "2026-01-04T12:00:00Z", "text": "Done (7.481s)! For help, type \"help\"" }],
  "stderr": [],
  "running": true
}
```

When no process is registered for the server, the response is
`{ "stdout": [], "stderr": [], "running": false, "message": "Server is not running" }`. This route
does not validate the server id, so an unknown id yields that same not-running body rather than a
`404`.

---

## Server files

All paths are resolved relative to the server's install directory and are rejected if they escape it.

#### `GET /api/servers/<server_id>/files`

**Query:** `path` (string, optional) — relative subdirectory; omit for the install root.

```json
{
  "currentPath": "config",
  "absolutePath": "/srv/servers/srv_a1b2c3d4/config",
  "entries": [
    {
      "name": "fabric",
      "size": 40960,
      "updatedAt": "2026-01-04T12:00:00Z",
      "path": "/srv/servers/srv_a1b2c3d4/config/fabric",
      "relativePath": "config/fabric",
      "isDir": true
    }
  ]
}
```

Directories are listed first, then files, both alphabetically. Directory sizes are the recursive
sum of their contents.

**Errors:** `400` invalid path · `404` directory not found

#### `GET /api/servers/<server_id>/files/search`

Recursive name search over the install directory.

**Query:** `q` (string, **required**) — case-insensitive substring matched against entry names ·
`path` (string, optional) — restrict the search to a subdirectory · `limit` (int, optional,
default `200`, max `500`)

```json
{
  "query": "properties",
  "scope": "",
  "results": [
    {
      "name": "mod.properties",
      "size": 1420,
      "updatedAt": "2026-01-04T12:00:00Z",
      "path": "/srv/servers/srv_a1b2c3d4/config/mod.properties",
      "relativePath": "config/mod.properties",
      "parentPath": "config",
      "isDir": false
    }
  ],
  "truncated": false
}
```

Symlinked directories are not followed. `size` is `null` for directories — unlike the browse
endpoint, search does not compute recursive directory sizes. `truncated` is `true` when the hit
`limit` was reached or the internal scan cap (200,000 entries) fired, meaning more matches may
exist.

**Errors:** `400` missing `q` or invalid path · `404` directory not found

#### `GET /api/servers/<server_id>/files/content`

**Query:** `path` (string, **required**)

**Response (200):** `{ "path": "server.properties", "content": "..." }`
**Errors:** `400` path missing/invalid, or the file is not UTF-8 text · `404` file not found

#### `PUT /api/servers/<server_id>/files/content`

**Body:** `{ "path": "server.properties", "content": "..." }`

Content is saved as raw UTF-8 text, not parsed or reformatted. The web editor
supports `.json5` configs (including comments, trailing commas, single-quoted
strings and unquoted keys) with JSON5 syntax diagnostics; `.json` files retain
strict JSON diagnostics. Saving preserves the text rather than serializing it
as JSON.

**Response (200):** `{ "success": true }`
**Errors:** `400` path/content missing or invalid · `404` file not found · `500` write failed

#### `GET /api/servers/<server_id>/icon`

The server's `server-icon.png`. **Errors:** `404` no icon set

#### `PUT /api/servers/<server_id>/icon`

Set the server-list icon. **Body:** the raw image bytes (any format Pillow reads, up to 10 MB and 4096×4096). Centre-cropped to square, resized to 64×64 and written as `server-icon.png`. Minecraft reads it at start, so a running server needs a restart.

**Response (200):** `{ "success": true }`
**Errors:** `400` not an image / too many pixels · `409` server not installed yet · `413` over 10 MB

#### `DELETE /api/servers/<server_id>/icon`

Remove the icon. Succeeds when there is none. **Response (200):** `{ "success": true }`

---

## Mods

#### `GET /api/servers/<server_id>/mods`

List installed mod files, sorted by name. Same entry shape as the file browser.

#### `DELETE /api/servers/<server_id>/mods/<filename>`

**Response (200):** `{ "success": true, "message": "sodium.jar removed" }`
**Errors:** `400` invalid path · `404` mod file not found

#### `DELETE /api/servers/<server_id>/mods`

Bulk delete. **Body:** `{ "filenames": ["a.jar", "b.jar"] }`

**Response (200):** `{ "success": true, "deleted": ["a.jar"], "errors": [{ "filename": "b.jar", "error": "File not found" }] }`
**Error (400):** `filenames` missing or empty

#### `PATCH /api/servers/<server_id>/mods`

Bulk enable/disable by renaming `x.jar` ↔ `x.jar.disabled`. Loaders only load `*.jar`, so a disabled jar stays in the list but isn't loaded; takes effect on the next start. Jars already in the requested state are skipped silently.

**Body:** `{ "filenames": ["a.jar", "b.jar.disabled"], "enabled": false }`

**Response (200):** `{ "success": true, "renamed": { "a.jar": "a.jar.disabled" }, "errors": [{ "filename": "c.jar", "error": "c.jar.disabled already exists" }] }`
**Error (400):** `filenames` missing or empty, or `enabled` not a boolean

---

## Console & metrics

#### `POST /api/servers/<server_id>/console`

Send a command to the running server's stdin.

**Body:** `{ "command": "say hello" }`

**Response (200):** the registry result (`success: true`). **Error (400):** command missing, or the
registry rejected it (e.g. the server is not running).

#### `GET /api/servers/<server_id>/metrics`

```json
{ "status": "running", "ram": { "rssBytes": 2147483648 }, "pid": 12345 }
```

#### `GET /api/metrics/system`

```json
{
  "cpu": { "percent": 12.4 },
  "memory": { "percent": 48.2, "totalBytes": 16777216000, "usedBytes": 8087896064 }
}
```

**Error (500):** `psutil` is not installed.

### Performance history and guided profiling

All paths below are relative to `/api/servers/<server_id>/diagnostics` and
require server visibility. Responses are `Cache-Control: no-store`.
MCP tokens cannot access these endpoints.

| Method / path | Permission | Request |
|---|---|---|
| `GET` (base path) | `server.metrics.read` | `window=1h` (default), `6h` or `24h`; other values return 400 |
| `PUT /sampling` | `diagnostics.manage` | `{"jvmSampling": true}` to enable; `false` to disable |
| `POST /captures` | `diagnostics.manage` | `{"durationSeconds": 60, "acceptUpload": true}` |

History returns `samples`, `captures`, `capability`, `captureReady` (live runtime
readiness), `jvmSampling`, `intervalSeconds` (30), `retentionSeconds` (86400),
`samplingEnabled` and `samplingError`. Each sample has Unix seconds `ts`,
`running`, an opaque JVM `run` boundary when running, and available fields:

| Fields | Meaning |
|---|---|
| `tps`, `mspt` | Loader TPS or vanilla tick-cost estimate; measured milliseconds/tick |
| `cpu`, `rssBytes`, `heapUsedBytes` | Raw process CPU% (may exceed 100), resident and JVM heap bytes |
| `gcCollections`, `gcTimeMs` | Cumulative JVM GC counters and milliseconds, not pause percentiles |
| `volumeUsedBytes`, `volumeTotalBytes` | Shared server filesystem capacity, not world size or process I/O |

**Missing fields are unavailable, not zero.** TPS/MSPT use the latest non-stale
observations, not independent instantaneous measurements. MSPT comes from
vanilla-family `tick query` or Forge overall output; modern Paper also receives
`minecraft:tick query` without replacing its rolling one-minute TPS. Charts
break at unavailable samples, collection gaps and restarts; times are local.

Opt-in heap/GC sampling runs `jstat -gc` next to the running server's Java binary,
with a two-second timeout. Young/old heap KiB are converted to bytes (excluding
metaspace); `GCT` seconds become milliseconds. Missing tools, unsupported output
and attach failures produce a `jvmUnavailable` reason, not invented readings.
The existing opt-in Overview `jcmd` display is unchanged.

Captures accept only 60, 120 or 300 seconds and require explicit external-upload
consent and a ready matching runtime. Unsupported runtimes, duplicate captures
or failed commands return 409. Submission returns 202 with `requested` status,
not a confirmed capture. The sole command is `spark profiler start --timeout
<seconds>`. No installation, restart or runtime upgrade occurs.

Guided capture supports released Paper 1.21+ in the `1.x` version scheme:
[Paper profiling](https://docs.papermc.io/paper/profiling/),
[spark commands](https://spark.lucko.me/docs/Command-Usage). Forks, other loaders,
snapshots and unknown/year-numbered versions need manual compatibility checks.
Bundled spark can still be disabled or fail; acknowledgement and report receipt
are tracked separately.

Spark uploads when the timeout ends. **Reports may contain operational data;
anyone with the URL may view them.** Show this consent before every capture.
Only status, start time, duration and a validated
`https://spark.lucko.me/<alphanumeric-id>` reference are retained, not report
contents or logs. References follow server metrics permissions, open externally
without a referrer, and are never fetched or embedded by Fabricator.
Statuses are `requested`, `running`, `ready`, `failed`, or `interrupted`.
No acknowledgement is not proof of failure; missing reports are not retried.
Interrupted tracking may leave the bounded command running: inspect the console.
Local reference expiry does not delete the external spark report.

`diagnostics.sqlite3` lives next to the server index. Background collection runs
without a browser; retention is 24 hours, at most 2880 samples and 20 captures
per server. Sweeps expire records and remove orphans; server deletion clears its
state. Restarts mark unfinished tracking interrupted. Only one guided capture
per server is tracked. `FABRICATOR_DISABLE_DIAGNOSTICS=1` disables collection,
not reads; sampling failures appear in `samplingError` and application logs.

---

## Loader versions

Lists the Minecraft and loader versions a registered loader can install. The frontend
(`ServerCreateModal`) uses these as soon as a loader is picked. Loaders registered in
`LOADER_REGISTRY` are exposed here automatically. Lookup is case-insensitive.
Registered today: `fabric`, `forge`, `neoforge`, `quilt`, `paper`, `folia`,
`purpur`, `pufferfish`, `vanilla`.

#### `GET /api/loaders/<loader>/versions/game`

Supported Minecraft versions, in a normalized schema:

```json
[
  { "version": "1.21.4", "stable": true,  "type": "release"  },
  { "version": "24w45a", "stable": false, "type": "snapshot" }
]
```

`stable` is the only field every consumer can rely on. `type` is loader-native (e.g.
`release`/`snapshot` for Vanilla) and may be absent when a loader makes no such distinction.

**Error (404):** `{ "error": "Unknown loader: <name>" }`

#### `GET /api/loaders/<loader>/versions/loader`

Loader-specific versions. Loaders without a separate loader version (e.g. Vanilla) return `[]`.

**Query:** `mc_version` (string, optional) — filter to one Minecraft version.

**Response (200):** a loader-native array; the shape varies per loader and the frontend passes it
through opaquely.

Fabric example:
```json
[{ "loader": { "version": "0.16.0", "stable": true } }]
```

---

## Java management

Fabricator can download and manage its own Temurin runtimes alongside whatever Java is on `PATH`.
Set `FABRICATOR_SKIP_JAVA_CHECK=1` to bypass enforcement.

#### `GET /api/java/status`

**Query:** `mc_version` (string, optional) · `loader` (string, optional) · `required_java` (int, optional) ·
`java_path` (string, optional, default `java`)

With `loader=forge` and a 1.16.x-or-older `mc_version`, `meets_requirement` is also false when
Java is too new: legacy Forge only boots on Java 8 (≤1.12.2) or Java 8–11 (1.13–1.16.5).

```json
{
  "required_java": 21,
  "install_major": 21,
  "system_java": { "path": "java", "version": 17, "meets_requirement": false },
  "managed_java": { "path": "/home/u/.fabricator/java/21", "installed": true, "major": 21, "substituted": false },
  "asset": {
    "download_url": "https://api.adoptium.net/v3/binary/...",
    "filename": "OpenJDK21U-jre_x64_linux_hotspot.tar.gz",
    "size_bytes": 44000000,
    "checksum_algorithm": "sha256",
    "install_major": 21,
    "substituted": false
  },
  "asset_error": null,
  "arch": "x64",
  "compatibility": { "required_java": 21, "enforceable": true },
  "recommended_install": { "required_java": 21, "download_url": "...", "linux_install_command": "sudo apt install openjdk-21-jre-headless", "installer_type": "tar.gz", "arch": "x64" }
}
```

The flat legacy fields (`installed`, `version`, `detected_major`, `java_path`, `meets_requirement`,
`java_enforcement_skipped`, `platform`, `download_url`, `linux_install_command`) are still present
for backward compatibility.

#### `POST /api/java/install`

Start a managed Java install.

**Body:** `{ "major": 21 }`

**Response (200):** `{ "task_id": "...", "status": "queued", "requested_major": 21, "install_major": 21, "substituted": false }`
**Error (400):** `major` is not an integer, or is outside 8–99.

#### `GET /api/java/install/progress/<task_id>`

Poll an install task. **Error (404):** unknown task id.

#### `DELETE /api/java/install/<task_id>`

Signal cancellation. Best-effort: the worker only notices between phases or download chunks.

**Response (200):** the updated task
**Errors:** `404` unknown task · `409` task already in a terminal state

#### `GET /api/java/installed`

```json
{
  "managed": [{ "major": 21, "path": "/home/u/.fabricator/java/21/bin/java", "version": 21 }],
  "system": { "path": "java", "version": 17, "installed": true }
}
```

Managed entries are sorted by `major` ascending and are removable; `major` is the directory name and
`version` is the major actually reported by the binary (`null` if the probe fails). The system entry
is informational only — it lives outside Fabricator's data directory.

#### `DELETE /api/java/installed/<major>`

Remove a managed runtime. Safe: if a server later needs it, the normal resolution flow prompts to
reinstall.

**Response (200):** `{ "success": true, "major": 21 }`
**Errors:** `400` invalid major · `404` no managed install for that major

---

## Players

Player names must match `[A-Za-z0-9_]{1,16}`. Ban/kick reasons must be a single line of ≤ 256
characters. When the server is running, changes are applied through console commands; when it is
stopped, the JSON files are edited directly.

Offline-mode servers (`onlineMode: false`) are the exception while running. A console command that
names a player who has never joined makes Minecraft resolve a UUID that player doesn't log in with.
So whitelist changes edit `whitelist.json` with the offline UUID and then send `whitelist reload`.
Ops and bans (add or remove) for such a player return `409`. Once the player has joined, they go
through the console as usual.

Shared error responses: `409` when the operation needs a different server state ·
`404` when the player is not in the list or is unknown to Mojang · `502` when the Mojang lookup fails.

#### `GET /api/servers/<server_id>/players/state`

`whitelist`, `ops`, `bans`, `ipBans` and `knownPlayers` are passed through verbatim from the
server's `whitelist.json`, `ops.json`, `banned-players.json`, `banned-ips.json` and `usercache.json`
— so their entries carry whatever vanilla writes, not a Fabricator-defined schema. A missing file
reads as `[]`.

```json
{
  "whitelist": [{ "uuid": "...", "name": "Steve" }],
  "ops": [{ "uuid": "...", "name": "Steve", "level": 4, "bypassesPlayerLimit": false }],
  "bans": [],
  "ipBans": [],
  "knownPlayers": [],
  "whitelistActive": false,
  "enforceWhitelist": false,
  "onlineMode": true
}
```

#### `GET /api/servers/<server_id>/players/online`

Currently connected players, tracked from the server's stdout. Returns `[]` when the server is not
running.

```json
[{ "name": "Steve", "uuid": "853c80ef-3c37-49fd-aa49-938b674adae6", "joinedAt": "2026-01-04T12:00:00+00:00" }]
```

`uuid` is backfilled from the local Mojang cache when available and is `null` otherwise — this
endpoint makes no network calls.

#### `POST` / `DELETE /api/servers/<server_id>/players/whitelist`

Add or remove a whitelist entry. **Body:** `{ "name": "Steve" }`

Adding a name that is already listed under a different UUID replaces that UUID. This repairs
entries that vanilla's own `whitelist add` wrote with the wrong UUID on offline-mode servers.

#### `PATCH /api/servers/<server_id>/players/whitelist/active`

Toggle the whitelist at runtime. **Body:** `{ "active": true }`

#### `PATCH /api/servers/<server_id>/players/whitelist/enforce`

Persist the `enforce-whitelist` property. Only while stopped — returns `409` when running (use
`whitelist/active` instead). **Body:** `{ "active": true }`

**Response (200):** `{ "enforceWhitelist": true }`

#### `POST /api/servers/<server_id>/players/ops`

**Body:** `{ "name": "Steve", "level": 4 }` (`level` 1–4, default `4`)

Vanilla's `op` command takes no level argument, so a running server returns `409` for any level
other than `4`.

#### `PATCH /api/servers/<server_id>/players/ops`

Change an op level. **Body:** `{ "name": "Steve", "level": 2 }`

Minecraft only reads `ops.json` at startup, so this returns `409` while the server is running.

#### `DELETE /api/servers/<server_id>/players/ops`

**Body:** `{ "name": "Steve" }`

#### `POST` / `DELETE /api/servers/<server_id>/players/bans`

**Body:** `{ "name": "Steve", "reason": "griefing" }` (`reason` optional; ignored on `DELETE`)

#### `POST` / `DELETE /api/servers/<server_id>/players/bans/ip`

**Body:** `{ "ip": "192.168.1.10", "reason": "..." }`

IPv4 or a wildcard such as `192.168.*`. Invalid values return `400`.

#### `POST /api/servers/<server_id>/players/kick`

Running server only. **Body:** `{ "name": "Steve", "reason": "..." }`

#### `DELETE /api/servers/<server_id>/players/known`

Remove a player from `knownPlayers` by deleting their `usercache.json` entry. Use it to clear names
that linger after every role was removed. **Body:** `{ "name": "Steve" }`

On a stopped server the entry is deleted right away. A running server keeps the cache in memory and
rewrites `usercache.json` from it, so there the player is hidden from `knownPlayers` immediately and
the entry is deleted just before the next start. World data is not touched, and a player who joins
again is listed again.

---

## Backups & snapshots

Backup work runs asynchronously: the endpoints return a `job_id` you poll via
`GET /api/backup-jobs/<job_id>`.

### Configs

#### `GET /api/servers/<server_id>/backup-configs`

Each config is annotated with `nextRunTime` pulled live from the scheduler.

#### `POST /api/servers/<server_id>/backup-configs`

**Body** (`name` is required; everything else has a default):
```json
{
  "name": "Nightly",
  "storagePath": "",
  "maxSnapshots": 0,
  "flush": true,
  "shutdown": false,
  "compress": true,
  "exclusions": [],
  "schedule": {
    "enabled": true,
    "frequencyHours": 24,
    "timeOfDay": "03:00",
    "timezone": "Europe/Berlin"
  }
}
```

An empty `storagePath` defaults to `<install>/backups`. `maxSnapshots: 0` means unlimited. An empty
`timezone` falls back to the host zone; a non-empty one must be a valid IANA zone.

`destination` defaults to `{"type":"local"}` (including pre-existing configs).
For S3-compatible storage, provide:

```json
{
  "destination": {
    "type": "s3",
    "profile": "default",
    "bucket": "my-backups",
    "prefix": "minecraft",
    "region": "us-east-1",
    "endpointUrl": ""
  }
}
```

The backend profile must approve the bucket and endpoint; profile/server
restrictions apply before saving or transferring. Empty region/endpoint fields
use profile defaults. Unknown destination fields, including credential fields,
are rejected. Saving validates configuration, not remote connectivity.
`storagePath` is ignored for S3: local transfer staging uses `BACKUPS_DIR`.
See [S3 operator setup and recovery](deploy/backups/README.md).

**Response (201):** the stored config, with defaults filled in and `nextRunTime` annotated.

```json
{
  "id": "bkc_dc4cb89bbf",
  "serverId": "srv_4f16a5d0",
  "name": "Nightly",
  "storagePath": "",
  "maxSnapshots": 0,
  "flush": true,
  "shutdown": false,
  "compress": true,
  "exclusions": [],
  "schedule": { "enabled": false, "frequencyHours": 24, "timeOfDay": "03:00", "timezone": "" },
  "nextRunTime": null,
  "createdAt": "2026-07-17T09:59:23.953048Z",
  "updatedAt": "2026-07-17T09:59:23.953048Z"
}
```

**Error (400):** missing `name` (`Field 'name' is required`), non-integer or negative
`maxSnapshots`, `frequencyHours <= 0`, or an invalid timezone

#### `PUT /api/servers/<server_id>/backup-configs/<config_id>`

Partial update — the same validation applies to whichever fields are present. `404` if unknown.

#### `DELETE /api/servers/<server_id>/backup-configs/<config_id>`

**Query:** `purge` (bool, optional) — when set, deletes archive files that are both inside the
config's effective storage path and recorded as snapshots of this config.

```json
{
  "success": true,
  "config_id": "bkc_dc4cb89bbf",
  "purge": true,
  "deleted_files": 3,
  "deleted_paths": ["..."],
  "retained_files": 1,
  "retained_paths": ["..."]
}
```

The response always reports both lists — no silent orphans. Without `purge`,
archives and snapshot records remain accessible and appear under `retained_paths`.
S3 locations use `s3://bucket/exact-key`; purge deletes only ownership-checked
keys recorded on each snapshot, even after changing the config destination.
Remote deletion failure returns `502` and retains the config and failed snapshot
records for retry (earlier successful deletes in the same purge stay deleted).

### Snapshots

#### `GET /api/servers/<server_id>/snapshots`

Newest-first snapshot records. Remote records have `filePath: null`,
`destination`, `objectKey` and `sha256`; these describe the original destination,
not the current configuration. `availability` is `available`, `unavailable`,
`unchecked` (remote, not checked) or `none` (no archive). Optional
`?check_remote=1` checks HEAD size/checksum metadata and returns a sanitized
`availabilityError` on failure; it does not download the archive.

#### `DELETE /api/servers/<server_id>/snapshots/<snapshot_id>`

Deletes the recorded local archive or exact remote archive/metadata keys, then
removes the record. **Errors:** `404` unknown snapshot · `500` archive deletion
failed (record retained for retry).

#### `GET /api/servers/<server_id>/snapshots/<snapshot_id>/download`

**Query:** `format` (`tar` default, or `zip`)

`zip` repacks the archive on the fly into a flat directory tree for browsers; the temp file is
cleaned up after the response.

S3 archives are downloaded to temporary local storage and SHA-256 verified before
sending either format. Temporary files are cleaned on response close.
**Errors:** `404` unknown snapshot, or local archive missing · `502` S3 failure
or checksum mismatch.

#### `POST /api/servers/<server_id>/snapshots/<snapshot_id>/restore`

**Body:** `{ "mode": "in_place" | "reset" }`

**Response (202):** `{ "success": true, "job_id": "..." }`
**Errors:** `400` invalid mode · `404` unknown snapshot

Remote archives are downloaded and verified before stopping the server. S3
configs use `<install>/backups` for the mandatory local pre-restore safety copy.
No live files change if download/verification or the safety snapshot fails.
Job progress includes `uploading`, `verifying`, `publishing_metadata` and
`downloading`, with `transferred_bytes` and `total_bytes` during transfers.

### Running backups

#### `POST /api/servers/<server_id>/backup-configs/<config_id>/run`

Run a configured backup now. **Response (202):** `{ "success": true, "job_id": "..." }`

#### `POST /api/servers/<server_id>/backup-quick`

Ad-hoc backup without a stored config.

**Body:** `{ "storagePath": null, "compress": true, "flush": true, "shutdown": false }` (all optional)

**Response (202):** `{ "success": true, "job_id": "..." }`

#### `GET /api/servers/<server_id>/backup-summary`

```json
{
  "total_snapshots": 12,
  "total_size_bytes": 4823449600,
  "last_snapshot": { "id": "...", "createdAt": "..." },
  "next_run": { "config_id": "bkc_dc4cb89bbf", "config_name": "Nightly", "next_run_time": "..." },
  "configs_count": 2,
  "defaultStoragePath": "/srv/servers/srv_a1b2c3d4/backups"
}
```

#### `POST /api/servers/<server_id>/world-import`

Upload a world archive and replace the server's active world. The raw archive bytes are the request
body (`fetch(url, { body: file })`); the display name comes from the `filename` query parameter or
the `X-Filename` header.

**Response (202):** `{ "success": true, "job_id": "..." }`
**Errors:** `400` empty upload or invalid archive · `413` upload exceeds the limit
(`FABRICATOR_MAX_WORLD_UPLOAD_BYTES`)

#### `GET /api/backup-jobs/<job_id>`

Poll any backup, restore or world-import job. Job ids are globally unique, so this route is not
server-scoped.

**Response (200):** `{ "active": true, ... }` · **Error (404):** unknown job

---

## Scheduled commands

A schedule is an ordered list of steps that run when its trigger fires. Each step is a console
`command`, or a `start`, `stop` or `restart` of the server, and waits `delaySeconds` (0–3600)
before it runs. Schedules are stored on the server record and re-registered at boot.

If the server is not running when a schedule fires, the run is **skipped**, unless the first step is
`start`. A failed step ends the run. Only the last run is kept (`lastRun`, in memory, cleared on
panel restart).

The routes below are UI-only: API tokens of either scope get `403`. A `read` token still sees the
list as the `schedules` field of the server record (`GET /api/servers/<server_id>`), the same way
the console log shows commands that have run.

#### `GET /api/servers/<server_id>/schedules`

The server's schedules, each annotated with `nextRunTime` (ISO UTC, `null` when disabled) and
`lastRun` (`{ "at", "status": "ok" | "skipped" | "error", "message" }` or `null`).

#### `POST /api/servers/<server_id>/schedules`

**Body:**
```json
{
  "name": "Daily restart",
  "enabled": true,
  "timezone": "Europe/Berlin",
  "trigger": { "type": "daily", "time": "04:00", "days": ["mon", "fri"] },
  "steps": [
    { "action": "command", "command": "say Restarting in 5 minutes", "delaySeconds": 0 },
    { "action": "restart", "delaySeconds": 300 }
  ]
}
```

`trigger` is one of:

| `type` | Fields | Notes |
|---|---|---|
| `interval` | `minutes` (1–10080) | Counted from when the schedule is saved or the panel starts. |
| `daily` | `time` (`HH:MM`), `days` (`mon`…`sun`) | Empty `days` means every day. |
| `cron` | `expression` (5 fields) | The day-of-week field must use names (`mon-fri`), not numbers, and may not be combined with a day of month. |

Times are read in `timezone` (IANA). An empty `timezone` falls back to the host zone. There can be
1–20 steps. A `command` must be a single line of at most 1000 characters.

**Response (201):** the stored schedule with its `id` (`sch_…`), `nextRunTime` and `lastRun`.
**Error (400):** `{ "error": "..." }` naming the invalid field.

#### `PUT /api/servers/<server_id>/schedules/<schedule_id>`

Replaces the schedule. The body has the same shape as `POST`. **Error (404):** unknown schedule.

#### `DELETE /api/servers/<server_id>/schedules/<schedule_id>`

**Response (200):** `{ "success": true }` · **Error (404):** unknown schedule

#### `POST /api/servers/<server_id>/schedules/<schedule_id>/run`

Runs the schedule now, in the background, whether or not it is enabled. Check `lastRun` for the
result. **Response (202):** `{ "success": true }`

---

## Modrinth integration

**Rate limiting.** Modrinth allows 300 requests/minute **per IP**. Every call Fabricator makes goes
through one process-wide token bucket (250/min, leaving headroom for anything else sharing the IP),
which also reads the `X-Ratelimit-Remaining` the API reports and pauses everyone on a 429 for as
long as `Retry-After` asks. Any endpoint below can therefore return:

**Error (429):** `{ "error": "...", "retry_after": 12.0 }` — also sent as a `Retry-After` header.
`retry_after` is seconds; wait that long rather than retrying immediately.

#### `GET /api/modrinth/servers/<server_id>/resolve-installed`

Identify every `.jar` in the server's mods folder by content hash. One request covers the whole
folder — the backend hashes the jars and asks Modrinth's bulk `version_files` endpoint, then caches
the results (a file hash maps to one version permanently, so a page refresh costs nothing).

Use this instead of looking mods up by name: filenames are not reliable identifiers, and per-file
lookups do not fit in the rate limit for a large modpack.

```json
{
  "resolved": {
    "sodium-fabric-0.6.0+mc1.21.1.jar": {
      "projectId": "AANobbMI",
      "slug": "sodium",
      "title": "Sodium",
      "iconUrl": "https://cdn.modrinth.com/...",
      "versionId": "xexnGRr6",
      "versionNumber": "mc1.21.1-0.6.0"
    }
  }
}
```

Jars Modrinth does not recognise — hand-modified, repackaged, or never published there — are simply
absent from `resolved`. A missing mods folder returns `{ "resolved": {} }`.

**Error (404):** unknown server · **(400):** mods folder could not be resolved

#### `GET /api/modrinth/servers/<server_id>/updates`

Jars in the mods folder that have a newer release for the server's Minecraft version and loader.
One upstream request for the whole folder (Modrinth's bulk `version_files/update`). Releases only;
jars pinned to a chosen version and jars Modrinth does not recognise are left out.

```json
{
  "updates": {
    "sodium-fabric-0.6.0+mc1.21.1.jar": {
      "projectId": "AANobbMI",
      "versionId": "u1PEsyCU",
      "versionNumber": "mc1.21.1-0.6.13-fabric"
    }
  },
  "dependencies": [
    { "projectId": "P7dR8mSH", "slug": "fabric-api", "title": "Fabric API" }
  ]
}
```

`dependencies` lists required dependencies of those new versions that no installed jar provides
(matched by hash and the install manifest). Install them first with a plain
`POST /api/modrinth/mod/<projectId>/install`, then apply each update with `version_id`, `replaces`
(the old filename) and `pin: false`.

**Error (404):** unknown server · **(400):** mods folder could not be resolved

#### `GET /api/modrinth/search`

**Query:** `query` (string) · `mc_version` (string, optional) · `loader` (string, optional) ·
`project_type` (`mod` default, or `plugin` — Bukkit-family servers browse plugins through this
same route; any other value falls back to `mod`) · `limit` (int, default `20`) ·
`offset` (int, default `0`) ·
`index` (`downloads` default, `relevance`, `follows`, `newest`, `updated`)

```bash
GET /api/modrinth/search?query=sodium&mc_version=1.20.1&loader=fabric&limit=10
```

```json
{
  "hits": [
    {
      "project_id": "AANobbMI",
      "slug": "sodium",
      "title": "Sodium",
      "description": "A modern rendering engine...",
      "downloads": 90301924,
      "icon_url": "https://cdn.modrinth.com/...",
      "project_type": "mod",
      "versions": ["1.20.1", "1.20.2"],
      "client_side": "required",
      "server_side": "optional"
    }
  ],
  "offset": 0,
  "limit": 10,
  "total_hits": 1
}
```

#### `GET /api/modrinth/modpacks/search`

Same parameters and response shape as `/search`, restricted to modpacks.

#### `GET /api/modrinth/project/<project_id>`

Project details. `GET /api/modrinth/mod/<mod_id>` is a legacy alias with identical behaviour.

```json
{
  "id": "AANobbMI",
  "slug": "sodium",
  "title": "Sodium",
  "description": "The fastest and most compatible...",
  "downloads": 90301924,
  "categories": ["optimization", "fabric"],
  "client_side": "required",
  "server_side": "optional",
  "versions": ["versionId1", "versionId2"],
  "game_versions": ["1.20.1", "1.20.2"],
  "loaders": ["fabric"]
}
```

#### `GET /api/modrinth/project/<project_id>/versions`

All versions of a project. `GET /api/modrinth/mod/<mod_id>/versions` is a legacy alias.

**Query:** `loaders` (repeatable) · `game_versions` (repeatable) · `featured` (bool, optional)

```bash
GET /api/modrinth/project/sodium/versions?loaders=fabric&game_versions=1.20.1
```

```json
[
  {
    "id": "versionId",
    "version_number": "0.5.13",
    "name": "Sodium 0.5.13 for Fabric",
    "version_type": "release",
    "date_published": "2025-03-03T17:45:49.132919Z",
    "downloads": 12345,
    "game_versions": ["1.20.1"],
    "loaders": ["fabric"],
    "files": [
      {
        "url": "https://cdn.modrinth.com/.../sodium-fabric-0.5.13+mc1.20.1.jar",
        "filename": "sodium-fabric-0.5.13+mc1.20.1.jar",
        "primary": true,
        "size": 1234567,
        "hashes": { "sha512": "...", "sha1": "..." }
      }
    ]
  }
]
```

#### `GET /api/modrinth/project/<project_id>/resolve-version`

Resolve the best version for a target.

**Query:** `mc_version` (string, **required**) · `loader` (string, optional)

```json
{
  "project_id": "sodium",
  "mc_version": "1.20.1",
  "loader": "fabric",
  "version": { "id": "...", "version_number": "0.5.13", "files": ["..."] },
  "download_url": "https://cdn.modrinth.com/..."
}
```

`version` is the complete Modrinth version object (the same shape `/versions` returns), not a
summary. `loader` echoes back whatever you sent, including `null`.

**Errors:** `400` `mc_version` missing · `404` no suitable version found

#### `GET /api/modrinth/version/<version_id>`

A single Modrinth version by id.

#### `GET /api/modrinth/mod/<mod_id>/download-url`

Direct download URL for the best matching version.

**Query:** `mc_version` (string, **required**) · `loader` (string, optional, default `fabric`)

```json
{ "download_url": "https://cdn.modrinth.com/data/AANobbMI/versions/OihdIimA/sodium-fabric-0.5.13%2Bmc1.20.1.jar" }
```

**Errors:** `400` `mc_version` missing · `404` no suitable version found

#### `POST /api/modrinth/mod/<mod_id>/install`

Download a mod and install it into the server's mods folder. The download is hash-verified.

**Body:** `mc_version` (string, **required**) · `server_id` (string, **required**) ·
`loader` (string, optional, default `fabric`) · `version_id` (string, optional: install exactly this
version, recorded as pinned) · `replaces` (string, optional: jar to remove once the new one is on
disk) · `pin` (bool, optional: `false` installs `version_id` without pinning it, as update-all does)

```json
{ "mc_version": "1.20.1", "server_id": "srv_a1b2c3d4", "loader": "fabric" }
```

**Response (200):**
```json
{
  "success": true,
  "message": "Mod installed successfully",
  "file": "sodium-fabric-0.5.13+mc1.20.1.jar",
  "path": "/srv/servers/srv_a1b2c3d4/mods/sodium-fabric-0.5.13+mc1.20.1.jar"
}
```

**Errors:** `400` `server_id` or `mc_version` missing, or a `mods_folder` override was sent (no
longer allowed) · `404` server not found, or no suitable version · `409` another operation holds
the server lock

#### `POST /api/modrinth/modpack/<project_id>/install`

Install a modpack into a server. The server must be stopped.

**Body:** `server_id` (string, **required**) · `mc_version` (string, optional) ·
`loader` (string, optional) · `clean_install` (bool, default `true`) ·
`create_backup` (bool, default `true`) · `allow_missing` (bool, default `false`) ·
`mod_side_overrides` (object, optional)

**Response (200):** the install result plus `success`, `message`, `backup_file` (when a backup was
made) and `java_warning` (when the pack's Minecraft version needs a newer Java than the one detected).

**Errors:** `400` server running, or install path unresolvable · `404` server not found ·
`409` an install is already in progress · `500` backup or install failure

#### `POST /api/modrinth/modpack/upload`

Stage a `.mrpack` exported from the Modrinth app. Nothing is installed — the response describes
what the pack declares, so a server can be created to match it before the pack is installed.

**Body:** the raw archive bytes (`Content-Type: application/octet-stream`). The display filename
comes from the `filename` query param or the `X-Filename` header.

**Response (201):**

```json
{
  "success": true,
  "upload_id": "8f14e45fceea167a5a36dedd4bea2543",
  "filename": "my-pack.mrpack",
  "size_bytes": 41233,
  "name": "My Pack",
  "version": "1.2.3",
  "summary": "...",
  "minecraft_version": "1.20.1",
  "loader": "fabric",
  "loader_version": "0.15.7",
  "file_count": 118,
  "client_only_count": 24,
  "dependencies": { "minecraft": "1.20.1", "fabric-loader": "0.15.7" },
  "has_overrides": true,
  "has_server_overrides": false
}
```

Staged uploads expire after 6 hours and are swept on the next upload.

**Errors:** `400` empty body, not a zip, no `modrinth.index.json`, malformed index, or a pack for
another game · `413` over `FABRICATOR_MAX_MRPACK_UPLOAD_BYTES` (default 2 GiB), or expanding past
`FABRICATOR_MAX_MRPACK_EXTRACTED_BYTES` (default 16 GiB)

#### `POST /api/modrinth/modpack/upload/<upload_id>/install`

Install a staged `.mrpack` onto a server. The server must be stopped. Same install semantics as
`/modpack/<project_id>/install` — side classification, overrides, the missing-file 409 and the
non-blocking `uncertain_mod_files` report all behave identically.

**Body:** `server_id` (string, **required**) · `loader` (string, optional — defaults to what the
pack declares) · `clean_install` (bool, default `true`) · `create_backup` (bool, default `true`) ·
`allow_missing` (bool, default `false`) · `mod_side_overrides` (object, optional) ·
`force` (bool, default `false`)

The upload survives a failed install so the missing-file retry can reuse it; a successful install
consumes it.

**Errors:** `400` server running, or install path unresolvable · `404` server not found, or the
upload expired · `409` an install is already in progress; missing files; or — without `force` —
the pack targets a different Minecraft version or loader than the server runs, in which case the
body carries `can_continue_with_mismatch`, `pack_mc_version`, `pack_loader`, `server_mc_version`,
`server_loader` and `reasons`

#### `DELETE /api/modrinth/modpack/upload/<upload_id>`

Discard a staged upload. **Errors:** `404` unknown or already-consumed upload.

#### `GET /api/modrinth/modpack/install-progress/<server_id>`

**Response (200):** `{ "active": true, "stage": "downloading", "current": 12, "total": 80, "detail": "..." }`
or `{ "active": false }` when nothing is running.

#### `GET /api/modrinth/categories`

```json
[{ "icon": "...", "name": "adventure", "project_type": "mod", "header": "Adventure" }]
```

#### `GET /api/modrinth/loaders`

```json
[{ "icon": "...", "name": "fabric", "supported_project_types": ["mod", "modpack"] }]
```

#### `GET /api/modrinth/game-versions`

```json
[{ "version": "1.20.1", "version_type": "release", "date": "2023-06-12T00:00:00Z", "major": true }]
```

Upstream Modrinth failures are surfaced with the upstream status code (or `502` when there is none).

---

## playit.gg tunnel

Exposes a server publicly through a playit.gg tunnel. Not supported on Windows — every endpoint
then reports `status: "unsupported"`.

#### `GET /api/playit/status`

```json
{
  "status": "running",
  "claim_url": null,
  "error_reason": null,
  "binary_trust": "verified",
  "tunnels": [
    {
      "local_port": 25565,
      "address": "example.gl.at.ply.gg:12345",
      "disabled_reason": null,
      "name": "my-tunnel",
      "tunnel_type": "minecraft-java"
    }
  ],
  "tunnels_known": true
}
```

`status` is daemon/account level and is one of `unsupported`, `stopped`, `provisioning`, `claiming`,
`starting`, `running`, `error`. `error` means a real account/daemon failure — a per-tunnel
`disabled_reason` is not a global error and rides on the tunnel instead. `provisioning` appears on
the first start of an install that has no playit binaries yet: Fabricator downloads the pinned
release itself, then continues into the normal `claiming`/`starting` sequence.

`binary_trust` describes the binaries currently on disk, computed by hashing them against the pinned
release (it replaces the former `binary_verified` boolean, which was declared once at install time
and was never set at all for Docker installs):

| Value | Meaning |
| --- | --- |
| `verified` | Both binaries match the release Fabricator pins. |
| `unverified` | A binary in a Fabricator-managed directory does not match the pin — worth surfacing. |
| `system` | The binaries came from the operator's own install (e.g. a distro package, which discovery prefers). Not verified, and not a fault. |
| `missing` | Nothing installed. `error_reason` covers it once a start is attempted. |

Derive a server's public address by matching `tunnel.local_port` to that server's port; `address`
may be `null`. "No matching tunnel" is a per-server hint, not an error. `tunnels_known` stays
`false` until the first successful poll, so callers can tell "zero tunnels" apart from "not
reachable yet"; both leave `tunnels` empty.

#### `POST /api/playit/start` · `POST /api/playit/stop` · `POST /api/playit/reset`

Each performs the action and returns the full post-action status, so no follow-up poll is needed.

---

## System updates

#### `GET /api/system/update/status`

Current updater state plus the latest-version check. `selfUpdateDisabled` is `true` when the
deployment manages updates out-of-band (the container image sets `FABRICATOR_DISABLE_SELF_UPDATE=1`).

#### `POST /api/system/update`

Start an asynchronous self-update.

**Body:** `{ "version": "1.2.3" }` — optional; a release tag or `latest`.

**Response (202):** `{ "started": true, "requestedVersion": "1.2.3" }`

**Errors:**
- `403` self-update is disabled for this deployment (`FABRICATOR_DISABLE_SELF_UPDATE=1`).
- `409` the update did not start. The route maps **every** unstarted outcome to `409`, so this
  covers an update already in progress, an invalid version string, and a failure to queue the
  request — read `error` in the body to tell them apart. Note that an invalid version yields `409`
  here, not `400`.

---

## Configuration

### Environment variables

Set these in `.env` or the environment.

```bash
# Flask
FLASK_ENV=development            # or 'production'
HOST=127.0.0.1                   # loopback by default; set explicitly for remote access
PORT=5000
SECRET_KEY=...                   # session signing key; auto-generated and persisted if unset

# CORS — comma-separated allowlist. '*' is rejected; entries need an http(s) scheme and a host.
CORS_ORIGINS=http://localhost:3000

# Storage (defaults: %APPDATA%\Fabricator on Windows, ~/.fabricator on POSIX;
# /var/lib/fabricator/... under FLASK_ENV=production)
SERVER_ROOT=...                  # server install directories
SERVER_INDEX_FILE=...            # servers.json
JAVA_ROOT=...                    # managed Java runtimes
BACKUPS_DIR=...                  # backup archives
FABRICATOR_BACKUP_S3_PROFILES=... # JSON operator-approved S3 profiles/bucket grants; see deploy/backups/README.md
FABRICATOR_APPDATA=...           # override the whole data directory

# Authentication
FABRICATOR_DISABLE_AUTH=1        # turn off the built-in login entirely
FABRICATOR_AUTH_PASSWORD_HASH=... # declare the password out-of-band (blocks /change-password)
FABRICATOR_SESSION_COOKIE_SECURE=1

# Server process
SERVER_COMMAND="java -Xmx4G -jar server.jar nogui"   # fallback launch command

# Behaviour toggles
FABRICATOR_SKIP_JAVA_CHECK=1     # bypass Java version enforcement (dev/testing only)
FABRICATOR_DISABLE_SCHEDULER=1   # do not boot the backup scheduler
FABRICATOR_DISABLE_SELF_UPDATE=1 # make POST /api/system/update return 403
FABRICATOR_MAX_WORLD_UPLOAD_BYTES=...  # world-import cap; default 10 GiB
FABRICATOR_MAX_MRPACK_UPLOAD_BYTES=... # .mrpack upload cap; default 2 GiB
FABRICATOR_MAX_MRPACK_EXTRACTED_BYTES=...  # .mrpack uncompressed-size cap; default 16 GiB
FABRICATOR_MANAGED=1             # managed-hosting mode: lock deployment-owned controls (third-party hosts only)
FABRICATOR_MANAGED_MEMORY_GB=... # managed mode: pinned per-server JVM heap in whole GB; required when managed

# Self-updater
FABRICATOR_REPO=bmiddha/Fabricator
FABRICATOR_UPDATE_MODE=...
FABRICATOR_UPDATE_DIR=/var/lib/fabricator/update

# playit.gg
PLAYIT_ENABLED=true              # must be the literal 'true' (see below)
PLAYIT_RUNTIME_DIR=...           # writable dir for the agent's secret and, if the binaries are
                                 # missing, the ones Fabricator downloads (<dir>/bin)
```

`PLAYIT_BINARY_VERIFIED` is **no longer read**. `binary_trust` is now computed by hashing the
binaries on disk against the pinned release, so a value asserted once at install time can no longer
go stale. `tools/install.sh` still writes the variable into the env file; it is inert.

**Truthiness is not uniform.** Most flags (`FABRICATOR_DISABLE_AUTH`, `FABRICATOR_SKIP_JAVA_CHECK`,
`FABRICATOR_SESSION_COOKIE_SECURE`, `FABRICATOR_MANAGED`) go through `bool_from_str`, which accepts
`1`, `true`, `yes` or `on`, case-insensitively. But `FABRICATOR_DISABLE_SCHEDULER` and `FABRICATOR_DISABLE_SELF_UPDATE`
match the literal string `1` only, and `PLAYIT_ENABLED` matches the literal
lowercase `true` only — `PLAYIT_ENABLED=1` does **not** work.

`PLAYIT_ENABLED` is only a fallback: the state file written by the dashboard toggle wins when present.

`SECRET_KEY` and `FABRICATOR_AUTH_PASSWORD_HASH` are sensitive — never log them.

### Configuration classes

In `backend/core/config.py`. `get_config()` builds a fresh instance per call, so environment
overrides always take effect (no import-time snapshotting).

- `DevelopmentConfig` — debug on; data under the user's appdata directory.
- `ProductionConfig` — debug off; data under `/var/lib/fabricator`.

---

## Loader registry (developers)

The loader dispatch layer lives in `backend/server/installer/__init__.py`. Adding a loader takes
three steps:

1. **Subclass `InstallerBase`** (`backend/server/installer/base.py`) and implement:
   - `loader_name` (property) — the registry key, e.g. `"neoforge"`
   - `get_minecraft_versions()` → `List[{version, stable, type?}]` in the normalized schema (see
     `/api/loaders/<loader>/versions/game`)
   - `get_available_versions(mc_version)` → loader-native array; `[]` when the loader has no
     separate loader version
   - `install(mc_version, loader_version=None)` → `InstallResult` with `launch: LaunchSpec` set. The
     `LaunchSpec` is persisted in `servers.json` and drives `ServerProcessRegistry._build_command`

2. **Register it in `LOADER_REGISTRY`:**
   ```python
   from .neoforge import NeoForgeInstaller

   LOADER_REGISTRY: Dict[str, Type[InstallerBase]] = {
       "fabric": FabricInstaller,
       "vanilla": VanillaInstaller,
       "neoforge": NeoForgeInstaller,
       "quilt": QuiltInstaller,
       "forge": ForgeInstaller,
       "paper": PaperInstaller,
       "folia": FoliaInstaller,
       "purpur": PurpurInstaller,
       "pufferfish": PufferfishInstaller,
   }
   ```

   `get_installer_for(loader, install_path)` (case-insensitive) and `supported_loaders()` pick up
   the new entry automatically — routes and the install flow need no changes.

3. **Frontend:** add an option to `loaderOptions` in
   `frontend/src/components/modals/ServerCreateModal.vue` using the same `value` as `loader_name`.

If your installer needs Java at install time (not just at runtime), set
`requires_java_for_install` so the install route runs the extra Java guard.
`_build_command` raises `ValueError` on an unknown `LaunchSpec.type` — that is deliberate, so a
record written by a newer build is caught early on an older one.

---

## Modrinth client (Python)

`ModrinthClient` can be used directly:

```python
from pathlib import Path
from backend.modrinth.client import ModrinthClient

client = ModrinthClient()

results = client.search(project_type="mod", query="sodium", mc_version="1.20.1", loader="fabric")
resolved = client.get_project_download_url(project_id="sodium", mc_version="1.20.1", loader="fabric")
file = client.download_mod(resolved["url"], Path("./mods"), hashes=resolved["hashes"])
```

Key methods:

- `search(project_type, query, mc_version, loader, limit, offset, index)` — search projects
- `get_project(project_id)` — project details
- `get_project_versions(project_id, loaders, game_versions, featured)` — versions
- `get_version(version_id)` — a single version
- `resolve_project_version(project_id, mc_version, loader)` — best matching version + URL
- `get_project_download_url(project_id, mc_version, loader)` — resolve straight to a URL + hashes
- `download_mod(url, target_folder, hashes=...)` — hash-verified download
- `install_modpack(project_id, install_path, ...)` — resolve a Modrinth pack, download, install
- `install_mrpack_archive(mrpack_path, install_path, ...)` — install an on-disk `.mrpack`

Failures raise `ModrinthApiError`, which carries the upstream `status_code` and any `details`.

---

## Worked examples

Authentication is on unless you set `FABRICATOR_DISABLE_AUTH=1`, so log in first and reuse the
session cookie. The default base URL is `http://localhost:5000`.

```bash
# Log in and store the session cookie
curl -c cookies.txt -X POST http://localhost:5000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"password": "your-password"}'
```

### Create, install and start a server

```bash
# 1. Create the record (returns 201 with the new id, e.g. srv_a1b2c3d4)
curl -b cookies.txt -X POST http://localhost:5000/api/servers \
  -H 'Content-Type: application/json' \
  -d '{"name": "Survival", "version": "1.20.1", "loader": "fabric",
       "port": 25565, "installPath": "/var/lib/fabricator/servers/survival"}'

# 2. Lay down the files (returns 202 — the work runs in the background)
curl -b cookies.txt -X POST http://localhost:5000/api/servers/srv_a1b2c3d4/install

# 3. Poll until "active": false
curl -b cookies.txt http://localhost:5000/api/servers/srv_a1b2c3d4/install/progress

# 4. Start it
curl -b cookies.txt -X POST http://localhost:5000/api/servers/srv_a1b2c3d4/start
```

### Find and install a mod

```bash
curl -b cookies.txt "http://localhost:5000/api/modrinth/search?query=create&mc_version=1.20.1&loader=fabric"

# server_id is required — the mods folder is resolved from it
curl -b cookies.txt -X POST http://localhost:5000/api/modrinth/mod/create-fabric/install \
  -H 'Content-Type: application/json' \
  -d '{"mc_version": "1.20.1", "loader": "fabric", "server_id": "srv_a1b2c3d4"}'
```

### Back up a server

```bash
# Ad-hoc backup, no stored config needed (returns 202 + job_id)
curl -b cookies.txt -X POST http://localhost:5000/api/servers/srv_a1b2c3d4/backup-quick \
  -H 'Content-Type: application/json' -d '{"compress": true, "flush": true}'

# Poll the job
curl -b cookies.txt http://localhost:5000/api/backup-jobs/<job_id>
```

### Inspect a running server

```bash
curl -b cookies.txt http://localhost:5000/api/servers/srv_a1b2c3d4/metrics
curl -b cookies.txt "http://localhost:5000/api/servers/srv_a1b2c3d4/logs?limit=50"
curl -b cookies.txt -X POST http://localhost:5000/api/servers/srv_a1b2c3d4/console \
  -H 'Content-Type: application/json' -d '{"command": "say hello"}'
```

---

## Testing

```bash
pytest
```

---

## Security notes

- **Authentication**: single-operator password login, enabled by default; disable with
  `FABRICATOR_DISABLE_AUTH=1`. The `/api/` gate is default-deny.
- **CORS**: explicit allowlist only — `*` is rejected at startup.
- **Path traversal**: file, mod and backup paths are resolved and rejected if they escape the
  server's install directory.
- **Self-update**: gate it with `FABRICATOR_DISABLE_SELF_UPDATE=1` when the panel is exposed and
  updates come from elsewhere.
- **Modrinth rate limits**: the client sets an identifying `User-Agent` as Modrinth's policy
  requires, but it does **not** throttle, back off, or retry. Requests go out as fast as callers
  make them, and a `429` from Modrinth is surfaced to you unchanged (`ModrinthApiError` →
  HTTP `429`) rather than being retried. Modrinth's documented limit is 300 requests/minute; staying
  under it is the caller's responsibility.
- **Outbound timeouts**: Modrinth calls use a 15-second timeout.

---

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0).
Copyright © 2026 Philipp Noél Derks and Linus Sommermeyer.

AGPL-3.0 is a network copyleft licence: if you run a modified Fabricator as a network service, you
must offer its users the corresponding source of your modified version.

## Contributing

Bug reports and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the full
process. In short:

- **Bugs** and small self-contained fixes: open an issue; a PR alongside it is fine.
- **New features**, refactors, restructuring, build/CI/packaging changes: open an issue and wait for
  agreement **before** writing code. Unsolicited structural PRs are likely to be declined.
- **Security vulnerabilities**: do not open a public issue or PR — report privately.
- New behavior should come with a test; a bug fix should come with a test that fails before the fix.
  `pytest` is already in `requirements.txt`, so there is no separate dev install.

Opening a pull request means agreeing to the Contributor License Agreement in
[CONTRIBUTING.md](CONTRIBUTING.md): contributions are licensed to the public under AGPL-3.0, and you
also grant the maintainers a perpetual, royalty-free licence to relicense them, including
commercially. The CLA Assistant bot records your acceptance once per GitHub account.

## Support

- GitHub Issues: [Fabricator fork issues](https://github.com/bmiddha/Fabricator/issues)
- Modrinth API docs: [docs.modrinth.com](https://docs.modrinth.com)

## MCP integration (API tokens)

> **Beta.** This feature is young and its client-side setup may change between releases. The
> machine running your AI client needs [`uv`](https://docs.astral.sh/uv/) installed.

Fabricator can issue scoped **API tokens** so an MCP client — an AI assistant such as Claude — can
read your server's state and perform a small set of maintenance actions over the same HTTP API this
document describes. The typical use is modpack crash diagnosis: read the crash log, list the
installed mods, check a mod against Modrinth, remove or update it, restart.

The feature is **off by default**. Nothing changes for your panel until you turn it on and mint a
token.

### Enabling it and minting a token

Open **Settings** in the panel sidebar and find the **Model Context Protocol** panel.

1. Turn **Enable MCP access** on.
2. Choose **Create token**, give it a name and a scope (`read` or `manage`).
3. Copy the token immediately — it is shown **once** and cannot be retrieved afterwards. Only a
   SHA-256 hash of it is stored.

Revoke a token from the same panel; revocation takes effect on the next request.

Tokens look like `fab_<id>_<secret>`. The `<id>` is a non-secret lookup key that appears in the
token list; the `<secret>` half is what is never shown again.

### Scopes

| Scope | Can do |
|---|---|
| `read` | Read-only routes: server list and details, logs, installed mods, metrics, Java status, backups and snapshot listings, the Modrinth catalog. |
| `manage` | Everything `read` can do, plus start / stop / restart, re-install the configured loader, install or update a mod by Modrinth ID, and remove, enable or disable installed mods. |

Every `/api` route is classified into exactly one of three buckets, and the classification is
enforced by the panel itself, not by the client: **36 read**, **8 manage**, **72 never**. Routes in
the `never` bucket are refused for **every** token regardless of scope — the console, scheduled
commands, all file read
and write routes, server settings, autostart, server create and delete, Java installation, the
self-updater, playit, backup restore/download/delete, world import, whole-modpack install (including
the `.mrpack` upload routes), all player administration (bans, kicks, ops, whitelist), the
player-data reads (they contain player names, UUIDs and IP addresses), and the token-management
routes themselves. A token can never mint a token, revoke one, or flip the switch.

Requests are answered with `401` when the credential itself is not accepted (bad, revoked or
expired token, or the switch is off) and `403` when the token is valid but the route is out of its
scope or in the `never` bucket. A `403` is a fact about your configuration, not a temporary
condition — retrying it will not change the answer.

### What a token never receives

Responses on the token path are filtered server-side before they leave the panel:

- **Credential fields are removed** — for example the RCON password stored in a server record.
- **Host filesystem paths are removed**, including absolute paths embedded in error messages. A path
  is reported relative to the panel's own directories, so a failure still names the file it failed
  on without disclosing your machine's layout.
- `GET /api/java/status` **refuses** a caller-supplied `java_path`, because that value names a
  binary the panel would execute.

None of this applies to your own logged-in session: the operator sees the panel exactly as before.

### Managing tokens over HTTP

These four routes are **session-only** — they require a logged-in operator and are refused to every
token:

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/integrations/mcp` | Switch state and token metadata (never a secret) |
| `PUT` | `/api/integrations/mcp` | `{"enabled": bool}` — turn MCP access on or off |
| `POST` | `/api/integrations/mcp/tokens` | `{"name": str, "scope": "read"\|"manage"}` — mint; the response is the only time the token is shown |
| `DELETE` | `/api/integrations/mcp/tokens/<token_id>` | Revoke |

Using a token is the standard bearer scheme:

```bash
curl -H "Authorization: Bearer fab_xxx_yyy" http://localhost:5000/api/servers
```

### Behaviour worth knowing

- **Turning the switch off does not delete your tokens.** They stop being accepted (`401`) and start
  working again when you turn it back on.
- **Before the panel has a password** (first-run setup), no token can be minted or used.
- **Managed installs** hide the Model Context Protocol panel entirely.
- Tokens are stored in the same `0600` `auth.json` as your password hash.

### Security notes

- **Server logs contain text written by mods and by players**, and a `manage` token lets an
  assistant act on what it reads there. Treat a `manage` token as you would giving someone else the
  panel: prefer a `read` token for pure diagnosis, and hand out `manage` only when you want the
  assistant to be able to change something.
- **The token is stored in plain text in your MCP client's configuration file.** Treat that file
  like a password.
- Tokens carry no expiry. Revoke any token you no longer use.

### Connecting a client

The MCP server that consumes these tokens lives in [`mcp/`](mcp/) in this repository. It runs on
**your** machine, next to your assistant, and talks to the panel over the API documented above.

It needs [`uv`](https://docs.astral.sh/uv/) on your `PATH` — the one hard prerequisite, and not
something MCP clients bundle. **If `uv` is missing, your client reports the server as failing to
launch or disconnecting at startup**, which looks like a broken server rather than a panel or
token problem, because the process never starts at all.

Put this in your client's MCP configuration (the Settings panel generates it with your URL and
token filled in):

```json
{
  "mcpServers": {
    "fabricator": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/bmiddha/Fabricator@main#subdirectory=mcp",
        "fabricator-panel-mcp"
      ],
      "env": {
        "FABRICATOR_URL": "http://127.0.0.1:5000",
        "FABRICATOR_TOKEN": "YOUR_TOKEN_HERE"
      }
    }
  }
}
```

The package installs directly from the fork's active development branch. It does
not use the upstream PyPI package; pin a commit instead of `main` for reproducibility.

The token goes in `env`, never in `args` — a command line is readable by every process on the
machine and is written to shell history.

### What each scope reaches through the MCP server

| Scope | Tools |
|---|---|
| `read` | list servers · read logs · list and identify installed mods · CPU/memory · Java version checks · install progress and failure reasons · Modrinth search, project info and compatibility checks |
| `manage` | all of the above, plus start/stop/restart, install or update a mod by Modrinth id, and delete mod jars |

**`read` is the recommended default**: it answers every diagnostic question and cannot change
anything. The tool set is deliberately narrower than what a token may reach, and it is not a
security boundary — the panel is. Tools are never hidden based on scope; a `manage` tool called
with a `read` token returns the panel's `403` and the client says so.

**Mods in subfolders:** only jars directly in a server's `mods` folder are listed, and only those
can be removed through the MCP server. A jar in a subfolder must be managed in the panel UI.
