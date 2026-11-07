# World downloads (experimental)

Open **Backups → World downloads** on a Java instance. Export requires a POSIX
host (Linux, macOS or Linux containers). Windows, managed mode and proxies are
not supported. No live snapshot or automatic server stop is provided.

1. Stop the instance and external file writers. Select a **complete world group**
   and **Export world archive**. The source is unchanged.
2. Inspect the private download for sensitive data and compatibility.
3. **Review & share** requires privacy consent. Links expire in 1 hour to 7 days.
   Copy immediately: only the token hash is stored, never the retrievable secret.
4. Revocation, expiry, artifact deletion or server deletion blocks new requests.
   Already-downloaded and in-flight bytes cannot be recalled.

Existing archives remain downloadable while the instance runs.

Activity records export, archive deletion, share creation and revocation.
Bearer links and world contents are never included in those events.

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
World or dimension names overlapping exclusions, or a world named
`fabricator-world.json`, block export. Mods using reserved directories need
manual migration. Symlinks, hardlinks, special files, nested mount points,
unsafe names, paths longer than 240 characters and aliased directories are
refused. No arbitrary source path API exists.

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

Recipients need HTTPS (or localhost) and service workers enabled. A worker
scoped to `/world-`, not dashboard/admin routes, streams the cookie-free POST
to the browser download manager without Blob buffering or caching. Its
one-use local URL contains **no token**, expires after 60 seconds and is not
a public network API. The browser shows progress/completion. There is no
range/resume support; retries must pass current grant checks.

## Storage, locking, recovery and deployment limits

Archives and share metadata live in
`<server-index-parent>/world-exports/<sha256-server-id>/`, outside the live
instance. One generation per backend process holds the server lock through
discovery, source hashing, streamed creation/verification and atomic publication.
Power, transfer, backup and file operations using that lock serialize with export.
External writers must be stopped. Files are checked against initial and final
source manifests; detected changes or I/O failures publish nothing. Downloads
open a fixed handle under the lock, so cleanup cannot switch their bytes.

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

Downloads are limited to four concurrent streams and 30 anonymous requests/minute
per process, including invalid requests. Token bodies are capped at 512 bytes.
Throttling returns `429` with `Retry-After`; busy streams return `503`.
These limits are not bandwidth controls or DDoS protection. Configure proxy
rate, connection and bandwidth limits. Use **one backend process**: server locks
and these limits are not distributed.

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

Run `pytest -q tests/test_world_exports.py` for archives, grants, recovery and
OIDC checks; include client-pack/transfer/route-bucket suites when changing
shared primitives. UI checks use `npm --prefix frontend run build` and
`node --test frontend/src/components/WorldExportsPanel.test.js`.

After building, `pytest -q tests/test_world_exports_browser.py` checks a native
>16 MiB exact-byte download with cookies omitted and desktop/mobile states.
It uses synthetic worlds on localhost, never a live game server, and skips
without Python Playwright/Chromium or the frontend build. Set
`FABRICATOR_BROWSER_ARTIFACTS` to retain screenshots. Other browsers and privacy
policies need deployment acceptance.
