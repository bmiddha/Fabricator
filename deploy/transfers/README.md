# Server forks and world copies (experimental)

These are **Minecraft instance** operations, not Git forks. Use **Overview >
Instances & worlds**. They are unavailable in managed one-server-per-host mode
and to MCP API tokens.

## Before either operation

Stop the source in Fabricator; for world copies also stop the destination.
Stop any externally launched JVMs, scripts, editors, sync clients or plugins
that write these folders. Fabricator can coordinate its own process registry
and per-server locks, not unrelated host processes.

There is deliberately no live-copy or automatic source-shutdown option. Both
instances remain locked through validation, copying, verification and commit.
Their locks are acquired in ID order, shared with panel power actions, file
writes, backups, imports, installs and deletion. Registry-level starts also
honor this lock, including autostart. The source's runtime state is never
changed by a transfer.

Choose player-state inclusion explicitly, inspect the preview and acknowledge
its configuration/dependency risks. Nothing runs Minecraft to test a copy.
An initialized world's compressed `level.dat` must expose a non-snapshot
`Version.Name` and `DataVersion` matching its instance record; unverifiable
older metadata and snapshots are refused rather than guessed.

## Fork / create from existing

1. Choose a stopped, installed source and a unique new name. Names allow 1-80
   ASCII letters, numbers, dots, underscores or hyphens, beginning with a
   letter/number; filesystem-reserved names and trailing dots are refused.
2. Explicitly include the existing world group or omit it to let Minecraft
   generate a new world at first start. Generation settings, including any
   configured seed, are retained.
3. Explicitly include or omit world `playerdata`, `advancements` and `stats`.
4. Preview, acknowledge the copy scope and submit. The new instance stays
   stopped with autostart set to `never`.

The fork gets a new ID, independent writable files, its own managed directory
and three distinct available ports reserved for game, query and RCON. Copies
use ordinary file copies, not mutable hardlinks or symlinks. Ports are checked
against registered game/query/RCON ports and host TCP/UDP binds; registration
rechecks reservations atomically against concurrent panel creates. An unrelated
host process can still take a port later; normal start errors surface that.

### Exact inherited scope

Copied root directories: `libraries`, `versions`, `mods`, `plugins`, `config`,
`defaultconfigs`, `datapacks`, `.fabric`, `.quilt`, and the active world group's
directories when selected. Root `*.jar` files, `eula.txt`, `bukkit.yml`,
`spigot.yml`, `paper.yml` and Fabric/Quilt launcher properties are eligible.
`server-icon.png` is also retained. Other root files/directories are not copied.

`server.properties` is rebuilt from the source properties, retaining game
settings and unknown properties except for the known controls reset below.
Game/loader/build versions, installer launch specification, heap settings and
installed-content provenance are retained. The installer jar or relative
Forge/NeoForge argument-file target must exist in the selected tree. Custom
commands, custom launch arguments, host-specific/nested launch argument files
and unsupported launch formats require manual migration. `javaPath` and
user `jvmArgs` overrides are not inherited: compatible managed Java is resolved
normally for the copied game/loader version.

Never inherited as panel authority: source IDs/paths, runtime processes,
autostart, roles/grants, DNS/Velocity routes and bindings, playit/FRP allocations
or credentials, backup configs/history/jobs, scheduled commands, client-pack
exports/shares, logs, crash dumps, player caches or administrative files.
Root scripts, `.env`, unknown host files, `.secret`/`.log`/`.lck` files and
Fabricator forwarding configuration backups are excluded.

Ops, whitelist, player/IP bans and caches are **always fresh**, not a hidden
copy option. Online authentication is enabled; whitelist enforcement, query,
RCON, JMX, management-server and proxy forwarding are disabled. The RCON and
known Paper Velocity secrets are cleared. Paper/Spigot forwarding flags are
reset. Game listens on `127.0.0.1` until the operator changes it; configure
fresh public access rather than inheriting a public route.

**Copied mod/plugin configuration is trusted server content, not a sanitized
export.** It may contain credentials, plugin-specific administrator accounts,
absolute paths, shared external databases, web/map/voice listener ports or
remote storage references. The copied local files are independent; external
systems referenced by those files are not. Review and reconfigure them before
starting. There is no universal mod/plugin secret scrubber or port discovery,
and third-party ports are not automatically allocated. No new public tunnel,
DNS name or proxy route is provisioned.

## Copy world to instance

Select the source instance, its supported world group, a different stopped
destination and player-state policy. Explicitly choose **Leave destination
stopped** or **Start destination explicitly**; the latter also requires
`server.start` permission and only runs after a successful commit.

The active destination name comes from its actual `server.properties`, not a
stale record. The selected group is renamed to that name without editing the
destination's properties or loader/mod/plugin configuration. Existing world
trees are **replaced**, never merged. For an existing destination group, type
its exact world name in a separate destructive-replacement confirmation.

Before any world mutation, a complete, read-back-verified local safety `.tar`
of the destination (excluding old `backups`) is written into its `backups`
folder and registered as a `safety` snapshot. A skipped file, incomplete archive,
verification problem or failed snapshot registration aborts the operation
without changing destination worlds. Safety backups are retained even if a
later copy fails; they are not subject to this operation's cleanup.

### Supported layouts and compatibility

| Server type | Group copied without flattening |
|---|---|
| Vanilla, Fabric, Quilt, Forge, NeoForge | Actual `<level-name>/` including nested `DIM-1`, `DIM1`, legacy `DIM<number>` directories, modern `dimensions/<namespace>/<dimension>`, datapacks and integral mod world state |
| Paper, Purpur, Folia, Pufferfish | Actual `<level-name>/` plus existing `<level-name>_nether/` and `<level-name>_the_end/` siblings, retaining their internal `DIM-1`/`DIM1` trees |

This version selects the **active world group only**. Custom Bukkit
`world-container` paths, additional plugin-managed top-level worlds, ambiguous
mixed/split layouts, external/symlinked dimension storage and proxy/Bedrock
instances require manual migration. Cross-layout conversion is not offered.
Orphaned destination Bukkit Nether/End siblings are removed as part of group
replacement when absent from the source, with their previous bytes in the
safety snapshot.

World copies require **identical game version, loader and recorded loader
build**, matching world `DataVersion` when a destination world exists, and
identical installed enabled/disabled mod/plugin jar hashes. Modded loaders
also require a known loader build and identical `config`/`defaultconfigs`
trees. Vanilla/Bukkit instances additionally require identical launch-jar
hashes. There is no override for missing dependencies, upgrades, downgrades,
snapshot worlds or unverifiable formats. This is intentionally conservative,
not a claim that arbitrary modpacks are interchangeable.

Bukkit `uid.dat` is omitted so the destination generates its own plugin world
UUID. `session.lock` is not transferred. Integral `level.dat` UUIDs, mod data,
entity state and dimension paths are preserved. Plugins referencing old world
names/UUIDs or external databases can require manual relinking even when jar
hashes match. Omitting player directories is not an anonymity scrub: other
world state may contain player identities and in-world privileges. Destination
ops, whitelist, bans and non-world config are unchanged.

## Capacity, transaction and recovery

Both operations reject selected symlinks, hardlinked regular files, special
files, repeated directory identities, nested mount points and aliased or
overlapping source/destination paths. The selected tree is capped at 200,000
entries and `FABRICATOR_MAX_TRANSFER_BYTES` (default 100 GiB). `level.dat`
decompression is capped at 16 MiB with additional NBT depth/count bounds.
Preflight checks reserve space for staging, destination safety-archive
overhead and a 64 MiB free-space margin; disk/copy errors still fail closed.

Content-bound preview tokens are recomputed under both locks before execution.
Each file is copied into same-filesystem staging and SHA-256 verified against
the source. Publication is checked again. Fork registration happens only
after the complete directory is published and verified; registration failures
remove both the new record and owned directory. World replacements use
rename-aside rollback, never an overlay.

World transactions journal into `<destination>/.fabricator-transfer`; fork
journals use `.fork-<new-id>` in the managed server root. Startup recovery runs
before schedulers/autostart: incomplete replacements restore the old group,
uncommitted forks are removed, and committed transactions finish cleanup.
Starts are blocked while these journals remain. If rollback/recovery fails,
the journal is retained and errors are logged; do not delete it to bypass the
guard. Stop Fabricator and preserve the journal, old/staged directories and
mandatory safety tar, then restore/recover with an administrator. A corrupt
or unsafe recovery journal deliberately prevents startup.

Progress phases are queued, safety snapshot (world copy), copying bytes,
verifying, publishing, optional starting, and done/failed. A start or cleanup
failure after commit is reported as **failed with `published: true`**: files
may already be copied, so inspect rather than blindly retrying. Job state is
in memory; after a panel restart the UI reports lost contact/unknown outcome,
not success or automatic retry. Check the registry, recovered destination and
safety snapshot. The source is not removed or mutated by recovery.

## Permissions

Each per-server grant below additionally requires `server.view` on that
instance; generic view rights alone never allow cross-instance reads.

| Operation | Required capabilities |
|---|---|
| List supported source worlds | `worlds.read` on source |
| Fork preview / execute / job polling | `server.fork`, `files.read`, `server.settings.read` on source, plus global `servers.create` |
| World-copy preview / execute / job polling | `worlds.read` on source; `worlds.import` and `backups.create` on destination |
| Explicit destination start | Additional `server.start` on destination |

OIDC job polling is bound to the initiating issuer/subject and rechecks both
instances' current grants. Transfer jobs cannot be read through the generic
backup-job endpoint. New instances receive **no copied per-server grants**;
an administrator may need to assign access to the new ID. All transfer and
world-discovery endpoints are classified `never` for MCP API tokens.
