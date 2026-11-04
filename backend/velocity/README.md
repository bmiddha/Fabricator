# Velocity backend

Register `backend.velocity.routes.velocity_bp` with `/api` as its URL prefix.
In OIDC mode configuration reads require `server.settings.read`; writes require
`server.settings.write`. Forwarding preview requires `server.settings.read`
on the proxy and every selected backend; apply additionally requires
`server.settings.write` on all of them. Every target requires `server.view`.
All Velocity configuration and forwarding endpoints remain session-only.

Create/install/start/stop/restart/delete use the existing server endpoints, with
`loader: "velocity"` and the **Velocity project version** in `version`.
`GET /api/loaders/velocity/versions/game` lists those project versions, including
snapshots; there is no separate loader version. Installation uses the PaperMC
Fill v3 API, verifies the published SHA-256, and persists a jar LaunchSpec without
`nogui`. Velocity requires Java 21+, uses `plugins/`, shuts down with `shutdown`,
and does not create an EULA, game properties, or a TPS sampler. Managed mode
does not support creating proxies. The existing game-server upgrade endpoint
does not support Velocity upgrades.

## Structured configuration

`GET /api/servers/<id>/velocity/config` returns:

```json
{
  "listen": {"host": "0.0.0.0", "port": 25565},
  "onlineMode": true,
  "forwardingMode": "modern",
  "backends": [
    {"name": "lobby", "serverId": "srv_example", "address": "127.0.0.1:25566"}
  ],
  "try": ["lobby"],
  "forcedHosts": {"play.example.com": ["lobby"]},
  "forwardingSecretConfigured": true
}
```

`PUT` to the same URL accepts this complete configuration. `address` and
`forwardingSecretConfigured` are response-only values which may be echoed; they
are ignored on write. Backend addresses are derived from referenced game-server
records, never caller-provided endpoints. Unspecified backend bind addresses
become loopback addresses. Backend names must be unique alphanumeric names with
optional hyphens/underscores, excluding `try`. A game server can have one name
per proxy. Proxies cannot be backends, and referenced servers cannot be deleted.
The first entry in `try` is the default server; later entries are ordered
fallbacks. Forced hosts use fully qualified IDNA-normalized hostnames and
ordered lists of configured backend names. Wildcards are not supported.

Only `modern` and `none` forwarding modes are supported. Existing legacy or
BungeeGuard configurations require manual migration, not silent conversion.
The proxy must be fully stopped for a configuration write. Invalid input is
400, a running/stopping/busy server is 409, and filesystem failures are 500.
Unrelated TOML sections, values, and comments are preserved. Writes back up
existing files beside the originals with `.fabricator-backup-<random>` suffixes.
Backend address references are refreshed before the proxy starts.

The public Python interface for DNS is `backend.velocity.service.get_config(server)`,
which returns this same object without writing files or exposing credentials.
DNS should point public hostnames at the **proxy**, using `listen.port`, not at
the game-server backend ports. `forcedHosts` is routing configuration and does
not itself publish DNS records. Server create/delete, port changes, and proxy
routing updates request DNS reconciliation when that subsystem is available.

Proxy server GET/create responses include `kind: "proxy"` and `capabilities`
flags: `proxy`, `velocityConfig`, `serverProperties`, `worlds`, `players`, `tps`,
`eula`, and `contentKind`. Game-only capabilities are false; content kind is
`plugin`. Frontends should hide game properties, world/player controls, EULA,
and TPS displays for proxies, and use the dedicated routing form.
World-import, player administration, Minecraft modpack installs, and the
game-server upgrade API reject proxy targets even when called directly.
Modrinth search/version/install/update/dependency requests for Velocity use the
`velocity` loader facet **without** treating the proxy project version as a
Minecraft game-version filter.

## Explicit modern forwarding setup

1. Configure the proxy's named backends and set `forwardingMode: "modern"`.
2. Stop the proxy and affected game servers.
3. `POST /api/servers/<id>/velocity/forwarding/preview` with
   `{"serverIds":["srv_example"]}`.
4. Review the returned `backends[].files`, `backends[].changes`, and `warnings`.
5. `POST /api/servers/<id>/velocity/forwarding/apply` with those `serverIds`,
   the returned `previewToken`, and `confirmOfflineMode: true`.
6. Restart both the proxy and the backends.

Preview returns `{proxyId, backends, previewToken,
requiresOfflineModeConfirmation, warnings}`. Apply adds `applied: true` and
`backups` (backup filenames, not secret-bearing file content). The preview
token binds the plan to current config and file content; any change requires
a new preview. Preview is read-only and neither changes backend authentication
nor creates missing secrets.

Supported targets are initialized Paper, Folia, Purpur, and Pufferfish release
servers on Minecraft 1.13.2+. Other loaders, snapshots, malformed YAML, and
conflicting proxy ownership are rejected. Minecraft 1.19+ uses
`config/paper-global.yml` (`proxies.velocity`); older releases use `paper.yml`
(`settings.velocity-support`). Apply enables modern forwarding, synchronizes
the proxy's online-mode flag, disables Spigot's BungeeCord mode, and **explicitly
sets backend `online-mode=false` only after confirmation**. Unrelated YAML and
properties and their comments are retained. The protected secret is inserted
internally and is never part of previews or JSON server records.

Backend ports should be private/loopback or protected by a firewall. Removing
a backend route or deleting a proxy does not silently undo authentication or
restore old YAML. Restore the generated backups deliberately if reverting
forwarding. A backend can be configured for a replacement proxy after its
previous proxy is deleted. Game settings cannot change a referenced
forwarding-managed backend's authentication. Bind/port changes require the
referencing proxies to be stopped; their endpoints are refreshed on next start.

## Secrets and recovery

The stable, randomly generated secret lives outside the server directory in
`<server-index-directory>/velocity-secrets/<proxy-id>.secret` with owner-only
permissions. `velocity.toml` refers to its absolute path. Reinstall and restart
reuse it; an existing proxy with a missing or invalid secret fails closed
instead of rotating the secret and breaking its backends. Restore the original
secret from an administrator-controlled backup. Deleting the proxy removes
its secret.

Raw file APIs protect `velocity.toml`, forwarding-managed backend configuration,
and their backup files, preventing credential disclosure and routing-validation
bypasses. Administrators with filesystem access or full-server backup access
can still read secret-bearing backend YAML; those backups must be protected.
The external secret directory is **not** included in ordinary server snapshots,
so back it up separately. Configuration writes are staged, fsynced, and replaced;
multi-file forwarding writes roll back replaced content if a replacement fails.
