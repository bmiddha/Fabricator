"""Deployment-owned roles and a default-deny API permission catalog."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from flask import current_app, g, jsonify, request

# None denotes an instance-wide operation. Other rules resolve a server first.
RULES: dict[tuple[str, str], tuple[tuple[str, ...], str | None]] = {}


def _rule(method, path, *permissions, target=None):
    RULES[method, "/api" + path] = (permissions, target)


def _server(method, suffix, *permissions):
    _rule(method, "/servers/<server_id>" + suffix, *permissions, target="path")


_rule("GET", "/servers", target="collection")
_rule("POST", "/servers", "servers.create")
_rule("POST", "/servers/import", "servers.import")
_rule("POST", "/servers/import/inspect", "servers.import")
_rule("PUT", "/servers/order", "servers.reorder")
_server("POST", "/transfers/<kind>/preview", "server.view")
_server("POST", "/transfers/<kind>", "server.view")
_server("GET", "/worlds", "worlds.read")
# These handlers verify both instances, the operation-specific permissions and
# job ownership before exposing metadata. Generic server.view is insufficient.
_rule("GET", "/transfer-jobs/<job_id>", target="transfer-job")
for method, suffix, permission in [
    ("GET", "", "server.view"),
    ("DELETE", "", "server.delete"),
    ("GET", "/metrics", "server.metrics.read"),
    ("GET", "/logs", "server.logs.read"),
    ("GET", "/icon", "server.view"),
    ("PUT", "/icon", "server.icon.write"),
    ("DELETE", "/icon", "server.icon.write"),
    ("GET", "/java-status", "server.view"),
    ("GET", "/map", "map.read"),
    ("POST", "/map/session", "map.read"),
    ("POST", "/map/setup", "map.manage"),
    ("PUT", "/map/enabled", "map.manage"),
    ("GET", "/dns", "dns.manage"),
    ("PUT", "/dns", "dns.manage"),
    ("GET", "/velocity/config", "server.settings.read"),
    ("PUT", "/velocity/config", "server.settings.write"),
    ("POST", "/velocity/forwarding/preview", "server.settings.read"),
    ("POST", "/velocity/forwarding/apply", "server.settings.write"),
    ("POST", "/start", "server.start"),
    ("POST", "/stop", "server.stop"),
    ("POST", "/restart", "server.restart"),
    ("POST", "/console", "server.console.execute"),
    ("POST", "/install", "server.install"),
    ("GET", "/install/progress", "server.install"),
    ("POST", "/upgrade", "server.upgrade"),
    ("PUT", "/settings", "server.settings.write"),
    ("PUT", "/autostart", "server.autostart.write"),
    ("GET", "/files", "files.list"),
    ("GET", "/files/search", "files.list"),
    ("GET", "/files/content", "files.read"),
    ("PUT", "/files/content", "files.write"),
    ("POST", "/files/upload", "files.upload"),
    ("POST", "/files/folder", "files.create_directory"),
    ("DELETE", "/files", "files.delete"),
    ("GET", "/mods", "mods.read"),
    ("GET", "/client-pack", "clientpacks.read"),
    ("PUT", "/client-pack", "clientpacks.manage"),
    ("POST", "/client-pack/import", "clientpacks.manage"),
    ("POST", "/client-pack/exports", "clientpacks.export"),
    ("GET", "/client-pack/exports", "clientpacks.download"),
    ("GET", "/client-pack/exports/<artifact_id>/download", "clientpacks.download"),
    ("DELETE", "/mods", "mods.delete"),
    ("DELETE", "/mods/<path:filename>", "mods.delete"),
    ("PATCH", "/mods", "mods.toggle"),
    ("GET", "/snapshots", "backups.list"),
    ("GET", "/backup-summary", "backups.list"),
    ("GET", "/snapshots/<snapshot_id>/download", "backups.download"),
    ("POST", "/snapshots/<snapshot_id>/restore", "backups.restore"),
    ("DELETE", "/snapshots/<snapshot_id>", "backups.delete"),
    ("GET", "/backup-configs", "backups.config.read"),
    ("POST", "/backup-configs", "backups.config.write"),
    ("PUT", "/backup-configs/<config_id>", "backups.config.write"),
    ("DELETE", "/backup-configs/<config_id>", "backups.config.write"),
    ("POST", "/backup-configs/<config_id>/run", "backups.create"),
    ("POST", "/backup-quick", "backups.create"),
    ("POST", "/world-import", "worlds.import"),
    ("GET", "/schedules", "schedules.read"),
    ("POST", "/schedules", "schedules.create"),
    ("PUT", "/schedules/<schedule_id>", "schedules.update"),
    ("DELETE", "/schedules/<schedule_id>", "schedules.delete"),
    ("POST", "/schedules/<schedule_id>/run", "schedules.run"),
    ("GET", "/players/state", "players.read"),
    ("GET", "/players/online", "players.read"),
    ("POST", "/players/kick", "players.kick"),
    ("DELETE", "/players/known", "players.known.delete"),
]:
    _server(method, suffix, permission)
_server("POST", "/map/install-bluemap", "map.manage", "mods.install")
for method, suffix in [
    ("GET", "/client-pack/shares"),
    ("POST", "/client-pack/exports/<artifact_id>/shares"),
    ("DELETE", "/client-pack/shares/<share_id>"),
    ("DELETE", "/client-pack/exports/<artifact_id>"),
]:
    _server(method, suffix, "clientpacks.manage", "clientpacks.download")
for suffix, methods, permission in [
    ("/players/bans", ("POST", "DELETE"), "players.bans.manage"),
    ("/players/bans/ip", ("POST", "DELETE"), "players.bans.manage"),
    ("/players/ops", ("POST", "PATCH", "DELETE"), "players.ops.manage"),
    ("/players/whitelist", ("POST", "DELETE"), "players.whitelist.manage"),
    ("/players/whitelist/active", ("PATCH",), "players.whitelist.manage"),
    ("/players/whitelist/enforce", ("PATCH",), "players.whitelist.manage"),
]:
    for method in methods:
        _server(method, suffix, permission)
for method, path, permission in [
    ("GET", "/dns", "dns.manage"),
    ("PUT", "/dns/config", "dns.manage"),
    ("PUT", "/dns/records", "dns.manage"),
    ("POST", "/dns/plan", "dns.manage"),
    ("POST", "/dns/sync", "dns.manage"),
    ("GET", "/metrics/system", "host.metrics.read"),
    ("GET", "/java/status", "java.read"),
    ("GET", "/java/installed", "java.read"),
    ("GET", "/java/install/progress/<task_id>", "java.read"),
    ("POST", "/java/install", "java.install"),
    ("DELETE", "/java/install/<task_id>", "java.install"),
    ("DELETE", "/java/installed/<int:major>", "java.delete"),
    ("GET", "/system/update/status", "updates.read"),
    ("POST", "/system/update", "updates.apply"),
    ("GET", "/playit/status", "tunnels.read"),
    ("POST", "/playit/start", "tunnels.manage"),
    ("POST", "/playit/stop", "tunnels.manage"),
    ("POST", "/playit/reset", "tunnels.manage"),
    ("PUT", "/integrations/mcp", "mcp.configure"),
    ("POST", "/integrations/mcp/tokens", "tokens.self.manage"),
]:
    _rule(method, path, permission)
# These handlers perform ownership-aware checks, in addition to the gate.
_rule("GET", "/integrations/mcp", target="tokens")
_rule("DELETE", "/integrations/mcp/tokens/<token_id>", target="tokens")
_rule("GET", "/backup-jobs/<job_id>", "backups.create", target="backup-job")
for suffix, permission in [
    ("/resolve-installed", "mods.read"),
    ("/updates", "mods.read"),
    ("/project/<project_id>/missing-dependencies", "mods.read"),
]:
    _rule("GET", "/modrinth/servers/<server_id>" + suffix, permission, target="path")
_rule("GET", "/modrinth/modpack/install-progress/<server_id>", "modpacks.install", target="path")
_rule("POST", "/modrinth/mod/<mod_id>/install", "mods.install", target="body")
_rule("POST", "/modrinth/modpack/<project_id>/install", "modpacks.install", target="body")
_rule("POST", "/modrinth/modpack/upload", "modpacks.install", target="any-server")
_rule("DELETE", "/modrinth/modpack/upload/<upload_id>", "modpacks.install", target="upload")
_rule("POST", "/modrinth/modpack/upload/<upload_id>/install", "modpacks.install", target="upload-body")
for path in [
    "/loaders/<loader>/versions/game", "/loaders/<loader>/versions/loader",
    "/modrinth/search", "/modrinth/modpacks/search", "/modrinth/mod/<mod_id>",
    "/modrinth/project/<project_id>", "/modrinth/mod/<mod_id>/versions",
    "/modrinth/project/<project_id>/versions", "/modrinth/project/<project_id>/resolve-version",
    "/modrinth/mod/<mod_id>/download-url", "/modrinth/version/<version_id>",
    "/modrinth/categories", "/modrinth/loaders", "/modrinth/game-versions",
]:
    _rule("GET", path, target="catalog")

CATALOG = frozenset(
    {"server.settings.read"} | {p for perms, _ in RULES.values() for p in perms}
    | {"tokens.all.manage", "server.fork", "worlds.read"}
)
GLOBAL = frozenset(
    p for p in CATALOG
    if p.startswith(("servers.", "host.", "java.", "updates.", "tunnels.", "tokens.", "mcp.", "dns."))
)
DEFAULT_ROLES = {
    "admin": CATALOG,
    "viewer": frozenset({"server.view", "server.metrics.read", "server.logs.read"}),
    "operator": frozenset({
        "server.view", "server.metrics.read", "server.logs.read",
        "server.start", "server.stop", "server.restart",
    }),
    "token-user": frozenset({"tokens.self.manage"}),
}


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def config_json(raw: str, name: str):
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError):
        raise ValueError(f"{name} must be valid JSON without duplicate keys") from None


@dataclass(frozen=True)
class Principal:
    subject: str
    issuer: str
    roles: tuple[str, ...]
    global_permissions: frozenset[str]
    servers: dict[str, frozenset[str]]
    server_roles: dict[str, tuple[str, ...]]

    def permissions(self, server_id=None):
        if server_id is None:
            return self.global_permissions
        return self.global_permissions | self.servers.get(server_id, frozenset())

    def can(self, permission, server_id=None):
        if permission in GLOBAL:
            return permission in self.global_permissions
        return permission in self.permissions(server_id)

    @property
    def owner(self):
        return {"issuer": self.issuer, "sub": self.subject}

    def payload(self):
        return {
            "sub": self.subject, "roles": list(self.roles),
            "permissions": sorted(self.global_permissions),
            "servers": {
                key: {"roles": list(self.server_roles[key]), "permissions": sorted(value | self.global_permissions)}
                for key, value in self.servers.items()
            },
        }


def load_users(issuer: str, raw: str) -> dict[str, Principal]:
    roles = dict(DEFAULT_ROLES)
    overrides = config_json(os.environ.get("FABRICATOR_AUTH_ROLES", "{}"), "FABRICATOR_AUTH_ROLES")
    if not isinstance(overrides, dict):
        raise ValueError("FABRICATOR_AUTH_ROLES must be an object")
    for name, permissions in overrides.items():
        if (
            not isinstance(name, str) or not name.strip()
            or not isinstance(permissions, list)
            or any(not isinstance(p, str) or p not in CATALOG for p in permissions)
        ):
            raise ValueError("FABRICATOR_AUTH_ROLES contains an invalid role or unknown permission")
        roles[name] = frozenset(permissions)
    users = config_json(raw, "FABRICATOR_OIDC_USERS")
    if not isinstance(users, dict) or not users:
        raise ValueError("FABRICATOR_OIDC_USERS must be a nonempty object")

    def resolve(names):
        if not isinstance(names, list) or any(not isinstance(n, str) or n not in roles for n in names):
            raise ValueError("FABRICATOR_OIDC_USERS references invalid or unknown roles")
        return frozenset().union(*(roles[n] for n in names))

    result = {}
    for sub, entry in users.items():
        if not sub.strip() or not isinstance(entry, dict) or set(entry) - {"roles", "servers"}:
            raise ValueError("FABRICATOR_OIDC_USERS contains an invalid user")
        names = entry.get("roles", [])
        permissions = resolve(names)
        server_entries = entry.get("servers", {})
        if not isinstance(server_entries, dict):
            raise ValueError("user servers must be an object")
        grants, server_roles = {}, {}
        for server_id, assigned in server_entries.items():
            if not server_id.strip():
                raise ValueError("server IDs must be nonempty")
            grants[server_id] = resolve(assigned) - GLOBAL
            server_roles[server_id] = tuple(assigned)
            if grants[server_id] and "server.view" not in grants[server_id] | permissions:
                raise ValueError("server permissions require server.view")
        if permissions - GLOBAL and "server.view" not in permissions:
            raise ValueError("global server permissions require server.view")
        result[sub] = Principal(sub, issuer, tuple(names), permissions, grants, server_roles)
    if not any(CATALOG <= user.global_permissions for user in result.values()):
        raise ValueError("configure at least one user with every global permission to prevent lockout")
    return result


def principal():
    return g.get("principal")


def can(permission, server_id=None):
    user = principal()
    return user is None or user.can(permission, server_id)


def _deny(status=403):
    return jsonify({"error": "server not found" if status == 404 else "permission denied"}), status


def authorize(user: Principal):
    g.principal = user
    method = "GET" if request.method == "HEAD" else request.method
    rule = RULES.get((method, request.url_rule.rule if request.url_rule else ""))
    if rule is None:
        return _deny()
    required, target = rule
    if target == "collection":
        return None
    if target == "transfer-job":
        return None  # Ownership and both instance grants checked by handler.
    if target == "tokens":
        return None if any(user.can(p) for p in ("tokens.self.manage", "tokens.all.manage", "mcp.configure")) else _deny()
    if target in {"catalog", "any-server"}:
        options = required or (
            "mods.read", "mods.install", "modpacks.install", "clientpacks.read", "clientpacks.manage", "server.install",
            "server.upgrade", "servers.create",
        )
        if any(user.can(p) for p in options):
            return None
        return None if any(user.can("server.view", sid) and any(user.can(p, sid) for p in options) for sid in user.servers) else _deny()
    sid = (request.view_args or {}).get("server_id")
    if target in {"body", "upload-body"}:
        body = request.get_json(silent=True)
        sid = body.get("server_id") if isinstance(body, dict) else None
    if target == "backup-job":
        from backend.backups.progress import get
        job = get(request.view_args["job_id"])
        sid = job.get("server_id") if job else None
        if job:
            required = ({"restore": "backups.restore", "world_import": "worlds.import"}.get(job.get("kind"), "backups.create"),)
    if target in {"upload", "upload-body"}:
        from backend.modrinth import mrpack
        upload = mrpack.get(request.view_args["upload_id"])
        if upload is None or upload.owner != user.owner:
            return _deny(404)
        if target == "upload":
            return None
    if target is not None:
        if not isinstance(sid, str) or not user.can("server.view", sid):
            return _deny(404)
        if request.path.endswith("/install/progress"):
            return None if any(user.can(p, sid) for p in ("server.install", "server.upgrade")) else _deny()
        if any(not user.can(p, sid) for p in required):
            return _deny()
        if method == "POST" and request.url_rule.rule == "/api/servers/<server_id>/install":
            from backend.modrinth import mrpack, pending
            body = request.get_json(silent=True)
            intent = pending.normalize_intent(body.get("modpack")) if isinstance(body, dict) else None
            if intent:
                if not user.can("modpacks.install", sid):
                    return _deny()
                if intent["source"] == "upload":
                    upload = mrpack.get(intent["upload_id"])
                    if upload is None or upload.owner != user.owner:
                        return _deny(404)
        if request.method == "DELETE" and "backup-configs" in request.path:
            from backend.utils.strings import bool_from_str
            if bool_from_str(request.args.get("purge")) and not user.can("backups.delete", sid):
                return _deny()
    elif request.url_rule.rule == "/api/java/install/progress/<task_id>":
        return None if any(user.can(p) for p in ("java.read", "java.install")) else _deny()
    elif any(not user.can(p) for p in required):
        return _deny()
    return None


_SERVER_FIELDS = frozenset({
    "id", "name", "version", "loader", "status", "running", "runtime",
    "createdAt", "created_at", "online", "players", "maxPlayers", "port", "autoStart",
})
_RUNTIME_FIELDS = frozenset({
    "status", "running", "cpu", "cpuCores", "ram", "uptime", "players",
    "tps", "tpsSupported", "loader", "version",
})


def project_server(record, user):
    if not isinstance(record, dict):
        return record
    sid = record.get("id")
    value = dict(record) if user.can("server.settings.read", sid) else {
        k: v for k, v in record.items() if k in _SERVER_FIELDS
    }
    if user.can("server.metrics.read", sid):
        value.update({k: record[k] for k in ("memory", "memoryUnit") if k in record})
    if isinstance(value.get("runtime"), dict) and (
        not user.can("server.settings.read", sid) or not user.can("server.metrics.read", sid)
    ):
        fields = _RUNTIME_FIELDS if user.can("server.metrics.read", sid) else {"status", "running"}
        value["runtime"] = {k: v for k, v in value["runtime"].items() if k in fields}
    return value


def filter_response(response):
    if current_app.extensions.get("oidc") and request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    user = principal()
    if user is None or not response.is_json:
        return response
    rule = request.url_rule.rule if request.url_rule else ""
    data = response.get_json()
    if rule in {"/api/servers", "/api/servers/order"} and isinstance(data, list):
        data = [project_server(item, user) for item in data if user.can("server.view", item.get("id"))]
    elif rule in {
        "/api/servers", "/api/servers/import", "/api/servers/<server_id>",
        "/api/servers/<server_id>/settings",
    } and isinstance(data, dict) and "id" in data:
        data = project_server(data, user)
    if isinstance(data, dict) and isinstance(data.get("server"), dict):
        data["server"] = project_server(data["server"], user)
    if (
        "server_id" in (request.view_args or {})
        and not user.can("server.settings.read", request.view_args["server_id"])
        and (rule.endswith(("/logs", "/metrics", "/java-status", "/start", "/stop", "/restart"))
             or response.status_code >= 400)
    ):
        from backend.auth.redaction import redact, scrub_paths, _known_roots
        data = scrub_paths(redact(data), _known_roots())
    response.set_data(current_app.json.dumps(data))
    return response
