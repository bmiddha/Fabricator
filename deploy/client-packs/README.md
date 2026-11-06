# Client modpack exports (experimental)

Open a Fabric, Quilt, Forge or NeoForge server's **Mods → Client modpack** panel.
Vanilla, plugin and Velocity servers are not supported. No restart is required.

1. Open the editor and refresh sources. Enabled `mods/*.jar` files are identified
   by content hashes, never filenames. Known server-only and disabled mods are
   excluded. Unknown jars or ambiguous client compatibility block generation
   until explicitly excluded or corrected.
2. Use **Add client-only Modrinth mods**, inspect **Details**, choose an exact
   compatible version, and mark it required or optional. These selections are
   metadata only: they never install anything into the dedicated server.
3. Optionally select individual client settings, then **Save and review**.
   Resolve all blocking errors before **Generate .mrpack**. Download the
   authenticated archive and import it into a compatible Minecraft launcher.

Shared mods always use the exact bytes/version installed on the server. Client
requirements are operator intent, not a translation of Modrinth project
environment tags. Imported `env.client` declarations are retained when possible;
otherwise supported mods default to required. Required dependencies are followed
transitively, compatible with the pinned Minecraft version and loader. Their
versions appear in the review and the final index. A required dependent promotes
its dependencies to required, including through cycles. Optional and embedded
dependency declarations do not add extra downloads. Incompatible projects,
missing versions, collisions and conflicts with shared pins stop generation;
Fabricator never upgrades a server mod to make a client selection work. Choose
another pinned client version or deliberately change the server separately.

Optional files use the format's `env.client: "optional"` declaration. Launcher
behavior varies: this does **not** promise every launcher will offer a chooser.
Server-derived files preserve an imported required/optional `env.server`
declaration where available, otherwise defaulting to `required`. Separately
selected client files and their client-only dependencies declare `unsupported`.

## Imported packs and existing servers

Successful new Modrinth and uploaded `.mrpack` installs retain their original
index and bounded client-setting candidates outside the server directory.
Client entries intentionally skipped on the server remain available for export.
The installed server's current shared versions take precedence over older
imported versions; the review explains reconciliation. Previously installed
shared files which are now removed/disabled are not resurrected from provenance.

Older installs may have discarded the original archive. Those missing
client-only entries cannot be inferred. Add them manually or upload the original
archive using **Reimport client metadata** (maximum 64 MiB). This only replaces
retained import metadata; it never installs mods, changes configs, or restarts
the server. Explicit editor selections survive. Shared entries absent on the
server after metadata-only reimport require explicit include/exclude review.
An archive targeting a different Minecraft version/loader is rejected.

The loader version is recorded by new supported installer results. Older or
externally imported servers, and installers which do not report the actual
build, require the operator to enter the **installed** version. Fabricator does
not infer it from the latest release or assume an imported pack's loader is
the one currently running. A known installed version cannot be overridden.

## Client settings and redistribution

Nothing is included by default. The fixed, deliberately small allowlist is:

- `options.txt`
- `config/sodium-options.json`
- `config/iris.properties`
- `config/reeses-sodium-options.json`
- `config/modmenu.json`
- `config/emi.css`
- `config/emi.json`

Select either an existing server copy or the retained imported copy per path.
Common `overrides/` candidates are layered beneath `client-overrides/` candidates.
`server-overrides/` is never retained for clients. Output settings live under
`client-overrides/`. Each must be UTF-8 text, at most 256 KiB; symlinks, junctions,
unsafe relative paths and changed-during-read files are refused. Recognizable
credential/personal-data fields and private keys block export. **Review content
before selecting it**: field detection is a defense, not a general secret
scrubber or permission to export arbitrary configuration.

Other overrides are listed as excluded paths, not copied. Bundled jars/ZIPs
require explicit exclusion; they are never redistributed. Worlds, logs, player
data, administrative files and arbitrary `config/` files cannot be selected.
Custom client binary inclusion is not implemented. Unsupported/non-Modrinth
mods need legitimate external provider downloads and operator redistribution
review; explicitly excluding a required mod does not make the resulting game
compatible automatically.

Exports contain pinned HTTPS download references, SHA-1, SHA-512 and byte sizes,
not embedded mod binaries. Only the official Modrinth-supported download hosts
are accepted: `cdn.modrinth.com`, `github.com`, `raw.githubusercontent.com`,
`gitlab.com`. File identity must match Modrinth metadata, including size and both
hashes. Provider restrictions are not bypassed. See the
[official format specification](https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack).

## Access, storage and freshness

Management endpoints are session-authenticated, server-scoped and unavailable to
MCP API tokens. OIDC custom roles can grant `clientpacks.read`, `clientpacks.manage`,
`clientpacks.export` and `clientpacks.download` separately, alongside
`server.view`. The full editor normally needs read + manage; generation needs
read + export; download-only users see existing snapshots without reading server
mods/settings. Built-in administrator/password-session behavior is unchanged.
Archives remain private unless a manager explicitly creates a share link.

Configuration, provenance and exports are stored in `client-packs/` alongside
the configured `servers.json`, not inside the server directory. Provenance is
bounded to 8 MiB per source and 2,000 indexed files; archives retain at most five
successful generations per server for 24 hours unless pinned by a share link.
Each generated archive is capped at 16 MiB (80 MiB of retained archives per
server, plus a bounded generation-in-progress). Expiration/retention cleanup runs
on the next export list, generation or download access. Incomplete archives are
removed on failure. Downloads have safe generated filenames and private/no-store
cache headers.
Deleting a server also removes its retained client-pack state and exports.

Each successful archive is immutable and gets its own version ID. Reviewed
fingerprints bind configuration, enabled jar hashes, imported metadata, resolved
versions and selected settings. Generation rechecks sources before publishing
and refuses mismatches. **Refresh sources** compares snapshots to current
sources and marks outdated ones; a download-only view explicitly cannot claim
freshness. Existing snapshots remain valid downloads until expiry. Catalog
metadata is briefly cached to avoid one upstream request per jar on each review;
this is not continuous compatibility monitoring. Pinning protects file identity,
not the future availability of external downloads.

## Optional anonymous share links

After generating a pack, select **Share archive**, choose a lifetime (1 hour,
24 hours, or 7 days), and select **Create anonymous link**. **Anyone with the
link can download the entire selected archive, including its client settings**,
without a password or OIDC account. Review the content first. The link authorizes
only that immutable archive; it cannot browse the server, export a new pack,
read other files, or follow later generations.

Creating/listing/revoking links and deleting archives require **both**
`clientpacks.manage` and `clientpacks.download`, plus `server.view`. OIDC managers
see and revoke only links they created for that server. Password administrators
share the existing single-operator identity. The URL is shown only once and held
only in browser memory; copy it before hiding/leaving the editor. Only its
SHA-256 hash is persisted, not a recoverable token. Lost links must be revoked
and replaced. Existing links remain valid until expiry or explicit revocation;
changing a creator's role does not revoke already-issued bearer grants.

There are at most 20 active links per server, with required expiry between
3,600 and 604,800 seconds from creation. An active link pins its archive through
its expiry, even beyond ordinary 24-hour cleanup. Pin counts and the latest
effective expiry appear beside each archive. The five-archive cap is unchanged:
generation fails visibly if all five slots are pinned. Revoke links to release
a slot, wait for expiry, or deliberately delete an archive. Archive deletion
requires explicit confirmation and invalidates **all** its links, including
other managers' links. Server deletion also invalidates links. Revocation and
deletion prevent new downloads; bytes already downloaded or streaming cannot
be recalled.

Links use a fragment (`/client-pack-share#...`), not a token in the HTTP path or
query. The minimal download view removes the fragment from browser history and
POSTs it in a bounded JSON body with cookies omitted. Standard HTTP access logs
therefore record only the fixed route, not the credential. The view and download
responses prohibit caching/referrers and the view blocks external scripts and
connections. No analytics are installed on this view. **Use HTTPS**, retain the
response headers through your reverse proxy, and disable request/response-body
capture for share creation and `/api/client-pack-shares/download` in proxies,
APM, browser extensions and other instrumentation. Those systems can still
capture secrets if configured to record bodies, fragments or browser state.
Treat copied links and browser history/sync as credentials; recipients should
retain their original link if they need to reopen it.

Public downloads have a process-local token bucket (burst 30, refill 30/minute)
and return `429` with `Retry-After` when exhausted. It is separate from signed-in
APIs and does not provide a bandwidth quota or distributed abuse protection;
apply appropriate reverse-proxy connection/bandwidth limits when exposing the
panel publicly. Unknown, expired, revoked or deleted links return the same
generic `404`. Setup-mode lockdown remains unchanged. Sharing needs no account
for recipients, but intentionally disabling authentication still exposes all
panel management as before.

## Round-trip regression

Run `.venv/bin/python -m pytest -q tests/test_client_pack.py -k export_download_upload_install_round_trip`
to exercise export/download, upload/staging and the actual dedicated-server
importer against isolated fixture downloads. It checks pinned shared jars and
both hashes, dependency reuse, required/optional client declarations, skipped
client files, retained settings and client metadata through re-export, and
rejection of corrupted download bytes. Only upstream catalog/HTTP responses are
replaced; the export, archive, streaming hash verification and import code run
normally. No Minecraft process is started.

Fabricator imports packs onto dedicated servers, not desktop clients. This
round-trip is **not** a launcher-level client installation check: the client
index, referenced file bytes and override contents are checked independently,
but no external launcher is run.
