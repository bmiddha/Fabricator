"""Read-only S3 discovery and transactional creation of stopped instances."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import struct
import threading
import time
import uuid
import zipfile
from ruamel.yaml.error import YAMLError

from backend.backups import destinations, portable, progress
from backend.server import importer, storage, transfers, worlds
from backend.server.java_compat import resolve_required_java


logger = logging.getLogger(__name__)
_lock = threading.RLock()
_slots = threading.BoundedSemaphore(2)
_TTL = 3600
_META_LIMIT = 64 * 1024
_WARNINGS = [
    "Imported jars, mods, plugins and configuration are trusted code. Nothing is started or installed automatically.",
    "The source backup may contain credentials, player identities, plugin privileges, native binaries, external database links and custom listeners. Review these before starting; secret detection is not exhaustive.",
    "Instance IDs, game/query/RCON ports, administration, map consent, DNS/tunnel bindings and Fabricator grants are not inherited. Known listeners are disabled; custom plugin listeners require manual review.",
    "Java is not downloaded or changed. Check the target Java runtime and platform-specific mod/plugin dependencies before starting.",
]


def connection(value):
    destination = destinations.normalise(value)
    if destination["type"] != "s3":
        raise ValueError("Instance recovery requires an S3 destination")
    if destinations._profile(destination, None).get("allowImport") is not True:
        raise destinations.DestinationError("S3 profile must explicitly enable allowImport for instance recovery")
    return destination


def _namespace(destination):
    return "/".join(filter(None, (destination["prefix"], "fabricator"))) + "/"


def _identity(destination, key):
    if not isinstance(key, str) or not key.startswith(_namespace(destination)):
        raise ValueError("Select a recorded Fabricator object in the configured namespace")
    parts = key[len(_namespace(destination)):].split("/")
    if (len(parts) != 3
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", p) for p in parts[:2])
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}\.tar", parts[2])):
        raise ValueError("Object is not a supported Fabricator archive")
    return parts


def _load(client, destination, key):
    from botocore.exceptions import ClientError

    server_id, snapshot_id, filename = _identity(destination, key)
    head = client.head_object(Bucket=destination["bucket"], Key=key)
    fields = head.get("Metadata", {})
    size = head["ContentLength"]
    digest = fields.get("sha256", "")
    if (fields.get("fabricator-format") not in {"1", "2"}
            or fields.get("server-id") != server_id or fields.get("snapshot-id") != snapshot_id
            or not re.fullmatch(r"[a-f0-9]{64}", digest) or type(size) is not int
            or not 0 < size <= portable.byte_limit()):
        raise ValueError("Object is not a supported, checksummed Fabricator backup")
    metadata = None
    try:
        response = client.get_object(Bucket=destination["bucket"], Key=key + ".fabricator.json")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
            raise
    else:
        body = response["Body"]
        try:
            raw = body.read(_META_LIMIT + 1)
        finally:
            body.close()
        if len(raw) > _META_LIMIT:
            raise ValueError("Fabricator recovery metadata exceeds the size limit")
        try:
            metadata = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("Fabricator recovery metadata is invalid JSON") from None
        if (not isinstance(metadata, dict) or metadata.get("format") != "fabricator-backup"
                or type(metadata.get("version")) is not int or metadata.get("version") not in {1, 2}):
            raise ValueError("Unsupported Fabricator recovery metadata version")
        snapshot = metadata.get("snapshot")
        if (not isinstance(snapshot, dict) or snapshot.get("id") != snapshot_id
                or snapshot.get("serverId") != server_id or snapshot.get("objectKey") != key
                or snapshot.get("fileName") != filename or snapshot.get("sizeBytes") != size
                or snapshot.get("sha256") != digest):
            raise ValueError("Fabricator sidecar does not match the recorded archive")
    snapshot = {
        "id": snapshot_id, "serverId": server_id, "fileName": filename, "objectKey": key,
        "sizeBytes": size, "sha256": digest, "destination": destination, "filePath": None,
        "createdAt": fields.get("created-at", ""),
    }
    recovery = metadata.get("recovery") if metadata and metadata["version"] == 2 else None
    if recovery is not None and (not isinstance(recovery, dict)
            or recovery.get("format") != "fabricator-instance"
            or type(recovery.get("version")) is not int or recovery.get("version") != 1):
        raise ValueError("Unsupported portable instance manifest")
    return snapshot, recovery


def discover(value, cursor=None):
    destination = connection(value)
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 8192):
        raise ValueError("Invalid S3 discovery cursor")
    options = {"Bucket": destination["bucket"], "Prefix": _namespace(destination), "MaxKeys": 100}
    if cursor:
        options["ContinuationToken"] = cursor
    items, rejected = [], 0
    with destinations._client(destination, None) as client:
        response = client.list_objects_v2(**options)
        for entry in response.get("Contents", []):
            key = entry["Key"]
            if not key.endswith(".tar"):
                continue
            try:
                snapshot, recovery = _load(client, destination, key)
            except ValueError:
                rejected += 1
                continue
            portable_server = None
            if recovery and not recovery.get("requiresOperatorMetadata"):
                try:
                    portable_server = portable.server_metadata(recovery.get("server"))
                except ValueError:
                    rejected += 1
                    continue
            items.append({
                "objectKey": key, "snapshotId": snapshot["id"], "sourceServerId": snapshot["serverId"],
                "createdAt": snapshot["createdAt"], "sizeBytes": snapshot["sizeBytes"],
                "legacy": portable_server is None,
                "loader": portable_server["loader"] if portable_server else None,
                "version": portable_server["version"] if portable_server else None,
            })
    return {"snapshots": items, "cursor": response.get("NextContinuationToken"), "rejectedObjects": rejected}


def _base():
    root = transfers.registry().base_dir
    root.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Recovery storage must not contain symlinks")
    return root


def _preview_path(token):
    if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
        raise ValueError("Recovery preview not found")
    return _base() / (".s3-preview-" + token)


def _receipt(token, owner):
    root = _preview_path(token)
    if root.is_symlink() or not (root / "receipt.json").is_file():
        raise ValueError("Recovery preview is missing; inspect the backup again")
    path = root / "receipt.json"
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Recovery preview receipt is invalid")
    receipt = json.loads(path.read_text())
    if receipt.get("owner") != owner:
        raise ValueError("Recovery preview not found")
    if receipt.get("expiresAt", 0) <= time.time():
        raise ValueError("Recovery preview expired; inspect the backup again")
    return root, receipt


def preview_status(token, owner):
    _, receipt = _receipt(token, owner)
    return receipt["preview"]


def discard(token, owner):
    with _lock:
        root, _ = _receipt(token, owner)
        _check_preview(root)
        transfers._remove(root)


def _check_preview(root):
    for child in root.iterdir():
        if child.is_symlink():
            raise ValueError("Recovery staging contains a symlink; manual cleanup is required")
        if child.is_dir():
            transfers._scan(child)
        elif not child.is_file() or child.stat().st_nlink != 1:
            raise ValueError("Recovery staging contains an unsafe file; manual cleanup is required")


def _validate_instance(server, root, source_platform, explicit):
    for relative in ("server.properties", "bukkit.yml", "paper.yml", "spigot.yml", "config/paper-global.yml"):
        path = root / relative
        if path.exists() and (not path.is_file() or path.stat().st_size > 1024 * 1024):
            raise ValueError("Instance control configuration exceeds the 1 MiB inspection limit")
    launch = server["launch"]
    target = root / (launch.get("jar") or launch["args_file"])
    if not target.is_file():
        raise ValueError("Launch target is missing from the backup; supply the correct legacy launch path")
    if launch["type"] == "jar":
        if target.stat().st_size > 512 * 1024 * 1024:
            raise ValueError("Recovery launch jar exceeds the 512 MiB inspection limit")
        with target.open("rb") as handle:
            handle.seek(max(0, target.stat().st_size - 65557))
            tail = handle.read(65557)
        end = tail.rfind(b"PK\x05\x06")
        if (end < 0 or len(tail) - end < 22 or b"PK\x06\x07" in tail[max(0, end - 20):end]
                or struct.unpack_from("<I", tail, end + 12)[0] > 16 * 1024 * 1024):
            raise ValueError("Launch jar has an unsupported or oversized ZIP directory")
        if not zipfile.is_zipfile(target):
            raise ValueError("Launch target is not a readable Java jar")
        with zipfile.ZipFile(target) as jar:
            if len(jar.infolist()) > 100_000:
                raise ValueError("Launch jar has too many entries")
            if "version.json" in jar.namelist() and jar.getinfo("version.json").file_size > 64 * 1024:
                raise ValueError("Launch jar version metadata exceeds the size limit")
        detected_version = importer._jar_mc_version(target)
        if detected_version and detected_version != server["version"]:
            raise ValueError("Launch jar and selected Minecraft version do not match")
    else:
        if source_platform not in (None, "windows" if os.name == "nt" else "posix") and not explicit:
            raise ValueError("Argument-file launch is platform-specific; supply the target platform launch path explicitly")
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("Launch argument file exceeds the size limit")
        text = target.read_text()
        if re.search(r"(?:^|\s)@|[A-Za-z]:[\\/]|(?:^|[=;\s])[/\\]|(?:^|[/\\])\.\.(?:[/\\]|$)", text):
            raise ValueError("Launch argument file contains host paths or nested includes; manual migration is required")
    try:
        discovered = worlds.discover(server, root)
    except YAMLError:
        raise ValueError("Invalid world-layout YAML; repair the archive offline before recovery") from None
    worlds.require_supported_worlds(root, discovered)
    return discovered


def _sanitize(root, server, name, groups):
    excluded = transfers._ADMIN | {
        ".env", "auth.json", "servers.json", "frpc.toml", "frpc.ini", "frpc.yml",
        "frp", "frpc", "playit", "backups", "logs", "crash-reports", "velocity.toml",
        "forwarding.secret", ".fabricator-map.json", ".fabricator-transfer",
    }
    entries, _ = transfers._scan(root)
    removed = []
    for relative in entries:
        parts = Path(relative).parts
        if (parts[0] in excluded or parts[0].startswith((".env", ".fabricator-", ".fork-", ".s3-preview-"))
                or relative == "config/fabricator-map.json"):
            path = root / relative
            if path.exists():
                transfers._remove(path) if path.is_dir() else path.unlink()
                removed.append(relative)
    # Reuse the same property, proxy and BlueMap consent resets as a local fork.
    try:
        record = transfers._fork_record(server, root, root, {"name": name, "worlds": groups}, "unregistered")
    except YAMLError:
        raise ValueError("Invalid proxy YAML; repair the archive offline before recovery") from None
    record["jvmArgs"] = server["jvmArgs"]
    return record, removed


def _secrets(root):
    total = 0
    suspected = []
    truncated = False
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".toml", ".yml", ".yaml", ".conf", ".properties", ".txt"}:
            continue
        size = path.stat().st_size
        if size > 1024 * 1024 or total + size > 16 * 1024 * 1024:
            truncated = True
            continue
        total += size
        text = path.read_bytes().decode("utf-8", errors="replace")
        if re.search(r'(?im)["\']?(?:password|passwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret)'
                     r'["\']?[ \t]*[:=][ \t]*["\']?[^"\';\s#}\]]+', text):
            suspected.append(path.relative_to(root).as_posix())
    return {"suspectedFiles": suspected[:100], "suspectedCount": len(suspected),
            "scanTruncated": truncated}


def prepare(data, owner=None, job_id=None):
    job_id = job_id or progress.generate_job_id("s3recovery")
    progress.update(job_id, kind="s3-preview", phase="queued", owner=owner)
    destination = connection(data.get("destination"))
    name = worlds.safe_name(data.get("name"))
    if any(s["name"].casefold() == name.casefold() for s in storage.get_all_servers()):
        raise ValueError("Choose a new instance name; recovery never overwrites an existing instance")
    with destinations._client(destination, None) as client:
        snapshot, recovery = _load(client, destination, data.get("objectKey"))
    explicit = data.get("legacy") is not None
    if not recovery or recovery.get("requiresOperatorMetadata"):
        if not explicit:
            raise ValueError("Legacy backup requires explicit original loader, version, build and launch inputs")
    server = portable.server_metadata(data["legacy"] if explicit else recovery.get("server"))
    token = uuid.uuid4().hex
    root = _preview_path(token)
    transfers._space(_base(), snapshot["sizeBytes"])
    root.mkdir(mode=0o700)
    stage = root / "instance"
    stage.mkdir()
    try:
        with destinations.materialise(snapshot, job_id, directory=root, import_operation=True) as archive:
            progress.update(job_id, phase="inspecting")
            metrics = portable.inspect_archive(archive)
            transfers._space(root, metrics["expandedBytes"])
            progress.update(job_id, phase="extracting")
            portable.extract_archive(archive, stage)
        groups = _validate_instance(server, stage, recovery.get("platform") if recovery else None, explicit)
        record, removed = _sanitize(stage, server, name, groups)
        secrets = _secrets(stage)
        entries, size = transfers._scan(stage)
        hashes = transfers._manifest(stage, entries)
        transfers._sync_tree(stage, entries)
        preview = {
            "previewId": token, "name": name, "sourceServerId": snapshot["serverId"],
            "snapshotId": snapshot["id"], "archiveBytes": snapshot["sizeBytes"],
            "expandedBytes": size, "entries": len(entries), "sha256": snapshot["sha256"],
            "loader": server["loader"], "version": server["version"], "loaderVersion": server["loaderVersion"],
            "java": resolve_required_java(server["version"], server["loader"]).to_dict(),
            "worlds": [w["name"] for w in groups], "legacy": explicit,
            "removedAdministrativeFiles": len(removed), "secrets": secrets, "warnings": _WARNINGS,
            "expiresAt": time.time() + _TTL,
        }
        receipt = {
            "owner": owner, "expiresAt": preview["expiresAt"], "preview": preview,
            "record": record, "hashes": hashes, "destination": destination,
        }
        if len(json.dumps(receipt).encode()) > 32 * 1024 * 1024:
            raise ValueError("Recovery file manifest exceeds the bounded preview size")
        transfers._write_json(root / "receipt.json", receipt)
        progress.update(job_id, phase="done", result=preview)
        return preview
    except Exception:
        transfers._remove(root)
        raise


def import_instance(data, owner=None, job_id=None):
    job_id = job_id or progress.generate_job_id("s3recovery")
    progress.update(job_id, kind="s3-import", phase="queued", owner=owner)
    with _lock:
        root, receipt = _receipt(data.get("previewId"), owner)
        connection(receipt["destination"])
        preview = receipt["preview"]
        if data.get("acknowledgeRisks") is not True or data.get("confirmName") != preview["name"]:
            raise ValueError("Confirm the inspected instance name and trusted-content risks explicitly")
        if ((preview["secrets"]["suspectedCount"] or preview["secrets"]["scanTruncated"])
                and data.get("retainEmbeddedSecrets") is not True):
            raise ValueError("Explicitly acknowledge retained or uninspected plugin/configuration secrets")
        stage = root / "instance"
        entries, size = transfers._scan(stage)
        if transfers._manifest(stage, entries) != receipt["hashes"]:
            raise ValueError("Inspected files changed; inspect the S3 backup again")
        transfers._space(_base(), size)
        server_id = storage.generate_server_id()
        target = _base() / server_id
        txn = _base() / (".fork-" + server_id)
        if target.exists() or target.is_symlink() or txn.exists() or txn.is_symlink():
            raise ValueError("Recovery identity collision; retry without overwriting any instance")
        record = {**receipt["record"], "id": server_id, "installPath": str(target),
                  "status": "stopped", "autoStart": "never"}
        txn.mkdir(mode=0o700)
        committed = False
        try:
            transfers._write_json(txn / "journal.json", {"id": server_id, "committed": False})
            transfers._copy(stage, txn / "stage", entries, job_id, lambda _: False)
            copied = txn / "stage"
            groups = worlds.discover(record, copied)
            record = transfers._fork_record(record, copied, copied,
                                             {"name": preview["name"], "worlds": groups}, server_id)
            record["jvmArgs"] = receipt["record"]["jvmArgs"]
            copied_entries, _ = transfers._scan(copied)
            copied.chmod(0o700)
            transfers._sync_tree(copied, copied_entries)
            progress.update(job_id, phase="registering")
            os.replace(txn / "stage", target)
            transfers._sync_dir(_base())
            storage.register_fork(record)
            transfers._write_json(txn / "journal.json", {"id": server_id, "committed": True})
            committed = True
            result = {"serverId": server_id, "name": record["name"], "status": "stopped"}
            progress.update(job_id, phase="cleaning_up", result=result, published=True, server_id=server_id)
            transfers._remove(txn)
            transfers._remove(root)
            progress.update(job_id, phase="done", result=result, server_id=server_id)
            return result
        except Exception:
            if not committed:
                current = storage.get_server(server_id)
                if current and current.get("installPath") == str(target):
                    storage.delete_server(server_id)
                if target.exists():
                    transfers._remove(target)
                transfers._remove(txn)
            raise


def enqueue(kind, data, owner):
    if _lock.acquire(blocking=False):
        try:
            recover(cleanup_incomplete=False)
        finally:
            _lock.release()
    if not _slots.acquire(blocking=False):
        raise ValueError("Two recovery jobs are already active; wait before submitting another")
    job_id = progress.generate_job_id("s3recovery")
    progress.update(job_id, phase="queued", kind=kind, owner=owner)
    def worker():
        try:
            (prepare if kind == "s3-preview" else import_instance)(data, owner, job_id)
        except (ValueError, destinations.DestinationError) as exc:
            progress.update(job_id, phase="failed", error=str(exc))
        except Exception:
            logger.exception("S3 recovery worker failed")
            published = progress.get(job_id).get("published")
            progress.update(job_id, phase="failed", error=(
                "The stopped instance was registered, but cleanup failed. Restart Fabricator to finish "
                "journal recovery; do not retry creation." if published else
                "Recovery failed. Check local storage and panel logs. If registration was interrupted, "
                "restart Fabricator to recover the journal before starting or retrying."
            ))
        finally:
            _slots.release()
    try:
        threading.Thread(target=worker, name=job_id, daemon=True).start()
    except Exception:
        _slots.release()
        progress.update(job_id, phase="failed", error="Recovery worker could not start; retry the operation")
        raise
    return job_id


def recover(*, cleanup_incomplete=True):
    """Keep ready, owner-bound previews; remove interrupted/expired staging."""
    for root in _base().glob(".s3-preview-*"):
        if not re.fullmatch(r"\.s3-preview-[a-f0-9]{32}", root.name) or root.is_symlink():
            continue
        path = root / "receipt.json"
        if not path.exists():
            if not cleanup_incomplete:
                continue
            _check_preview(root)
            transfers._remove(root)
            continue
        if path.is_symlink():
            raise ValueError("Invalid S3 recovery receipt; manual recovery required")
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Invalid S3 recovery receipt; manual recovery required")
        receipt = json.loads(path.read_text())
        if receipt.get("expiresAt", 0) <= time.time():
            _check_preview(root)
            transfers._remove(root)
