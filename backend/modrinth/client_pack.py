"""Client pack planning and immutable exports, separate from server content.

Only public Modrinth file references and explicitly selected, narrowly allowlisted
client settings enter an export. No jars are copied or installed by this module.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from backend.modrinth import installed, mrpack
from backend.modrinth.client import ModrinthApiError
from backend.server import storage

LOADERS = {value: key for key, value in mrpack.LOADER_DEPENDENCY_KEYS.items()}
SAFE_SETTINGS = frozenset({
    "options.txt",
    "config/sodium-options.json",
    "config/iris.properties",
    "config/reeses-sodium-options.json",
    "config/modmenu.json",
    "config/emi.css",
    "config/emi.json",
})
MAX_SETTING_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_FILES = 2000
ARTIFACT_TTL = 24 * 3600
SHARE_PAGE = "/client-pack-share"
MAX_ARTIFACTS = 5
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
CLIENT_TAGS = {
    "client_only", "client_only_server_optional", "client_and_server",
    "client_or_server", "client_or_server_prefers_both", "server_only_client_optional",
}
SERVER_TAGS = {"server_only", "dedicated_server_only", "singleplayer_only"}


class PackError(ValueError):
    pass


class _ArchiveWriter(io.BufferedWriter):
    def write(self, data):
        if self.tell() + len(data) > MAX_ARTIFACT_BYTES:
            raise PackError("Client archive exceeds the 16 MiB storage limit; reduce selected settings or files")
        return super().write(data)


def _key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _root():
    return storage._resolve_servers_file().parent / "client-packs"


def _directory(server):
    return _root() / _key(server["id"])


def _source_path(install_path):
    return _root() / "sources" / (_key(Path(install_path).resolve()) + ".json")


def _read(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relative_path(value):
    if not isinstance(value, str) or not value or len(value) > 240:
        raise PackError("Invalid pack path")
    path = PurePosixPath(value)
    if (path.is_absolute() or "\\" in value or ":" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise PackError(f"Unsafe pack path: {value!r}")
    return value


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _read_handle(handle, relative, limit, hash_only):
    before = os.fstat(handle.fileno())
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise PackError(f"Not a regular file within the size limit: {relative}")
    sha1, sha512 = hashlib.sha1(), hashlib.sha512()
    chunks, length = [], 0
    while chunk := handle.read(min(1024 * 1024, limit + 1 - length)):
        length += len(chunk)
        if hash_only:
            sha1.update(chunk)
            sha512.update(chunk)
        else:
            chunks.append(chunk)
        if length > limit:
            break
    after = os.fstat(handle.fileno())
    if length > limit or length != before.st_size or _signature(before) != _signature(after):
        raise PackError(f"File changed while reading: {relative}")
    if hash_only:
        return {"sha1": sha1.hexdigest(), "sha512": sha512.hexdigest(), "size": length}
    return b"".join(chunks)


def _safe_read(root, relative, limit=MAX_SETTING_BYTES, *, hash_only=False):
    """Open through directory fds, refusing symlinks in every component."""
    relative_path(relative)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        # Windows does not support directory-relative opens. Compare the
        # opened file identity to lstat snapshots and reject reparse points.
        paths = [Path(root)]
        for part in relative.split("/"):
            paths.append(paths[-1] / part)
        snapshots = [path.lstat() for path in paths]
        if any(stat.S_ISLNK(item.st_mode) or getattr(item, "st_file_attributes", 0) & 0x400
               for item in snapshots):
            raise PackError(f"Symlinks and junctions cannot be exported: {relative}")
        fd = os.open(paths[-1], os.O_RDONLY | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            expected = snapshots[-1]
            if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
                raise PackError(f"File changed or is not a regular file: {relative}")
            result = _read_handle(handle, relative, limit, hash_only)
        if any(_signature(path.lstat()) != _signature(snapshot) for path, snapshot in zip(paths, snapshots)):
            raise PackError(f"File path changed while reading: {relative}")
        return result
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=descriptor)
        with os.fdopen(fd, "rb") as handle:
            return _read_handle(handle, relative, limit, hash_only)
    finally:
        os.close(descriptor)


def _validate_setting(path, data):
    text = data.decode("utf-8")
    if ("\x00" in text or "PRIVATE KEY-----" in text
            or re.search(r"""(?i)\b(?:password|passwd|secret|token|credential|access[_-]?key|lastServer|username|uuid|ip[_-]?address)["']?\s*[:=]""", text)):
        raise PackError(f"Client setting {path} contains sensitive or personal-data fields; remove them before export")


def prepare_source(archive):
    """Validate/bound client metadata before a server install changes files.

    Unknown override paths are recorded as review items, never retained as bytes.
    Only the small client-settings allowlist can be selected for redistribution.
    """
    index = mrpack.read_index(archive)
    if len(index.get("files") or []) > MAX_FILES:
        raise PackError("Pack contains too many files for client metadata")
    for entry in index.get("files") or []:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("hashes", {}), dict)
                or not isinstance(entry.get("env", {}), dict)
                or any(not isinstance(value, str) or value not in {"required", "optional", "unsupported"}
                       for value in entry.get("env", {}).values())):
            raise PackError("Imported client manifest has malformed file metadata")
    settings, unsupported = {}, []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        if zf.namelist().count(mrpack.INDEX_NAME) != 1:
            raise PackError("Archive must contain exactly one modrinth.index.json")
        for prefix in ("overrides/", "client-overrides/"):
            seen = set()
            for member in zf.infolist():
                if not member.filename.startswith(prefix) or member.is_dir():
                    continue
                relative = member.filename[len(prefix):]
                if relative in seen:
                    raise PackError(f"Duplicate override: {member.filename}")
                seen.add(relative)
                try:
                    relative_path(relative)
                except PackError:
                    unsupported.append(member.filename)
                    continue
                mode = member.external_attr >> 16
                if relative not in SAFE_SETTINGS or stat.S_ISLNK(mode):
                    unsupported.append(member.filename)
                    continue
                if member.file_size > MAX_SETTING_BYTES:
                    raise PackError(f"Client override is too large: {relative}")
                data = zf.read(member)
                try:
                    data.decode("utf-8")
                except UnicodeError:
                    unsupported.append(member.filename)
                    continue
                total += len(data)
                if total > MAX_SOURCE_BYTES:
                    raise PackError("Client override metadata exceeds storage limit")
                settings[relative] = base64.b64encode(data).decode("ascii")
    value = {
        "index": index, "settings": settings, "unsupported_overrides": unsupported,
        "captured_at": time.time(),
    }
    if len(json.dumps(value).encode()) > MAX_SOURCE_BYTES:
        raise PackError("Client manifest metadata exceeds storage limit")
    return value


def store_source(value, install_path, server_files=None, side_overrides=None):
    manifest_paths = {entry["path"] for entry in value["index"].get("files") or []}
    value = {
        **value,
        "server_files": [path for path in server_files if path in manifest_paths] if server_files is not None else None,
        "side_overrides": {path: side for path, side in (side_overrides or {}).items() if path in manifest_paths},
    }
    if len(json.dumps(value).encode()) > MAX_SOURCE_BYTES:
        raise PackError("Client manifest metadata exceeds storage limit")
    _atomic_json(_source_path(install_path), value)
    return value


def capture_source(archive, install_path, server_files=None, side_overrides=None):
    return store_source(prepare_source(archive), install_path, server_files, side_overrides)


def source(install_path):
    return _read(_source_path(install_path), {})


def configuration(server):
    return _read(_directory(server) / "config.json", {
        "revision": "", "name": f'{server.get("name", "Minecraft")} clients',
        "loader_version": "", "selections": [], "overrides": [],
    })


def save_configuration(server, raw):
    if not isinstance(raw, dict):
        raise PackError("Configuration must be an object")
    previous = configuration(server)
    if raw.get("revision") != previous["revision"]:
        raise PackError("Client pack was edited elsewhere; reload before saving")
    name = raw.get("name", "")
    loader_version = raw.get("loader_version", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise PackError("Pack name must contain 1-120 characters")
    if not isinstance(loader_version, str) or not re.fullmatch(r"[A-Za-z0-9._+-]{0,80}", loader_version):
        raise PackError("Invalid loader version")
    selections = raw.get("selections", [])
    overrides = raw.get("overrides", [])
    if not isinstance(selections, list) or len(selections) > MAX_FILES:
        raise PackError("Invalid selections list")
    seen = set()
    cleaned = []
    for selection in selections:
        if not isinstance(selection, dict):
            raise PackError("Each selection must be an object")
        key, requirement = selection.get("key"), selection.get("requirement")
        if (not isinstance(key, str) or not key or len(key) > 260 or key in seen
                or requirement not in {"required", "optional", "excluded"}):
            raise PackError("Selections need unique keys and required/optional/excluded intent")
        version = selection.get("version_id", "")
        if not isinstance(version, str) or (version and not re.fullmatch(r"[A-Za-z0-9]{1,80}", version)):
            raise PackError("Invalid Modrinth version ID")
        seen.add(key)
        cleaned.append({"key": key, "requirement": requirement, "version_id": version})
    if not isinstance(overrides, list) or len(overrides) > len(SAFE_SETTINGS) * 2:
        raise PackError("Invalid overrides list")
    for value in overrides:
        if not isinstance(value, str) or value not in {
            f"{origin}:{path}" for origin in ("server", "imported") for path in SAFE_SETTINGS
        }:
            raise PackError("Only explicitly allowlisted client settings can be exported")
    if len(overrides) != len(set(overrides)):
        raise PackError("Duplicate override selection")
    paths = [value.split(":", 1)[1] for value in overrides]
    if len(paths) != len(set(paths)):
        raise PackError("Choose only one source for each client override")
    result = {
        "revision": uuid.uuid4().hex, "name": name.strip(),
        "loader_version": loader_version, "selections": cleaned, "overrides": overrides,
    }
    _atomic_json(_directory(server) / "config.json", result)
    return result


def _client_support(project):
    raw = project.get("environment") or []
    tags = {raw} if isinstance(raw, str) else set(raw)
    positive, negative = bool(tags & CLIENT_TAGS), bool(tags & SERVER_TAGS)
    if positive and negative:
        return "unknown"
    if positive:
        return "supported"
    if negative:
        return "unsupported"
    legacy = project.get("client_side")
    if legacy in {"required", "optional"}:
        return "supported"
    return "unsupported" if legacy == "unsupported" else "unknown"


def _download_file(file, path, requirement):
    relative_path(path)
    hashes = file.get("hashes") or {}
    if (not re.fullmatch(r"[a-fA-F0-9]{40}", str(hashes.get("sha1", "")))
            or not re.fullmatch(r"[a-fA-F0-9]{128}", str(hashes.get("sha512", "")))):
        raise PackError(f"Missing SHA-1/SHA-512 for {path}")
    size = file.get("size", file.get("fileSize"))
    if type(size) is not int or size < 0:
        raise PackError(f"Missing file size for {path}")
    urls = file.get("downloads") or [file.get("url")]
    for url in urls:
        if not isinstance(url, str) or not url.isascii() or re.search(r'[\s<>"{}|\\^`]', url):
            raise PackError(f"Invalid download URL for {path}")
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {
            "cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com",
        } or parsed.username or parsed.password or parsed.port not in (None, 443)
                or parsed.fragment):
            raise PackError(f"Download provider is not allowlisted for {path}")
    return {
        "path": path, "hashes": {key: hashes[key].lower() for key in ("sha1", "sha512")},
        "env": {"client": requirement, "server": "unsupported"},
        "downloads": urls, "fileSize": size,
    }


def _inventory(install_path):
    folder = Path(install_path) / "mods"
    if folder.is_symlink():
        raise PackError("The server mods directory must not be a symlink")
    records = {}
    if not folder.exists():
        return records
    for path in sorted(folder.iterdir()):
        if not path.name.endswith(".jar"):
            continue
        if len(records) >= MAX_FILES:
            raise PackError("Too many installed mods")
        records[f"mods/{path.name}"] = _safe_read(
            install_path, f"mods/{path.name}", 512 * 1024 * 1024, hash_only=True,
        )
    return records


def _sha1(entry):
    return str((entry.get("hashes") or {}).get("sha1") or "").lower()


def plan(server, install_path, client):
    """Build a reviewable, fully pinned plan. Errors forbid archive generation."""
    config = configuration(server)
    imported = source(install_path)
    errors, warnings, rows = [], [], []
    selections = {entry["key"]: entry for entry in config["selections"]}
    inventory = _inventory(install_path)
    manifest = (imported.get("index") or {}).get("files") or []
    hashes = [entry["sha1"] for entry in inventory.values()]
    hashes += [_sha1(entry) for entry in manifest if isinstance(entry, dict)]
    versions = installed.resolve_versions(client, hashes)
    projects = installed.resolve_projects(client, [
        version["project_id"] for version in versions.values() if version.get("project_id")
    ])
    declared = {}
    for entry in manifest:
        if isinstance(entry, dict):
            digest = _sha1(entry)
            identity = versions.get(digest) or {}
            if identity.get("project_id"):
                declared[identity["project_id"]] = entry
    selected, paths, excluded_projects = {}, {}, set()
    version_cache = {}
    missing_versions = set()
    project_cache = dict(projects)

    def get_version(version_id):
        if version_id not in version_cache:
            version_cache[version_id] = installed.version_details(client, version_id)
        return version_cache[version_id]

    def get_project(project_id):
        if project_id not in project_cache:
            resolved = installed.resolve_projects(client, [project_id])
            if project_id not in resolved:
                errors.append(f"Modrinth project metadata is unavailable for {project_id}; retry or remove the selection")
            project_cache[project_id] = resolved.get(project_id, {})
        return project_cache[project_id]

    loader = str(server.get("loader", "")).lower()
    minecraft = server.get("version", "")
    loader_version = server.get("loaderVersion") or config["loader_version"]
    if loader not in LOADERS:
        errors.append("Client packs require Fabric, Quilt, Forge or NeoForge; vanilla/plugin/proxy servers are unsupported.")
    if not minecraft or not loader_version:
        errors.append("Exact Minecraft and loader versions are required. Enter the installed loader version; latest is never assumed.")
    if loader_version and not re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", str(loader_version)):
        errors.append("Installed loader version is invalid; correct the server metadata.")
    if server.get("loaderVersion") and config["loader_version"] and config["loader_version"] != server["loaderVersion"]:
        errors.append("Configured loader version differs from the installed server loader.")

    def add(version, key, origin, requirement="required", path=None, digest=None):
        project_id = version.get("project_id")
        project = get_project(project_id)
        support = _client_support(project)
        explicit = selections.get(key)
        if explicit:
            requirement = explicit["requirement"]
        row = {
            "key": key, "origin": origin, "project_id": project_id,
            "version_id": version.get("id"), "title": project.get("title") or project_id,
            "version": version.get("version_number"), "requirement": requirement,
            "support": support, "path": path,
        }
        rows.append(row)
        if requirement == "excluded":
            excluded_projects.add(project_id)
            return
        if support == "unsupported":
            if explicit or origin in {"client", "dependency", "imported"}:
                errors.append(f"{row['title']} does not support multiplayer clients; remove or exclude it.")
            row["requirement"] = "excluded"
            return
        if support == "unknown":
            errors.append(f"Client compatibility is unknown for {row['title']}; explicitly exclude it or correct upstream metadata.")
            return
        if project.get("project_type") not in (None, "mod"):
            errors.append(f"{row['title']} is not a Modrinth mod project.")
            return
        if minecraft not in (version.get("game_versions") or []) or loader not in (version.get("loaders") or []):
            errors.append(f"{row['title']} {row['version']} is not declared compatible with Minecraft {minecraft}/{loader}.")
            return
        files = version.get("files") or []
        if digest:
            matches = [file for file in files if (file.get("hashes") or {}).get("sha1") == digest["sha1"]]
        else:
            matches = [file for file in files if file.get("primary")]
            if not matches and len(files) == 1:
                matches = files
        if len(matches) != 1:
            errors.append(f"Select an unambiguous Modrinth file for {row['title']}.")
            return
        file = matches[0]
        try:
            filename = relative_path(file.get("filename"))
            if "/" in filename or not filename.endswith(".jar"):
                raise PackError("Only Modrinth mod jars are supported")
            target = path or f"mods/{filename}"
            if (not target.startswith("mods/") or len(PurePosixPath(target).parts) != 2
                    or not target.endswith(".jar")):
                raise PackError(f"Only top-level mods/*.jar destinations are supported: {target}")
            output = _download_file(file, target, requirement)
            if origin == "server":
                declared_server = (declared.get(project_id, {}).get("env") or {}).get("server")
                output["env"]["server"] = declared_server if declared_server in {"required", "optional"} else "required"
            elif origin == "imported":
                declaration = declared.get(project_id, {})
                output["env"]["server"] = (declaration.get("env") or {}).get("server", "required")
            if digest and (
                any(digest.get(key) and output["hashes"][key] != str(digest[key]).lower()
                    for key in ("sha1", "sha512"))
                or digest.get("size") is not None and output["fileSize"] != digest["size"]
            ):
                raise PackError(f"Installed file does not match pinned Modrinth hashes/size: {target}")
        except (PackError, ValueError) as exc:
            errors.append(str(exc))
            return
        if project_id in selected:
            old = selected[project_id]
            if old["version"]["id"] != version["id"] or old["file"]["path"] != target:
                errors.append(f"Conflicting duplicate project {row['title']}; remove the duplicate selection (shared server versions cannot be replaced).")
            elif requirement == "required":
                old["file"]["env"]["client"] = "required"
            return
        if target.casefold() in paths:
            errors.append(f"Colliding pack path: {target}")
            return
        paths[target.casefold()] = project_id
        row["path"] = target
        selected[project_id] = {"version": version, "file": output, "row": row}

    for path, digest in inventory.items():
        identity = versions.get(digest["sha1"])
        key = f"server:{path}"
        if not identity:
            requirement = selections.get(key, {}).get("requirement", "required")
            rows.append({"key": key, "path": path, "title": path, "origin": "server",
                         "requirement": requirement, "support": "unknown"})
            if requirement != "excluded":
                errors.append(f"Unknown/non-Modrinth jar {path}: explicitly exclude it and arrange a legitimate external download if needed. No binary redistribution is performed.")
            continue
        declaration = declared.get(identity["project_id"], {})
        requirement = (declaration.get("env") or {}).get("client", "required")
        if requirement == "unsupported":
            requirement = "excluded"
        if declaration and _sha1(declaration) != digest["sha1"]:
            warnings.append(f"{path}: using the current installed version, not the older imported version.")
        add(get_version(identity["version_id"]), key, "server", requirement, path, digest)

    installed_projects = {
        versions[value["sha1"]]["project_id"] for value in inventory.values()
        if value["sha1"] in versions
    }
    for entry in manifest:
        if not isinstance(entry, dict):
            errors.append("Imported manifest contains an invalid file entry.")
            continue
        path = entry.get("path", "")
        env = entry.get("env") or {}
        identity = versions.get(_sha1(entry)) or {}
        project_id = identity.get("project_id")
        if env.get("client") == "unsupported" or project_id in installed_projects:
            continue
        # Shared entries removed or disabled on the server must stay removed.
        if path in (imported.get("server_files") or []):
            warnings.append(f"{path}: no longer enabled on server; not restored from the imported pack.")
            continue
        key = f"imported:{path}"
        requirement = selections.get(key, {}).get("requirement", env.get("client", "required"))
        project = get_project(project_id) if project_id else {}
        raw_tags = project.get("environment") or []
        tags = {raw_tags} if isinstance(raw_tags, str) else set(raw_tags)
        known_client_only = bool(tags & {"client_only", "client_only_server_optional"}) or project.get("server_side") == "unsupported"
        forced_client = imported.get("side_overrides", {}).get(path) == "client"
        if (env.get("server") != "unsupported" and not known_client_only and not forced_client
                and key not in selections):
            rows.append({"key": key, "path": path, "title": path, "origin": "imported",
                         "requirement": requirement, "support": "unknown"})
            errors.append(f"{path}: imported shared file is absent/disabled on the server. Explicitly include or exclude this imported entry; metadata does not restore server files.")
            continue
        if requirement == "excluded":
            if project_id:
                excluded_projects.add(project_id)
            rows.append({"key": key, "path": path, "title": path, "origin": "imported",
                         "requirement": requirement})
        elif not project_id or not path.startswith("mods/"):
            rows.append({"key": key, "path": path, "title": path, "origin": "imported",
                         "requirement": requirement, "support": "unknown"})
            errors.append(f"Imported file {path} needs review: only hash-identified Modrinth mod jars can be exported; explicitly exclude unsupported entries.")
        else:
            add(get_version(identity["version_id"]), key, "imported", requirement, path, {
                **(entry.get("hashes") or {}), "sha1": _sha1(entry), "size": entry.get("fileSize"),
            })

    for selection in config["selections"]:
        if not selection["key"].startswith("client:") or selection["requirement"] == "excluded":
            continue
        if not selection["version_id"]:
            errors.append("Client-only selections must pin a Modrinth version ID.")
            continue
        try:
            version = get_version(selection["version_id"])
        except ModrinthApiError as exc:
            if exc.status_code != 404:
                raise
            errors.append(f"Pinned client version {selection['version_id']} is no longer available on Modrinth; remove or replace this client selection.")
            continue
        if selection["key"] != f'client:{version.get("project_id")}':
            errors.append("Client selection project and version do not match.")
            continue
        if version.get("project_id") in installed_projects:
            errors.append("A client-only selection duplicates an installed server project. Remove the client-only selection; edit its server-derived requirement instead.")
            continue
        add(version, selection["key"], "client", selection["requirement"])

    # Follow the full required graph without ever swapping an existing pin.
    queue = list(selected)
    index = 0
    while index < len(queue):
        project_id = queue[index]
        index += 1
        parent = selected[project_id]
        for dep in parent["version"].get("dependencies") or []:
            kind = dep.get("dependency_type")
            if kind not in {"required", "incompatible"}:
                continue
            try:
                dep_version = get_version(dep["version_id"]) if dep.get("version_id") else None
            except ModrinthApiError as exc:
                if exc.status_code != 404:
                    raise
                missing_versions.add(dep["version_id"])
                errors.append(f"{parent['row']['title']}: pinned dependency {dep['version_id']} is no longer available; choose another parent version.")
                continue
            dep_id = dep.get("project_id") or (dep_version or {}).get("project_id")
            if not dep_id:
                errors.append(f"{parent['row']['title']}: dependency identity is missing; resolve manually.")
                continue
            if dep_version and dep_version.get("project_id") != dep_id:
                errors.append(f"{parent['row']['title']}: dependency project and pinned version disagree; resolve upstream metadata.")
                continue
            existing = selected.get(dep_id)
            if kind == "incompatible":
                if existing and (not dep_version or existing["version"]["id"] == dep_version["id"]):
                    errors.append(f"Incompatible projects: {parent['row']['title']} and {existing['row']['title']}.")
                continue
            if existing:
                if dep_version and existing["version"]["id"] != dep_version["id"]:
                    errors.append(f"{parent['row']['title']} requires {dep_id} version {dep_version['id']}, conflicting with pinned {existing['version']['id']}. Change selections/server deliberately; no automatic upgrade.")
                if parent["file"]["env"]["client"] == "required":
                    existing["file"]["env"]["client"] = "required"
                continue
            if dep_id in excluded_projects:
                errors.append(f"{dep_id} is excluded but required by {parent['row']['title']}; resolve the contradictory selection.")
                continue
            if len(queue) >= MAX_FILES:
                errors.append("Dependency graph exceeds the client pack limit.")
                break
            if dep_version is None:
                dep_version = client.pick_best_version(client.get_project_versions(
                    dep_id, loaders=[loader], game_versions=[minecraft],
                ))
            if not dep_version:
                errors.append(f"No compatible client dependency for {dep_id}, required by {parent['row']['title']}.")
                continue
            dep_key = f"client:{dep_id}"
            if selections.get(dep_key, {}).get("requirement") == "excluded":
                errors.append(f"{dep_id} is excluded but required by {parent['row']['title']}.")
                continue
            add(dep_version, dep_key, "dependency", parent["file"]["env"]["client"])
            if dep_id in selected:
                queue.append(dep_id)
    # Requirement upgrades propagate through cycles and already-visited nodes.
    changed = True
    while changed:
        changed = False
        for item in selected.values():
            if item["file"]["env"]["client"] != "required":
                continue
            for dep in item["version"].get("dependencies") or []:
                if dep.get("dependency_type") != "required":
                    continue
                if dep.get("version_id") in missing_versions:
                    continue
                dep_id = dep.get("project_id")
                if not dep_id and dep.get("version_id"):
                    dep_id = get_version(dep["version_id"]).get("project_id")
                target = selected.get(dep_id)
                if target and target["file"]["env"]["client"] != "required":
                    target["file"]["env"]["client"] = "required"
                    changed = True
    # Check incompatibilities again after all transitively added entries exist.
    for item in selected.values():
        item["row"]["requirement"] = item["file"]["env"]["client"]
        for dep in item["version"].get("dependencies") or []:
            if dep.get("dependency_type") == "incompatible":
                if dep.get("version_id") in missing_versions:
                    continue
                dep_version = get_version(dep["version_id"]) if dep.get("version_id") else None
                target = selected.get(dep.get("project_id") or (dep_version or {}).get("project_id"))
                if target and (not dep_version or target["version"]["id"] == dep_version["id"]):
                    errors.append(f"Incompatible projects: {item['row']['title']} and {target['row']['title']}.")

    override_data, candidates = {}, []
    for path in sorted(SAFE_SETTINGS):
        server_path = Path(install_path) / path
        if server_path.exists() or server_path.is_symlink():
            candidates.append(f"server:{path}")
        if path in imported.get("settings", {}):
            candidates.append(f"imported:{path}")
    for key in config["overrides"]:
        origin, path = key.split(":", 1)
        try:
            if origin == "server":
                data = _safe_read(install_path, path)
            else:
                if path not in imported.get("settings", {}):
                    raise PackError(f"Imported client setting is no longer available: {path}")
                data = base64.b64decode(imported["settings"][path], validate=True)
            _validate_setting(path, data)
            override_data[path] = data
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f"Cannot export selected client setting {path}: {exc}")
    unsupported = imported.get("unsupported_overrides") or []
    if unsupported:
        warnings.append(f"{len(unsupported)} imported override(s) outside the client-safe allowlist are not included. Review the excluded paths.")
    for path in unsupported:
        if path.lower().endswith((".jar", ".zip")):
            key = f"imported-override:{path}"
            requirement = selections.get(key, {}).get("requirement", "required")
            rows.append({"key": key, "path": path, "title": path, "origin": "imported",
                         "requirement": requirement, "support": "unknown"})
            if requirement != "excluded":
                errors.append(f"{path}: bundled binaries are not redistributed. Explicitly exclude this entry and arrange a legitimate provider download before sharing the pack.")
    if not imported:
        warnings.append("No original client manifest retained. Missing client-only entries cannot be inferred; add them manually or reimport the original .mrpack metadata.")
    files = sorted((value["file"] for value in selected.values()), key=lambda file: file["path"])
    snapshot = {
        "server": {key: server.get(key) for key in ("version", "loader", "loaderVersion")},
        "config": config, "inventory": inventory, "source": imported,
        "files": files, "overrides": {path: hashlib.sha256(data).hexdigest() for path, data in override_data.items()},
    }
    fingerprint = _key(json.dumps(snapshot, sort_keys=True))
    return {
        "config": config, "entries": rows, "files": files,
        "errors": list(dict.fromkeys(errors)), "warnings": warnings,
        "override_candidates": candidates, "excluded_imported_overrides": unsupported,
        "source_retained": bool(imported), "fingerprint": fingerprint,
        "dependencies": {"minecraft": minecraft, **({LOADERS[loader]: loader_version} if loader in LOADERS else {})},
        "file_count": len(files), "override_count": len(override_data),
        "_overrides": override_data,
    }


def public_plan(value):
    return {key: item for key, item in value.items() if not key.startswith("_")}


def _sweep(server, *, reserve=False):
    from backend.modrinth.client_pack_shares import active
    directory = _directory(server) / "exports"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    pinned = {record["artifact_id"] for record in active(server)}
    files = sorted(directory.glob("*.mrpack"), key=lambda path: path.stat().st_mtime, reverse=True)
    limit = MAX_ARTIFACTS - int(reserve)
    protected = [path for path in files if path.stem in pinned]
    if len(protected) > limit:
        raise PackError("All five archive slots are pinned by active share links. Revoke links or explicitly delete a shared archive before generating another.")
    kept = len(protected)
    for path in files:
        if path.stem in pinned:
            continue
        if kept >= limit or path.stat().st_mtime < time.time() - ARTIFACT_TTL:
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
        else:
            kept += 1
    for path in directory.iterdir():
        if (re.fullmatch(r"[a-f0-9]{32}\.(?:tmp|json)", path.name)
                and not path.with_suffix(".mrpack").exists()
                and path.stat().st_mtime < time.time() - ARTIFACT_TTL):
            path.unlink()
    return directory


def artifacts(server):
    from backend.modrinth.client_pack_shares import active
    directory = _sweep(server)
    grants = active(server)
    result = []
    for path in sorted(directory.glob("*.mrpack"), reverse=True):
        links = [record for record in grants if record["artifact_id"] == path.stem]
        result.append({
            **_read(path.with_suffix(".json"), {}),
            "active_shares": len(links),
            "pinned_until": max((record["expires_at"] for record in links), default=None),
        })
    return result


def build(server, install_path, client, expected):
    value = plan(server, install_path, client)
    if value["errors"]:
        raise PackError("; ".join(value["errors"]))
    if expected != value["fingerprint"]:
        raise PackError("Pack sources changed since review; reload and review before exporting")
    artifact_id = uuid.uuid4().hex
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": artifact_id,
        "name": value["config"]["name"], "files": value["files"],
        "dependencies": value["dependencies"],
    }
    directory = _sweep(server, reserve=True)
    target = directory / f"{artifact_id}.mrpack"
    temporary = directory / f"{artifact_id}.tmp"
    try:
        with _ArchiveWriter(io.FileIO(temporary, "x")) as output:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(mrpack.INDEX_NAME, json.dumps(index, ensure_ascii=False).encode("utf-8"))
                for path, data in value["_overrides"].items():
                    archive.writestr(f"client-overrides/{path}", data)
        # Detect changes from outside the application lock as well as API edits.
        if plan(storage.get_server(server["id"]) or server, install_path, client)["fingerprint"] != value["fingerprint"]:
            raise PackError("Sources changed during generation; no archive was published")
        metadata = {
            "id": artifact_id, "created_at": time.time(),
            "fingerprint": value["fingerprint"], "file_count": len(index["files"]),
            "override_count": len(value["_overrides"]), "size": temporary.stat().st_size,
            "filename": f"client-pack-{artifact_id[:12]}.mrpack",
        }
        _atomic_json(target.with_suffix(".json"), metadata)
        os.replace(temporary, target)
        return metadata
    finally:
        temporary.unlink(missing_ok=True)
        if not target.exists():
            target.with_suffix(".json").unlink(missing_ok=True)


def artifact_path(server, artifact_id):
    if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise PackError("Invalid export ID")
    directory = _sweep(server)
    path = directory / f"{artifact_id}.mrpack"
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Export expired or not found; generate a new archive")
    return path, _read(path.with_suffix(".json"), {})


def artifact_metadata_path(path):
    return path.with_suffix(".json")


def delete_state(server, install_path=None):
    directory = _directory(server)
    (directory / "shares.json").unlink(missing_ok=True)
    exports = directory / "exports"
    if exports.exists():
        for path in exports.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}\.(?:mrpack|json|tmp)", path.name):
                path.unlink()
        exports.rmdir()
    (directory / "config.json").unlink(missing_ok=True)
    if directory.exists():
        directory.rmdir()
    if install_path is not None:
        _source_path(install_path).unlink(missing_ok=True)
