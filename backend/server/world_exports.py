"""Immutable stopped-world archives. All callers hold the existing server lock."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import math
import os
from pathlib import PurePosixPath
import re
import stat
import tarfile
import threading
import time
import uuid

from backend.modrinth import client_pack_shares as shares
from backend.server import storage, transfers, worlds
from backend.utils.upload import env_max_bytes

SHARE_PAGE = "/world-share"
ARTIFACT_TTL = 24 * 3600
MAX_ARTIFACTS = 5
_BUILD = threading.BoundedSemaphore(1)
_RESERVED = {
    "server.properties", "server.properties.bak", "session.lock", "logs",
    "crash-reports", "backups", "plugins", "mods", "config", "defaultconfigs",
    "libraries", "versions", ".env", "auth.json", "forwarding.secret",
    "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json",
    "usercache.json", "usernamecache.json", "bukkit.yml", "spigot.yml", "paper.yml",
}


class PackError(ValueError):
    pass


def _key(value):
    from backend.modrinth.client_pack import _key as key
    return key(value)


def _read(path, default):
    from backend.modrinth.client_pack import _read as read
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PackError("World export state must not be a symlink")
    return read(path, default)


def _atomic_json(path, value):
    from backend.modrinth.client_pack import _atomic_json as write
    return write(path, value)


def _root():
    return storage._resolve_servers_file().parent / "world-exports"


def _directory(server):
    path = _root() / _key(server["id"])
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PackError("World export storage must not contain symlinks")
    return path


def _store():
    import sys
    return sys.modules[__name__]


def _state_temporary(name):
    return bool(re.fullmatch(r"\.(?:[a-f0-9]{32}\.json|shares\.json)-[a-f0-9]{32}\.tmp", name))


def _skip(rel):
    return any(part.lower() in _RESERVED
               or part.lower().endswith((".jar", ".jar.disabled", ".log", ".secret", ".lck"))
               or part.startswith((".fabricator-", ".env."))
               for part in rel.split("/"))


def _safe_path(rel):
    parts = rel.split("/")
    if (len(rel) > 240 or PurePosixPath(rel).is_absolute() or "\\" in rel or ":" in rel
            or any(p in {"", ".", ".."} for p in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in rel)):
        raise PackError("World contains a non-portable or unsafe archive path")


@contextmanager
def _open(root, rel):
    """Traverse anchored directory descriptors, refusing swaps to links."""
    _safe_path(rel)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    handle = None
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        handle = os.fdopen(file_fd, "rb")
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PackError("World source changed to a special or hardlinked file")
        yield handle
        if _signature(os.fstat(handle.fileno())) != _signature(info):
            raise PackError("World source changed during export; nothing was published")
    finally:
        if handle:
            handle.close()
        os.close(fd)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash(handle):
    digest = hashlib.sha256()
    for block in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _scan(server, root, group_id):
    groups = worlds.discover(server, root)
    worlds.require_supported_worlds(root, groups)
    selected = next((g for g in groups if g["id"] == group_id), None)
    if selected is None:
        raise PackError("Select a supported world group; individual dimensions cannot be exported")
    if any(_skip(name) or name == "fabricator-world.json" for name in selected["directories"]):
        raise PackError("World directory conflicts with excluded content or the archive manifest")
    for dimension in selected["dimensions"]:
        rel = dimension if selected["layout"] == "bukkit-split" else selected["id"] + "/" + dimension
        if not (root / rel).is_dir() or (root / rel).is_symlink():
            raise PackError("Discovered dimensions must be real directories")
        if _skip(rel):
            raise PackError("A dimension overlaps excluded server content; manual export review is required")
    names = set(selected["directories"])
    entries, total = transfers._scan(root, lambda rel: rel.split("/")[0] not in names or _skip(rel))
    for rel in entries:
        _safe_path(rel)
    return selected, entries, total


def _manifest(root, entries):
    result = {}
    for rel, size in entries.items():
        if size is None:
            result[rel] = None
        else:
            with _open(root, rel) as handle:
                if os.fstat(handle.fileno()).st_size != size:
                    raise PackError("World size changed during export")
                result[rel] = _hash(handle)
    return result


def _sweep(server, reserve=False):
    directory = _directory(server)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    pinned = {g["artifact_id"] for g in shares.active(server, _store())}
    files = sorted(directory.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    keep = MAX_ARTIFACTS - int(reserve)
    protected = sum(p.name[:-7] in pinned for p in files)
    if protected > keep:
        raise PackError("All five world archive slots are pinned; revoke links or explicitly delete an archive")
    count = protected
    for path in files:
        artifact_id = path.name[:-7]
        if artifact_id in pinned:
            continue
        if count >= keep or path.stat().st_mtime < time.time() - ARTIFACT_TTL:
            path.unlink()
            (directory / f"{artifact_id}.json").unlink(missing_ok=True)
        else:
            count += 1
    # Under the server lock, no generation is active. Recover partial publication.
    for path in directory.iterdir():
        if _state_temporary(path.name):
            path.unlink()
        elif (re.fullmatch(r"[a-f0-9]{32}\.(?:tmp|json)", path.name)
                and not (directory / f"{path.stem}.tar.gz").exists()):
            path.unlink()
    return directory


def artifacts(server):
    directory = _sweep(server)
    grants = shares.active(server, _store())
    result = []
    for path in directory.glob("*.tar.gz"):
        artifact_id = path.name[:-7]
        _, metadata = artifact_path(server, artifact_id, sweep=False)
        links = [g for g in grants if g["artifact_id"] == artifact_id]
        result.append({**metadata, "active_shares": len(links),
                       "pinned_until": max((g["expires_at"] for g in links), default=None)})
    return sorted(result, key=lambda m: m["created_at"], reverse=True)


def artifact_path(server, artifact_id, sweep=True):
    if not isinstance(artifact_id, str) or not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
        raise PackError("Invalid world export ID")
    directory = _sweep(server) if sweep else _directory(server)
    path = directory / f"{artifact_id}.tar.gz"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("World archive expired or not found")
    metadata = _read(directory / f"{artifact_id}.json", {})
    if (not isinstance(metadata, dict)
            or metadata.get("id") != artifact_id or type(metadata.get("size")) is not int
            or metadata["size"] < 0 or metadata["size"] != path.stat().st_size
            or not isinstance(metadata.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", metadata.get("sha256", ""))
            or type(metadata.get("created_at")) not in (int, float)
            or not math.isfinite(metadata["created_at"])
            or not isinstance(metadata.get("world"), dict)
            or not isinstance(metadata["world"].get("name"), str)
            or metadata.get("filename") != f"world-{artifact_id[:12]}.tar.gz"):
        raise PackError("Invalid world archive metadata; check storage")
    return path, metadata


def build(server, group_id):
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW"):
        raise PackError("Race-safe world exports currently require a POSIX host")
    if not _BUILD.acquire(blocking=False):
        raise PackError("Another world export is running; wait and retry")
    temporary = target = metadata_path = None
    published = False
    try:
        transfers._stopped(server)
        root = transfers._literal_root(server)
        group, entries, total = _scan(server, root, group_id)
        limit = env_max_bytes("FABRICATOR_MAX_WORLD_EXPORT_BYTES", 100 * 1024**3)
        if total > limit:
            raise PackError("World exceeds FABRICATOR_MAX_WORLD_EXPORT_BYTES")
        directory = _sweep(server, reserve=True)
        # Reserve space for gzip expansion and per-entry tar headers.
        required = total + len(entries) * 2048 + total // 100 + 1024**2
        transfers._space(directory, required)
        quota = env_max_bytes("FABRICATOR_WORLD_EXPORT_STORAGE_BYTES", 200 * 1024**3)
        used = sum(p.stat().st_size for p in _root().glob("*/*.tar.gz"))
        if used + required > quota:
            raise PackError("World archive storage quota exceeded; delete retained exports")
        expected = _manifest(root, entries)
        artifact_id = uuid.uuid4().hex
        target = directory / f"{artifact_id}.tar.gz"
        temporary = directory / f"{artifact_id}.tmp"
        metadata_path = directory / f"{artifact_id}.json"
        manifest = json.dumps({"format": 1, "world": group, "files": expected,
                               "loader": server.get("loader"), "loader_version": server.get("loaderVersion")},
                              ensure_ascii=True, sort_keys=True).encode()
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            with tarfile.open(fileobj=output, mode="w|gz", format=tarfile.PAX_FORMAT) as archive:
                info = tarfile.TarInfo("fabricator-world.json")
                info.size, info.mode = len(manifest), 0o600
                archive.addfile(info, io.BytesIO(manifest))
                for rel, size in entries.items():
                    info = tarfile.TarInfo(rel)
                    info.mode = 0o700 if size is None else 0o600
                    if size is None:
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    else:
                        info.size = size
                        with _open(root, rel) as handle:
                            if os.fstat(handle.fileno()).st_size != size:
                                raise PackError("World source size changed")
                            archive.addfile(info, handle)
            output.flush()
            os.fsync(output.fileno())
        # Verify the actual compressed archive, then independently rescan sources.
        actual = {}
        with tarfile.open(temporary, mode="r|gz") as archive:
            for member in archive:
                if member.name == "fabricator-world.json":
                    with archive.extractfile(member) as handle:
                        if handle.read(len(manifest) + 1) != manifest:
                            raise PackError("World archive manifest verification failed")
                elif member.isdir():
                    actual[member.name] = None
                elif member.isfile():
                    with archive.extractfile(member) as handle:
                        actual[member.name] = _hash(handle)
                else:
                    raise PackError("World archive contains an unsupported entry")
        transfers._stopped(server)
        group_now, entries_now, _ = _scan(server, root, group_id)
        if actual != expected or group_now != group or _manifest(root, entries_now) != expected:
            raise PackError("World changed during export; no archive was published")
        with temporary.open("rb") as handle:
            digest = _hash(handle)
        metadata = {"id": artifact_id, "created_at": time.time(), "world": group,
                    "loader": server.get("loader"), "loader_version": server.get("loaderVersion"),
                    "size": temporary.stat().st_size, "source_bytes": total,
                    "file_count": sum(n is not None for n in entries.values()),
                    "sha256": digest, "filename": f"world-{artifact_id[:12]}.tar.gz"}
        _atomic_json(metadata_path, metadata)
        os.replace(temporary, target)
        transfers._sync_dir(directory)
        published = True
        return metadata
    finally:
        try:
            if temporary:
                temporary.unlink(missing_ok=True)
            if target and not published and metadata_path:
                target.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
        finally:
            _BUILD.release()


def delete_state(server):
    directory = _directory(server)
    if directory.exists():
        (directory / "shares.json").unlink(missing_ok=True)
        for path in directory.iterdir():
            if re.fullmatch(r"[a-f0-9]{32}\.(?:tar\.gz|json|tmp)", path.name) or _state_temporary(path.name):
                path.unlink()
        directory.rmdir()


def artifact_metadata_path(path):
    return path.with_name(path.name[:-7] + ".json")


def recover():
    for server in storage.get_all_servers():
        with transfers.locked([server["id"]]):
            _sweep(server)
