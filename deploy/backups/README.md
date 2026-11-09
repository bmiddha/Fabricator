# S3-compatible backup destinations (experimental)

Saved backup configurations can use **one** local or S3-compatible destination.
Existing configurations remain local. Quick, pre-upgrade and world-import
backups remain local. S3 configurations have no permanent local archive mirror.

## Backend setup

Install the updated `requirements.txt` (including boto3), or use an image built
from this version. Set `FABRICATOR_BACKUP_S3_PROFILES` on the Fabricator backend.
It is a JSON object of operator-approved aliases, not an API for uploading keys:

```sh
FABRICATOR_BACKUP_S3_PROFILES='{"default":{"buckets":["my-backups"],"region":"us-east-1"},"minio":{"buckets":["minecraft-backups"],"region":"us-east-1","endpointUrl":"https://minio.example.net","awsProfile":"minecraft","addressingStyle":"path","serverIds":["srv_example"]}}'
```

Each profile **must** list allowed bucket names. Optional `serverIds` restricts
that alias to listed server IDs; without it, users with configuration-write
permission on any server may use the alias. Use separate aliases, IAM policies,
buckets/prefixes and server restrictions for separate trust boundaries.

An omitted `awsProfile` uses boto3's normal AWS credential chain: environment,
shared credential files, container/workload identity or instance role.
`awsProfile` selects a profile already provisioned on the backend. Mount
credentials/config read-only in containers as appropriate, and set
`AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` if necessary. Do not put keys in
backup configs, URLs, frontend environment variables or repository files.
The aliases never expose the AWS profile name or credentials through the API.

For MinIO or R2, configure the exact endpoint URL on the backend profile first.
The browser can omit the endpoint (using this approved value), or supply that
exact URL; arbitrary endpoints are rejected to prevent routing credentials to
user-chosen hosts. Do not change an alias's endpoint while snapshots reference
it: access intentionally fails until the original endpoint/profile is restored.
Credential rotation with the same alias and endpoint is supported. Preserve
aliases, bucket grants and credentials needed by old snapshots after editing a
configuration.

Use HTTPS outside a trusted local test environment. HTTP is allowed for private
MinIO installations, but it does not protect credentials or backup contents in
transit. TLS verification stays enabled. Fabricator does not set encryption
headers or promise encryption at rest: configure bucket/provider encryption and
any KMS permissions yourself.

Provision the bucket in advance. The runtime needs `s3:PutObject`,
`s3:GetObject`, `s3:DeleteObject` and `s3:AbortMultipartUpload` for its object
namespace. A representative AWS object resource is
`arn:aws:s3:::my-backups/minecraft/fabricator/*`. Operators doing recovery and
multipart cleanup also need bucket listing/multipart-list permissions.
There is no bucket creation, replication, mirroring or prefix-wide deletion.
Use immediately retrievable storage, not Glacier/archive tiers or lifecycle
rules that transition active snapshots into them. Versioned buckets retain old
object versions/delete markers according to their own lifecycle; Fabricator
deletes the current object, not historical versions.

## Configure and operate

In **Backups > Backup schedules**, select **S3-compatible storage**. Enter the
backend alias, bucket, optional prefix, region and optional approved endpoint.
An empty region uses the profile's default (otherwise `us-east-1`; R2 commonly
uses `auto`). No actual connection is made when saving. Run a backup to verify
write/read permissions; **Check S3 availability** checks recorded remote objects
without downloading them. A successful availability check compares HEAD size
and checksum metadata, not all archive bytes. Restore and download always verify
the actual downloaded bytes. Connection errors are deliberately sanitized;
check backend credentials, network and IAM settings rather than entering keys
in the browser.

Existing OIDC and MCP capability enforcement applies unchanged: config changes
require `backups.config.write`; archive listing/download/restore/deletion require
their respective capabilities; purging a configuration also requires
`backups.delete`. Profile access is additionally restricted by the backend
bucket and optional server allowlists. No new unauthenticated storage API exists.

The backend builds the existing plain or hybrid tar locally, uploads it using
managed multipart transfers above 16 MiB, checks HEAD metadata, reads the object
back to verify SHA-256, then writes a versioned recovery JSON sidecar. This
full read-back costs one extra archive download per backup. A backup is only
successful after both objects are durably accepted and the local snapshot index
is saved. Retention runs afterwards, never after failed upload/verification/
metadata publication. A stopped Minecraft server is restarted once the local
archive is stable, before the network transfer.

**Temporary local space is required.** Allow room for the staging tree, archive
and (for hybrid compression) inner archives under
`<BACKUPS_DIR>/transfer/<config-id>`. Restore/download need space for a full
remote archive in the system temporary directory (`TMPDIR` on Linux); ZIP
downloads additionally need the converted ZIP. Restore extraction needs its
normal staging space alongside the server installation.

Remote restore downloads and checks SHA-256 **before** stopping the server.
The mandatory pre-restore safety snapshot is then durably written to
`<install>/backups`, even for S3 configurations. It is an independent local
safety snapshot, not a local mirror of the remote archive. Any failure to write
or record it aborts restore before live files are modified. Both overlay and
reset restore modes retain their existing semantics.

Reset restore retains local archives before replacing the installation, including
the new safety archive, the backup index when stored inside the installation,
and unindexed/partial evidence in the default `backups/` directory. Custom local
destinations retain indexed files, archive siblings and backup staging evidence;
unrelated old game files are not copied into a reset installation. Recorded
locations remain authoritative after a configuration is removed. External local
destinations and S3 objects are not moved. World replacement/import likewise
retains archives nested inside the replaced world directories; independent
instance forks do not inherit the source's backup schedules or archives.

Retention uses hard links where supported and complete, fsynced copies otherwise.
Permission, space, unsafe symlink/special-file or directory-collision failures
abort before replacing live files. Existing retained archives take precedence
over regular-file archive collisions in extracted content. Already-missing
archive records are retained, not silently pruned or presented as recovered.
If publishing fails, the original tree is rolled back. If rollback also fails,
original and staging trees are kept at the locations in the error, available
local snapshot records are relocated to the retained original tree, and the
server is **not automatically restarted**. Recover the installation before
starting it; failed index recovery is explicitly reported and still leaves the
actual archive evidence intact. Cleanup failure after successful publication
retains the old holding tree and logs its location. This is filesystem-operation
rollback protection, not a guarantee against power loss or filesystem corruption.

Each snapshot records its bucket, endpoint, region, alias, exact object key and
checksum. Configuration edits never redirect old snapshots. Retention and
deletion act only on those exact, ownership-checked archive and sidecar keys,
never on a bucket prefix listing. Failed deletion keeps the affected snapshot
record for retry and reports an error. A partially completed purge may have
already deleted earlier snapshots; the remaining config and failed records
stay available. Deleting a config without purging retains its snapshots and
their locations so they remain restorable/deletable.

## Task status and archive availability

The Backups and server Activity pages show shared, backend-authoritative task
status: operation/server, queued or running stage, elapsed time and recent
terminal outcomes. Transfer bars appear only for measured byte counters;
archive preparation/restoration stages are indeterminate. Refresh or navigation
rediscovers active work from the existing durable activity journal. Backend
restart marks unfinished tasks **interrupted**, not completed. Stale updates and
lost contact are explicit; polling retries without treating disappearance as
success. Backup/world submissions are reserved before worker launch to reject
duplicate starts with HTTP 409, and the UI blocks duplicate acceptance requests.

Task summaries contain only allowlisted stage/identity/counter fields, not raw
worker errors, file paths, credentials or actors. Listing requires server-view
access plus the operation's permissions or activity-read access; cross-server
operations require access to both scopes. This activity-derived endpoint remains
unavailable to MCP bearer tokens. Auth changes clear retained frontend state and
late responses cannot repopulate another session/server. The status API returns
up to 100 active/recent records; the compact UI shows active work and three recent
terminal records. Terminal task summaries are shown for 24 hours; the activity
timeline retains its existing bounded journal history.

Indexed snapshots are not deleted when their archives disappear. A warning
triangle and **Archive missing** label identify confirmed local absence or an
explicit S3 missing-object response; restore/download are unavailable for those
entries. Keep the metadata for investigation, and recover bytes from another
retained copy before restoring. An index entry cannot reconstruct a deleted
archive. Permission/credential/provider errors or invalid archive metadata are
**Cannot verify archive**, not missing. Unchecked objects retain a distinct
question-mark indicator; ordinary lists do not issue remote HEAD requests.

**Check S3 availability** performs at most 20 uncached object checks per request.
Verified/missing remote results are cached for 60 seconds in a bounded cache,
with backend profile grants revalidated before reuse. Remaining objects stay
unchecked; repeat the check to advance through additional entries. Failed checks
are not cached as absence. Backend restore/download still verify actual bytes
and do not rely on cached availability. Hover an archive's status/recovery text
for its complete explanation; accessible text also accompanies the icons.

## Failure, crash and recovery

Ordinary transfer failures clean up local transfer files; download temporary
files are removed when the download response closes or restore exits. SDK
multipart transfer failures abort their multipart upload. Configure a bucket
**AbortIncompleteMultipartUpload** lifecycle rule as well: process termination,
network loss during abort, or power failure can leave incomplete multipart
uploads that Fabricator cannot safely discover from its local index.

A crash can also leave local staging/archive files in the transfer directory
or the system temporary directory. With Fabricator stopped, remove only stale
`<BACKUPS_DIR>/transfer/<config-id>` staging/archive files and
`fabricator-s3-download-*` temp directories belonging to the failed operation.
Do not delete the local pre-restore safety snapshots as temporary transfer data.

Uploads use unique snapshot IDs. If verification or metadata publication fails,
or the process exits between remote completion and local indexing, a completed
object can remain remotely without a success record. Retries use new keys and
do not overwrite it. Review these orphans manually; retention never guesses
ownership by listing a shared prefix. If metadata deletion succeeds but archive
deletion fails, the local record remains sufficient to retry both deletes.

Archives are stored at:

```text
<prefix>/fabricator/<server-id>/<snapshot-id>/<archive-name>.tar
<prefix>/fabricator/<server-id>/<snapshot-id>/<archive-name>.tar.fabricator.json
```

The sidecar has `format: "fabricator-backup"`, `version: 2`, a complete `snapshot`
record (including provenance, location, size and SHA-256), a non-secret config
ID/name, and the archive layout. The archive's S3 user metadata also has its
SHA-256, format version, snapshot ID, config ID, creation time and server ID.
New sidecars additionally contain `recovery: {format: "fabricator-instance",
version: 1, platform, server}` with game/loader/build, portable launch specification,
heap settings and supported JVM tuning. Absolute Java paths, custom commands,
credentials, DNS/tunnel/map registrations and installation grants are not
serialized. An unsupported/custom source launch instead sets
`requiresOperatorMetadata: true`: the backup remains useful, but recovery needs
explicit operator inputs. Version-1 sidecars remain supported as legacy backups.

No credentials are embedded **in this generated recovery manifest**. Server
archives can contain RCON, mod/plugin credentials and player/admin data.
Sidecars are recovery aids, not signatures: protect their write permissions
just like the archives.

For an existing registered server whose local index is lost, the following
manual index-repair path remains available. For a fresh installation or new
instance, use the independent recovery workflow below instead:

1. Use your provider's CLI to download the sidecar and its archive. Verify the
   archive against `snapshot.sha256` and `snapshot.sizeBytes`.
2. With Fabricator **stopped**, restore the sidecar's `snapshot` object into the
   `snapshots` array in `<BACKUPS_DIR>/<server-id>.json` (create the file with
   `{"configs": [], "snapshots": []}` if necessary). Preserve other entries,
   ensure the server ID corresponds to the intended registered server, and do
   not duplicate snapshot IDs. Restore the original backend alias/endpoint
   grants before using the recovered snapshot. No saved config is needed to
   restore a snapshot.
3. Alternatively unpack the archive offline: a plain tar is the server tree;
   a hybrid tar contains `data.tar.gz` and `worlds.tar`, both extracted into the
   same target directory. Do not unpack over a running server.

If no sidecar was published, the exact key and archive user metadata still
identify the orphan and checksum for offline recovery. An orphan with intact Fabricator S3 user metadata is also discoverable in the
workflow below, but requires explicit legacy launch inputs. Objects without
Fabricator ownership/format/checksum metadata are not supported import sources.
There is no automatic orphan deletion.

## Create an instance from S3: migration and repaving

This workflow creates an **independent, stopped Minecraft instance** on a fresh,
different or repaved Fabricator installation. It needs neither the source
`servers.json` nor source backup JSON. It never replaces an existing instance,
starts archived code, installs Java, deletes source objects, or clones source
backup schedules.

On the **target backend**, provision your own credentials and enable a separate
operator-approved recovery alias, for example:

```sh
FABRICATOR_BACKUP_S3_PROFILES='{"recovery":{"buckets":["my-backups"],"region":"us-east-1","allowImport":true}}'
```

For MinIO/R2, also set this target alias's `endpointUrl`, `addressingStyle` and
optional `awsProfile` as described above. The original installation's alias
and credentials are not needed. Recovery reads use the supplied target alias;
the original source server/snapshot identity is retained only as provenance.
Use `s3:ListBucket` limited to the original `<prefix>/fabricator/` namespace,
plus `s3:GetObject` for archives and sidecars. Recovery itself needs no PUT,
DELETE or multipart-write access.

**`allowImport` is an explicit trust boundary.** It permits users with both
global `servers.import` and `servers.create` capabilities to discover/read
Fabricator objects in the alias's allowed buckets and IAM namespaces.
`serverIds` restrictions still protect normal backup operations, but do not
apply to these independent recovery reads: the source ID may not exist locally.
Do not enable this flag on a broadly privileged shared alias unless that access
is intended. All recovery routes refuse MCP bearer tokens and managed mode.

1. On the target, open **Create server > Import existing server >
   Recover from an S3 backup**. Enter the target backend alias, original bucket
   and original prefix. Credentials are never entered into the browser.
2. Discover supported Fabricator snapshots. Listing is paginated and does not
   turn arbitrary third-party archives into import candidates. Select a snapshot
   and a unique, portable new instance name.
3. New portable manifests supply the original loader/game/build and launch data.
   A legacy sidecar, unpublished/lost sidecar, custom launch or platform-specific
   argument-file migration needs explicit original metadata. Supply the actual
   released game version, loader/build, relative jar/argument-file target and
   heap settings. These values cannot be invented from data an old backup never
   stored. Unsupported host paths, nested argument-file includes, commands and
   unsafe flags require manual offline migration.
4. **Download & inspect snapshot** verifies the full archive SHA-256, preflights
   both plain/hybrid tar layers, checks sizes and free space, extracts into private
   staging, validates launch/game/world compatibility, and identifies configuration
   files that may contain secrets. Preview shows provenance, bytes, worlds, Java
   requirement and reset/privacy risks. Secret values are never displayed.
5. Review the risks, explicitly accept any retained/uninspected configuration
   secrets, and type the new instance name. **Create stopped instance** verifies
   the inspected file hashes again, copies independent bytes, assigns a new ID
   and fresh available game/query/RCON ports, and transactionally registers it.
   Check Java and mod/plugin prerequisites before manually starting.

Supported recovery uses Java game loaders supported by instance transfers
(vanilla, Fabric, Quilt, Forge, NeoForge and Paper-family loaders), released
Minecraft versions, normal jar or portable argument-file launches, and supported
Java nested/Bukkit split world layouts. Custom world containers, additional
plugin-managed root worlds, proxies, worlds lacking supported NBT version
metadata and snapshot-version worlds need manual
migration. Native mods and cross-platform dependencies may need operator work;
argument-file platform changes require explicit target launch input. Launch jar
inspection is bounded to 512 MiB, 100,000 ZIP entries and 64 KiB game-version
metadata; ZIP64 launch jars require manual migration.

Whole archived world/mod/plugin/config/loader files are retained, including world
playerdata, statistics, advancements and mod capabilities. Known source
administration (`ops`, whitelist/bans/user caches), installation auth/environment
files, backup/log/tunnel controls, map capability/nonces and proxy secrets are
removed or reset. Known RCON/query/JMX/management/proxy listeners are disabled;
the game binds loopback with online mode enabled. BlueMap web serving and resource
download consent are disabled; configure maps afresh on the target. Bukkit UUIDs
are regenerated. **Other plugin permissions, secrets, web listeners, external
databases and world identities are not universally scrubbed.** Review/rotate
those yourself before starting. Text detection is bounded to 1 MiB per file and
16 MiB total, with large/non-text/uninspected content explicitly disclosed.

Fabricator accounts, signing keys, OIDC/MCP grants, DNS registrations, global
tunnels, diagnostics, notification configuration, schedules and Java paths are
not transplanted. An administrator must grant access to the **new** instance ID
when global roles do not already cover it. Reconfigure target networking and
backups explicitly; existing instances and provider objects are untouched.
Exclusions or missing files in the source backup cannot be recovered by this
workflow. Create full-instance backups and test recovery before depending on them.

### Temporary disk and interrupted jobs

Default archive and expanded-byte limit: **100 GiB**, with **200,000 entries**
including implicit parent directories, at most 200,000 exposed tar headers,
**1 MiB per archive metadata record / control configuration**, **256 MiB total
archive header/metadata reads**, and bounded portable relative paths.
`FABRICATOR_MAX_S3_IMPORT_BYTES` overrides
the byte limit; the existing transfer byte limit also applies to extracted
trees/copying. Space checks keep a 64 MiB reserve. Allow the archive plus an
extracted tree during inspection, then **two extracted copies** during
confirmation. This is temporary staging, not a permanent mirror.

Inspected previews live in private `<SERVER_ROOT>/.s3-preview-<opaque-id>`
directories for one hour and survive a panel restart. Preview receipts and jobs
are owner-bound; reopening the dialog resumes a saved preview and requires
fresh confirmation. Discarding a preview deletes only its local staging, never
S3 objects. Expired ready previews are swept on the next recovery submission
and startup; incomplete preview staging is cleaned at startup. Ordinary failed
inspection cleans its temporary files. At most two recovery workers run at once.

Imports reuse the durable `.fork-srv_<id>` registration journal. An interrupted,
uncommitted import rolls back before autostart; a committed import is preserved
and its journal cleaned. Starts remain blocked while a registration journal is
present. Copy/registration failure retains a valid inspected preview for retry.
If publication completed but cleanup failed, the job reports the registered
instance and requires panel restart instead of blindly retrying creation.
After losing job contact or restarting, check the instance list first; lost
in-memory job progress is not proof that creation failed. Unsafe/malformed
filesystem journals fail closed and require operator review rather than
guessing which data to remove.

### What to retain outside a host before repaving

Keep the **bucket, original prefix, provider endpoint/region**, the archive and
its sidecar, independent target access credentials/workload-identity setup, and
any required encryption/KMS/customer-managed keys and decrypt permissions.
Provider-controlled encryption keys cannot be recreated from a snapshot.
Retain notes for unsupported custom launch flags, native dependencies and
external databases. Prefer a separate read-only recovery credential and protect
it off the machine being repaved.

This recovers **Minecraft instances**, not the whole Fabricator administration
installation. Preserve or redeploy installation authentication/OIDC, MCP tokens,
DNS owner inventory/provider configuration, tunnel ownership, notification
destinations and Java/operator configuration separately. Do not put
installation-wide auth secrets into a server archive as a substitute for that
separate recovery plan.

## Verification and limitations

Run the S3 tests against the isolated in-process Moto S3 emulator:

```sh
pip install -r requirements.txt -r tests/requirements-s3.txt
python -m pytest -q tests/test_backups_s3.py tests/test_s3_recovery.py
```

No real credentials or buckets are needed. AWS S3, MinIO and R2 are protocol
targets, not a claim of live-provider certification. Providers must support
standard PUT/GET/HEAD/DELETE, multipart upload/abort, ListObjectsV2 and user metadata. Unsupported
provider behavior fails the operation rather than claiming success. No
presigned browser URLs, remote-to-remote replication, automatic original-index rebuild,
archive-tier recalls, KMS provisioning or remote safety-copy policy is provided.
