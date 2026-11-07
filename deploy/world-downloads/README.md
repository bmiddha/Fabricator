# World downloads (experimental)

Open **Backups → World downloads** on a Java instance. Export is supported on
POSIX hosts (Linux, macOS and Linux containers); race-safe Windows export is
not implemented and is explicitly refused. Managed mode and proxies cannot
generate world archives. No separate public distributor, deployment, live
snapshot or automatic server stop is provided.

1. Stop the instance in Fabricator. Also stop external JVMs and external
   filesystem writers; Fabricator cannot control them. Select a discovered
   **complete world group**, then **Export world archive**.
2. Download and inspect the private archive, including sensitive world data
   and game/mod compatibility. Generation preserves the source and never
   starts/stops a server. Existing archives can be downloaded while it runs.
3. **Review & share** explicitly acknowledges privacy risk and creates a
   bearer link expiring in 1 hour to 7 days. Copy the link immediately:
   only its SHA-256 token hash is persisted; the secret cannot be retrieved.
4. Revoke links or delete an archive (with confirmation if shared) to block
   future requests. Expiration, server deletion and artifact deletion
   invalidate access. Already-downloaded and in-flight bytes cannot be recalled.

## Exact content and compatibility

The gzip-compressed POSIX/PAX tar archive has this shape:

```text
fabricator-world.json
<level-name>/level.dat
<level-name>/region/...
<level-name>/playerdata/...
<level-name>/advancements/...
<level-name>/stats/...
<level-name>/data/...
<level-name>/datapacks/...
<level-name>/DIM-1/...                         (nested Java)
<level-name>/DIM1/...
<level-name>/DIM<number>/...                    (legacy/mod dimensions)
<level-name>/dimensions/<namespace>/<path>/...  (custom/mod dimensions)
<level-name>_nether/DIM-1/...                   (Bukkit split instead)
<level-name>_the_end/DIM1/...
```

Only directories actually present are included, and empty directories are
preserved. The manifest records format version `1`, selected world name,
layout, complete directories/dimensions, game version/DataVersion, recorded
loader/build (possibly unknown on older imported instances) and a
SHA-256 map for every included file (`null` for directories). API metadata
also records loader/build, creation time, size and whole-archive SHA-256.
Archives contain no absolute host path, server ID, token or panel permission.
Archive entry permissions are normalized to private files/directories;
timestamps and host ownership are not copied. Bukkit `uid.dat` is preserved,
unlike independent world copies; review UUID collisions and external plugin
database/name dependencies before importing into another server.

Vanilla, Fabric, Quilt, Forge and NeoForge preserve nested dimension layouts.
Paper, Purpur, Pufferfish and Folia preserve split sibling layouts. All
dimensions travel together; individual dimension exports, mixed layouts,
custom Bukkit world containers and additional plugin-managed worlds are
refused, not silently dropped. Snapshot or unverifiable/mismatched `level.dat`
versions are refused under the existing world-discovery policy.

This is not a `.mrpack`, full-server backup or universally portable
singleplayer save. Use the matching game version, loader build, mods/plugins,
required datapacks and configuration. These dependencies are **not embedded**.
Bukkit split worlds need a compatible server or deliberate manual conversion.
Inspect the archive before using any import tool: Fabricator's existing world
import has its own normalization policies and upload limits, and is not a
guaranteed round-trip converter for every modded dimension layout.

### Exclusion boundary and privacy

Only selected world directories are eligible. Unrelated server-root files
(including launchers, server configuration, credentials, logs, binaries,
backups and administrative access lists) never enter the archive.

Inside those world trees, any path component equal to one of the following
case-insensitive names is excluded:

```text
server.properties server.properties.bak session.lock logs crash-reports
backups plugins mods config defaultconfigs libraries versions .env auth.json
forwarding.secret ops.json whitelist.json banned-players.json banned-ips.json
usercache.json usernamecache.json bukkit.yml spigot.yml paper.yml
```

Components ending in `.jar`, `.jar.disabled`, `.log`, `.secret`, `.lck`, or
starting with `.fabricator-` / `.env.` are also excluded. Datapacks (including
ZIP datapacks), integral saved mod data, player inventories, advancements,
stats, entities, coordinates, signs and books otherwise remain byte-exact.
A discovered dimension overlapping exclusions blocks export. A mod that
stores integral data under another reserved directory needs manual migration;
do not assume it is portable. Symlinks, hardlinks, special files, nested mount
points, unsafe names, paths longer than 240 characters and aliased directories
are refused. No arbitrary source path API exists.

**World data is neither anonymized nor guaranteed secret-free.** It may contain
player identities, private locations, books/signs and credentials or private
mod-specific data in otherwise integral files. Review your private download
and redistribution rights before creating an anonymous link. Excluding known
server files is a boundary, not a universal sanitization promise.

## Permissions and anonymous access

Every private endpoint needs session authentication and `server.view`.
OIDC custom roles can separately grant `worlds.read` + `worlds.export` for
generation, `worlds.download` for private downloads/listing, and `worlds.share`
and `worlds.download` for creation/revocation/deletion. Download-only users do
not read the live filesystem. Users see/revoke only their own shares; confirmed
artifact deletion invalidates all shares to that artifact. Admin gets these
capabilities by default. All world-export endpoints are unavailable to MCP
API tokens.

Anonymous links use `/world-share#<server-key>.<256-bit-secret>`.
The fragment is removed immediately, before requests; redemption is a
cookie-free JSON POST to `/api/world-shares/download`. Grants resolve only
one immutable archive and never authorize server/world changes, filesystem
browsing, administrative APIs or another archive. Client-pack tokens and
world tokens use separate state stores and cannot be interchanged.

Recipients need HTTPS and a browser with service workers enabled (localhost
also works). A worker scoped to the `/world-` prefix (the share page and its
local download bridge, not dashboard/admin routes) bridges the
cookie-free POST stream to the browser's native download manager, without
buffering an archive into a Blob or persisting it in a cache. Its one-use
local random download URL contains **no bearer token**; it expires after
60 seconds and is not an anonymous network API. Browser privacy modes or
policies disabling workers may require opening the link in a supported
browser. Download completion/progress is shown by the browser, not inferred
from receiving HTTP headers. There is no range/resume support; a fresh retry
must pass current grant checks.

## Storage, locking, recovery and deployment limits

Archives and share metadata live in
`<server-index-parent>/world-exports/<sha256-server-id>/`, outside the live
instance. Artifacts are private immutable files. One generation at a time
per backend process holds the existing server lock across discovery, source
hashing, streamed tar/gzip creation, streamed verification, source recheck and
atomic publication. All per-server panel operations using that lock (including
power/transfer/backup/file changes) serialize with export. External filesystem
writers remain outside this guarantee and must be stopped. Generation verifies
every archive file against the initial and final source manifests; detected
changes or I/O failures publish nothing. Downloads open a fixed archive handle
under the lock; later cleanup cannot switch its bytes.

Five archives are retained per server. Unshared artifacts expire after 24 hours
and oldest slots are reclaimed on generation. Active links pin retention;
five pinned archives block generation until links expire/revoke or an archive
is explicitly deleted. Expired unshared files and interrupted `.tmp` / orphan
metadata are swept on startup and subsequent archive operations. An interrupted
export never edits live world files, so no live rollback is required.

`FABRICATOR_MAX_WORLD_EXPORT_BYTES` defaults to 100 GiB of source files.
The existing `FABRICATOR_MAX_TRANSFER_BYTES` (100 GiB default) and 200,000-entry
scan ceiling also apply. `FABRICATOR_WORLD_EXPORT_STORAGE_BYTES` defaults to
200 GiB **globally** for retained archives, unlike the small client-pack cap.
Generation checks free space for incompressible data, archive overhead and a
64 MiB reserve before staging. This is a preflight, not a filesystem quota:
external writers can exhaust disk after it; resulting I/O errors are surfaced
and temporary files cleaned up. Budget storage separately from S3 backups;
world exports are local, not automatically uploaded to S3.

There are four simultaneous streamed world downloads per process and a global
anonymous redemption limit of 30 requests/minute, including invalid requests;
the token JSON body is bounded to 512 bytes. `429` has `Retry-After`; exhausted
download slots return `503`. These bounds are not a bandwidth budget,
per-recipient fairness mechanism or internet DDoS protection. Use deliberate
HTTPS reverse-proxy rate/connection/bandwidth controls. Deploy a single backend
process: existing server locks and these limits are process-local, not
multi-worker/distributed locks. Do not run multiple backends against this state.

Generation is synchronous; configure proxy/WSGI request and download timeouts
for large worlds and avoid response buffering/disk spooling. If the UI loses
contact after submission, refresh the archive list before generating again;
a completed export can be persistent even when its response was lost.
Inspect downloads using the metadata/`X-Archive-SHA256` header and, for example,
`sha256sum world-....tar.gz`. No assumption is made about gateway byte limits.

A private admin origin stays private. For external recipients the operator
must deliberately route the same origin's share page, required static assets,
`/world-download-worker.js`, worker-local scope and `/api/world-shares/download`
through an HTTPS proxy. Retain authentication on management APIs; do not make
the whole panel anonymous or log POST token bodies. Security headers prohibit
caching, indexing and referrer disclosure on share pages/downloads. Link
holders can redistribute downloaded world data; no DRM/recall is possible.

## Fixture verification

`pytest -q tests/test_world_exports.py` covers real archive contents for all
supported loaders, large streamed bytes, source changes, unsafe paths,
retention/pins, interrupted metadata cleanup, expiry/revocation/deletion,
cross-artifact isolation and OIDC permissions. No live game server is started.
Run the related client-pack, transfer, OIDC and route-bucket suites when changing
the shared grant provider.

`npm --prefix frontend run build` and
`node --test frontend/src/components/WorldExportsPanel.test.js` cover the UI.
Optional browser acceptance uses the Python `playwright` package with its
Chromium engine: after building, run
`pytest -q tests/test_world_exports_browser.py`. It uses a temporary localhost
Flask server and synthetic world data, checks a native >16 MiB exact-byte
download with ambient cookies omitted and captures desktop/mobile states.
Without Playwright or the built frontend, that optional test skips. Set
`FABRICATOR_BROWSER_ARTIFACTS` to retain its synthetic screenshots outside the
test temporary directory. Chromium is the browser exercised by this check;
other browser/privacy-policy combinations need deployment acceptance.
