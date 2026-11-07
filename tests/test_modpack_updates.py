"""Fixture-only staged updates: no network downloads or real game processes."""
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

from tests.test_transfers import fixture_servers


def mod_jar(identifier="example", **fields):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({
            "schemaVersion": 1, "id": identifier, "version": "2.0", "environment": "server", **fields,
        }))
    return buffer.getvalue()


@pytest.fixture
def update_fixture(fixture_servers, monkeypatch, tmp_path):
    from backend.modrinth import updates, mrpack, client_pack
    from backend.modrinth.client import ModrinthClient
    t, storage, registry, server, root, *_ = fixture_servers
    storage.update_server("source", {"loader": "fabric", "loaderVersion": "0.16.0"})
    (root / "mods").mkdir()
    (root / "mods" / "old.jar").write_bytes(mod_jar("old"))
    (root / "config").mkdir()
    (root / "config" / "pack.json").write_text('{"value":1}\n')
    (root / "config" / "local.json").write_text('{"local":true}\n')
    known = {"mods/old.jar": t._digest(root / "mods" / "old.jar"),
             "config/pack.json": t._digest(root / "config" / "pack.json")}
    storage.update_server("source", {"modpack": {"name": "Old pack", "updateFiles": known}})
    # Keep the real archive extraction/classification and metadata capture;
    # only replace the upstream lookup, so the suite is fully offline.
    monkeypatch.setattr(ModrinthClient, "_apply_modrinth_side_metadata", lambda *a, **k: None)
    monkeypatch.setattr(updates, "resolve_required_java", lambda *a: SimpleNamespace(enforceable=False))
    running = set()
    starts = []

    def start(record, **kwargs):
        starts.append(record["id"])
        running.add(record["id"])
        return {"status": "running"}

    def stop(sid):
        running.discard(sid)
        return {"status": "stopped"}

    monkeypatch.setattr(registry, "start_server", start)
    monkeypatch.setattr(registry, "stop_server", stop)
    monkeypatch.setattr(registry, "get_status", lambda sid: {"status": "running" if sid in running else "stopped"})
    monkeypatch.setattr(registry, "_get_or_create_manager", lambda record: SimpleNamespace(startup_ready=True))

    def prepare(extra=None, *, replacements=None, loader="0.16.0", owner=None):
        archive_path = tmp_path / ("pack-" + str(len(list(tmp_path.glob("pack-*.mrpack")))) + ".mrpack")
        index = {"formatVersion": 1, "game": "minecraft", "versionId": "2", "name": "Updated pack",
                 "dependencies": {"minecraft": "1.21.1", "fabric-loader": loader}, "files": []}
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("modrinth.index.json", json.dumps(index))
            for name, value in {
                "mods/new.jar": mod_jar(), "config/pack.json": '{"value":2}\n', **(extra or {}),
            }.items():
                archive.writestr("overrides/" + name, value)
        pack = mrpack.stage(archive_path, filename="update.mrpack", size_bytes=archive_path.stat().st_size,
                            summary=mrpack.describe(index), owner=owner)
        plan = updates.create("source", {"source": "upload", "upload_id": pack.upload_id,
                                        "replaceFiles": replacements or []}, owner)
        updates.prepare("source", plan["id"])
        return updates.read("source", plan["id"])

    def stage(plan):
        updates.stage("source", plan["id"], {"name": "test-" + plan["id"][:8]})
        return updates.read("source", plan["id"])

    return SimpleNamespace(u=updates, t=t, storage=storage, registry=registry, root=root,
                           prepare=prepare, stage=stage, starts=starts, running=running, cp=client_pack)


def test_end_to_end_preserves_production_world_and_creates_matching_backup(update_fixture):
    f = update_fixture
    initial_world = (f.root / "island" / "region" / "r.0.0.mca").read_bytes()
    plan = f.prepare()
    assert plan["status"] == "review"
    assert {(c["path"], c["action"]) for c in plan["changes"]} == {
        ("mods/old.jar", "remove"), ("mods/new.jar", "add"), ("config/pack.json", "replace"),
    }
    assert '"value":2' in next(c["diff"] for c in plan["changes"] if c["category"] == "config")
    assert "config/local.json" in plan["preservedFiles"]
    plan = f.stage(plan)
    sid = plan["stagingId"]
    staging = f.storage.get_server(sid)
    stage_root = Path(staging["installPath"])
    assert (f.root / "mods" / "old.jar").exists()
    assert (stage_root / "mods" / "new.jar").is_file()
    assert not (stage_root / "mods" / "old.jar").exists()
    assert staging["serverIp"] == "127.0.0.1" and staging["autoStart"] == "never"
    assert not staging["enableRcon"] and not staging["enableQuery"]
    assert f.t.read_properties(stage_root)["server-port"] == str(staging["port"])
    (stage_root / "island" / "region" / "r.0.0.mca").write_bytes(b"stale staging world")
    # Production may legitimately evolve its world between preview and approval.
    (f.root / "island" / "region" / "r.0.0.mca").write_bytes(initial_world + b"production changes")
    f.u.validate("source", plan["id"])
    assert f.starts == [sid] and not f.running
    f.u.promote("source", plan["id"])
    promoted = f.u.read("source", plan["id"])
    assert promoted["status"] == "promoted" and promoted["safetySnapshotId"]
    assert (f.root / "island" / "region" / "r.0.0.mca").read_bytes() == initial_world + b"production changes"
    assert not (f.root / "mods" / "old.jar").exists()
    assert (f.root / "mods" / "new.jar").is_file()
    assert json.loads((f.root / "config" / "pack.json").read_text()) == {"value": 2}
    assert json.loads((f.root / "config" / "local.json").read_text()) == {"local": True}
    from backend.backups import storage as backup_storage
    snapshot = next(s for s in backup_storage.list_snapshots("source") if s["id"] == promoted["safetySnapshotId"])
    with tarfile.open(snapshot["filePath"]) as archive:
        assert archive.extractfile("mods/old.jar").read() == mod_jar("old")
        assert archive.extractfile("island/region/r.0.0.mca").read() == initial_world + b"production changes"
        assert archive.extractfile("config/pack.json").read() == b'{"value":1}\n'
    assert f.cp.source(f.root)["index"]["versionId"] == "2"
    assert not (f.root / ".fabricator-update").exists()


def test_local_conflicts_default_to_preserve_and_explicit_replacement_is_new_review(update_fixture):
    f = update_fixture
    (f.root / "config" / "pack.json").write_text('{"operator":true}\n')
    first = f.prepare()
    change = next(c for c in first["changes"] if c["path"] == "config/pack.json")
    assert change["conflict"] and change["action"] == "preserve"
    second = f.prepare(replacements=["config/pack.json"])
    assert second["reviewToken"] != first["reviewToken"]
    assert next(c for c in second["changes"] if c["path"] == "config/pack.json")["action"] == "replace"
    assert (f.root / "config" / "pack.json").read_text() == '{"operator":true}\n'


@pytest.mark.parametrize("mutation", ["config", "mod", "runtime", "metadata", "source"])
def test_stale_production_inputs_are_rejected(update_fixture, mutation):
    f = update_fixture
    plan = f.prepare()
    if mutation == "config":
        (f.root / "config" / "local.json").write_text("changed")
    elif mutation == "mod":
        (f.root / "mods" / "old.jar").write_bytes(mod_jar("modified"))
    elif mutation == "runtime":
        (f.root / "server.jar").write_bytes(b"different runtime")
    elif mutation == "metadata":
        f.storage.update_server("source", {"loaderVersion": "changed"})
    else:
        f.cp._atomic_json(f.cp._source_path(f.root), {"index": {}})
    with pytest.raises(f.u.StalePlan, match="changed"):
        f.stage(plan)
    assert len(f.storage.get_all_servers()) == 2
    assert not f.starts


def test_loader_change_is_visible_but_cannot_be_staged(update_fixture):
    f = update_fixture
    plan = f.prepare(loader="0.17.0")
    assert plan["loaderChanges"] == [{"field": "loaderVersion", "current": "0.16.0", "proposed": "0.17.0"}]
    with pytest.raises(ValueError, match="migration"):
        f.stage(plan)


@pytest.mark.parametrize("relative", ["server.properties", "world/level.dat", "config/bluemap/webserver.conf", "../escape"])
def test_pack_cannot_replace_worlds_listeners_or_escape_scope(update_fixture, relative):
    with pytest.raises(ValueError):
        update_fixture.prepare({relative: "not allowed"})
    assert not update_fixture.starts


def test_symlinks_fail_closed(update_fixture, tmp_path):
    f = update_fixture
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (f.root / "config" / "linked").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlinks"):
        f.prepare()
    assert outside.read_bytes() == b"outside"


def test_production_must_be_stopped(update_fixture):
    f = update_fixture
    f.running.add("source")
    with pytest.raises(ValueError, match="Stop"):
        f.prepare()
    assert not f.starts


def test_promotion_requires_validation_and_successful_backup(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.stage(f.prepare())
    with pytest.raises(f.u.StalePlan, match="Validate"):
        f.u.promote("source", plan["id"])
    f.u.validate("source", plan["id"])
    monkeypatch.setattr(f.t, "_safety", lambda *a, **kw: (_ for _ in ()).throw(OSError("backup failed")))
    with pytest.raises(OSError, match="backup failed"):
        f.u.promote("source", plan["id"])
    assert (f.root / "mods" / "old.jar").exists()
    assert not (f.root / ".fabricator-update").exists()


def test_validation_never_accepts_exit_or_generated_config_edits(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.stage(f.prepare())
    monkeypatch.setattr(f.registry, "_get_or_create_manager", lambda s: SimpleNamespace(startup_ready=False))
    monkeypatch.setattr(f.u.time, "sleep", lambda _: f.running.clear())
    with pytest.raises(ValueError, match="exited"):
        f.u.validate("source", plan["id"])
    assert not f.running
    staging_root = Path(f.storage.get_server(plan["stagingId"])["installPath"])

    def start(record, **kw):
        f.running.add(record["id"])
        (staging_root / "config" / "generated.json").write_text("{}")
        return {"status": "running"}
    monkeypatch.setattr(f.registry, "start_server", start)
    monkeypatch.setattr(f.registry, "_get_or_create_manager", lambda s: SimpleNamespace(startup_ready=True))
    with pytest.raises(f.u.StalePlan, match="Staging inputs changed"):
        f.u.validate("source", plan["id"])
    assert not f.u.read("source", plan["id"]).get("validatedToken")


@pytest.mark.parametrize("relative", ["server.jar", "server.properties", "libraries/generated.jar"])
def test_startup_runtime_changes_cannot_become_validated_inputs(update_fixture, monkeypatch, relative):
    f = update_fixture
    plan = f.stage(f.prepare())
    staging_root = Path(f.storage.get_server(plan["stagingId"])["installPath"])

    def start(record, **kw):
        f.running.add(record["id"])
        path = staging_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"changed during startup")
        return {"status": "running"}

    monkeypatch.setattr(f.registry, "start_server", start)
    with pytest.raises(f.u.StalePlan, match="Staging inputs changed"):
        f.u.validate("source", plan["id"])
    assert not f.running
    assert not f.u.read("source", plan["id"]).get("validatedToken")
    with pytest.raises(f.u.StalePlan):
        f.u.promote("source", plan["id"])
    assert (f.root / "mods" / "old.jar").exists()


def test_staging_changes_invalidate_successful_validation(update_fixture):
    f = update_fixture
    plan = f.stage(f.prepare())
    f.u.validate("source", plan["id"])
    stage_root = Path(f.storage.get_server(plan["stagingId"])["installPath"])
    (stage_root / "mods" / "new.jar").write_bytes(mod_jar("changed"))
    with pytest.raises(f.u.StalePlan):
        f.u.promote("source", plan["id"])
    assert (f.root / "mods" / "old.jar").exists()


def test_promotion_failure_rolls_back_files_and_metadata(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.stage(f.prepare())
    f.u.validate("source", plan["id"])
    previous = f.storage.get_server("source")["modpack"]
    original = f.t._publish_worlds

    def fail(root, txn, journal):
        original(root, txn, journal)
        raise OSError("injected publication failure")
    monkeypatch.setattr(f.t, "_publish_worlds", fail)
    with pytest.raises(OSError, match="injected"):
        f.u.promote("source", plan["id"])
    assert f.storage.get_server("source")["modpack"] == previous
    assert (f.root / "mods" / "old.jar").exists()
    assert not (f.root / "mods" / "new.jar").exists()
    assert not (f.root / ".fabricator-update").exists()


def test_recovery_rolls_back_an_interrupted_publication(update_fixture):
    f = update_fixture
    plan = f.prepare()
    txn = f.root / ".fabricator-update"
    (txn / "old").mkdir(parents=True)
    (txn / "stage").mkdir()
    (f.root / "mods").rename(txn / "old" / "mods")
    (f.root / "mods").mkdir()
    (f.root / "mods" / "partial.jar").write_bytes(b"partial")
    journal = {"names": list(f.u.SCOPES), "existing": ["mods", "config"], "committed": False,
               "oldModpack": plan["signature"]["modpack"], "oldSource": plan["sourceMetadata"],
               "oldContent": plan["signature"]["modContent"]}
    f.t._write_json(txn / "journal.json", journal)
    f.u.recover()
    assert (f.root / "mods" / "old.jar").is_file()
    assert not (f.root / "mods" / "partial.jar").exists()
    assert not txn.exists()


def test_routes_enforce_owner_opt_in_and_explicit_approval(update_fixture, app, monkeypatch):
    f = update_fixture
    from backend.auth import permissions
    from backend.modrinth import update_routes
    plan = f.prepare()
    client = app.test_client()
    base = f"/api/servers/source/modpack-updates/{plan['id']}"
    assert client.post(base + "/stage", json={"reviewToken": "wrong"}).status_code == 409
    assert client.post(base + "/stage", json={"reviewToken": plan["reviewToken"]}).status_code == 400
    plan = f.stage(plan)
    assert client.post(base + "/validate", json={"reviewToken": plan["reviewToken"]}).status_code == 400
    assert not f.starts
    f.u.validate("source", plan["id"])
    assert client.post(base + "/promote", json={"reviewToken": plan["reviewToken"], "confirmName": "wrong"}).status_code == 400
    owner = permissions.Principal("other", "issuer", (), permissions.CATALOG, {}, {})
    monkeypatch.setattr(update_routes.permissions, "principal", lambda: owner)
    assert client.get(base).status_code == 403
    assert client.get("/api/servers/source/modpack-updates").json == {"plans": []}


def test_promote_permission_is_separate_from_install(update_fixture, app, monkeypatch):
    f = update_fixture
    from backend.auth import permissions
    from backend.modrinth import update_routes
    plan = f.stage(f.prepare())
    f.u.validate("source", plan["id"])
    user = permissions.Principal("operator", "issuer", (), permissions.CATALOG - {"modpacks.promote"}, {}, {})
    plan = f.u.read("source", plan["id"])
    plan["owner"] = user.owner
    f.u._write(f.u._path("source", plan["id"]), plan)
    monkeypatch.setattr(update_routes.permissions, "principal", lambda: user)
    response = app.test_client().post(
        f"/api/servers/source/modpack-updates/{plan['id']}/promote",
        json={"reviewToken": plan["reviewToken"], "confirmName": "source"})
    assert response.status_code == 403
    assert (f.root / "mods" / "old.jar").exists()


def test_dependency_review_surfaces_duplicate_and_missing_ids(update_fixture):
    f = update_fixture
    plan = f.prepare({"mods/duplicate.jar": mod_jar("example", depends={"missing": ">=1.0"})})
    assert any("Duplicate mod id" in value for value in plan["dependencyReview"])
    assert any("requires missing" in value for value in plan["dependencyReview"])


def test_readiness_timeout_stops_staging_without_approval(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.stage(f.prepare())
    monkeypatch.setattr(f.registry, "_get_or_create_manager", lambda s: SimpleNamespace(startup_ready=False))
    clock = iter(range(0, 10000, 181))
    monkeypatch.setattr(f.u.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="180 seconds"):
        f.u.validate("source", plan["id"])
    assert not f.running
    assert not f.u.read("source", plan["id"]).get("validatedToken")


def test_failed_staging_copy_blocks_start_and_retains_identity(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.prepare()
    monkeypatch.setattr(f.u.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("copy failed")))
    with pytest.raises(OSError, match="copy failed"):
        f.stage(plan)
    failed = f.u.read("source", plan["id"])
    staging = f.storage.get_server(failed["stagingId"])
    stage_root = Path(staging["installPath"])
    assert (stage_root / ".fabricator-staging").exists()
    assert f.registry._start_server_locked(staging)["status"] == "error"
    assert not f.starts


def test_failed_rollback_retains_start_block_until_recovery(update_fixture, monkeypatch):
    f = update_fixture
    plan = f.stage(f.prepare())
    f.u.validate("source", plan["id"])
    original_publish = f.t._publish_worlds
    original_rollback = f.t._rollback

    def fail_publish(root, txn, journal):
        original_publish(root, txn, journal)
        raise OSError("publish failed")
    monkeypatch.setattr(f.t, "_publish_worlds", fail_publish)
    monkeypatch.setattr(f.t, "_rollback", lambda *a: (_ for _ in ()).throw(OSError("rollback failed")))
    with pytest.raises(OSError, match="rollback failed"):
        f.u.promote("source", plan["id"])
    assert (f.root / ".fabricator-update").is_dir()
    assert f.registry._start_server_locked(f.storage.get_server("source"))["status"] == "error"
    monkeypatch.setattr(f.t, "_rollback", original_rollback)
    f.u.recover()
    assert (f.root / "mods" / "old.jar").exists()
    assert not (f.root / ".fabricator-update").exists()


def test_committed_recovery_keeps_publication_and_repairs_metadata(update_fixture):
    f = update_fixture
    plan = f.prepare()
    txn = f.root / ".fabricator-update"
    (txn / "old").mkdir(parents=True)
    (txn / "stage").mkdir()
    (f.root / "mods").rename(txn / "old" / "mods")
    (f.root / "mods").mkdir()
    (f.root / "mods" / "new.jar").write_bytes(mod_jar())
    journal = {"names": list(f.u.SCOPES), "existing": ["mods", "config"], "committed": True,
               "newModpack": plan["modpack"], "newSource": plan["clientMetadata"], "newContent": plan["modContent"],
               "planId": plan["id"], "safetySnapshotId": "fixture-snapshot"}
    f.t._write_json(txn / "journal.json", journal)
    f.u.recover()
    assert (f.root / "mods" / "new.jar").exists()
    assert not (f.root / "mods" / "old.jar").exists()
    assert f.storage.get_server("source")["modpack"] == plan["modpack"]
    assert f.u.read("source", plan["id"])["status"] == "promoted"
    assert f.cp.source(f.root) == plan["clientMetadata"]


def test_recovery_does_not_inspect_missing_unrelated_external_instance(update_fixture):
    f = update_fixture
    f.storage.update_server("destination", {"external": True, "installPath": str(f.root.parent / "missing")})
    f.u.recover()


def test_full_http_workflow_polls_owner_bound_public_states(update_fixture, app, monkeypatch):
    f = update_fixture
    prepared = f.prepare()

    def enqueue(action, sid, pid, data=None):
        {"prepare": f.u.prepare, "stage": lambda s, p: f.u.stage(s, p, data),
         "validate": f.u.validate, "promote": f.u.promote}[action](sid, pid)
    monkeypatch.setattr(f.u, "enqueue", enqueue)
    client = app.test_client()
    collection = "/api/servers/source/modpack-updates"
    response = client.post(collection, json=prepared["intent"])
    assert response.status_code == 202
    url = collection + "/" + response.json["id"]
    response = client.get(url)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json["status"] == "review"
    assert not {"owner", "baseline", "runtime", "clientMetadata", "sourceMetadata", "signature"} & response.json.keys()
    token = response.json["reviewToken"]
    assert client.post(url + "/stage", json={
        "reviewToken": token, "name": "http-test", "acknowledgeRisks": True,
    }).status_code == 202
    assert client.get(url).json["status"] == "staged"
    assert client.post(url + "/validate", json={"reviewToken": token, "executePackCode": True}).status_code == 202
    assert client.get(url).json["status"] == "validated"
    assert client.post(url + "/promote", json={"reviewToken": token, "confirmName": "source"}).status_code == 202
    result = client.get(url).json
    assert result["status"] == "promoted" and result["safetySnapshotId"]
    assert f.starts == [result["stagingId"]]
    assert client.delete(url).json == {"deleted": True, "stagingRetained": True}
    assert f.storage.get_server(result["stagingId"])
    assert list((f.root / "backups").glob("safety-modpack-update-*.tar"))


def test_managed_mode_disables_even_review_metadata(update_fixture, app, monkeypatch):
    f = update_fixture
    plan = f.prepare()
    monkeypatch.setattr(f.u, "is_managed", lambda: True)
    client = app.test_client()
    base = "/api/servers/source/modpack-updates"
    assert client.get(base).status_code == 400
    assert client.get(base + "/" + plan["id"]).status_code == 400


def test_review_cannot_be_deleted_while_production_needs_recovery(update_fixture, app):
    f = update_fixture
    plan = f.prepare()
    (f.root / ".fabricator-update").mkdir()
    response = app.test_client().delete(f"/api/servers/source/modpack-updates/{plan['id']}")
    assert response.status_code == 400
    assert "Recover" in response.json["error"]
    assert f.u.read("source", plan["id"])


def test_worker_activity_keeps_initiator_and_does_not_duplicate_internal_fork(update_fixture, app, monkeypatch):
    from backend.events import store
    f = update_fixture
    plan = f.prepare()
    workers = []
    monkeypatch.setattr(f.u.threading, "Thread", lambda target, **kw:
                        SimpleNamespace(start=lambda: workers.append(target)))
    client = app.test_client()
    url = f"/api/servers/source/modpack-updates/{plan['id']}"
    for action, consent in [
        ("stage", {"name": "activity-test", "acknowledgeRisks": True}),
        ("validate", {"executePackCode": True}),
        ("promote", {"confirmName": f.storage.get_server("source")["name"]}),
    ]:
        response = client.post(url + "/" + action, json={"reviewToken": plan["reviewToken"], **consent})
        assert response.status_code == 202, response.json
        assert len(workers) == 1
        workers.pop()()
        plan = f.u.read("source", plan["id"])
    assert plan["status"] == "promoted" and plan["safetySnapshotId"]
    rows = store.history("source")["items"]
    assert len(rows) == 6
    for action in ("stage", "validate", "promote"):
        events = [row for row in rows if row["action"] == "modpacks.update." + action]
        assert {row["outcome"] for row in events} == {"accepted", "succeeded"}
        assert len(events) == 2
        assert all(row["actor"]["kind"] == "unattributed" for row in events)
    staging_rows = store.history(plan["stagingId"])["items"]
    assert {row["action"] for row in staging_rows} == {"modpacks.update.stage", "modpacks.update.validate"}
    assert len(staging_rows) == 2
    assert all(row["ref"] == plan["id"] for row in staging_rows)
    assert not any(row["action"] in {"server.fork", "backups.create"} for row in store.history(instance=True)["items"])


def test_worker_failure_is_attributed_and_alerted_without_error_payload(update_fixture, app, monkeypatch):
    from backend.events import store
    f = update_fixture
    plan = f.stage(f.prepare())
    workers = []
    monkeypatch.setattr(f.u.threading, "Thread", lambda target, **kw:
                        SimpleNamespace(start=lambda: workers.append(target)))
    def fail(*args):
        raise ValueError("private-error-canary")
    monkeypatch.setattr(f.u, "validate", fail)
    response = app.test_client().post(
        f"/api/servers/source/modpack-updates/{plan['id']}/validate",
        json={"reviewToken": plan["reviewToken"], "executePackCode": True})
    assert response.status_code == 202
    workers.pop()()
    assert f.u.read("source", plan["id"])["status"] == "failed"
    rows = store.history("source")["items"]
    assert [row["outcome"] for row in rows] == ["failed", "accepted"]
    assert rows[0]["alert"] == "job.failed"
    assert rows[0]["actor"]["kind"] == "unattributed"
    assert "private-error-canary" not in json.dumps(rows)
