"""Lossless Velocity configuration; secrets never enter JSON records or responses."""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import threading
import uuid

import tomlkit

from backend.server import storage
from backend.server.locks import try_acquire


topology_lock = threading.RLock()
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
PAPER_LOADERS = {"paper", "folia", "purpur", "pufferfish"}


class ConfigError(ValueError):
    pass


class BusyError(ConfigError):
    pass


def with_topology_lock(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with topology_lock:
            return func(*args, **kwargs)
    return wrapped


def is_velocity(server: dict) -> bool:
    return str(server.get("loader", "")).strip().lower() == "velocity"


def capabilities(server: dict) -> dict:
    proxy = is_velocity(server)
    return {
        "kind": "proxy" if proxy else "server",
        "capabilities": {
            "proxy": proxy, "velocityConfig": proxy,
            "serverProperties": not proxy, "worlds": not proxy,
            "players": not proxy, "tps": not proxy, "eula": not proxy,
            "contentKind": "plugin" if proxy else None,
        },
    }


def _registry():
    from backend.server.registry import get_server_process_registry
    return get_server_process_registry()


def _root(server: dict) -> Path:
    return _registry().resolve_install_path(server)


def _config_path(server: dict) -> Path:
    path = _root(server) / "velocity.toml"
    if path.is_symlink():
        raise ConfigError("velocity.toml must not be a symlink")
    return path


def _secret_path(server: dict) -> Path:
    sid = str(server.get("id", ""))
    if not re.fullmatch(r"srv_[a-zA-Z0-9_-]+", sid):
        raise ConfigError("Invalid proxy ID")
    return Path(os.fspath(storage.SERVERS_FILE)).resolve().parent / "velocity-secrets" / (sid + ".secret")


def _secret(server: dict, *, create=False) -> str:
    path = _secret_path(server)
    if path.is_symlink() or path.parent.is_symlink():
        raise ConfigError("Forwarding secret must not be a symlink")
    if not path.exists():
        if not create:
            raise ConfigError("Forwarding secret is missing; restore it before configuring forwarding")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            with path.open("x", encoding="utf-8") as handle:
                os.chmod(path, 0o600)
                handle.write(secrets.token_hex(32) + "\n")
        except FileExistsError:
            pass
    os.chmod(path, 0o600)
    value = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ConfigError("Forwarding secret is invalid; restore the original secret")
    return value


def delete_secret(server: dict) -> None:
    if is_velocity(server):
        _secret_path(server).unlink(missing_ok=True)


def protected_file(server: dict, path: Path) -> bool:
    """Prevent raw-file APIs bypassing structured routing or leaking YAML secrets."""
    name = path.name.lower()
    if is_velocity(server) and (name == "velocity.toml" or name.startswith("velocity.toml.fabricator-backup-")):
        return True
    if server.get("forwardingProxyId"):
        return any(name == base or name.startswith(base + ".fabricator-backup-")
                   for base in ("paper-global.yml", "paper.yml", "spigot.yml", "server.properties"))
    if any(name == base or name.startswith(base + ".fabricator-backup-")
           for base in ("paper-global.yml", "paper.yml")) and path.is_file():
        try:
            if re.search(r"(?m)^\s*secret\s*:", path.read_text(encoding="utf-8")):
                return True
        except (OSError, UnicodeError):
            return True
    return name.endswith(".secret")


def _load(server: dict):
    path = _config_path(server)
    if not path.exists():
        return tomlkit.document()
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        for key in ("servers", "forced-hosts"):
            if key in doc and not hasattr(doc[key], "items"):
                raise ConfigError(f"velocity.toml [{key}] must be a table")
        if "servers" in doc and not isinstance(doc["servers"].get("try", []), list):
            raise ConfigError("velocity.toml servers.try must be a list")
        if any(not isinstance(value, list) for value in doc.get("forced-hosts", {}).values()):
            raise ConfigError("velocity.toml forced-hosts values must be lists")
        return doc
    except Exception as exc:
        raise ConfigError("Invalid velocity.toml; correct its TOML syntax before continuing") from exc


def _port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ConfigError("Listen port must be an integer between 1 and 65535")
    return value


def _record_port(server: dict) -> int:
    value = server.get("port", 25565)
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    return _port(value)


def _bind(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def _listen(value: str) -> dict:
    try:
        host, port = str(value).rsplit(":", 1)
        return {"host": host.strip("[]"), "port": _port(int(port))}
    except (ValueError, TypeError) as exc:
        raise ConfigError("Invalid Velocity bind address") from exc


def _address(server: dict) -> str:
    host = server.get("serverIp") or "127.0.0.1"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError("Backend serverIp must be an IP address") from exc
    if ip.is_unspecified:
        host = "::1" if ip.version == 6 else "127.0.0.1"
    return _bind(str(host), _record_port(server))


def get_config(server: dict) -> dict:
    """Public DNS/UI interface. Does not initialize files, expose or rotate secrets."""
    if not is_velocity(server):
        raise ConfigError("This server is not a Velocity proxy")
    doc = _load(server)
    names = {b["name"]: b["serverId"] for b in (server.get("velocityRouting") or {}).get("backends", [])}
    endpoints = doc.get("servers", {})
    backends = [
        {"name": name, "serverId": names.get(name), "address": str(address)}
        for name, address in endpoints.items() if name != "try"
    ]
    return {
        "listen": _listen(doc.get("bind", _bind("0.0.0.0", server.get("port", 25565)))),
        "onlineMode": bool(doc.get("online-mode", True)),
        "forwardingMode": str(doc.get("player-info-forwarding-mode", "modern")),
        "backends": backends,
        "try": list(endpoints.get("try", [])),
        "forcedHosts": {str(host): list(targets) for host, targets in doc.get("forced-hosts", {}).items()},
        "forwardingSecretConfigured": _secret_path(server).is_file(),
    }


def _validate(server: dict, data: dict) -> dict:
    if not isinstance(data, dict):
        raise ConfigError("Configuration must be an object")
    allowed = {"listen", "onlineMode", "forwardingMode", "backends", "try", "forcedHosts", "forwardingSecretConfigured"}
    if set(data) - allowed:
        raise ConfigError("Unknown configuration fields: " + ", ".join(sorted(set(data) - allowed)))
    listen = data.get("listen")
    if not isinstance(listen, dict) or set(listen) != {"host", "port"}:
        raise ConfigError("listen must contain host and port")
    try:
        host = str(ipaddress.ip_address(listen["host"]))
    except (ValueError, TypeError) as exc:
        raise ConfigError("Listen host must be an IP address") from exc
    port = _port(listen["port"])
    for existing in storage.get_all_servers():
        if existing["id"] == server["id"]:
            continue
        try:
            existing_port = _record_port(existing)
        except ConfigError:
            continue
        if existing_port == port:
            raise ConfigError(f"Port {port} is already in use by another server")
    if not isinstance(data.get("onlineMode"), bool):
        raise ConfigError("onlineMode must be a boolean")
    mode = data.get("forwardingMode")
    if mode not in {"modern", "none"}:
        raise ConfigError("Only modern or none forwarding is supported; legacy and BungeeGuard configurations require manual migration")
    backends = data.get("backends")
    if not isinstance(backends, list):
        raise ConfigError("backends must be a list of name/serverId references")
    normalized, names, ids = [], set(), set()
    for backend in backends:
        if not isinstance(backend, dict) or set(backend) - {"name", "serverId", "address"}:
            raise ConfigError("Each backend must contain name and serverId")
        name, sid = backend.get("name"), backend.get("serverId")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name == "try" or name in names:
            raise ConfigError("Backend names must be unique alphanumeric names (hyphens/underscores allowed), not 'try'")
        if not isinstance(sid, str) or sid in ids:
            raise ConfigError("Each backend must reference a unique existing serverId")
        target = storage.get_server(sid)
        if not target or target["id"] == server["id"] or is_velocity(target):
            raise ConfigError(f"Backend {name} must reference an existing game server, not a proxy")
        normalized.append({"name": name, "serverId": sid, "address": _address(target)})
        names.add(name)
        ids.add(sid)

    def targets(value, field):
        if not isinstance(value, list) or not all(isinstance(n, str) and n in names for n in value) or len(value) != len(set(value)):
            raise ConfigError(f"{field} must be a list of unique configured backend names")
        return list(value)

    fallback = targets(data.get("try"), "try")
    if names and not fallback:
        raise ConfigError("Configure at least one default/fallback backend in try")
    forced = data.get("forcedHosts")
    if not isinstance(forced, dict):
        raise ConfigError("forcedHosts must be an object mapping hostnames to backend name lists")
    clean_hosts = {}
    for raw, value in forced.items():
        if not isinstance(raw, str):
            raise ConfigError("Forced hostnames must be strings")
        try:
            hostname = raw.rstrip(".").encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ConfigError("Invalid forced hostname") from exc
        if not _HOST.fullmatch(hostname) or "." not in hostname or hostname in clean_hosts:
            raise ConfigError("Forced hosts must be unique fully qualified hostnames")
        clean_hosts[hostname] = targets(value, f"forcedHosts.{hostname}")
        if not clean_hosts[hostname]:
            raise ConfigError("Forced host targets cannot be empty")
    return {"listen": {"host": host, "port": port}, "onlineMode": data["onlineMode"],
            "forwardingMode": mode, "backends": normalized, "try": fallback, "forcedHosts": clean_hosts}


def _render(server: dict, config: dict, doc):
    doc.setdefault("config-version", "2.7")
    doc["bind"] = _bind(**config["listen"])
    doc["online-mode"] = config["onlineMode"]
    doc["player-info-forwarding-mode"] = config["forwardingMode"]
    doc["forwarding-secret-file"] = str(_secret_path(server))
    endpoints = doc.setdefault("servers", tomlkit.table())
    keep = {b["name"] for b in config["backends"]} | {"try"}
    for name in list(endpoints):
        if name not in keep:
            del endpoints[name]
    for backend in config["backends"]:
        endpoints[backend["name"]] = backend["address"]
    endpoints["try"] = config["try"]
    forced = doc.setdefault("forced-hosts", tomlkit.table())
    for name in list(forced):
        if name not in config["forcedHosts"]:
            del forced[name]
    for name, targets in config["forcedHosts"].items():
        forced[name] = targets
    return tomlkit.dumps(doc)


def write_files(changes: dict[Path, str], *, backup=True, on_commit=None) -> list[str]:
    """Stage writes alongside originals; roll back the entire set on failure."""
    previous, staged, backups = {}, {}, []
    try:
        for path, content in changes.items():
            if path.is_symlink():
                raise ConfigError(f"Refusing to overwrite symlink: {path.name}")
            path.parent.mkdir(parents=True, exist_ok=True)
            previous[path] = path.read_bytes() if path.exists() else None
            if backup and path.exists():
                destination = path.with_name(path.name + ".fabricator-backup-" + uuid.uuid4().hex)
                shutil.copy2(path, destination)
                os.chmod(destination, 0o600)
                backups.append(str(destination.name))
            stage = path.with_name(path.name + ".fabricator-write-" + uuid.uuid4().hex)
            staged[path] = stage
            with stage.open("x", encoding="utf-8") as handle:
                os.chmod(stage, 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        replaced = []
        try:
            for path, stage in staged.items():
                os.replace(stage, path)
                replaced.append(path)
            if on_commit:
                on_commit()
        except Exception:
            for path in reversed(replaced):
                if previous[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous[path])
            raise
        return backups
    finally:
        for stage in staged.values():
            stage.unlink(missing_ok=True)


@contextmanager
def stopped_servers(servers):
    acquired = []
    try:
        for server in sorted(servers, key=lambda s: s["id"]):
            lock = try_acquire(server["id"])
            if lock is None:
                raise BusyError("Another operation is in progress; stop all affected servers first")
            acquired.append(lock)
            status = _registry().get_status(server["id"])
            if status.get("status") in {"running", "stopping"} or status.get("stopping") or server.get("status") in {"running", "stopping", "installing"}:
                raise BusyError("Stop the proxy and all affected backends before changing configuration")
            from backend.server import install_progress
            if install_progress.is_active(server["id"]):
                raise BusyError("Wait for installation to finish before changing configuration")
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def initialize(server: dict, *, install_path=None) -> None:
    if not is_velocity(server):
        raise ConfigError("This server is not a Velocity proxy")
    root = Path(install_path) if install_path else _root(server)
    if (root / "velocity.toml").exists():
        _secret(server)
        doc = _load(server)
        if doc.get("forwarding-secret-file") != str(_secret_path(server)):
            raise ConfigError("Velocity uses an unmanaged forwarding-secret-file; migrate it explicitly before continuing")
        return
    _secret(server, create=True)
    default = {"listen": {"host": "0.0.0.0", "port": server.get("port", 25565)},
               "onlineMode": True, "forwardingMode": "modern", "backends": [], "try": [], "forcedHosts": {}}
    write_files({root / "velocity.toml": _render(server, default, tomlkit.document())}, backup=False)


def put_config(server: dict, data: dict) -> dict:
    with topology_lock, stopped_servers([server]):
        server = storage.get_server(server["id"])
        config = _validate(server, data)
        initialize(server)
        doc = _load(server)
        old_mode = str(doc.get("player-info-forwarding-mode", "modern"))
        if old_mode not in {"modern", "none"}:
            raise ConfigError("Existing legacy/BungeeGuard forwarding requires manual migration")
        updated = {}
        def persist():
            updated.update(storage.update_servers({
                server["id"]: {"port": config["listen"]["port"], "velocityRouting": config},
            })[server["id"]])
        write_files({_config_path(server): _render(server, config, doc)}, on_commit=persist)
        _registry().invalidate(server["id"])
        return get_config(updated)


def refresh_addresses(server: dict) -> None:
    """Resolve referenced backend ports immediately before starting the proxy."""
    routing = server.get("velocityRouting")
    if not routing:
        return
    with topology_lock:
        config = _validate(server, routing)
        doc = _load(server)
        rendered = _render(server, config, doc)
        if rendered != _config_path(server).read_text(encoding="utf-8"):
            write_files({_config_path(server): rendered}, on_commit=lambda: storage.update_servers({
                server["id"]: {"velocityRouting": config},
            }))


def referencing_proxies(server_id: str, servers=None) -> list[str]:
    return [s["id"] for s in (servers if servers is not None else storage.get_all_servers())
            if is_velocity(s) and any(b.get("serverId") == server_id
                                     for b in (s.get("velocityRouting") or {}).get("backends", []))]
