"""Persistent, byte-bound modpack reviews and stopped-only promotion.

Only mods/config/defaultconfigs are promoted. A staging world is disposable,
never a source of production world data. Runtime migrations are not inferred.
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
import zipfile

from backend.managed import is_managed
from backend.modrinth import client_pack, mrpack, pending
from backend.server import storage, transfers
from backend.server.java_compat import resolve_required_java

logger = logging.getLogger(__name__)
SCOPES = ("mods", "config", "defaultconfigs")
LOADERS = {"fabric", "quilt", "forge", "neoforge"}
BUSY = {"preparing", "staging", "validating", "promoting"}
WARNINGS = [
    "Pack code runs with panel OS permissions, not in a sandbox. Review credentials, external databases and extra listeners before running staging.",
    "Startup checks readiness and shutdown only, not gameplay, migrations or complete dependency compatibility.",
    "Staging worlds never replace production. After a world migration, restore the matching world/config backup; older jars alone are not a rollback.",
    "Local files are preserved; review retained mods for dependency conflicts.",
    "Existing client exports and shares stay unchanged. Distribute a new export after promotion.",
]


class StalePlan(ValueError):
    pass


def _directory(server_id):
    return storage._resolve_servers_file().parent / "modpack-updates" / hashlib.sha256(server_id.encode()).hexdigest()


def _path(server_id, plan_id):
    if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
        raise ValueError("Invalid update plan")
    return _directory(server_id) / plan_id


def _write(directory, plan):
    client_pack._atomic_json(directory / "plan.json", plan)


def read(server_id, plan_id):
    path = _path(server_id, plan_id) / "plan.json"
    if not path.is_file():
        raise ValueError("Update plan not found")
    return json.loads(path.read_text(encoding="utf-8"))


def list_plans(server_id, owner):
    directory = _directory(server_id)
    if not directory.exists():
        return []
    return sorted(
        [value for path in directory.glob("*/plan.json")
         if (value := json.loads(path.read_text(encoding="utf-8"))).get("owner") == owner],
        key=lambda value: value["createdAt"], reverse=True,
    )


def _signature(server):
    return {key: copy.deepcopy(server.get(key)) for key in (
        "id", "installPath", "loader", "version", "loaderVersion", "launch",
        "command", "modpack", "modContent",
    )}


def _files(root, *, staging=False):
    values = {}
    for folder in SCOPES:
        path = root / folder
        if path.exists() or path.is_symlink():
            entries, _ = transfers._scan(path)
            values.update({folder + "/" + rel: transfers._digest(path / rel)
                           for rel, size in entries.items() if size is not None
                           and (not staging or folder + "/" + rel not in transfers._MAP_CONTROLS)})
    return values


def _runtime(root):
    folders = {"libraries", "versions", ".fabric", ".quilt"}
    files = {"server.properties", "eula.txt", "fabric-server-launcher.properties", "quilt-server-launcher.properties"}
    entries, _ = transfers._scan(root, lambda rel: rel.split("/")[0] not in folders | files
                                and not rel.split("/")[0].endswith(".jar"))
    return transfers._manifest(root, entries)


def _config_diff(root, incoming, rel, old, new):
    before, after = root / rel, incoming / rel
    if any(path.exists() and path.stat().st_size > 16 * 1024 for path in (before, after)):
        return ""
    try:
        left = before.read_text(encoding="utf-8").splitlines(True) if old else []
        right = after.read_text(encoding="utf-8").splitlines(True) if new else []
    except UnicodeError:
        return ""
    diff = "".join(difflib.unified_diff(left, right, fromfile="current/" + rel, tofile="proposed/" + rel))
    return diff[:32 * 1024]


def _server(server_id):
    if is_managed():
        raise ValueError("Staged modpack updates are disabled in managed mode")
    server, root = transfers._server(server_id)
    if server.get("loader") not in LOADERS:
        raise ValueError("Staged updates support Fabric, Quilt, Forge and NeoForge instances")
    if (root / ".fabricator-update").exists():
        raise ValueError("An interrupted modpack update requires recovery")
    if (root / ".fabricator-staging").exists():
        raise ValueError("Staging setup was interrupted; discard this staging instance and prepare a fresh review")
    return server, root


def _known_files(server, root):
    known = dict((server.get("modpack") or {}).get("updateFiles") or {})
    previous = client_pack.source(root)
    server_files = previous.get("server_files")
    for entry in previous.get("index", {}).get("files", []):
        rel = client_pack.relative_path(entry["path"])
        if rel.split("/")[0] not in SCOPES or server_files is None or rel not in server_files:
            continue
        file = root / rel
        if file.is_file():
            hashes = entry.get("hashes") or {}
            algorithm = "sha512" if hashes.get("sha512") else "sha1"
            if hashes.get(algorithm):
                actual = hashlib.new(algorithm, file.read_bytes()).hexdigest()
                if actual == hashes[algorithm]:
                    known.setdefault(rel, transfers._digest(file))
    return known


def _archive_scope(archive):
    index = mrpack.read_index(archive)
    paths = []
    for entry in index.get("files") or []:
        if not isinstance(entry, dict):
            raise ValueError("Invalid pack file entry")
        rel = client_pack.relative_path(entry.get("path"))
        if (entry.get("env") or {}).get("server") != "unsupported":
            hashes = entry.get("hashes") or {}
            if not any(isinstance(hashes.get(key), str) and re.fullmatch(pattern, hashes[key])
                       for key, pattern in (("sha512", r"[a-fA-F0-9]{128}"), ("sha1", r"[a-fA-F0-9]{40}"))):
                raise ValueError(f"Pack file has no verifiable hash: {rel}")
            paths.append(rel)
    with zipfile.ZipFile(archive) as pack:
        for info in pack.infolist():
            for prefix in ("overrides/", "server-overrides/"):
                if info.filename.startswith(prefix) and not info.is_dir():
                    paths.append(client_pack.relative_path(info.filename[len(prefix):]))
    for rel in paths:
        if rel.split("/")[0] not in SCOPES or rel in transfers._MAP_CONTROLS:
            raise ValueError(f"Pack changes unsupported or host-controlled content: {rel}. Use a separate manual migration.")


def _dependency_review(root):
    """Conservative checks, not a dependency/version solver."""
    identifiers, required, conflicts = {}, [], []
    mods = root / "mods"
    for jar in sorted(mods.glob("*.jar")) if mods.exists() else []:
        try:
            with zipfile.ZipFile(jar) as archive:
                if "fabric.mod.json" not in archive.namelist():
                    continue
                if archive.getinfo("fabric.mod.json").file_size > 1024 * 1024:
                    raise ValueError("Mod metadata exceeds 1 MiB")
                meta = json.loads(archive.read("fabric.mod.json"))
                identifier = meta.get("id")
                if not isinstance(identifier, str):
                    raise ValueError(f"Invalid Fabric mod identifier in {jar.name}")
                if identifier in identifiers:
                    conflicts.append(f"Duplicate mod id {identifier}: {identifiers[identifier]}, {jar.name}")
                identifiers[identifier] = jar.name
                provides, depends = meta.get("provides") or [], meta.get("depends") or {}
                if not isinstance(provides, list) or not isinstance(depends, dict):
                    raise ValueError(f"Malformed dependency metadata in {jar.name}")
                for alias in provides:
                    identifiers.setdefault(str(alias), jar.name)
                required.extend((jar.name, dep, str(version)) for dep, version in depends.items())
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
            raise ValueError(f"Unreadable mod metadata: {jar.name}") from exc
    builtin = {"minecraft", "java", "fabricloader", "quilt_loader"}
    for filename, dep, version in required:
        if dep not in identifiers and dep not in builtin:
            # Nested jars can provide dependencies; absence is a review item,
            # not an assertion that Minecraft's loader cannot resolve it.
            conflicts.append(f"{filename} requires {dep} {version}; not found in top-level Fabric mod metadata (may be nested)")
    return conflicts


def create(server_id, data, owner):
    with transfers.locked([server_id]):
        _server(server_id)
        plans = list_plans(server_id, owner)
        if any(plan["status"] in BUSY for plan in plans):
            raise ValueError("An update operation is already in progress")
        if len(plans) >= 5:
            raise ValueError("Delete an old update plan before preparing another (limit five)")
        source = data.get("source")
        if source not in {"upload", "project"}:
            raise ValueError("Choose an uploaded .mrpack or Modrinth project")
        key = "upload_id" if source == "upload" else "project_id"
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError("Pack source is required")
        replacements = data.get("replaceFiles", [])
        if not isinstance(replacements, list) or any(not isinstance(p, str) for p in replacements):
            raise ValueError("replaceFiles must be a list of reviewed paths")
        plan_id = uuid.uuid4().hex
        plan = {"id": plan_id, "serverId": server_id, "owner": owner, "createdAt": time.time(),
                "status": "preparing", "warnings": WARNINGS, "replaceFiles": replacements,
                "intent": {"source": source, key: data[key].strip()}}
        directory = _path(server_id, plan_id)
        _write(directory, plan)
        return plan


def prepare(server_id, plan_id):
    directory = _path(server_id, plan_id)
    plan = read(server_id, plan_id)
    from backend.modrinth.client import ModrinthClient
    client = ModrinthClient()
    prepared = pending.prepare(client, plan["intent"])
    try:
        _archive_scope(prepared.archive)
        summary = mrpack.describe(mrpack.read_index(prepared.archive))
        with transfers.locked([server_id]):
            server, root = _server(server_id)
            baseline = _files(root)
            signature = _signature(server)
            runtime = _runtime(root)
            known = _known_files(server, root)
            source_metadata = client_pack.source(root)
        incoming = directory / "incoming"
        incoming.mkdir()
        result = client.install_mrpack_archive(prepared.archive, incoming, loader=summary["loader"], allow_missing=False)
        if result.get("missing_files") or result.get("uncertain_mod_files"):
            raise ValueError("Pack contains missing or unclassified files; resolve the pack before staging")
        offered = _files(incoming)
        candidate = directory / "candidate"
        candidate.mkdir()
        with transfers.locked([server_id]):
            current, root = _server(server_id)
            if _files(root) != baseline or _signature(current) != signature or _runtime(root) != runtime:
                raise StalePlan("Production inputs changed during preparation; prepare a new plan")
            for folder in SCOPES:
                if (root / folder).exists():
                    shutil.copytree(root / folder, candidate / folder)
        changes, local = [], []
        diff_bytes = 0
        for rel in sorted(set(baseline) | set(offered)):
            old, new = baseline.get(rel), offered.get(rel)
            if old == new:
                continue
            owned = rel in known and known[rel] == old
            if new is None and not owned:
                local.append(rel)
                continue
            conflict = old is not None and not owned
            replace = rel in plan["replaceFiles"]
            action = "preserve" if conflict and not replace else ("remove" if new is None else "add" if old is None else "replace")
            change = {"path": rel, "action": action, "conflict": conflict,
                      "before": old, "after": new, "category": "mod" if rel.startswith("mods/") else "config"}
            if change["category"] == "config":
                diff = _config_diff(root, incoming, rel, old, new) if diff_bytes < 256 * 1024 else ""
                change["diff"] = diff[:256 * 1024 - diff_bytes]
                diff_bytes += len(change["diff"])
            changes.append(change)
            if action == "preserve":
                local.append(rel)
                continue
            target = candidate / rel
            if new is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(incoming / rel, target)
        unknown = set(plan["replaceFiles"]) - {c["path"] for c in changes if c["conflict"]}
        if unknown:
            raise ValueError("Replacement choices no longer match conflicting files")
        loader_changes = []
        for field, offered_value in (("version", summary["minecraft_version"]), ("loader", summary["loader"]),
                                     ("loaderVersion", summary["loader_version"])):
            if not offered_value or str(server.get(field) or "") != offered_value:
                loader_changes.append({"field": field, "current": server.get(field), "proposed": offered_value})
        result.update(prepared.identity)
        record = pending.modpack_record(result)
        candidate_files = _files(candidate)
        # A preserved local collision is not newly owned by the pack.
        record["updateFiles"] = {rel: digest for rel, digest in offered.items()
                                 if candidate_files.get(rel) == digest}
        with transfers.locked([server_id]):
            current, root = _server(server_id)
            if (_files(root) != baseline or _signature(current) != signature or _runtime(root) != runtime
                    or client_pack.source(root) != source_metadata):
                raise StalePlan("Production inputs changed during preparation; prepare a new plan")
            plan.update(status="review", summary=summary, baseline=baseline, signature=signature,
                        sourceMetadata=source_metadata, candidateFiles=candidate_files, runtime=runtime,
                        clientMetadata=client_pack.source(incoming), modpack=record,
                        modContent={name: value for name, value in (signature["modContent"] or {}).items()
                                    if baseline.get("mods/" + name) == candidate_files.get("mods/" + name)
                                    and "mods/" + name in candidate_files},
                        changes=changes, preservedFiles=local, loaderChanges=loader_changes,
                        dependencyReview=_dependency_review(candidate),
                        clientImpact={"previousFiles": len(source_metadata.get("index", {}).get("files", [])),
                                      "proposedFiles": summary["file_count"], "newExportRequired": True})
            plan["reviewToken"] = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
            _write(directory, plan)
    finally:
        prepared.cleanup()


def _check(plan):
    server, root = _server(plan["serverId"])
    if (_signature(server) != plan["signature"] or _files(root) != plan["baseline"] or _runtime(root) != plan["runtime"]
            or client_pack.source(root) != plan["sourceMetadata"]):
        raise StalePlan("Production mods, configs, runtime or pack metadata changed; prepare a new plan")
    if _files(_path(plan["serverId"], plan["id"]) / "candidate") != plan["candidateFiles"]:
        raise StalePlan("Reviewed candidate bytes changed; prepare a new plan")
    if plan["loaderChanges"]:
        raise ValueError("Minecraft/loader changes need a separate runtime migration; this plan cannot be promoted")
    return server, root


def stage(server_id, plan_id, data):
    plan = read(server_id, plan_id)
    directory = _path(server_id, plan_id)
    with transfers.locked([server_id]):
        _check(plan)
        if plan.get("stagingId"):
            raise ValueError("This plan already has a staging instance")
        options = {"name": data.get("name"), "includeWorlds": True, "includePlayerData": False,
                   "acknowledgeRisks": True}
        options["previewToken"] = transfers.preview("fork", server_id, options)["previewToken"]
        result = transfers.run("fork", server_id, options, audit=False)
        staging_id = result["serverId"]
        # Record the fork immediately so a later copy failure is not orphaned.
        plan.update(stagingId=staging_id, status="staging")
        _write(directory, plan)
        with transfers.locked([staging_id]):
            staging, target = _server(staging_id)
            transfers._write_json(target / ".fabricator-staging", {"planId": plan_id})
            for folder in SCOPES:
                if (target / folder).exists():
                    transfers._remove(target / folder)
                if (directory / "candidate" / folder).exists():
                    shutil.copytree(directory / "candidate" / folder, target / folder)
            # Reapply transfer controls after pack configs, not before.
            groups = transfers.worlds.discover(staging, target)
            isolated = transfers._fork_record(staging, target, target, {"name": staging["name"], "worlds": groups}, staging_id)
            storage.update_server(staging_id, {key: isolated[key] for key in (
                "port", "queryPort", "rconPort", "serverIp", "enableQuery", "enableRcon", "rconPassword",
            )})
            expected = {key: value for key, value in plan["candidateFiles"].items() if key not in transfers._MAP_CONTROLS}
            if _files(target, staging=True) != expected:
                raise ValueError("Transfer isolation changes reviewed configs; reconcile those settings before staging")
            storage.update_server(staging_id, {"modpack": plan["modpack"], "modContent": plan["modContent"], "autoStart": "never"})
            client_pack._atomic_json(client_pack._source_path(target), plan["clientMetadata"])
            plan.update(status="staged", stagingSignature=_signature(storage.get_server(staging_id)),
                        stagingFiles=_files(target), stagingRuntime=_runtime(target))
            _write(directory, plan)
            (target / ".fabricator-staging").unlink()
            transfers._sync_dir(target)


def _check_staging(plan):
    staging_id = plan.get("stagingId")
    if not staging_id or staging_id == plan["serverId"]:
        raise ValueError("Create an independent staging instance first")
    staging, root = _server(staging_id)
    if (_signature(staging) != plan["stagingSignature"] or _files(root) != plan["stagingFiles"]
            or _runtime(root) != plan["stagingRuntime"]
            or staging.get("serverIp") != "127.0.0.1" or staging.get("autoStart") != "never"):
        raise StalePlan("Staging inputs changed; prepare a new review")
    props = transfers.read_properties(root)
    if (props.get("server-ip") != "127.0.0.1" or props.get("enable-rcon") != "false"
            or props.get("enable-query") != "false" or props.get("management-server-enabled") != "false"):
        raise ValueError("Staging listener isolation was changed")
    return staging, root


def validate(server_id, plan_id):
    plan = read(server_id, plan_id)
    staging_id = plan.get("stagingId")
    if not staging_id or staging_id == server_id:
        raise ValueError("Create an independent staging instance first")
    with transfers.locked([server_id, staging_id]):
        _check(plan)
        staging, _ = _check_staging(plan)
        registry = transfers.registry()
        requirement = resolve_required_java(staging["version"], staging["loader"])
        try:
            result = registry.start_server(staging, required_java_major=requirement.required_java if requirement.enforceable else None)
            if result.get("status") != "running":
                raise ValueError("Staging start failed: " + result.get("message", "unknown error"))
            manager = registry._get_or_create_manager(staging)
            deadline = time.monotonic() + 180
            while not manager.startup_ready and time.monotonic() < deadline:
                if registry.get_status(staging_id).get("status") != "running":
                    raise ValueError("Staging exited before Minecraft became ready; inspect its console")
                time.sleep(0.25)
            if not manager.startup_ready or registry.get_status(staging_id).get("status") != "running":
                raise ValueError("No Minecraft readiness signal within 180 seconds; inspect staging console")
        finally:
            stopped = registry.stop_server(staging_id)
            if stopped.get("status") != "stopped" or registry.get_status(staging_id).get("status") != "stopped":
                raise ValueError("Could not stop staging after validation; stop it manually before continuing")
            storage.update_server_status(staging_id, "stopped")
        _check_staging(plan)
        plan.update(status="validated", validatedToken=plan["reviewToken"], validatedAt=time.time())
        _write(_path(server_id, plan_id), plan)


def promote(server_id, plan_id):
    plan = read(server_id, plan_id)
    staging_id = plan.get("stagingId")
    with transfers.locked([server_id, staging_id]):
        _, root = _check(plan)
        if plan.get("validatedToken") != plan["reviewToken"]:
            raise StalePlan("Validate the unchanged staging instance before promotion")
        _check_staging(plan)
        directory = _path(server_id, plan_id)
        entries, size = transfers._scan(root, lambda rel: rel.split("/")[0] == "backups")
        transfers._space(root, size * 3 + len(entries) * 1024 + 10240)
        safety = transfers._safety(server_id, root, purpose="modpack-update")
        _check(plan)
        txn = root / ".fabricator-update"
        txn.mkdir()
        (txn / "old").mkdir()
        (txn / "stage").mkdir()
        journal = {"names": list(SCOPES), "existing": [n for n in SCOPES if (root / n).exists()],
                   "committed": False, "oldModpack": plan["signature"]["modpack"],
                   "oldContent": plan["signature"]["modContent"], "newContent": plan["modContent"],
                   "oldSource": plan["sourceMetadata"], "newModpack": plan["modpack"],
                   "newSource": plan["clientMetadata"], "planId": plan_id, "safetySnapshotId": safety["id"]}
        transfers._write_json(txn / "journal.json", journal)
        try:
            for folder in SCOPES:
                source = directory / "candidate" / folder
                if source.exists():
                    shutil.copytree(source, txn / "stage" / folder)
            staged_entries, _ = transfers._scan(txn / "stage")
            transfers._sync_tree(txn / "stage", staged_entries)
            if _files(txn / "stage") != plan["candidateFiles"]:
                raise ValueError("Candidate copy verification failed")
            transfers._publish_worlds(root, txn, journal)
            if _files(root) != plan["candidateFiles"]:
                raise ValueError("Production publication verification failed")
            _restore_metadata(server_id, root, journal, "new")
            journal["committed"] = True
            transfers._write_json(txn / "journal.json", journal)
        except Exception:
            transfers._rollback(root, txn, journal)
            _restore_metadata(server_id, root, journal, "old")
            transfers._remove(txn)
            raise
        plan.update(status="promoted", safetySnapshotId=safety["id"], promotedAt=time.time())
        _write(directory, plan)
        transfers._remove(txn)
        transfers.registry().invalidate(server_id)


def _restore_metadata(server_id, root, journal, prefix):
    storage.update_server(server_id, {
        "modpack": journal[prefix + "Modpack"], "modContent": journal[prefix + "Content"],
    })
    client_pack._atomic_json(client_pack._source_path(root), journal[prefix + "Source"])


def recover():
    """Recover before schedulers/autostart; a failed recovery blocks app boot."""
    for server in storage.get_all_servers():
        raw = Path(server.get("installPath") or server["id"]).expanduser()
        root = raw if raw.is_absolute() else transfers.registry().base_dir / raw
        txn = root / ".fabricator-update"
        if not txn.exists():
            continue
        root = transfers._literal_root(server)
        transfers._scan(txn)
        journal = json.loads((txn / "journal.json").read_text())
        if (journal.get("names") != list(SCOPES) or type(journal.get("committed")) is not bool
                or not isinstance(journal.get("existing"), list)
                or any(name not in SCOPES for name in journal["existing"])):
            raise ValueError("Invalid modpack update journal; manual recovery required")
        if not journal["committed"]:
            transfers._rollback(root, txn, journal)
        prefix = "new" if journal["committed"] else "old"
        _restore_metadata(server["id"], root, journal, prefix)
        if journal["committed"]:
            directory = _path(server["id"], journal["planId"])
            plan = read(server["id"], journal["planId"])
            plan.update(status="promoted", safetySnapshotId=journal["safetySnapshotId"])
            _write(directory, plan)
        transfers._remove(txn)
    directory = storage._resolve_servers_file().parent / "modpack-updates"
    if directory.exists():
        for path in directory.glob("*/*/plan.json"):
            plan = json.loads(path.read_text(encoding="utf-8"))
            if plan.get("status") in BUSY:
                plan.update(status="failed", error="Panel restarted during this operation; inspect staging and prepare a fresh plan")
                _write(path.parent, plan)


def enqueue(action, server_id, plan_id, data=None):
    from backend.events.store import track_job
    key = f"staged-update:{plan_id}:{action}"
    event_action = "modpacks.update." + action
    track_job(key, server_id, event_action, "queued", ref=plan_id)

    def worker():
        try:
            if action == "stage":
                stage(server_id, plan_id, data)
            else:
                {"prepare": prepare, "validate": validate, "promote": promote}[action](server_id, plan_id)
            plan = read(server_id, plan_id)
            track_job(key, server_id, event_action, "done", ref=plan_id,
                      related_server=plan.get("stagingId") if action in {"stage", "validate"} else None)
        except Exception as exc:
            logger.exception("Staged modpack %s failed", action)
            track_job(key, server_id, event_action, "failed", ref=plan_id)
            plan = read(server_id, plan_id)
            published = plan.get("status") == "promoted"
            recovery_required = False
            server = storage.get_server(server_id)
            if action == "promote" and server:
                txn = transfers._literal_root(server) / ".fabricator-update"
                recovery_required = txn.exists()
                journal = txn / "journal.json"
                if journal.exists():
                    recorded = json.loads(journal.read_text(encoding="utf-8"))
                    published = recorded.get("committed") is True
                    if published:
                        plan["safetySnapshotId"] = recorded["safetySnapshotId"]
            plan.update(status="failed", failedAction=action, error=str(exc),
                        productionChanged=published, recoveryRequired=recovery_required)
            _write(_path(server_id, plan_id), plan)
    try:
        threading.Thread(target=worker, name=f"modpack-{action}-{plan_id}", daemon=True).start()
    except RuntimeError:
        track_job(key, server_id, event_action, "failed", ref=plan_id)
        raise
