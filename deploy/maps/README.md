# Private BlueMap and global waystones

Experimental, opt-in per-server Map tab. The first target is the pack requested
by the user, **COBBLEVERSE**, not arbitrary Waystones versions or all Fabricator
loaders. Nothing installs, restarts or publishes an existing server automatically.
There is no public sharing endpoint, rendering scheduler or pack distribution
service.

## Supported integration target

| Component | Exact initial target | Provenance |
|---|---|---|
| Pack | COBBLEVERSE 1.7.42 | Modrinth project `Jkb29YJU`, version `4SKGla61` |
| Minecraft | 1.21.1 | Pack index |
| Loader | Fabric 0.18.4 | Pack index; must be recorded as `loaderVersion` in Fabricator |
| Server Java | 21 | Minecraft/mod requirements; existing runtime enforcement is unchanged |
| Waystones | Balm-based Waystones 21.1.37 | Project `LOpKHB2A`, version `Ub2AL65v` |
| Balm | 21.0.63 | Project `MBAkmtvl`, version `R6U3hZRZ` |
| Fabric API | 0.116.7+1.21.1 used for adapter build; installed API must target 1.21.1 | Pinned build dependency |
| BlueMap | 5.7 Fabric artifact | Project `swbUV1cr`, version `Dr2hvJBc` |
| Adapter | Fabricator Global Waystones 1.0.0 | Built from `tools/global-waystones` |

This matrix is **build/API-verified**, with browser transport validation described
below; it is not a claim that a real COBBLEVERSE game server has been exercised.
The loader/mod checks intentionally reject other versions. Do not select the
latest BlueMap release by number alone: its Fabric artifact may target a
different Minecraft release. Do not upgrade server Java to a newer CLI
renderer requirement.

The COBBLEVERSE archive was checked against its published SHA-512:

```text
9af64e0d3a092ab8d72d966cc4d45512e8d875685e804bea9233adb88f5f5d4c30578b77304f70fe77e9ef2ee01e487d652f3341279bcc1850ff2587ae8aa8a4
```

The pack uses **TwelveIterations/Balm Waystones**, not
LordDeatHunter/FabricWaystones (`fwaystones`). The older
MrChresh/BlueMapFabricWaystonesMarkersMod targets the latter and is not installed.
Waystones 21.1.37 has its own BlueMap integration, but its source does not filter
ordinary waystones by GLOBAL visibility. Its normal marker sets are therefore
removed by the adapter and excluded at the proxy boundary.

## Operator setup

1. Back up the server. Stop it explicitly. In **Map > Map setup and access**,
   review the exact dependency preview. Missing, disabled, duplicate or wrong
   dependencies must be resolved; names of jar files are not compatibility proof.
2. With Java 21 available for the **build only**, build the server-only adapter:

   ```bash
   cd tools/global-waystones
   ./gradlew --no-daemon clean build
   sha256sum build/libs/fabricator-global-waystones-1.0.0.jar
   ```

   Windows operators can use `gradlew.bat`. The wrapper pins Gradle 9.2.1 and its
   distribution SHA-256. `gradle/verification-metadata.xml` pins the downloaded
   dependency checksums, including the actual Fabric/Balm/Waystones/BlueMap
   artifacts. Only Loom's locally generated mappings and remapped Minecraft/mod
   compilation jars are exempt: those are build outputs, not downloads, and the
   mappings archive has generation timestamps. Do not regenerate verification
   metadata to work around a failed
   integrity check. Compare the adapter's hash after transferring it to the
   server's `mods/` folder. This repository does not ship a remotely downloaded
   or automatically installed adapter binary.
3. Keep the pack's exact Balm and Waystones dependencies. Install the built
   adapter jar in `mods/` using Files (requires `files.upload`) or your existing
   operator workflow. **Install pinned BlueMap 5.7** is an explicit action
   requiring both `map.manage` and `mods.install`. It verifies this SHA-512
   before the atomic install:

   ```text
   b7483d6ff90f09258f994a6f487846c1dfe814f1d0af931bd50426fd0afc98ccad033c5f440705edc08a8ced03cf216171ba1571db2221aca4ec620d676443a4
   ```

4. Read the resource-download consent. Check it only if you own the applicable
   Minecraft Java Edition license, accept Mojang's EULA, and authorize BlueMap
   to download Minecraft client resources for rendering. Without this explicit
   consent setup refuses to write `accept-download: true`. No Minecraft EULA
   file is edited for you.
5. Click **Configure private map**. New managed configuration binds BlueMap to
   `127.0.0.1` on an available port in 8800-8999, fixes its webroot at
   `bluemap/web`, selects one rendering thread and disables BlueMap metrics.
   Existing `core.conf`, `webserver.conf` and adapter configuration are **not
   overwritten**. For an existing unmanaged BlueMap installation, back them up, move those
   specific files aside and review the new managed setup; do not delete the
   world, map data, map definitions or unrelated configuration.
6. Start the server yourself when ready. BlueMap generates its viewer/map
   configuration and downloads consented resources. Initial rendering can take
   time and substantial CPU/disk. Check the BlueMap/server logs. The Map tab
   waits for a successful adapter reconciliation and a fresh heartbeat from the
   exact process Fabricator started.
7. Use BlueMap's existing menu to select map/dimension, follow players and
   toggle **Global waystones**. Viewers need only `server.view` + `map.read`;
   configure managers with `map.manage` as an additional independent grant.
   Default viewer/operator roles do not acquire map access automatically.

Use the existing Java/runtime settings to select an appropriate installed
runtime if required. Setup never changes a runtime, modifies a firewall,
configures Pangolin, creates a tunnel or restarts a server.

The Minecraft JVM and Fabricator must share the same loopback/network namespace.
Separate game-server containers, remote hosts, arbitrary upstream URLs, custom
launch wrappers whose process ID is not the Minecraft JVM, other loaders and
other mod versions are not supported by this first integration.

## Privacy and synchronization

The adapter uses the real pinned `WaystonesAPI.getAllWaystones(server)` API and
Balm create/update/remove/load events, not NBT scraping or an imaginary JSON
contract. It takes an initial full snapshot and repeats full reconciliation
every 100 server ticks (normally five seconds). Events schedule reconciliation
after mutation, on the next server tick. Heavy server lag also delays this
interval; the map fails closed when the heartbeat is over 20 seconds old.

Only valid, non-transient **regular waystones** with
`WaystoneVisibility.GLOBAL` are included. Activation-only, team, shard-only,
sharestone, death/memorial, home and unknown visibility/type destinations are
omitted. Stable Waystone UUIDs identify markers. Dimension resource keys are
matched through BlueMap's world API, never inferred from folder names or
coordinate scaling. Each reconciliation replaces whole per-dimension marker
sets, so rename, deletion, movement, privatization, reload and restart remove
old entries. Labels use BlueMap's built-in escaping, and popup details are
explicitly HTML-escaped (the API's default detail copies the raw name).
Other marker providers' sets are dropped
from the map feed at the proxy boundary; hiding a checkbox is not the privacy
mechanism.

The heartbeat also enumerates the currently global marker IDs per exact
BlueMap map ID. The proxy intersects persisted marker files with this fresh
allowlist, so an asynchronously written file from before privatization,
deletion, a dimension move or restart cannot re-expose an old marker.

BlueMap's own `/maps/<map>/live/players.json` polling supplies live players.
Fabricator does not collect an alternative position feed, expose teleport or
console commands, or equate OIDC subjects with Minecraft UUIDs. BlueMap's
visibility filters remain authoritative. Configure its `hide-invisible`,
`hide-vanished`, sneaking, game-mode and other visibility settings as appropriate.
An arbitrary vanish plugin is **not** universally supported; verify its
BlueMap integration before using maps on a privacy-sensitive server.

BlueMap removes players absent from the next successful player-feed snapshot.
When the server, transport or adapter becomes unavailable, Fabricator removes
the iframe on the next readiness poll (five seconds) rather than leaving a
last-known player view indefinitely.

## Isolation and endpoint boundaries

The viewer has `sandbox="allow-scripts"` and no `allow-same-origin`. The
response itself repeats that sandbox through CSP, so directly opening a viewer
URL does not regain panel-origin authority. A first-party bootstrap replaces
inaccessible browser storage with **frame-local memory** before BlueMap starts.
BlueMap 5.7's bundled translation compiler requires `unsafe-eval`; this is
allowed only on the already opaque viewer, **not on the Fabricator application**.
Cookies, parent DOM, forms, plugins and top navigation remain inaccessible.
Fullscreen, persistent preferences and viewer downloads are not enabled.

Authenticated POST `/map/session` issues a random, server-bound five-minute
map capability. Assets, tiles, settings, fonts, marker JSON and player polling
all require it. Viewer resource requests use `Access-Control-Allow-Origin: null`
without credentialed CORS. OIDC ownership and the current `server.view` /
`map.read` grants are checked on each request. Logging out revokes the browser's
capabilities. Disabling panel map access revokes resource access immediately;
it does not stop rendering or open/close network listeners.

The narrow capability route is the only auth-gate exception. Management routes
still require normal sessions/capabilities and all map routes deny MCP API
tokens without a valid map-only capability. Unknown/traversal paths and
compressed marker JSON are refused. Transport only contacts the recorded
managed loopback port, verifies the adapter's per-server nonce and JVM PID,
ignores environment proxies and does not forward upstream cookies,
Authorization, request query parameters or redirect responses. Settings force
map and live roots to local relative paths and suppress extra scripts/styles.
Twelve concurrent upstream requests and 32 MiB/resource bound transport use.

Capabilities are not public share links. Do not copy viewer URLs into public
chat, logs or analytics. Configure reverse-proxy access logs to redact the
`/map/view/<capability>/` path component. All map responses are `no-store` and
`no-referrer`. If Fabricator authentication is explicitly disabled, anyone
who can reach the panel can obtain a map capability; do not claim private maps
on an authentication-disabled deployment.

Managed listener/core/adapter configuration is checksum-guarded. An external
edit makes map access fail closed rather than silently accepting a new host or
webroot. **Disable panel map access** still works in this state. Back up and
review the managed files. Restore the original generated files to re-enable
access; do not edit the manifest hashes to bypass the guard. If those originals
are unavailable, leave panel map access disabled: configuration adoption/reset
and force-overwrite are not implemented in this first integration.

When a development or reverse proxy rewrites the upstream Host header, include
the public browser origin in `CORS_ORIGINS`. The capability remembers that
allowlisted origin for its path-scoped CSP; it must not point asset/live requests
at the internal Flask origin. Do not strip the standard Origin header on the
authenticated session-creation POST.

## Validation and remaining acceptance

Backend tests cover OIDC read/manage separation, cross-server access, logout
and live grant revocation, every resource path, local-only transport, redirect
and proxy refusal, body limits, dependency/version checks, consent, persistence,
configuration preservation/drift, stale heartbeat and JVM restart identity.
Frontend DOM tests exercise the real Vue component's read-only state, sandbox,
explicit consent and stale-view removal. The frontend production build and the
adapter's pinned-API build/unit tests are part of validation.

A bounded browser test used the **actual BlueMap 5.7 bundled viewer** with an
isolated local HTTP upstream and clearly synthetic player/global-marker data.
The marker fixture is emitted by the actual pinned `MarkerGson`/`POIMarker`
API during adapter tests (`build/test-markers.json`), not a guessed viewer schema.
It demonstrated WebGL initialization, map/dimension switching, native HTTP
player/marker polling without cookies, removal after empty snapshots, exclusion
of a synthetic private layer, opaque origin, denied parent/cookie/API access,
and desktop/mobile layout. No real world, player data, paid account, server
deployment or renderer resource download was used.

This is **not a live Minecraft/Waystones end-to-end proof**. Before operational
use, an authorized operator still needs a disposable, licensed game-server
test on the exact matrix: render a small permitted world, connect/disconnect a
test player, test the actual vanish integration, create/rename/move/delete a
global waystone, change it to TEAM and ACTIVATION, reload BlueMap and restart
the JVM, checking both viewer and direct marker/player feeds. Verify that old
positions/markers disappear and the loopback listener is not publicly reachable.

Sources: [COBBLEVERSE](https://modrinth.com/modpack/cobbleverse),
[BlueMap 5.7 Fabric](https://modrinth.com/plugin/bluemap/version/Dr2hvJBc),
[Waystones 21.1.37 API and native integration](https://github.com/TwelveIterations/Waystones/tree/v21.1.37/common/src/main/java/net/blay09/mods/waystones),
[BlueMap 5.7 source](https://github.com/BlueMap-Minecraft/BlueMap/tree/v5.7).
