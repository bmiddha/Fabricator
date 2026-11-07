# Fabricator - Forked

> **Experimental fork:** Independently maintained fork of [philderks/Fabricator](https://github.com/philderks/Fabricator). `main` is the active development branch; features may be incomplete or change without notice.

## Experimental features

- **OIDC login** — authentication through an OpenID Connect provider.
- **DNS management** — manage DNS records for Minecraft servers.
- **S3 backups** — [S3-compatible destinations](deploy/backups/README.md) for saved backup configurations.
- **Client modpack exports** — [persistent client-only selections, private `.mrpack` downloads and opt-in expiring share links](deploy/client-packs/README.md), combined with exact installed server mod versions.
- **Instance forks & world copies** — [stopped-only independent server copies and dimension-preserving world transfers](deploy/transfers/README.md), with explicit scope, compatibility checks, mandatory destination safety backups and transactional recovery.


<div align="center">

<img src="frontend/public/favicon.svg" alt="Fabricator Logo" width="92" height="92" />

# Fabricator

**Self-hosted web dashboard for managing Minecraft servers.**

[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue?style=flat-square)](LICENSE)
[![Stars](https://img.shields.io/github/stars/bmiddha/Fabricator?style=flat-square)](https://github.com/bmiddha/Fabricator/stargazers)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20Docker-lightgrey?style=flat-square)](https://github.com/bmiddha/Fabricator)
[![Docker](https://img.shields.io/badge/ghcr.io-bmiddha%2Ffabricator-2496ED?style=flat-square&logo=docker&logoColor=white)](https://github.com/bmiddha/Fabricator/pkgs/container/fabricator)

[Documentation](API_DOCS.md) | [Fork issues](https://github.com/bmiddha/Fabricator/issues) | [MCP server for AI clients](mcp/README.md)

</div>

---

<div align="center">
  <img src="assets/screenshots/overview.png" alt="Fabricator dashboard — server overview" width="1280" />
</div>


---

## Features & Roadmap

| | Feature | Status |
|---|---|---|
| 🧩 | **Loaders**: Fabric, Quilt, NeoForge, Forge, Paper, Purpur, Folia, Pufferfish, Vanilla | ✅ Available |
| 📦 | **Mod & plugin management**: install, remove, enable/disable, and browse Modrinth mods and plugins; preview project descriptions from search or installed files; update checks with missing-dependency detection | ✅ Available |
| 🧰 | **Modpack import**: find popular Modrinth packs, search by name or link, or upload an exported `.mrpack` | ✅ Available |
| 📋 | **Logs & monitoring**: live log stream, TPS and RAM graphs | ✅ Available |
| 📁 | **File explorer & editor**: browse the server tree, search files, and edit configuration files in the panel | ✅ Available |
| 👥 | **Player management**: online players, player heads, custom actions | ✅ Available |
| 💾 | **Backups & restore**: manual and scheduled snapshots, restore from any backup | ✅ Available |
| ⏰ | **Scheduled commands**: run console commands and server actions on a schedule | ✅ Available |
| 🔄 | **One-click server upgrades**: Vanilla and Paper, with an automatic snapshot first | ✅ Available |
| 🌍 | **World import**: upload a world archive and swap it in | ✅ Available |
| 📂 | **Server import**: adopt an existing server folder into Fabricator | ✅ Available |
| 🎨 | **MOTD editor & server icon**: colors and formatting with a live server-list preview | ✅ Available |
| 🖥️ | **Multiple servers**: manage several instances from one dashboard, in your own order | ✅ Available |
| ▶️ | **Auto-start**: always, never, or last-state restore | ✅ Available |
| ☕ | **Managed Java runtimes**: downloaded and pinned per server | ✅ Available |
| 🌐 | **playit.gg tunnels**: public access without port forwarding | ✅ Available |
| 🐳 | **Docker**: single multi-arch (amd64/arm64) image, runs non-root | ✅ Available |
| ⌨️ | **CLI** (`fabricator`): `status`, `start`/`stop`, `update`, `version`, `uninstall` | ✅ Available |
| ⬆️ | **Self-update**: from the web UI, or via the CLI or install script | ✅ Available |
| 🤖 | **[MCP server](mcp/README.md)**: manage servers from AI clients | ✅ Available |
| ⌨️ | **CLI**: expanded server and mod management commands | 📋 Planned |
| 🔄 | **Upgrades for modded loaders** (Fabric, Quilt, NeoForge, Forge) | 📋 Planned |
| 📊 | **Metrics tab**: CPU, RAM, and usage over time | 📋 Planned |
| 🔐 | **OIDC user roles and permissions** | 🧪 Experimental |
| ✏️ | **Advanced editor & archived logs** | 📋 Planned |
| 📥 | **More install targets**: Proxmox VE, CasaOS, Umbrel, Unraid, Coolify/Dokploy | 📋 Planned |

---

## Screenshots

Use **Details** on a search result or installed mod/plugin to read its Modrinth
short description and full Markdown project body without leaving the panel.
Details load only when opened and are also available with `mods.read` access
(installation rights are not required). Installed files are identified through
their install record or content hash, including disabled files; unidentified
files show an unavailable state instead of guessing a project from the name.
Raw HTML in descriptions is displayed as text, and links open safely in a new tab.
Relative Markdown links and images resolve against `https://modrinth.com/`;
no separate repository README is fetched.

<table>
  <tr>
    <td width="50%">
      <img src="assets/screenshots/console.png" alt="Live console with log level filters and a command input" />
      <sub><b>Console</b> — live log stream, level filters, and a command input</sub>
    </td>
    <td width="50%">
      <img src="assets/screenshots/mods.png" alt="Browsing Modrinth mods from the panel" />
      <sub><b>Mods</b> — search Modrinth and install mods or plugins in one click</sub>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <img src="assets/screenshots/files.png" alt="File explorer with server.properties open in the editor" />
      <sub><b>Files</b> — browse the server tree and edit configs in the panel</sub>
    </td>
    <td width="50%">
      <img src="assets/screenshots/createserver.png" alt="Create New Server dialog with the mod loader dropdown open" />
      <sub><b>New server</b> — pick a version and loader, or import a modpack</sub>
    </td>
  </tr>
</table>

---

## Quick Start

### DNS and Velocity networks

Fabricator can publish public server hostnames and Minecraft SRV records through
an ExternalDNS-compatible webhook service. The initial backends are Cloudflare
(using libdns) and self-hosted PowerDNS Authoritative. See the opt-in
[DNS deployment recipes](deploy/dns/README.md); the default Compose installation
does not expose DNS or enable record management.

Configure the private webhook URL and allowed domain in **Settings > DNS**, then
enable **Settings > DNS** on each server to publish its reachable public address.
Manual A, AAAA, CNAME, SRV, TXT, MX, CAA, and child NS records are also supported.
Provider credentials belong in the webhook deployment, not in the dashboard.
Saved changes reconcile automatically; preview and synchronize controls show
pending operations and provider failures. Stopping a server does not remove DNS.
Disabling a binding or DNS management schedules owned records for removal.

Velocity proxies use the same server lifecycle controls with dedicated backend,
fallback, forced-host, and forwarding configuration. Publish DNS to the proxy,
not its private backends. Configuring DNS does not open ports, traverse NAT, or
isolate backends: restrict backend access to the proxy and review forwarding
changes before applying them.

The webhook service can also be used by Kubernetes ExternalDNS. Fabricator
itself does not create Kubernetes resources or require cluster credentials.
Use separate ownership IDs and partition names between writers; do not let
multiple controllers manage the same record set.

### Docker (recommended)

A single multi-arch fork image is built by the release workflow. It runs as a
non-root user and keeps all state on one `/data` volume.
Published images require a successful fork release and package access; no
upstream image is used as a fallback. To run unreleased `main`, clone this fork
and build locally first:

```bash
git clone https://github.com/bmiddha/Fabricator.git
cd Fabricator
docker build -t ghcr.io/bmiddha/fabricator:latest .
docker compose up -d --pull never
```

```bash
# Grab the compose file and start
curl -fsSL https://raw.githubusercontent.com/bmiddha/Fabricator/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

The packaged `docker-compose.yml` publishes the panel to **host loopback only**
(`127.0.0.1:5000`). On first use you'll be prompted to create the operator password.
Minecraft server ports aren't published by default; use the built-in playit.gg tunnel,
or map each server's port explicitly (e.g. `25565:25565`).

To update, pull the new image and recreate the container — your data persists in
the named volume:

```bash
docker compose pull && docker compose up -d
```

Docker is the simplest way to run Fabricator on **Linux, macOS, or Windows** (via
Docker Desktop / WSL2).

### Windows (portable .exe)

The fork's [panel release workflow](https://github.com/bmiddha/Fabricator/releases) builds a portable
`Fabricator-<version>.exe`. Download it and run it — no install needed. It opens the
panel in your browser at `http://127.0.0.1:5000` and adds a tray icon with **Open in
Browser**, **Open Data Folder**, and **Quit**. The panel listens on loopback only.

Data lives in `%APPDATA%\Fabricator`, so you can move or replace the `.exe` freely.
To update, use the update button in the panel's sidebar: it downloads the new `.exe`
and verifies its checksum.

### One-line installer

The script downloads a **release tarball** from the fork's [GitHub Releases](https://github.com/bmiddha/Fabricator/releases), installs dependencies, creates a `fabricator` user and systemd unit, and starts the app. Supported distros: Debian/Ubuntu (and derivatives), Arch, Fedora/RHEL family. **systemd** and **curl** are required. A fork panel release must exist first; missing releases fail explicitly rather than installing upstream.

```bash
curl -fsSL https://raw.githubusercontent.com/bmiddha/Fabricator/main/tools/install.sh | bash
```

By default this installs the **latest** published fork panel release, not the
current `main` checkout. `main` is the active development branch. Before the
first fork release, use manual installation below, or build `frontend/dist`
in a local clone and run `sudo FABRICATOR_LOCAL_SRC="$PWD" bash tools/install.sh`.
Local-source deployment still installs a system service; dashboard/CLI updates
subsequently target published fork releases, not automatic Git pulls.

The installer records its repository and script branch in root-owned
`/etc/fabricator/source.env`, used by the panel, CLI, and privileged updater.
`FABRICATOR_REPO` and `FABRICATOR_BRANCH` can select another explicit source
when running the installer; both are validated and persisted. To migrate an
existing upstream native install, explicitly run this fork's installer with
`FABRICATOR_REPO=bmiddha/Fabricator`. Server data and authentication settings
are preserved. No update path silently switches back to upstream.

**Updating.** The easiest way is from the panel itself: Fabricator checks GitHub Releases and you can update from the sidebar. You can also re-run the installer, which detects an existing install and updates in place (backing up `servers.json` and `fabricator.env`, keeping your data under `/var/lib/fabricator`):

```bash
curl -fsSL https://raw.githubusercontent.com/bmiddha/Fabricator/main/tools/install.sh | FABRICATOR_REPO=bmiddha/Fabricator bash
```

Or update from the CLI:

```bash
fabricator update
```

After install, open `http://<host>:5000` (the default packaged config binds to all interfaces — use a firewall or reverse proxy if the host is reachable from untrusted networks).

For local development, the app defaults to loopback-only; see `.env.example`.

<details>
<summary>Manual installation</summary>

```bash
# Clone
git clone https://github.com/bmiddha/Fabricator.git
cd Fabricator

# Backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Frontend
cd frontend
npm install
npm run build
cd ..

# Run (development — serves API + built frontend from frontend/dist)
python3 run.py
```

Environment variables are documented in `.env.example`. For production-like paths, set `FLASK_ENV=production`, `SERVER_ROOT`, and `SERVER_INDEX_FILE` (see Configuration).

</details>

### Fork publishing prerequisites

Enable Actions in this fork and allow its release workflow to write releases
and GHCR packages. Publish a panel `v*` release to build Linux tarballs,
Windows executables, and `ghcr.io/bmiddha/fabricator` images. Make the package
public or authenticate Docker pulls as appropriate. These repository/account
settings and the first release are manual actions, not performed by this change.
CI runs against `main`; merging does not itself publish a release or image.
Enable private vulnerability reporting before relying on the fork's security
reporting link.

MCP client configuration installs directly from this fork's `main`, not the
upstream PyPI package. The inherited MCP registry descriptor and package name
remain upstream-owned metadata; the publishing job is disabled for this fork.
Publishing a separate package would require registered fork-owned identities
and credentials first. Website deployment is also off by default; enable
`WEBSITE_DEPLOY_ENABLED=true` only with your own
`CLOUDFLARE_PAGES_DEPLOY_HOOK_URL` repository secret. No upstream website is
required to install or run the fork.

---

## Requirements

| | Requirement |
|---|---|
| Docker | Any OS with Docker Engine or Docker Desktop (recommended path) |
| OS | Linux (Debian/Ubuntu, Arch, or Fedora/RHEL, systemd) for the native installer; Windows for the portable `.exe` |
| Python | 3.11+ (manual/native install) |
| Node.js | 22.x (frontend build only) |

> The native installer targets Linux, and Windows has the portable `.exe`. On macOS, run Fabricator via Docker.

---

## Configuration

With Docker, set these as `environment:` entries in `docker-compose.yml`; the image already pins every data path onto the `/data` volume. For the native install, the installer writes `/etc/fabricator/fabricator.env` (group `fabricator`, mode `0640`). Common variables:

```env
# Listen address — packaged default is all interfaces; use a reverse proxy in production
HOST=0.0.0.0
PORT=5000
FLASK_ENV=production

# Server instances and index (paths must be writable by the fabricator user)
SERVER_ROOT=/var/lib/fabricator/servers
SERVER_INDEX_FILE=/var/lib/fabricator/servers.json

# Optional: comma-separated origins for a separate dev UI or external frontends (no *)
# CORS_ORIGINS=https://dashboard.example.com

# playit.gg tunnel agent — set to "true" to start the agent automatically on boot.
# The agent creates a public tunnel so the server is reachable without port-forwarding.
PLAYIT_ENABLED=false
```

The playit.gg tunnel makes a server reachable without port forwarding. The installer
provisions the pinned, sha256-verified `playit` and `playit-cli` binaries automatically —
no separate step. Turn the tunnel on per server under **Server → Settings → Network** (or
**Overview → Public access**), which walks you through the one-time playit.gg account claim.
Set `PLAYIT_ENABLED=true` above only if you want it to auto-start on boot.

Managed Java runtimes are stored under `/var/lib/fabricator/java` by default when `FLASK_ENV=production`.

After editing, restart the service:

```bash
sudo systemctl restart fabricator
```

<details>
<summary>Filesystem layout</summary>

```
/opt/fabricator/app              # Application code (fabricator:fabricator)
/opt/fabricator/venv             # Python virtualenv (fabricator:fabricator)
/var/lib/fabricator              # Server data, backups, managed Java
/etc/fabricator/fabricator.env   # Config (root:fabricator, 0640)
/etc/systemd/system/fabricator.service
```

</details>

### Authentication

The management panel requires a login by default. On **first boot** (no
credential configured yet) it starts in a locked **setup mode**: open the panel
and you'll be taken to a one-time page to create the operator password — in the
browser, the same on Linux and Docker (no terminal needed).
Until a password is set, only that page is reachable.

The session signing key is generated and persisted automatically on first boot;
the password is stored, hashed, in a `0600` `auth.json` in the data directory
next to `servers.json` (`/var/lib/fabricator` under systemd, `/data` in Docker).

**Advanced / declarative setup.** You can skip the setup page by providing the
credential up front — useful for Docker/automation and recommended on untrusted
networks (see the security note):

1. Optionally pin `SECRET_KEY` to a fixed value (otherwise auto-generated):
   `python -c "import secrets; print(secrets.token_hex(32))"`
2. Generate a password hash and set `FABRICATOR_AUTH_PASSWORD_HASH`:
   - systemd install: `fabricator hash-password`
   - Docker / source: `python -m backend.auth hash`

Precedence: env hash > persisted file > setup mode (and for the signing key:
env > file > auto-generated).

To run **without** the built-in login (only if you front Fabricator with your
own reverse-proxy authentication), set `FABRICATOR_DISABLE_AUTH=1`. This is the
only supported way to disable it.

**Change, turn off, or reset the password.** Once logged in, open
**Settings → Security** to change the password. From the same screen you can turn
password protection off (after confirming the current password) or turn it back on
with a new password. Only turn it off on a trusted network: anyone who can reach
Fabricator will then be able to manage your servers. Forgot it / locked out? Delete
`auth.json` from the data
directory and restart — the app drops back into setup mode so you can set a new
one. The file lives next to `servers.json`: `/var/lib/fabricator/auth.json` under
systemd, `/data/auth.json` in Docker. (Deleting it only resets the password/key;
your servers are untouched.)

> **Security note (trust-on-first-use).** The first-boot setup page is reachable
> by anyone who can reach the panel until the password is set. On an untrusted
> network, set `FABRICATOR_AUTH_PASSWORD_HASH` before first exposure instead of
> relying on the open setup page.

When Fabricator is served behind TLS, also set `FABRICATOR_SESSION_COOKIE_SECURE=1`
so the session cookie carries the `Secure` flag.

#### OIDC, multiple users and custom roles

Configure one OpenID Connect provider to replace password login and first-boot
password setup. Register a **confidential Authorization Code client**, with PKCE
S256 support, `openid` scope and this exact callback:
`https://panel.example/api/auth/oidc/callback`.

```dotenv
FABRICATOR_OIDC_ISSUER=https://identity.example/realms/home
FABRICATOR_OIDC_CLIENT_ID=fabricator
FABRICATOR_OIDC_CLIENT_SECRET=your-client-secret
FABRICATOR_OIDC_REDIRECT_URI=https://panel.example/api/auth/oidc/callback
FABRICATOR_SESSION_COOKIE_SECURE=1
FABRICATOR_OIDC_DISPLAY_NAME=Home SSO
FABRICATOR_OIDC_USERS='{"admin-sub":{"roles":["admin"]},"user-sub":{"roles":["token-user"],"servers":{"srv_1":["operator"],"srv_2":["viewer","backup-reader"]}}}'
FABRICATOR_AUTH_ROLES='{"backup-reader":["server.view","backups.list","backups.download"]}'
```

Use the provider's exact, case-sensitive **subject (`sub`)**, not an email,
display name or Minecraft account. Only configured subjects can sign in. Use
stable server IDs from the server URLs/API, not server names. Settings shows
the current account; role/user configuration stays deployment-owned.

The pre-provisioned roles are:

| Role | Default permissions |
| --- | --- |
| `admin` | Every permission. Assign globally for full administration. |
| `operator` | Server status, metrics and logs; start, stop and restart. |
| `viewer` | Server status, metrics and logs only. |
| `token-user` | Manage the signed-in user's own MCP tokens. Assign globally. |

Roles are fully customizable: `FABRICATOR_AUTH_ROLES` can add arbitrary role
names or **replace** any built-in's entire permission list. Unspecified built-ins
retain their defaults. Multiple roles combine additively. Assign global roles
with `roles`, or per-server roles with `servers`. Global server permissions
cover all existing and future servers; per-server roles cover only named IDs.
Instance-wide permissions in a per-server assignment are ignored. There are no
wildcards, explicit denies or role inheritance, and no special privilege attached
to the name `admin`. At least one configured user must retain every permission
globally to prevent administrative lockout.

See the [permission catalog](API_DOCS.md#oidc-permission-catalog) for individual
file, console, mod, backup, player, schedule and host operations. Grant carefully:
logs can expose game/chat data and secrets; console, file writes, mod installation,
restores and launch settings can execute code on the shared host. This is panel
authorization, not isolation between server processes.

**MCP tokens** are owned by the account that creates them and are limited by
both their read/manage scope and that account's current permissions. Grant
`token-user` globally for self-service mint/revoke. `tokens.all.manage` permits
listing/revoking other users' tokens, and `mcp.configure` controls the global
switch. Ownerless pre-OIDC tokens stop working in OIDC mode: replace them
through Settings. Tokens cannot manage tokens or the switch, even for an admin.

**Deployment and recovery.** Apply environment variables using the normal
source `.env`, Docker environment, systemd environment file or Windows executable
configuration, then restart. The callback uses the configured external URL,
not inferred proxy headers; forward `/api/` unchanged through your trusted TLS
proxy. Keep client secrets out of source control. For development, HTTP is
allowed only for loopback URLs; with Vite use
`http://localhost:3000/api/auth/oidc/callback` and its existing API proxy.
The same login flow is used by source, Docker and packaged Windows builds.

Incomplete or invalid configuration, conflicting `FABRICATOR_DISABLE_AUTH`,
unknown roles/permissions and administrative lockout fail startup explicitly.
Provider outages fail login closed with a retry message, never a password
fallback. OIDC ignores persisted password-disable flags and existing password
hashes without deleting them. To recover, fix deployment configuration, or
remove **all** OIDC/role variables and restart to restore the prior password
policy; a previously disabled password makes that restored policy open, so do
this only on a trusted network. Deleting `auth.json` does not bypass OIDC.
Changes to users/roles apply to existing sessions and tokens after restart.
Logout clears only Fabricator's session; provider SSO may sign the next login
back in without prompting. OIDC does not authenticate Minecraft players.

---

## CLI

The native (systemd) install ships a `fabricator` command for managing the service from the shell:

```bash
fabricator status            # systemd state + Flask/Minecraft API reachability (--json available)
fabricator start | stop      # control the systemd service
fabricator update            # update to the latest GitHub release (runs the installer)
fabricator version           # show the installed version
fabricator hash-password     # generate a password hash for FABRICATOR_AUTH_PASSWORD_HASH
fabricator uninstall         # remove app, data, config, systemd unit, and service user
fabricator help              # list all commands
```

The read commands (`status`, `version`, `help`) accept `--json` for scripting. The CLI is currently minimal and will grow to cover server and mod management.

---

> **A note on AI usage:** I use Claude while developing Fabricator. It lets me ship features and fixes faster than my free time would otherwise allow. I review and test every change myself before it is released, and backend tests run on every pull request. If you'd rather not run AI-assisted software, that's a fair choice, and I'd rather you know upfront.

---

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines. For larger changes, open an issue first.

---

## License

[AGPL-3.0](LICENSE)
