"""Preview-first, stopped-only modern forwarding for supported Paper servers."""
from __future__ import annotations

from collections.abc import MutableMapping
import hashlib
import io
import json
from pathlib import Path
import re

from ruamel.yaml import YAML

from backend.server import storage
from backend.velocity import service


def _yaml_update(path: Path, assignments: dict[tuple[str, ...], object]) -> str:
    if path.is_symlink():
        raise service.ConfigError(f"{path.name} must not be a symlink")
    parser = YAML()
    parser.preserve_quotes = True
    try:
        doc = parser.load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception as exc:
        raise service.ConfigError(f"Invalid {path.name}; correct its YAML before applying forwarding") from exc
    if doc is None:
        doc = {}
    if not isinstance(doc, MutableMapping):
        raise service.ConfigError(f"{path.name} must contain a YAML mapping")
    for keys, value in assignments.items():
        cursor = doc
        for key in keys[:-1]:
            if key not in cursor:
                cursor[key] = {}
            cursor = cursor[key]
            if not isinstance(cursor, MutableMapping):
                raise service.ConfigError(f"{path.name}: {'.'.join(keys[:-1])} must be a mapping")
        cursor[keys[-1]] = value
    out = io.StringIO()
    parser.dump(doc, out)
    return out.getvalue()


def _offline_properties(path: Path) -> str:
    if path.is_symlink():
        raise service.ConfigError("server.properties must not be a symlink")
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(r"(?m)^[ \t]*online-mode[ \t]*[=: \t][^\r\n]*$")
    if pattern.search(original):
        return pattern.sub("online-mode=false", original)
    return original + ("" if not original or original.endswith("\n") else "\n") + "online-mode=false\n"


def _selected(proxy: dict, data: dict) -> list[dict]:
    ids = data.get("serverIds")
    if not isinstance(ids, list) or not ids or not all(isinstance(sid, str) for sid in ids) or len(ids) != len(set(ids)):
        raise service.ConfigError("serverIds must be a non-empty list of unique configured backend IDs")
    allowed = {b["serverId"] for b in (proxy.get("velocityRouting") or {}).get("backends", [])}
    selected = []
    for sid in ids:
        target = storage.get_server(sid)
        if sid not in allowed or not target or service.is_velocity(target):
            raise service.ConfigError("Forwarding can only be applied to this proxy's configured backends")
        previous = target.get("forwardingProxyId")
        if previous not in {None, proxy["id"]} and storage.get_server(previous):
            raise service.ConfigError(f"{target.get('name', sid)} already uses another proxy's forwarding secret")
        selected.append(target)
    return selected


def _plan(proxy: dict, selected: list[dict]):
    config = service.get_config(proxy)
    if config["forwardingMode"] != "modern":
        raise service.ConfigError("Set the proxy forwardingMode to modern before configuring backends")
    secret = service._secret(proxy)
    changes, summaries = {}, []
    for backend in selected:
        loader = str(backend.get("loader", "")).lower()
        match = re.fullmatch(r"1\.(\d+)(?:\.(\d+))?", str(backend.get("version", "")))
        if loader not in service.PAPER_LOADERS or not match or (int(match[1]), int(match[2] or 0)) < (13, 2):
            raise service.ConfigError(
                f"{backend.get('name', backend['id'])}: modern forwarding is supported only for Paper/Folia/Purpur/Pufferfish Minecraft 1.13.2+ releases"
            )
        root = service._root(backend)
        if not (root / "server.properties").is_file() or backend.get("status") in {"pending", "failed"}:
            raise service.ConfigError("Install and initialize each backend before applying forwarding")
        modern = int(match[1]) >= 19
        paper_path = root / "config" / "paper-global.yml" if modern else root / "paper.yml"
        try:
            paper_path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise service.ConfigError("Backend configuration must not resolve outside its server directory") from exc
        prefix = ("proxies", "velocity") if modern else ("settings", "velocity-support")
        changes[paper_path] = _yaml_update(paper_path, {
            (*prefix, "enabled"): True,
            (*prefix, "online-mode"): config["onlineMode"],
            (*prefix, "secret"): secret,
        })
        changes[root / "server.properties"] = _offline_properties(root / "server.properties")
        changes[root / "spigot.yml"] = _yaml_update(root / "spigot.yml", {("settings", "bungeecord"): False})
        summaries.append({
            "serverId": backend["id"],
            "name": backend.get("name", backend["id"]),
            "files": [str(paper_path.relative_to(root)), "server.properties", "spigot.yml"],
            "changes": {
                "online-mode": False, "bungeecord": False, "velocity.enabled": True,
                "velocity.online-mode": config["onlineMode"], "velocity.secret": "(protected)",
            },
        })
    digest = hashlib.sha256()
    digest.update(json.dumps(config, sort_keys=True).encode())
    for path, content in sorted(changes.items(), key=lambda pair: str(pair[0])):
        digest.update(str(path).encode())
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
        digest.update(content.encode())
    return changes, {
        "proxyId": proxy["id"], "backends": summaries, "previewToken": digest.hexdigest(),
        "requiresOfflineModeConfirmation": True,
        "warnings": [
            "Applying explicitly changes backend server.properties online-mode to false; proxy authentication and the protected modern forwarding secret replace backend authentication.",
            "Restrict backend ports to the proxy (loopback or a firewall). Restart both the proxy and backends after applying.",
            "Backups are created next to changed files. They may contain credentials; retain them securely.",
        ],
    }


def configure(proxy: dict, data: dict, *, apply=False) -> dict:
    if not isinstance(data, dict):
        raise service.ConfigError("Request body must be an object")
    with service.topology_lock:
        proxy = storage.get_server(proxy["id"])
        if not proxy or not service.is_velocity(proxy):
            raise service.ConfigError("This server is not a Velocity proxy")
        selected = _selected(proxy, data)
        with service.stopped_servers([proxy, *selected]):
            changes, preview = _plan(proxy, selected)
            if not apply:
                return preview
            if data.get("confirmOfflineMode") is not True:
                raise service.ConfigError("Set confirmOfflineMode=true to explicitly authorize disabling backend online-mode")
            if data.get("previewToken") != preview["previewToken"]:
                raise service.ConfigError("A current previewToken is required; preview again after any configuration changes")
            updates = {backend["id"]: {"onlineMode": False, "forwardingProxyId": proxy["id"]}
                       for backend in selected}
            backups = service.write_files(changes, on_commit=lambda: storage.update_servers(updates))
            for backend in selected:
                service._registry().invalidate(backend["id"])
            return {**preview, "applied": True, "backups": backups}
