# Staged modpack updates

On a Fabric, Quilt, Forge or NeoForge instance's **Mods** page, open **Staged
modpack updates**. This is separate from the existing direct pack installer.
Managed deployments and MCP API tokens cannot use this workflow.

## Review, stage, validate, approve

1. Stop production and all external processes writing its files. Select a
   Modrinth project ID/slug (latest upstream release) or upload a `.mrpack`.
   **Prepare review** downloads and materializes an independent candidate;
   it does not install anything into production or start a game process.
2. Review additions, removals, replacements, bounded config diffs, runtime
   changes, dependency review items and client-manifest impact. A known,
   unchanged pack-owned file can be replaced/removed. Unknown files and local
   edits are preserved. To overwrite a conflicting local file, select it and
   **prepare a new review**; the old review's approval never carries over.
   The first staged update may preserve old override jars/configs if an older
   installer did not record their provenance. Reconcile those manually while
   stopped, then prepare again; do not assume preserved mods are compatible.
3. Choose a unique staging name and acknowledge the transfer/config risks.
   **Fork & apply to staging** reuses the independent server fork operation,
   including worlds/dimensions, but without playerdata, advancements, stats or
   administrative grants. Other integral world data can retain identities.
   Autostart is disabled, known proxy credentials are removed, RCON/query and
   known map/management listeners are disabled, and the game binds loopback.
4. Inspect the new staging instance's files before executing code. Opt in to
   **Run staging startup check** only if you trust the pack and its configs.
   This starts **only staging**, waits up to 180 seconds for a fresh Minecraft
   `Done (` readiness signal, and stops staging. An exited JVM, timeout,
   changed inputs/configs or failed stop cannot pass. Inspect staging's console
   for details. Automatically rewritten configs need reconciliation and a new
   review, not an implicit change to the approved candidate.
5. After validation, type the production instance's exact name and select
   **Back up & promote reviewed files**. Production and staging must be stopped.
   The worker rechecks runtime/metadata and content fingerprints under both
   instance locks, writes and verifies a full-instance local safety snapshot,
   then transactionally publishes the reviewed `mods/`, `config/` and
   `defaultconfigs/` trees. Local preserved files stay in the candidate.
   Production is left stopped. **No staging world is ever copied back**;
   production world changes since review are retained and captured by the
   new safety snapshot.

All stages are owner-bound, persisted on disk and survive a browser refresh.
There are five saved reviews per instance per owner; delete old reviews to
free candidate storage. Deleting a review does not delete staging or safety
backups. Plans live beside the server index in `modpack-updates/`; directories
are private and plan files are mode `0600`. Failed/interrupted operations
require a fresh review rather than silently continuing.

## Permissions

All operations need `server.view`, `modpacks.install`, `files.read` and
`server.settings.read` on production. Forking additionally needs `server.fork`
there and global `servers.create`. Validation needs view, settings-read,
file-read, `server.start` and `server.stop` on the staging instance. Promotion
additionally needs **`modpacks.promote`** and `backups.create` on production.
The deployment's admin role includes the new permission; custom roles must
grant it deliberately. New instances do not inherit panel grants, so scoped
operators may need a deployment administrator to grant staging access.

## Scope and limitations

- Minecraft version, loader type **and loader build** must match exactly.
  Mismatches are visible but block staging/promotion. Runtime upgrades need a
  separate migration, not an implied loader download.
- Only `mods/`, `config/` and `defaultconfigs/` server content is supported.
  Packs changing worlds, root server properties, launch scripts or
  Fabricator/BlueMap control files are rejected. Client-only manifest files
  are retained as client metadata, not installed on the server. Unsupported
  override trees need a separate manual migration.
- Index downloads require hashes; missing/unclassified server files fail
  preparation rather than being silently accepted. Static review detects
  duplicate top-level Fabric mod IDs and potentially missing required IDs.
  Nested jars, Forge/Quilt metadata and dependency version ranges are **not**
  fully solved. Startup is a smoke check, not gameplay or migration acceptance.
- The fork's known controls are isolated, but mod configs can still contain
  credentials, external database addresses, custom listeners and host paths.
  This runs with the panel's OS privileges, **not in a security sandbox**.
  Review configs and use host/container isolation where needed.
- Client source metadata updates on promotion. Existing client exports and
  share links remain immutable; generate and distribute a new export.

## Failure and recovery

Promotion uses `.fabricator-update/journal.json` and a start-blocking marker.
Before panel schedulers/autostart run, startup recovery restores an incomplete
publication (files and pack/content/client metadata), or finishes bookkeeping
for an already committed publication. A failed recovery stops panel startup
instead of allowing a half-updated game instance to start. An interrupted
staging setup retains `.fabricator-staging` and cannot start; discard that
staging instance and prepare a fresh review.

The safety snapshot is a normal local **safety** backup, named
`safety-modpack-update-<id>.tar`, available through the existing Backups page.
It contains the matching production worlds, mods and configs immediately
before promotion. If updated production has subsequently loaded/migrated a
world, recover using that matching **whole-instance backup**, while stopped.
Downgrading jars alone does not undo world migrations. Review restored pack
metadata/client exports before restarting or sharing a new pack.
