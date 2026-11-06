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

Each snapshot records its bucket, endpoint, region, alias, exact object key and
checksum. Configuration edits never redirect old snapshots. Retention and
deletion act only on those exact, ownership-checked archive and sidecar keys,
never on a bucket prefix listing. Failed deletion keeps the affected snapshot
record for retry and reports an error. A partially completed purge may have
already deleted earlier snapshots; the remaining config and failed records
stay available. Deleting a config without purging retains its snapshots and
their locations so they remain restorable/deletable.

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

The sidecar has `format: "fabricator-backup"`, `version: 1`, a complete `snapshot`
record (including provenance, location, size and SHA-256), a non-secret config
ID/name, and the archive layout. The archive's S3 user metadata also has its
SHA-256, format version, snapshot ID, config ID, creation time and server ID.
No credentials are embedded. Sidecars are recovery aids, not signatures: protect
their write permissions just like the archives.

If the local index is lost:

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
identify the orphan and checksum for offline recovery. There is intentionally
no automatic bucket-wide import or orphan deletion.

## Verification and limitations

Run the S3 tests against the isolated in-process Moto S3 emulator:

```sh
pip install -r requirements.txt -r tests/requirements-s3.txt
python -m pytest -q tests/test_backups_s3.py
```

No real credentials or buckets are needed. AWS S3, MinIO and R2 are protocol
targets, not a claim of live-provider certification. Providers must support
standard PUT/GET/HEAD/DELETE, multipart upload/abort and user metadata. Unsupported
provider behavior fails the operation rather than claiming success. No
presigned browser URLs, remote-to-remote replication, automatic index rebuild,
archive-tier recalls, KMS provisioning or remote safety-copy policy is provided.
