"""Audit fixed operation names, never user-controlled bodies, file names or values."""
from flask import g, request

from backend.events import store

_SERVER = {
    "create_server": "server.create", "import_server": "server.import",
    "update_server_file_content": "files.write", "upload_server_file": "files.upload",
    "delete_server_files": "files.delete", "create_server_folder": "files.create_directory",
    "delete_server_mod": "mods.delete", "bulk_delete_server_mods": "mods.delete",
    "toggle_server_mods": "mods.toggle", "set_server_icon": "server.icon",
    "delete_server_icon": "server.icon", "update_server_settings": "configs.update",
    "set_server_autostart": "configs.autostart", "delete_server_route": "server.delete",
    "install_server": "server.install", "upgrade_server": "server.upgrade",
    "start_server_by_id": "server.start", "stop_server_by_id": "server.stop",
    "restart_server_by_id": "server.restart",
    "install_java_endpoint": "java.install", "cancel_java_install": "java.cancel",
    "uninstall_installed_java": "java.delete",
}
_IGNORED = {"GET", "HEAD", "OPTIONS"}


def capture_actor():
    g.audit_actor = store.actor()


def record_response(response):
    if request.method in _IGNORED or not request.url_rule:
        return response
    # Validation/permission failures have not changed state. Never turn arbitrary
    # denied server IDs, URL paths or payloads into a readable audit side channel.
    if 400 <= response.status_code < 500:
        return response
    endpoint = request.endpoint or ""
    group, _, operation = endpoint.partition(".")
    action = _SERVER.get(operation) if group == "server" else None
    path = request.url_rule.rule
    sid = (request.view_args or {}).get("server_id")
    if group == "modrinth" and operation in {"install_mod", "install_modpack", "install_uploaded_modpack"}:
        action = "mods.install" if operation == "install_mod" else "modpacks.install"
        body = request.get_json(silent=True)
        sid = body.get("server_id") if isinstance(body, dict) else None
    elif group == "backups":
        if "backup-configs" in path and not path.endswith("/run"):
            action = "backups.configure"
        elif path.endswith("/restore"):
            action = "worlds.restore"
        elif path.endswith("/world-import"):
            action = "worlds.import"
        elif request.method == "DELETE":
            action = "backups.delete"
        elif path.endswith(("/run", "/backup-quick")):
            action = "backups.create"
    elif group == "players":
        if any(part in path for part in ("/ops", "/whitelist", "/bans")):
            action = "access." + ("ops" if "/ops" in path else "whitelist" if "/whitelist" in path else "bans")
    elif group == "schedules":
        action = "schedules.run" if path.endswith("/run") else "schedules.configure"
    elif group == "auth" and operation in {"setup", "change_password", "disable_password", "enable_password"}:
        action = "access." + operation.replace("_", ".")
    elif group == "integrations":
        action = "access.tokens" if "token" in path else "access.mcp"
    elif group == "events" and operation in {"create_destination", "update_destination", "delete_destination"}:
        action = "notifications.configure"
    elif group == "velocity" and request.method == "PUT":
        action = "configs.velocity"
    elif group == "velocity" and path.endswith("/apply"):
        action = "configs.forwarding"
    elif group == "dns" and not path.endswith("/plan"):
        action = "configs.dns"
    elif group == "maps" and "/view/" not in path and not path.endswith("/session"):
        action = "configs.map"
    elif group == "client_pack" and request.method != "GET":
        action = "clientpacks.configure"
    elif group == "modpack_updates":
        if operation == "collection" and request.method == "POST":
            action = "modpacks.update.prepare"
        elif operation == "detail" and request.method == "DELETE":
            action = "modpacks.update.delete"
        elif operation == "action":
            selected = (request.view_args or {}).get("action")
            if selected in {"stage", "validate", "promote"}:
                action = "modpacks.update." + selected
    elif group == "world_exports":
        action = {
            "build_export": "worlds.export", "create_share": "worlds.share.create",
            "revoke_share": "worlds.share.revoke", "delete_export": "worlds.export.delete",
        }.get(operation)
    elif group == "diagnostics":
        action = {"sampling": "diagnostics.configure", "capture": "diagnostics.capture"}.get(operation)
    # Instance creation returns the assigned ID; never copy any other fields.
    if not sid and group == "server" and operation in {"create_server", "import_server"} and response.is_json:
        data = response.get_json()
        sid = data.get("id") or (data.get("server") or {}).get("id")
    if action:
        outcome = "accepted" if response.status_code == 202 else "succeeded"
        data = response.get_json() if response.is_json else {}
        if response.status_code >= 500 or (isinstance(data, dict) and (data.get("success") is False or data.get("error"))):
            outcome = "failed"
        store.emit(action, server_id=sid, who=g.get("audit_actor"),
                   outcome=outcome)
    return response
