"""Version-scoped BlueMap setup and bounded loopback transport.

The viewer runs in an opaque sandbox. Its short-lived capability can read only
one server's map; it is not a panel session or a public sharing mechanism.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

import requests
from flask import current_app, g, request, session

from backend.auth import oidc
from backend.modrinth.client import ModrinthClient, ModrinthApiError
from backend.server import storage
from backend.server.registry import get_server_process_registry

LAYER = "fabricator:global-waystones"
BLUE_MAP = {
    "version_id": "Dr2hvJBc", "version": "5.7",
    "filename": "bluemap-5.7-fabric.jar",
    "url": "https://cdn.modrinth.com/data/swbUV1cr/versions/Dr2hvJBc/bluemap-5.7-fabric.jar",
    "sha512": "b7483d6ff90f09258f994a6f487846c1dfe814f1d0af931bd50426fd0afc98ccad033c5f440705edc08a8ced03cf216171ba1571db2221aca4ec620d676443a4",
}
VERSIONS = {
    "waystones": "21.1.37", "balm": "21.0.63", "bluemap": "5.7",
    "fabricator_global_waystones": "1.0.0",
}
MAX_BODY = 32 * 1024 * 1024
_transport_slots = threading.BoundedSemaphore(12)
_lock = threading.Lock()
_setup_lock = threading.Lock()
_tickets: dict[str, dict] = {}
TICKET_SECONDS = 300


class MapError(ValueError):
    pass


def root(server):
    try:
        path = get_server_process_registry().resolve_install_path(server)
    except ValueError:
        raise MapError("Server installation is unavailable.") from None
    if not path.is_dir() or path.is_symlink():
        raise MapError("Server installation is unavailable.")
    return path.resolve()


def child(base, relative):
    path = base / relative
    if any(p.is_symlink() for p in (path, *path.parents) if p != base.parent):
        raise MapError("Map configuration and mods must not use symbolic links.")
    if not path.resolve().is_relative_to(base):
        raise MapError("Invalid map configuration path.")
    return path


def manifest(server):
    base = root(server)
    path = child(base, ".fabricator-map.json")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("schema") != 1 or type(value.get("port")) is not int
                or not 8800 <= value["port"] <= 8999
                or not isinstance(value.get("nonce"), str)
                or not re.fullmatch(r"[a-f0-9]{64}", value["nonce"])
                or not isinstance(value.get("config_hashes"), dict)):
            raise ValueError
    except (ValueError, TypeError, OSError):
        raise MapError("Map configuration is invalid; review setup before enabling it.") from None
    for relative, expected in value.get("config_hashes", {}).items():
        if relative not in {"config/bluemap/core.conf", "config/bluemap/webserver.conf",
                            "config/fabricator-map.json"}:
            raise MapError("Map configuration is invalid.")
        file = child(base, relative)
        try:
            matches = file.is_file() and hashlib.sha256(file.read_bytes()).hexdigest() == expected
        except OSError:
            raise MapError("Managed map configuration cannot be read. Review its file permissions.") from None
        if not matches:
            raise MapError("Managed map configuration changed. Disable maps and review it before setup.")
    if len(value.get("config_hashes", {})) != 3:
        raise MapError("Map configuration is incomplete.")
    return value


def preview(server):
    compatible = (server.get("loader") == "fabric" and server.get("version") == "1.21.1"
                  and server.get("loaderVersion") == "0.18.4")
    found = {}
    mods = child(root(server), "mods")
    if mods.is_dir():
        for jar in sorted(mods.glob("*.jar")):
            child(root(server), "mods/" + jar.name)
            try:
                with zipfile.ZipFile(jar) as archive:
                    info = archive.getinfo("fabric.mod.json")
                    if info.file_size > 256 * 1024:
                        continue
                    meta = json.loads(archive.read(info))
                    if not isinstance(meta, dict):
                        continue
                    if meta.get("id") in VERSIONS or meta.get("id") == "fabric-api":
                        key = meta["id"]
                        if key in found:
                            raise MapError("Duplicate map dependency: " + key)
                        found[key] = str(meta.get("version", ""))
            except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError):
                continue
    dependencies = [
        {"id": key, "required": version, "installed": found.get(key),
         "compatible": found.get(key) == version}
        for key, version in VERSIONS.items()
    ]
    dependencies.append({"id": "fabric-api", "required": "1.21.1-compatible",
                         "installed": found.get("fabric-api"),
                         "compatible": bool(found.get("fabric-api", "").endswith("+1.21.1"))})
    return {
        "compatible": compatible,
        "target": "COBBLEVERSE 1.7.42 / Minecraft 1.21.1 / Fabric 0.18.4 / Java 21",
        "dependencies": dependencies,
        "ready_to_configure": compatible and all(d["compatible"] for d in dependencies),
        "bluemap_download": BLUE_MAP,
        "restart_required": True,
    }


def configure(server, consent):
    with _setup_lock:
        _configure(server, consent)


def _configure(server, consent):
    if consent is not True:
        raise MapError("Explicit BlueMap Minecraft resource-download consent is required.")
    if (server.get("status") != "stopped"
            or get_server_process_registry().get_status(server["id"]).get("status") != "stopped"):
        raise MapError("Stop the server before configuring maps. Fabricator will not restart it.")
    plan = preview(server)
    if not plan["ready_to_configure"]:
        raise MapError("Install the exact compatible dependencies shown in the preview first.")
    base = root(server)
    if manifest(server):
        raise MapError("Maps are already configured.")
    # Never overwrite an operator's HOCON. Configuration adoption needs review,
    # not a regex edit that could leave includes or duplicate keys effective.
    files = {
        "config/bluemap/core.conf": "accept-download: true\nrender-thread-count: 1\nmetrics: false\n",
        "config/bluemap/webserver.conf": "",
        "config/fabricator-map.json": "",
    }
    if any(child(base, name).exists() for name in files):
        raise MapError("Existing BlueMap configuration is preserved. Back it up and move core.conf, "
                       "webserver.conf and config/fabricator-map.json aside before managed setup.")
    used = {s.get("map", {}).get("port") for s in storage.get_all_servers()
            if isinstance(s.get("map"), dict)}
    port = None
    for candidate in range(8800, 9000):
        if candidate in used:
            continue
        try:
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", candidate))
        except OSError:
            continue
        port = candidate
        break
    if port is None:
        raise MapError("No managed map port is available.")
    nonce = secrets.token_hex(32)
    files["config/bluemap/webserver.conf"] = (
        f'enabled: true\nip: "127.0.0.1"\nport: {port}\nwebroot: "bluemap/web"\n'
    )
    files["config/fabricator-map.json"] = json.dumps({"nonce": nonce}) + "\n"
    created = []
    try:
        for name, content in files.items():
            path = child(base, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            created.append(path)
        data = {"schema": 1, "port": port, "nonce": nonce,
                "config_hashes": {name: hashlib.sha256(text.encode()).hexdigest()
                                  for name, text in files.items()}}
        path = child(base, ".fabricator-map.json")
        with path.open("x", encoding="utf-8") as handle:
            json.dump(data, handle)
        created.append(path)
        storage.update_server(server["id"], {"map": {"enabled": True, "port": port}})
    except (OSError, ValueError):
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise


def install_bluemap(server):
    plan = preview(server)
    if not plan["compatible"]:
        raise MapError("BlueMap setup supports Minecraft 1.21.1 on Fabric 0.18.4 only.")
    if (server.get("status") != "stopped"
            or get_server_process_registry().get_status(server["id"]).get("status") != "stopped"):
        raise MapError("Stop the server before installing BlueMap.")
    dependency = next(d for d in plan["dependencies"] if d["id"] == "bluemap")
    if dependency["installed"]:
        raise MapError("BlueMap is already installed. Review its version in Mods rather than replacing it.")
    mods = child(root(server), "mods")
    if child(root(server), "mods/" + BLUE_MAP["filename"]).exists():
        raise MapError("The BlueMap target file already exists. Review it in Mods before installing.")
    try:
        ModrinthClient().download_mod(BLUE_MAP["url"], mods, hashes={"sha512": BLUE_MAP["sha512"]})
    except ModrinthApiError:
        current_app.logger.exception("Pinned BlueMap installation failed")
        raise MapError("BlueMap download or integrity verification failed; no replacement was installed.") from None


def upstream(config, path):
    """Fixed managed loopback endpoint, without proxies, cookies or redirects."""
    if not _transport_slots.acquire(blocking=False):
        raise MapError("Map request limit reached; retry shortly.")
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.get(f'http://127.0.0.1:{config["port"]}/{quote(path, safe="/.-_")}',
                             timeout=(2, 8), allow_redirects=False, stream=True,
                             headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise MapError("BlueMap resource is unavailable.")
                data = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    data.extend(chunk)
                    if len(data) > MAX_BODY:
                        raise MapError("BlueMap resource exceeds the size limit.")
                return bytes(data)
    except requests.RequestException:
        raise MapError("BlueMap is not responding. Check the server and BlueMap logs.") from None
    finally:
        _transport_slots.release()


def health(server, config):
    runtime = get_server_process_registry().get_status(server["id"])
    if server.get("status") != "running" or runtime.get("status") != "running":
        raise MapError("Start the server after setup to load BlueMap and the global-waystones adapter.")
    try:
        data = json.loads(upstream(config, "fabricator-status.json"))
        if (not isinstance(data, dict) or data.get("nonce") != config["nonce"] or data.get("schema") != 1
                or type(data.get("pid")) is not int or data["pid"] != runtime.get("pid")
                or type(data.get("global_waystones")) is not int or data["global_waystones"] < 0
                or not isinstance(data.get("global_markers"), dict)
                or any(not isinstance(ids, list) or any(not isinstance(id, str) for id in ids)
                       for ids in data["global_markers"].values())
                or not isinstance(data.get("updated"), (int, float))
                or not 0 <= time.time() - data["updated"] <= 20):
            raise ValueError
        return {"global_waystones": data["global_waystones"], "global_markers": data["global_markers"]}
    except (ValueError, TypeError):
        raise MapError("Global-waystones adapter is not ready or its heartbeat is stale.") from None


def issue_ticket(server):
    now = time.time()
    principal = getattr(g, "principal", None)
    origin = request.headers.get("Origin") or request.host_url.rstrip("/")
    if origin not in {request.host_url.rstrip("/"), *current_app.config["CORS_ORIGINS"]}:
        raise MapError("Map browser origin is not allowed. Configure CORS_ORIGINS for the panel URL.")
    ticket = secrets.token_urlsafe(32)
    browser = (session.setdefault("map_browser", secrets.token_urlsafe(24))
               if current_app.config.get("FABRICATOR_AUTH_ENABLED") else None)
    with _lock:
        for key in list(_tickets):
            if _tickets[key]["expires"] <= now:
                del _tickets[key]
        if len(_tickets) >= 1024:
            raise MapError("Too many active map sessions; retry in five minutes.")
        _tickets[ticket] = {
            "server": server["id"], "expires": now + TICKET_SECONDS,
            "owner": principal.owner if principal else None,
            "auth_enabled": current_app.config.get("FABRICATOR_AUTH_ENABLED"),
            "app": current_app._get_current_object(), "browser": browser,
            "origin": origin,
        }
    return ticket


def ticket_origin(ticket):
    with _lock:
        entry = _tickets.get(ticket)
        if not entry:
            raise MapError("Map session expired. Reload the Map tab.")
        return entry["origin"]


def ticket_allowed(server_id, ticket):
    with _lock:
        entry = _tickets.get(ticket)
    if not entry or entry["expires"] <= time.time() or entry["server"] != server_id:
        return False
    if entry["app"] is not current_app._get_current_object() or current_app.config.get("FABRICATOR_NEEDS_SETUP"):
        return False
    if entry["auth_enabled"] != current_app.config.get("FABRICATOR_AUTH_ENABLED"):
        return False
    owner = entry["owner"]
    provider = oidc.provider()
    if owner:
        user = provider.users.get(owner["sub"]) if provider and provider.issuer == owner["issuer"] else None
        return bool(user and user.can("server.view", server_id) and user.can("map.read", server_id))
    return provider is None


def revoke_browser_tickets():
    browser = session.get("map_browser")
    if browser:
        with _lock:
            for key in list(_tickets):
                if _tickets[key]["browser"] == browser:
                    del _tickets[key]


def safe_path(path):
    if not path:
        return "index.html"
    if (len(path) > 1024 or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
            or any(p in {"", ".", ".."} for p in path.split("/"))):
        raise MapError("Invalid map resource path.")
    if path in {"settings.json", "index.html", "fabricator-sandbox.js"}:
        return path
    if path.startswith(("assets/", "lang/", "maps/")) and path.endswith(
            (".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ttf", ".woff", ".woff2",
             ".json", ".prbm", ".prbm.gz", ".conf", ".glsl")):
        return path
    raise MapError("Map resource is not allowed.")


def filter_json(path, raw, global_markers):
    data = json.loads(raw)
    if path == "settings.json":
        # Viewer resources stay within the capability path, including polling.
        data.update(mapDataRoot="maps", liveDataRoot="maps", scripts=[], styles=[], useCookies=False)
    elif path.endswith("/markers.json"):
        # Never pass another mod's death/home/private marker sets through.
        if not isinstance(data, dict):
            raise ValueError("Invalid marker sets")
        if LAYER in data:
            layer = data[LAYER]
            if not isinstance(layer, dict) or not isinstance(layer.get("markers"), dict):
                raise ValueError("Invalid global markers")
            allowed = set(global_markers.get(path.split("/")[1], []))
            # BlueMap's asynchronous file writer may still have the previous
            # snapshot after a restart or visibility change. Fail closed now.
            layer["markers"] = {id: marker for id, marker in layer["markers"].items() if id in allowed}
            data = {LAYER: layer}
        else:
            data = {}
    return json.dumps(data).encode()
