"""Stopped-only, journaled server forks and world copies.

The existing per-server locks serialize these operations with panel writes,
power actions, backups and imports. Journals block starts until rollback or
commit cleanup succeeds, including after an interrupted process.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import threading
import tarfile
import uuid

from backend.backups import archive, progress
from backend.backups import storage as backup_storage
from backend.managed import is_managed
from backend.server import storage, worlds
from backend.server.importer import read_properties
from backend.server.locks import get_server_lock
from backend.server.registry import get_server_process_registry
from backend.utils.strings import escape_properties_value
from backend.utils.upload import env_max_bytes

logger = logging.getLogger(__name__)
_MAX_FILES = 200_000
_MARGIN = 64 * 1024 * 1024
_ADMIN = {"ops.json", "whitelist.json", "banned-players.json", "banned-ips.json",
          "usercache.json", "usernamecache.json"}
_PLAYER = {"playerdata", "advancements", "stats"}
_EXCLUDED = _ADMIN | {
    "logs", "crash-reports", "backups", "cache", ".cache", "debug",
    "playit", "frp", "frpc", "velocity.toml", "forwarding.secret",
    "server.properties", "server.properties.bak", "session.lock",
}
_FORK_DIRS = {"mods", "plugins", "config", "defaultconfigs", "libraries", "versions",
              ".fabric", ".quilt", "datapacks"}
_FORK_FILES = {"eula.txt", "server-icon.png", "bukkit.yml", "spigot.yml", "paper.yml",
               "fabric-server-launcher.properties", "quilt-server-launcher.properties"}
_MAP_CONTROLS = {"config/fabricator-map.json", "config/bluemap/core.conf",
                 "config/bluemap/webserver.conf"}
_WARNINGS = [
    "Both instances must be stopped in Fabricator; external JVMs and external filesystem writers must also be stopped.",
    "Mod/plugin configs and data are trusted executable server content and may contain credentials, remote databases or embedded listener ports. Review them before starting; no universal secret scrubber or port discovery is possible.",
    "World playerdata, advancements and stats are optional. Other world data (entities, datapacks, mod capabilities and saved state) is integral and may retain player identities or in-world privileges.",
    "Bukkit uid.dat is regenerated; plugins using world UUIDs, names or external databases may need manual relinking. Custom world containers and additional plugin-managed worlds are not supported.",
]


def registry():
    return get_server_process_registry()


def _literal_root(server):
    base = registry().base_dir
    value = Path(server.get("installPath") or server["id"]).expanduser()
    value = value if value.is_absolute() else base / value
    if any(p.is_symlink() for p in (value, *value.parents)):
        raise ValueError("Instance paths must not contain symlinks")
    root = value.resolve()
    if not root.is_dir():
        raise ValueError("Instance directory is missing")
    if root == base or root in base.parents:
        raise ValueError("Instance directory overlaps the server root")
    return root


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents or os.path.samefile(a, b)


def _stopped(server):
    if server.get("status", "stopped") != "stopped" or registry().get_status(server["id"]).get("status") != "stopped":
        raise ValueError("Stop the instance before previewing or copying; this operation never stops a server")
    if (_literal_root(server) / ".fabricator-transfer").exists():
        raise ValueError("An interrupted transfer needs recovery before using this instance")


@contextmanager
def locked(ids):
    locks = [get_server_lock(sid) for sid in sorted(set(ids))]
    acquired = []
    try:
        for lock in locks:
            lock.acquire()
            acquired.append(lock)
        yield
    finally:
        for lock in reversed(acquired):
            lock.release()


def _server(sid):
    server = storage.get_server(sid)
    if not server:
        raise ValueError("Instance no longer exists")
    _stopped(server)
    return server, _literal_root(server)


def _scan(root, skip=lambda rel: False):
    """Fail closed on links, aliases, special files, unreadable entries and limits."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Selected content must be a real directory, not a symlink")
    result = {}
    total = 0
    visited = set()
    limit = env_max_bytes("FABRICATOR_MAX_TRANSFER_BYTES", 100 * 1024**3)

    def visit(directory):
        nonlocal total
        info = directory.stat()
        identity = (info.st_dev, info.st_ino)
        if identity in visited or info.st_dev != root.stat().st_dev:
            raise ValueError("Aliased directories and nested mount points are not supported")
        visited.add(identity)
        for entry in sorted(directory.iterdir()):
            rel = entry.relative_to(root).as_posix()
            if skip(rel):
                continue
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Symlinks are not supported: {rel}")
            if stat.S_ISDIR(info.st_mode):
                result[rel] = None
                visit(entry)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ValueError(f"Hardlinked files are not supported: {rel}")
                total += info.st_size
                result[rel] = info.st_size
            else:
                raise ValueError(f"Special files are not supported: {rel}")
            if len(result) > _MAX_FILES or total > limit:
                raise ValueError("Transfer exceeds the configured byte or 200,000-entry limit")
    visit(root)
    return result, total


def _digest(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _manifest(root, entries):
    return {rel: None if size is None else _digest(root / rel) for rel, size in entries.items()}


def _space(root, needed):
    if shutil.disk_usage(root).free < needed + _MARGIN:
        raise ValueError("Insufficient disk space for staging, safety backup and rollback (including 64 MiB reserve)")


def _copy(root, destination, entries, job_id, skip):
    destination.mkdir(parents=True, exist_ok=True)
    expected = {}
    copied = 0
    progress.update(job_id, phase="copying", total_bytes=sum(n or 0 for n in entries.values()), copied_bytes=0)
    for rel, size in entries.items():
        source, target = root / rel, destination / rel
        # Scan again immediately before reading, so panel-controlled changes
        # cannot introduce a link between planning and copying.
        if source.is_symlink() or any(p.is_symlink() for p in source.parents):
            raise ValueError("Source changed to a symlink during the transfer")
        if size is None:
            target.mkdir(parents=True, exist_ok=True)
            expected[rel] = None
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            expected[rel] = _digest(source)
            shutil.copyfile(source, target)
            with target.open("rb") as handle:
                os.fsync(handle.fileno())
            copied += size
            progress.update(job_id, copied_bytes=copied)
    progress.update(job_id, phase="verifying")
    actual, _ = _scan(destination)
    source_now, _ = _scan(root, skip)
    if _manifest(destination, actual) != expected or _manifest(root, source_now) != expected:
        raise ValueError("Source or copied bytes changed during transfer; nothing was published")
    _sync_tree(destination, actual)


def _write_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    _sync_dir(path.parent)


def _sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sync_tree(root, entries):
    for rel, size in entries.items():
        if size is not None:
            with (root / rel).open("rb") as handle:
                os.fsync(handle.fileno())
    for rel, size in reversed(list(entries.items())):
        if size is None:
            _sync_dir(root / rel)
    _sync_dir(root)


def _fork_skip(groups, include_worlds, include_players):
    world_names = {name for group in groups for name in group["directories"]}

    def skip(rel):
        parts = rel.split("/")
        if rel in _MAP_CONTROLS:
            return True
        if parts[0] not in world_names | _FORK_DIRS | _FORK_FILES and not parts[0].endswith(".jar"):
            return True
        if parts[0] in _EXCLUDED or parts[0].startswith(".") and parts[0] not in {".fabric", ".quilt"}:
            return True
        if any(part.endswith((".secret", ".log", ".lck")) or part == "session.lock" for part in parts):
            return True
        if any(".fabricator-backup-" in part or ".fabricator-write-" in part for part in parts):
            return True
        if not include_worlds and parts[0] in world_names:
            return True
        if not include_players and parts[0] in world_names and any(part in _PLAYER for part in parts[1:]):
            return True
        # Management agents and alternate launch scripts are never inherited.
        return len(parts) == 1 and (parts[0].endswith((".sh", ".bat", ".ps1", ".env")) or parts[0].startswith("hs_err_pid"))
    return skip


def _world_skip(group, include_players):
    names = set(group["directories"])
    return lambda rel: (
        rel.split("/")[0] not in names
        or rel.split("/")[-1] == "session.lock"
        or group["layout"] == "bukkit-split" and len(rel.split("/")) == 2 and rel.split("/")[-1] == "uid.dat"
        or not include_players and any(p in _PLAYER for p in rel.split("/")[1:])
    )


def _content(root):
    result = {}
    for folder in ("mods", "plugins"):
        path = root / folder
        if path.exists():
            entries, _ = _scan(path)
            result.update({folder + "/" + rel: _digest(path / rel) for rel, size in entries.items()
                           if size is not None and rel.endswith((".jar", ".jar.disabled"))})
    return result


def _mod_config(root):
    result = {}
    for folder in ("config", "defaultconfigs"):
        path = root / folder
        if path.exists():
            if path.is_symlink():
                raise ValueError("Mod configuration folders must not be links")
            entries, _ = _scan(path)
            result[folder] = _manifest(path, entries)
    return result


def _launch_jar_hash(server, root):
    launch = server.get("launch") or {"type": "jar", "jar": "server.jar"}
    name = launch.get("jar")
    if launch.get("type") != "jar" or not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("A verifiable relative launch jar is required for Vanilla/Bukkit world copies")
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("Installed launch jar is missing or symlinked")
    return _digest(path)


def _plan(kind, source_id, data):
    if is_managed():
        raise ValueError("Instance transfers are disabled in managed mode")
    source, root = _server(source_id)
    groups = worlds.discover(source, root)
    worlds.require_supported_worlds(root, groups)
    if type(data.get("includePlayerData")) is not bool:
        raise ValueError("Choose whether to include world player data")
    plan = {"kind": kind, "sourceId": source_id, "worlds": groups, "warnings": _WARNINGS}
    if kind == "fork":
        name = worlds.safe_name(data.get("name"))
        if any(str(s.get("name") or "").casefold() == name.casefold() for s in storage.get_all_servers()):
            raise ValueError("Choose a unique instance name")
        if type(data.get("includeWorlds")) is not bool:
            raise ValueError("Choose whether to include worlds")
        if source.get("command"):
            raise ValueError("Custom launch commands require manual migration")
        launch = source.get("launch") or {"type": "jar", "jar": "server.jar"}
        if launch.get("type") not in {"jar", "args_file"}:
            raise ValueError("This launch format requires manual migration")
        if launch.get("jvm_args") or launch.get("program_args") not in (None, [], ["nogui"]):
            raise ValueError("Custom launch arguments require manual migration")
        for arg in [*(launch.get("jvm_args") or []), *(launch.get("program_args") or [])]:
            if (not isinstance(arg, str) or str(root) in arg or arg.startswith(("/", "\\"))
                    or any(key in arg.lower() for key in ("jmxremote", "jdwp", "javaagent", "agentlib"))):
                raise ValueError("Host-specific launch arguments require manual migration")
        target = launch.get("jar") if launch.get("type") == "jar" else launch.get("args_file")
        if not isinstance(target, str) or Path(target).is_absolute() or ".." in Path(target).parts:
            raise ValueError("Launch targets must be relative to the instance")
        if not (root / target).is_file():
            raise ValueError("Install the source before forking it")
        entries, size = _scan(root, _fork_skip(groups, data["includeWorlds"], data["includePlayerData"]))
        if target not in entries:
            raise ValueError("Launch target is outside the supported copy scope")
        if launch["type"] == "args_file":
            text = (root / target).read_text(encoding="utf-8")
            if (str(root) in text or re.search(r"""(^|[\s=:;'"])(?:/|[A-Za-z]:[\\/])""", text)
                    or re.search(r"(^|[\s\\/])\.\.[\\/]", text) or re.search(r"(^|\s)@", text)):
                raise ValueError("Host-specific or nested launch argument files require manual migration")
        _space(registry().base_dir, size * 2)
        plan.update(name=name, includeWorlds=data["includeWorlds"], includePlayerData=data["includePlayerData"],
                    bytes=size, administrativeAccess="fresh", autoStart=False)
    else:
        dest_id = data.get("destinationId")
        if not isinstance(dest_id, str) or dest_id == source_id:
            raise ValueError("Choose a different destination instance")
        destination, dest = _server(dest_id)
        if _overlap(root, dest):
            raise ValueError("Source and destination directories alias or overlap")
        dest_groups = worlds.discover(destination, dest)
        worlds.require_supported_worlds(dest, dest_groups)
        group = next((g for g in groups if g["id"] == data.get("worldId")), None)
        if group is None:
            raise ValueError("Choose a supported source world")
        for field in ("version", "loader", "loaderVersion"):
            if not source.get(field) and field != "loaderVersion" or source.get(field) != destination.get(field):
                raise ValueError("World copies require identical game, loader and loader-build versions")
        if source["loader"] not in worlds.PAPER | {"vanilla"} and not source.get("loaderVersion"):
            raise ValueError("A verified loader-build version is required for modded world copies")
        if source["loader"] in worlds.PAPER | {"vanilla"} and _launch_jar_hash(source, root) != _launch_jar_hash(destination, dest):
            raise ValueError("Installed launch jar hashes differ; synchronize the game/loader build before copying")
        if _content(root) != _content(dest):
            raise ValueError("Installed mod/plugin jar hashes differ; synchronize dependencies before copying")
        if source["loader"] not in worlds.PAPER | {"vanilla"} and _mod_config(root) != _mod_config(dest):
            raise ValueError("Mod configuration differs; reconcile world-generation and dimension dependencies before copying")
        if dest_groups and group["dataVersion"] != dest_groups[0]["dataVersion"]:
            raise ValueError("World DataVersions differ; automatic upgrades and downgrades are not supported")
        dest_name = worlds.safe_name(read_properties(dest).get("level-name", destination.get("levelName") or "world"))
        # Check the whole destination before archiving; safety backups may not
        # silently skip unreadable entries or follow links.
        dest_entries, dest_bytes = _scan(dest, lambda rel: rel.split("/")[0] == "backups")
        if (dest / "backups").is_symlink():
            raise ValueError("Safety backup directory must not be a symlink")
        entries, size = _scan(root, _world_skip(group, data["includePlayerData"]))
        _space(dest, size * 2 + dest_bytes * 2 + len(dest_entries) * 1024 + 10240)
        plan.update(destinationId=dest_id, destinationWorld=dest_name, world=group,
                    includePlayerData=data["includePlayerData"], bytes=size,
                    replaces=bool(dest_groups), requiresSafetyBackup=True, layout=group["layout"])
    # A full source fingerprint binds preview to the actual bytes, not just
    # names, sizes or timestamps; the worker recomputes under both locks.
    fingerprint = _manifest(root, entries)
    extra = {}
    if kind != "fork":
        dest_entries, _ = _scan(dest, lambda rel: rel.split("/")[0] == "backups")
        extra = _manifest(dest, dest_entries)
    token = hashlib.sha256(json.dumps([plan, source, data.get("restart", False), fingerprint, extra],
                                      sort_keys=True).encode()).hexdigest()
    return {**plan, "previewToken": token}, source, root, entries


def preview(kind, source_id, data):
    ids = [source_id] + ([data["destinationId"]] if isinstance(data.get("destinationId"), str) else [])
    with locked(ids):
        return _plan(kind, source_id, data)[0]


def list_worlds(source_id):
    with locked([source_id]):
        source, root = _server(source_id)
        groups = worlds.discover(source, root)
        worlds.require_supported_worlds(root, groups)
        return groups


def _ports():
    used = set()
    for server in storage.get_all_servers():
        used.update(storage.port_values(server, default_game=True))
    result = []
    for port in range(25565, 65536):
        if port in used:
            continue
        try:
            with socket.socket() as tcp, socket.socket(type=socket.SOCK_DGRAM) as udp:
                tcp.bind(("0.0.0.0", port))
                udp.bind(("0.0.0.0", port))
        except OSError:
            continue
        result.append(port)
        if len(result) == 3:
            return result
    raise ValueError("No free game/query/RCON port set is available")


def _fork_record(source, root, stage, plan, server_id):
    from backend.server.routes import _record_fields_from_properties
    record = _record_fields_from_properties(read_properties(root))
    for field in ("version", "loader", "loaderVersion", "launch", "memory", "memoryMin", "memoryUnit", "modContent"):
        if field in source:
            record[field] = copy.deepcopy(source[field])
    game, query, rcon = _ports()
    record.update(id=server_id, name=plan["name"], status="stopped", installPath=str(registry().base_dir / server_id),
                  autoStart="never", port=game, queryPort=query, rconPort=rcon,
                  enableQuery=False, enableRcon=False, rconPassword="", enableJmxMonitoring=False,
                  onlineMode=True, serverIp="127.0.0.1", whitelist=False, enforceWhitelist=False,
                  textFilteringConfig="", resourcePack="", requireResourcePack=False)
    # Keep unknown game properties, but never inherit known listener secrets,
    # remote filtering endpoints or proxy authentication.
    props = read_properties(root)
    props = {k: v for k, v in props.items() if not k.startswith("management-server-")}
    props.update({"server-port": str(game), "query.port": str(query), "rcon.port": str(rcon),
                  "enable-query": "false", "enable-rcon": "false", "rcon.password": "",
                  "enable-jmx-monitoring": "false", "online-mode": "true", "server-ip": "127.0.0.1",
                  "white-list": "false", "enforce-whitelist": "false", "text-filtering-config": "",
                  "resource-pack": "", "require-resource-pack": "false"})
    props["management-server-enabled"] = "false"
    (stage / "server.properties").write_text("".join(
        f"{k}={escape_properties_value(v)}\n" for k, v in props.items()), encoding="utf-8")
    if (root / "config" / "bluemap").exists():
        config = stage / "config" / "bluemap"
        config.mkdir(parents=True, exist_ok=True)
        (config / "core.conf").write_text(
            "accept-download: false\nrender-thread-count: 1\nmetrics: false\n", encoding="utf-8")
        (config / "webserver.conf").write_text("enabled: false\n", encoding="utf-8")
    from backend.velocity.forwarding import _yaml_update
    for rel, assignments in [
        ("config/paper-global.yml", {("proxies", "velocity", "enabled"): False, ("proxies", "velocity", "secret"): ""}),
        ("paper.yml", {("settings", "velocity-support", "enabled"): False, ("settings", "velocity-support", "secret"): ""}),
        ("spigot.yml", {("settings", "bungeecord"): False}),
    ]:
        path = stage / rel
        if path.exists():
            path.write_text(_yaml_update(path, assignments), encoding="utf-8")
    for group in plan["worlds"]:
        if group["layout"] == "bukkit-split":
            for name in group["directories"]:
                (stage / name / "uid.dat").unlink(missing_ok=True)
    return record


def _safety(server_id, root, purpose="world-copy"):
    location = root / "backups"
    location.mkdir(exist_ok=True)
    final = location / f"safety-{purpose}-{uuid.uuid4().hex}.tar"
    partial = final.with_suffix(".partial")
    try:
        skip = lambda rel: rel.split("/")[0] in {"backups", ".fabricator-transfer"}
        entries, _ = _scan(root, skip)
        expected = _manifest(root, entries)
        warnings = archive.write_tree_tar(root, partial, skip=skip)
        if warnings:
            raise ValueError("Safety backup was incomplete; destination has not been changed")
        captured = {}
        with tarfile.open(partial) as backup:
            for member in backup:
                if member.isdir():
                    captured[member.name] = None
                elif member.isfile():
                    with backup.extractfile(member) as handle:
                        digest = hashlib.sha256()
                        for block in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(block)
                        captured[member.name] = digest.hexdigest()
                else:
                    raise ValueError("Safety archive contains an unsupported member")
        if captured != expected or _manifest(root, entries) != expected:
            raise ValueError("Safety backup verification failed; destination has not been changed")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, final)
        _sync_dir(location)
        return backup_storage.record_snapshot(server_id, {
            "type": "safety", "configId": None, "filePath": str(final), "fileName": final.name,
            "sizeBytes": final.stat().st_size, "status": "success", "message": "Before " + purpose.replace("-", " "),
        })
    finally:
        partial.unlink(missing_ok=True)


def _remove(path):
    if path.is_symlink():
        raise ValueError("Refusing to clean up a symlink")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _rollback(root, txn, journal):
    for name in journal["names"]:
        worlds.safe_name(name)
        old, staged, live = txn / "old" / name, txn / "stage" / name, root / name
        if old.exists():
            _remove(live)
            os.replace(old, live)
        elif name not in journal["existing"] and not staged.exists():
            _remove(live)
    _sync_dir(root)


def _publish_worlds(root, txn, journal):
    for name in journal["names"]:
        live = root / name
        if live.exists():
            os.replace(live, txn / "old" / name)
        staged = txn / "stage" / name
        if staged.exists():
            os.replace(staged, live)
        _sync_dir(root)
        _sync_dir(txn / "old")
        _sync_dir(txn / "stage")


def run(kind, source_id, data, job_id=None):
    job_id = job_id or progress.generate_job_id("transfer")
    ids = [source_id] + ([data["destinationId"]] if isinstance(data.get("destinationId"), str) else [])
    server_id = storage.generate_server_id() if kind == "fork" else None
    if server_id:
        ids.append(server_id)
    txn = None
    final = None
    committed = False
    journal = None
    root_dest = None
    progress.update(job_id, phase="queued")
    try:
        with locked(ids):
            plan, source, root, entries = _plan(kind, source_id, data)
            if data.get("previewToken") != plan["previewToken"]:
                raise ValueError("Preview is stale; preview again before confirming")
            if data.get("acknowledgeRisks") is not True:
                raise ValueError("Acknowledge the copy scope and configuration risks")
            if kind == "fork":
                if storage.get_server(server_id):
                    raise ValueError("Generated identity already exists; retry the fork")
                candidate = registry().base_dir / (".fork-" + server_id)
                candidate.mkdir()
                txn = candidate
                _sync_dir(txn.parent)
                _write_json(txn / "journal.json", {"id": server_id, "committed": False})
                stage = txn / "stage"
                skip = _fork_skip(plan["worlds"], data["includeWorlds"], data["includePlayerData"])
                _copy(root, stage, entries, job_id, skip)
                record = _fork_record(source, root, stage, plan, server_id)
                staged_entries, _ = _scan(stage)
                expected = _manifest(stage, staged_entries)
                _sync_tree(stage, staged_entries)
                target = registry().base_dir / server_id
                progress.update(job_id, phase="publishing")
                if target.exists():
                    raise ValueError("Generated destination already exists")
                os.replace(stage, target)
                final = target
                _sync_dir(final.parent)
                final_entries, _ = _scan(final)
                if _manifest(final, final_entries) != expected:
                    raise RuntimeError("Published instance filesystem verification failed")
                created = storage.register_fork(record)
                if storage.get_server(server_id) != created or not final.is_dir():
                    raise RuntimeError("Persisted instance verification failed")
                _write_json(txn / "journal.json", {"id": server_id, "committed": True})
                committed = True
                result = {"serverId": server_id, "name": created["name"], "port": created["port"]}
            else:
                root_dest = _literal_root(storage.get_server(plan["destinationId"]))
                if plan["replaces"] and data.get("confirmReplace") != plan["destinationWorld"]:
                    raise ValueError("Type the destination world name to confirm replacement")
                if type(data.get("restart")) is not bool:
                    raise ValueError("Choose whether to start the destination after copying")
                progress.update(job_id, phase="safety_snapshot")
                safety = _safety(plan["destinationId"], root_dest)
                progress.update(job_id, safetySnapshotId=safety["id"])
                candidate = root_dest / ".fabricator-transfer"
                candidate.mkdir()
                txn = candidate
                staged_source = txn / "source"
                _copy(root, staged_source, entries, job_id, _world_skip(plan["world"], data["includePlayerData"]))
                stage = txn / "stage"
                stage.mkdir()
                dest_name, source_name = plan["destinationWorld"], plan["world"]["name"]
                for name in plan["world"]["directories"]:
                    os.replace(staged_source / name, stage / (dest_name + name[len(source_name):]))
                _sync_dir(stage)
                _sync_dir(staged_source)
                names = [dest_name, dest_name + "_nether", dest_name + "_the_end"] if plan["layout"] == "bukkit-split" else [dest_name]
                (txn / "old").mkdir()
                journal = {"names": names, "existing": [n for n in names if (root_dest / n).exists()], "committed": False}
                staged_entries, _ = _scan(stage)
                expected = _manifest(stage, staged_entries)
                _write_json(txn / "journal.json", journal)
                progress.update(job_id, phase="publishing")
                try:
                    _publish_worlds(root_dest, txn, journal)
                    for name in names:
                        if (stage / name).exists():
                            raise RuntimeError("World publication verification failed")
                    check = worlds.discover(storage.get_server(plan["destinationId"]), root_dest)
                    if not check or check[0]["dataVersion"] != plan["world"]["dataVersion"]:
                        raise RuntimeError("Published world metadata verification failed")
                    published_entries, _ = _scan(root_dest, lambda rel: rel.split("/")[0] not in names)
                    if _manifest(root_dest, published_entries) != expected:
                        raise RuntimeError("Published world filesystem verification failed")
                    journal["committed"] = True
                    _write_json(txn / "journal.json", journal)
                    committed = True
                except Exception:
                    _rollback(root_dest, txn, journal)
                    journal = None
                    raise
                registry().invalidate(plan["destinationId"])
                result = {"destinationId": plan["destinationId"], "safetySnapshotId": safety["id"]}
            _remove(txn)
            txn = None
            if kind == "world-copy" and data["restart"]:
                progress.update(job_id, phase="restarting")
                # The ordinary route enforces Java checks. Registry.start_server
                # also enforces them when given the resolved requirement.
                from backend.server.java_compat import resolve_required_java
                destination = storage.get_server(plan["destinationId"])
                required = resolve_required_java(destination["version"], destination["loader"])
                started = registry().start_server(destination, required_java_major=required.required_java if required.enforceable else None)
                if started.get("status") != "running":
                    raise RuntimeError("World was copied safely, but destination start failed: " + started.get("message", "unknown error"))
                storage.update_server_status(destination["id"], "running")
            progress.update(job_id, phase="done", result=result)
            return result
    except Exception as exc:
        logger.exception("Instance transfer failed")
        progress.update(job_id, phase="failed", error=str(exc), published=committed)
        raise
    finally:
        if txn is not None:
            # Retain a journal if rollback itself failed: start remains blocked,
            # and startup recovery can retry without losing the old world.
            if kind == "fork":
                if not committed:
                    current = storage.get_server(server_id)
                    if final and current and current.get("installPath") == str(final):
                        storage.delete_server(server_id)
                    if final and final.exists():
                        _remove(final)
                _remove(txn)
            elif journal is None or committed:
                _remove(txn)


def enqueue(kind, source_id, data, owner):
    job_id = progress.generate_job_id("transfer")
    progress.update(job_id, phase="queued", kind=kind, sourceId=source_id,
                    destinationId=data.get("destinationId"), restart=data.get("restart", False), owner=owner)
    def worker():
        try:
            run(kind, source_id, data, job_id)
        except Exception:
            pass  # run logs the exception and records the explicit failed state.
    threading.Thread(target=worker, name=job_id, daemon=True).start()
    return job_id


def recover():
    """Recover only bounded, validated journals, before schedulers/autostart."""
    for server in storage.get_all_servers():
        raw = Path(server.get("installPath") or server["id"]).expanduser()
        raw = raw if raw.is_absolute() else registry().base_dir / raw
        if not (raw / ".fabricator-transfer").exists():
            continue
        root = _literal_root(server)
        txn = root / ".fabricator-transfer"
        if not txn.exists():
            continue
        with locked([server["id"]]):
            if txn.is_symlink():
                raise ValueError("Transfer journal directory is a symlink; manual recovery required")
            _scan(txn)
            path = txn / "journal.json"
            if path.exists():
                journal = json.loads(path.read_text())
                names = journal.get("names")
                existing = journal.get("existing")
                if (not isinstance(names, list) or not 1 <= len(names) <= 3
                        or not isinstance(existing, list) or type(journal.get("committed")) is not bool
                        or any(name not in names for name in existing)):
                    raise ValueError("Invalid transfer journal; manual recovery required")
                for name in names:
                    worlds.safe_name(name)
                if not journal.get("committed"):
                    _rollback(root, txn, journal)
            _remove(txn)
    for txn in registry().base_dir.glob(".fork-srv_*"):
        if not re.fullmatch(r"\.fork-srv_[0-9a-f]{8}", txn.name) or txn.is_symlink():
            continue
        server_id = txn.name.removeprefix(".fork-")
        path = txn / "journal.json"
        _scan(txn)
        journal = json.loads(path.read_text()) if path.exists() else {}
        if path.exists() and (journal.get("id") != server_id or type(journal.get("committed")) is not bool):
            raise ValueError("Invalid fork journal; manual recovery required")
        if not journal.get("committed"):
            current = storage.get_server(server_id)
            if current and current.get("installPath") == str(registry().base_dir / server_id):
                storage.delete_server(server_id)
            _remove(registry().base_dir / server_id)
        _remove(txn)
