"""Fixture-only end-to-end transfers: real bytes, registry, rollback and grants."""
import gzip
import json
import os
from pathlib import Path
import struct
import tarfile
import threading

import pytest


def nbt(version="1.21.1", data_version=3955):
    def text(value):
        raw = value.encode()
        return struct.pack(">H", len(raw)) + raw
    return gzip.compress(
        b"\x0a\x00\x00\x0a" + text("Data")
        + b"\x03" + text("DataVersion") + struct.pack(">i", data_version)
        + b"\x0a" + text("Version") + b"\x08" + text("Name") + text(version)
        + b"\x01" + text("Snapshot") + b"\x00\x00\x00\x00"
    )


@pytest.fixture
def fixture_servers(app, monkeypatch):
    from backend.server import storage, transfers
    from backend.server.registry import get_server_process_registry
    monkeypatch.setattr(storage, "_request_dns_sync", lambda: None)
    registry = get_server_process_registry()

    def make(name, loader="vanilla", level="island"):
        root = registry.base_dir / name
        root.mkdir()
        (root / "server.properties").write_text(f"level-name={level}\nserver-port=25565\nrcon.password=secret\n")
        (root / "server.jar").write_bytes(b"fixture-server-not-executable")
        (root / "eula.txt").write_text("eula=true")
        (root / level / "region").mkdir(parents=True)
        (root / level / "level.dat").write_bytes(nbt())
        (root / level / "region" / "r.0.0.mca").write_bytes(name.encode() * 100)
        (root / level / "playerdata").mkdir()
        (root / level / "playerdata" / "player.dat").write_bytes(b"player-world-state")
        server = storage.create_server({
            "id": name, "name": name, "installPath": str(root), "loader": loader,
            "version": "1.21.1", "loaderVersion": "test-build",
            "levelName": "stale-record-name", "status": "stopped", "port": 25565 if name == "source" else 25566,
            "launch": {"type": "jar", "jar": "server.jar", "jvm_args": [], "program_args": ["nogui"]},
        })
        return server, root
    source, src = make("source")
    destination, dst = make("destination")
    return transfers, storage, registry, source, src, destination, dst, make


def fork_options(**kw):
    return {"name": "new-instance", "includeWorlds": True, "includePlayerData": False,
            "acknowledgeRisks": True, **kw}


def world_options(**kw):
    return {"destinationId": "destination", "worldId": "island", "includePlayerData": True,
            "confirmReplace": "island", "restart": False, "acknowledgeRisks": True, **kw}


def prepare(t, kind, data):
    return {**data, "previewToken": t.preview(kind, "source", data)["previewToken"]}


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_fork_independent_files_ports_and_fresh_authority(fixture_servers):
    t, storage, registry, source, src, _, _, _ = fixture_servers
    for folder in ("config", "mods", "logs", "backups"):
        (src / folder).mkdir()
        (src / folder / "test.txt").write_bytes(b"original")
    (src / "mods" / "mod.jar").write_bytes(b"trusted-mod")
    (src / "ops.json").write_text('[{"level":4}]')
    (src / ".env").write_text("HOST_SECRET=never")
    (src / "auth.json").write_text('{"password":"never"}')
    (src / "frpc.toml").write_text("token=never")
    storage.update_server(source["id"], {
        "autoStart": True, "rconPassword": "never", "dns": {"name": "never"},
        "schedules": [{"command": "stop"}], "forwardingProxyId": "proxy", "javaPath": "/host/java",
        "modContent": {"mod.jar": {"projectId": "trusted-project"}},
    })
    before = tree(src)
    result = t.run("fork", "source", prepare(t, "fork", fork_options()))
    record = storage.get_server(result["serverId"])
    clone = Path(record["installPath"])
    assert len({record[k] for k in ("port", "queryPort", "rconPort")}) == 3
    assert not {25565, 25566} & {record[k] for k in ("port", "queryPort", "rconPort")}
    assert record["id"] != source["id"] and clone != src
    assert record["status"] == "stopped" and record["autoStart"] == "never"
    assert record["version"] == "1.21.1" and record["loaderVersion"] == "test-build"
    assert record["modContent"]["mod.jar"]["projectId"] == "trusted-project"
    assert not {"schedules", "dns", "forwardingProxyId", "javaPath"} & record.keys()
    assert not record["enableRcon"] and record["rconPassword"] == "" and record["onlineMode"]
    for excluded in ("ops.json", ".env", "auth.json", "frpc.toml", "logs", "backups"):
        assert not (clone / excluded).exists()
    assert not (clone / "island" / "playerdata").exists()
    assert (clone / "island" / "level.dat").read_bytes() == (src / "island" / "level.dat").read_bytes()
    assert registry._build_command(record)[-3:] == ["-jar", "server.jar", "nogui"]
    (clone / "config" / "test.txt").write_text("independent")
    (clone / "island" / "region" / "r.0.0.mca").write_bytes(b"independent")
    assert tree(src) == before
    assert not list(registry.base_dir.glob(".fork-*"))


def test_fork_without_worlds_and_with_players(fixture_servers):
    t, storage, _, _, src, _, _, _ = fixture_servers
    for include_worlds, include_players in [(False, False), (True, True)]:
        result = t.run("fork", "source", prepare(t, "fork", fork_options(
            name=f"fork-{include_worlds}", includeWorlds=include_worlds, includePlayerData=include_players)))
        clone = Path(storage.get_server(result["serverId"])["installPath"])
        assert (clone / "island").exists() == include_worlds
        assert (clone / "island" / "playerdata").exists() == include_players


@pytest.mark.parametrize("loader", ["vanilla", "fabric", "forge", "neoforge", "quilt", "paper", "folia", "purpur", "pufferfish"])
def test_world_copy_layouts_safety_and_source_unchanged(fixture_servers, loader):
    t, storage, _, _, src, _, dst, _ = fixture_servers
    storage.update_servers({"source": {"loader": loader}, "destination": {"loader": loader}})
    if loader in t.worlds.PAPER:
        (src / "island_nether" / "DIM-1").mkdir(parents=True)
        (src / "island_nether" / "DIM-1" / "region.mca").write_bytes(b"nether")
        (dst / "island_the_end" / "DIM1").mkdir(parents=True)
        (dst / "island_the_end" / "DIM1" / "old.mca").write_bytes(b"old-end")
        (src / "island" / "uid.dat").write_bytes(b"old-plugin-uuid")
    else:
        (src / "island" / "DIM-1").mkdir()
        (src / "island" / "DIM-1" / "nether.mca").write_bytes(b"nether")
        custom = src / "island" / "dimensions" / "test" / "moon"
        custom.mkdir(parents=True)
        (custom / "moon.mca").write_bytes(b"moon")
    (dst / "config").mkdir()
    (dst / "config" / "keep.toml").write_bytes(b"destination-settings")
    if loader not in t.worlds.PAPER | {"vanilla"}:
        (src / "config").mkdir()
        (src / "config" / "keep.toml").write_bytes(b"destination-settings")
    before, old = tree(src), tree(dst)
    data = prepare(t, "world-copy", world_options(includePlayerData=False))
    result = t.run("world-copy", "source", data)
    assert tree(src) == before
    assert (dst / "server.properties").read_bytes() == old["server.properties"]
    assert (dst / "config" / "keep.toml").read_bytes() == b"destination-settings"
    assert (dst / "island" / "region" / "r.0.0.mca").read_bytes() == before["island/region/r.0.0.mca"]
    assert not (dst / "island" / "playerdata").exists()
    assert not (dst / "island" / "uid.dat").exists()
    if loader in t.worlds.PAPER:
        assert (dst / "island_nether" / "DIM-1" / "region.mca").read_bytes() == b"nether"
        assert not (dst / "island_the_end").exists()
    else:
        assert (dst / "island" / "dimensions" / "test" / "moon" / "moon.mca").read_bytes() == b"moon"
    from backend.backups import storage as backups
    snapshot = next(s for s in backups.list_snapshots("destination") if s["id"] == result["safetySnapshotId"])
    with tarfile.open(snapshot["filePath"]) as archive:
        assert archive.extractfile("island/region/r.0.0.mca").read() == old["island/region/r.0.0.mca"]
    assert not (dst / ".fabricator-transfer").exists()


@pytest.mark.parametrize("fault", ["copy", "register", "publish"])
def test_fork_failure_has_no_registration_or_partial_directory(fixture_servers, monkeypatch, fault):
    t, storage, registry, _, src, _, _, _ = fixture_servers
    data = prepare(t, "fork", fork_options())
    before = tree(src)
    def fail(*a, **kw):
        raise OSError("injected failure")
    if fault == "copy":
        monkeypatch.setattr(t.shutil, "copyfile", fail)
    elif fault == "register":
        monkeypatch.setattr(storage, "register_fork", fail)
    else:
        replace = os.replace
        def publish(a, b):
            if Path(a).name == "stage":
                fail()
            return replace(a, b)
        monkeypatch.setattr(t.os, "replace", publish)
    with pytest.raises(OSError):
        t.run("fork", "source", data)
    assert {s["id"] for s in storage.get_all_servers()} == {"source", "destination"}
    assert {p.name for p in registry.base_dir.iterdir()} == {"source", "destination"}
    assert tree(src) == before


@pytest.mark.parametrize("fault", ["safety", "copy", "publish", "journal"])
def test_world_failure_preserves_destination(fixture_servers, monkeypatch, fault):
    t, _, _, _, src, _, dst, _ = fixture_servers
    data = prepare(t, "world-copy", world_options())
    before, source_before = tree(dst), tree(src)
    def fail(*a, **kw):
        raise OSError("injected failure")
    if fault == "safety":
        monkeypatch.setattr(t.archive, "write_tree_tar", fail)
    elif fault == "copy":
        monkeypatch.setattr(t.shutil, "copyfile", fail)
    elif fault == "publish":
        original = t._publish_worlds
        def partial(root, txn, journal):
            os.replace(root / "island", txn / "old" / "island")
            os.replace(txn / "stage" / "island", root / "island")
            fail()
        monkeypatch.setattr(t, "_publish_worlds", partial)
    else:
        original = t._write_json
        def write(path, value):
            if value.get("committed"):
                fail()
            original(path, value)
        monkeypatch.setattr(t, "_write_json", write)
    with pytest.raises(OSError):
        t.run("world-copy", "source", data)
    assert {k: v for k, v in tree(dst).items() if not k.startswith("backups/")} == before
    assert tree(src) == source_before
    assert not (dst / ".fabricator-transfer").exists()


def test_crash_recovery_and_start_guard(fixture_servers, monkeypatch):
    t, storage, registry, _, src, destination, dst, _ = fixture_servers
    data = prepare(t, "world-copy", world_options())
    before = tree(dst)
    def interrupt(root, txn, journal):
        os.replace(root / "island", txn / "old" / "island")
        os.replace(txn / "stage" / "island", root / "island")
        raise KeyboardInterrupt("simulate process interruption")
    monkeypatch.setattr(t, "_publish_worlds", interrupt)
    with pytest.raises(KeyboardInterrupt):
        t.run("world-copy", "source", data)
    assert registry.start_server(destination)["status"] == "error"
    t.recover()
    assert {k: v for k, v in tree(dst).items() if not k.startswith("backups/")} == before


@pytest.mark.parametrize("case", ["running-source", "running-dest", "mismatch", "mods", "alias", "link", "hardlink",
                                  "unknown-world", "mixed-layout", "data-version", "space"])
def test_unsafe_world_copies_rejected(fixture_servers, monkeypatch, case):
    t, storage, _, _, src, _, dst, _ = fixture_servers
    if case.startswith("running"):
        storage.update_server("source" if case == "running-source" else "destination", {"status": "running"})
    elif case == "mismatch":
        storage.update_server("destination", {"version": "1.20.1"})
    elif case == "mods":
        (src / "mods").mkdir()
        (src / "mods" / "dependency.jar").write_bytes(b"missing-dependency")
    elif case == "alias":
        storage.update_server("destination", {"installPath": str(src)})
    elif case == "link":
        (src / "island" / "outside").symlink_to(dst, target_is_directory=True)
    elif case == "hardlink":
        os.link(src / "island" / "level.dat", src / "island" / "alias.dat")
    elif case == "unknown-world":
        (src / "extra").mkdir()
        (src / "extra" / "level.dat").write_bytes(nbt())
    elif case == "mixed-layout":
        (src / "island_nether").mkdir()
    elif case == "data-version":
        (dst / "island" / "level.dat").write_bytes(nbt(data_version=1))
    elif case == "space":
        monkeypatch.setattr(t.shutil, "disk_usage", lambda _: type("Usage", (), {"free": 0})())
    before = tree(dst)
    with pytest.raises(ValueError):
        t.preview("world-copy", "source", world_options())
    assert tree(dst) == before


def test_stale_preview_and_replacement_confirmation(fixture_servers):
    t, _, _, _, src, _, dst, _ = fixture_servers
    data = prepare(t, "world-copy", world_options())
    (src / "island" / "region" / "r.0.0.mca").write_bytes(b"changed")
    with pytest.raises(ValueError, match="stale"):
        t.run("world-copy", "source", data)
    data = prepare(t, "world-copy", world_options(confirmReplace="wrong"))
    before = tree(dst)
    with pytest.raises(ValueError, match="confirm replacement"):
        t.run("world-copy", "source", data)
    assert tree(dst) == before


def test_transfer_lock_blocks_concurrent_start(fixture_servers, client, monkeypatch):
    t, _, _, _, _, _, _, _ = fixture_servers
    data = prepare(t, "world-copy", world_options())
    entered, release = threading.Event(), threading.Event()
    original = t._copy
    errors = []
    def wait(*a):
        entered.set()
        assert release.wait(5)
        return original(*a)
    monkeypatch.setattr(t, "_copy", wait)
    def worker():
        try:
            t.run("world-copy", "source", data)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert entered.wait(5)
        for sid in ("source", "destination"):
            assert client.post(f"/api/servers/{sid}/start").status_code == 409
            assert client.delete(f"/api/servers/{sid}").status_code == 409
        assert t.registry().start_server(t.storage.get_server("source"))["busy"]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


def test_routes_cross_instance_grants_and_job_ownership(fixture_servers, app):
    from backend.auth import permissions
    from backend.backups import progress
    from flask import request
    t, _, _, _, _, _, _, _ = fixture_servers
    users = {
        "viewer": permissions.Principal("viewer", "test", (), frozenset({"servers.create"}), {
            "source": frozenset({"server.view"}), "destination": frozenset({"server.view"}),
        }, {"source": (), "destination": ()}),
        "copier": permissions.Principal("copier", "test", (), frozenset(), {
            "source": frozenset({"server.view", "worlds.read"}),
            "destination": frozenset({"server.view", "worlds.import", "backups.create"}),
        }, {"source": (), "destination": ()}),
    }
    @app.before_request
    def principal():
        return permissions.authorize(users[request.headers.get("X-Test-User", "viewer")])
    client = app.test_client()
    path = "/api/servers/source/transfers/world-copy/preview"
    assert client.post(path, json=world_options()).status_code == 403
    assert client.get("/api/servers/source/worlds").status_code == 403
    copier = {"X-Test-User": "copier"}
    assert client.post(path, json=world_options(), headers=copier).status_code == 200
    assert client.get("/api/servers/source/worlds", headers=copier).status_code == 200
    assert client.post(path, json=world_options(destinationId="ungranted"), headers=copier).status_code == 403
    assert client.post(path, json=world_options(restart=True), headers=copier).status_code == 403
    assert client.post("/api/servers/source/transfers/fork/preview", json=fork_options()).status_code == 403
    progress.update("owned-transfer", kind="world-copy", phase="queued", sourceId="source",
                    destinationId="destination", owner=users["copier"].owner)
    assert client.get("/api/transfer-jobs/owned-transfer").status_code == 404
    assert client.get("/api/transfer-jobs/owned-transfer", headers=copier).status_code == 200
    assert client.get("/api/backup-jobs/owned-transfer", headers=copier).status_code == 404


def test_async_route_persisted_success(fixture_servers, client):
    import time
    t, storage, _, _, src, _, _, _ = fixture_servers
    data = prepare(t, "fork", fork_options())
    response = client.post("/api/servers/source/transfers/fork", json=data)
    assert response.status_code == 202
    job_id = response.json["jobId"]
    for _ in range(100):
        job = client.get("/api/transfer-jobs/" + job_id).json
        if job["phase"] in {"done", "failed"}:
            break
        time.sleep(.02)
    assert job["phase"] == "done", job
    assert storage.get_server(job["result"]["serverId"])
    assert client.get("/api/backup-jobs/" + job_id).status_code == 404


def test_world_copy_renames_group_without_merging(fixture_servers):
    t, storage, _, _, src, _, dst, _ = fixture_servers
    (dst / "island").rename(dst / "survival")
    (dst / "server.properties").write_text("level-name=survival\nmotd=destination\n")
    before = (dst / "server.properties").read_bytes()
    result = t.run("world-copy", "source", prepare(t, "world-copy", world_options(confirmReplace="survival")))
    assert result["destinationId"] == "destination"
    assert (dst / "survival" / "region" / "r.0.0.mca").read_bytes() == (src / "island" / "region" / "r.0.0.mca").read_bytes()
    assert not (dst / "island").exists()
    assert (dst / "server.properties").read_bytes() == before


def test_safety_warning_and_verification_failure_abort(fixture_servers, monkeypatch):
    t, _, _, _, _, _, dst, _ = fixture_servers
    before = tree(dst)
    original = t.archive.write_tree_tar
    def incomplete(*a, **kw):
        original(*a, **kw)
        return ["injected unreadable file"]
    monkeypatch.setattr(t.archive, "write_tree_tar", incomplete)
    with pytest.raises(ValueError, match="incomplete"):
        t.run("world-copy", "source", prepare(t, "world-copy", world_options()))
    assert tree(dst) == before


def test_fork_registration_error_after_persist_is_rolled_back(fixture_servers, monkeypatch):
    t, storage, registry, _, _, _, _, _ = fixture_servers
    original = storage.register_fork
    def fail_after_save(record):
        original(record)
        raise OSError("injected post-registration failure")
    monkeypatch.setattr(storage, "register_fork", fail_after_save)
    with pytest.raises(OSError):
        t.run("fork", "source", prepare(t, "fork", fork_options()))
    assert {s["id"] for s in storage.get_all_servers()} == {"source", "destination"}
    assert {p.name for p in registry.base_dir.iterdir()} == {"source", "destination"}


def test_generated_identity_collision_preserves_existing_instance(fixture_servers, monkeypatch):
    t, storage, registry, _, src, _, _, _ = fixture_servers
    monkeypatch.setattr(storage, "generate_server_id", lambda: "source")
    before = tree(src)
    with pytest.raises(ValueError, match="identity already exists"):
        t.run("fork", "source", prepare(t, "fork", fork_options()))
    assert tree(src) == before and storage.get_server("source")


def test_recover_uncommitted_and_committed_forks(fixture_servers):
    t, storage, registry, _, _, _, _, _ = fixture_servers
    for server_id, committed in [("srv_12345678", False), ("srv_87654321", True)]:
        final = registry.base_dir / server_id
        final.mkdir()
        (final / "server.jar").write_bytes(b"fixture")
        txn = registry.base_dir / (".fork-" + server_id)
        txn.mkdir()
        t._write_json(txn / "journal.json", {"id": server_id, "committed": committed})
        storage.create_server({"id": server_id, "name": server_id, "installPath": str(final)})
    t.recover()
    assert not storage.get_server("srv_12345678")
    assert not (registry.base_dir / "srv_12345678").exists()
    assert storage.get_server("srv_87654321")
    assert (registry.base_dir / "srv_87654321" / "server.jar").exists()
    assert not list(registry.base_dir.glob(".fork-*"))


def test_mod_config_dependencies_and_unverifiable_world_blocked(fixture_servers):
    t, storage, _, _, src, _, dst, _ = fixture_servers
    storage.update_servers({"source": {"loader": "fabric"}, "destination": {"loader": "fabric"}})
    (src / "config").mkdir()
    (src / "config" / "dimensions.json").write_text('{"moon": 7}')
    with pytest.raises(ValueError, match="Mod configuration differs"):
        t.preview("world-copy", "source", world_options())
    (dst / "config").mkdir()
    (dst / "config" / "dimensions.json").write_text('{"moon": 7}')
    assert t.preview("world-copy", "source", world_options())["world"]["dataVersion"] == 3955
    (src / "island" / "level.dat").write_bytes(gzip.compress(b"not-nbt"))
    with pytest.raises(ValueError):
        t.preview("world-copy", "source", world_options())


def test_ancillary_ports_reserved_against_concurrent_creation(fixture_servers):
    t, storage, _, _, _, _, _, _ = fixture_servers
    result = t.run("fork", "source", prepare(t, "fork", fork_options()))
    record = storage.get_server(result["serverId"])
    with pytest.raises(ValueError, match="reserved"):
        storage.create_server({"name": "colliding", "port": record["queryPort"]})
    with pytest.raises(ValueError, match="reserved"):
        storage.update_server("destination", {"port": record["rconPort"]})
    with pytest.raises(ValueError, match="reserved"):
        storage.update_servers({"destination": {"port": record["rconPort"]}})


def test_published_byte_corruption_rolls_back(fixture_servers, monkeypatch):
    t, _, _, _, _, _, dst, _ = fixture_servers
    original = t._publish_worlds
    def corrupt(root, txn, journal):
        original(root, txn, journal)
        (root / "island" / "region" / "r.0.0.mca").write_bytes(b"corrupt")
    monkeypatch.setattr(t, "_publish_worlds", corrupt)
    before = tree(dst)
    with pytest.raises(RuntimeError, match="filesystem verification"):
        t.run("world-copy", "source", prepare(t, "world-copy", world_options()))
    assert {k: v for k, v in tree(dst).items() if not k.startswith("backups/")} == before


def test_managed_mode_blocks_transfers(fixture_servers, monkeypatch):
    t, _, _, _, _, _, _, _ = fixture_servers
    monkeypatch.setenv("FABRICATOR_MANAGED", "1")
    with pytest.raises(ValueError, match="managed mode"):
        t.preview("fork", "source", fork_options())


def test_forge_fork_preserves_relative_launch_and_libraries(fixture_servers):
    t, storage, registry, _, src, _, _, _ = fixture_servers
    args = "libraries/net/minecraftforge/forge/test/unix_args.txt"
    (src / args).parent.mkdir(parents=True)
    (src / args).write_text("-p libraries/module.jar\n--fml.mcVersion 1.21.1\n")
    (src / "libraries" / "module.jar").write_bytes(b"fixture-module")
    storage.update_server("source", {"loader": "forge", "launch": {
        "type": "args_file", "args_file": args, "jvm_args": [], "program_args": ["nogui"],
    }})
    result = t.run("fork", "source", prepare(t, "fork", fork_options()))
    record = storage.get_server(result["serverId"])
    clone = Path(record["installPath"])
    assert registry._build_command(record)[-2:] == ["@" + args, "nogui"]
    assert (clone / args).read_bytes() == (src / args).read_bytes()
    assert (clone / "libraries" / "module.jar").read_bytes() == b"fixture-module"
    (src / args).write_text("-p /host/shared/module.jar\n")
    with pytest.raises(ValueError, match="Host-specific"):
        t.preview("fork", "source", fork_options(name="unsafe-launch"))


def test_fork_clears_forwarding_and_excludes_secret_backups(fixture_servers):
    t, storage, _, _, src, _, _, _ = fixture_servers
    (src / "config").mkdir()
    (src / "config" / "paper-global.yml").write_text("proxies:\n  velocity:\n    enabled: true\n    secret: must-not-copy\n")
    (src / "config" / "paper-global.yml.fabricator-backup-test").write_text("secret: must-not-copy")
    (src / "spigot.yml").write_text("settings:\n  bungeecord: true\n")
    with (src / "server.properties").open("a") as handle:
        handle.write("management-server-secret=must-not-copy\nmanagement-server-enabled=true\n")
    result = t.run("fork", "source", prepare(t, "fork", fork_options()))
    clone = Path(storage.get_server(result["serverId"])["installPath"])
    assert "must-not-copy" not in (clone / "config" / "paper-global.yml").read_text()
    assert "bungeecord: false" in (clone / "spigot.yml").read_text()
    assert not (clone / "config" / "paper-global.yml.fabricator-backup-test").exists()
    assert "must-not-copy" not in (clone / "server.properties").read_text()


def test_game_build_hash_and_nested_legacy_dimensions(fixture_servers):
    t, _, _, _, src, _, dst, _ = fixture_servers
    (src / "island" / "DIM7").mkdir()
    (src / "island" / "DIM7" / "region.mca").write_bytes(b"legacy-dimension")
    assert "DIM7" in t.preview("world-copy", "source", world_options())["world"]["dimensions"]
    (dst / "server.jar").write_bytes(b"different-build")
    with pytest.raises(ValueError, match="launch jar hashes differ"):
        t.preview("world-copy", "source", world_options())


def test_explicit_start_only_after_commit_and_failure_reports_publication(fixture_servers, monkeypatch):
    from backend.backups import progress
    t, storage, registry, _, src, _, dst, _ = fixture_servers
    calls = []
    def failed_start(server, **kw):
        assert not (dst / ".fabricator-transfer").exists()
        calls.append(server["id"])
        return {"status": "stopped", "message": "fixture missing Java"}
    monkeypatch.setattr(registry, "start_server", failed_start)
    data = prepare(t, "world-copy", world_options(restart=True))
    with pytest.raises(RuntimeError, match="copied safely"):
        t.run("world-copy", "source", data, "start-failure")
    assert calls == ["destination"]
    assert progress.get("start-failure")["published"] is True
    assert (dst / "island" / "region" / "r.0.0.mca").read_bytes() == (src / "island" / "region" / "r.0.0.mca").read_bytes()
    assert storage.get_server("source")["status"] == "stopped"


def test_failed_rollback_retains_journal_and_blocks_starts(fixture_servers, monkeypatch):
    t, _, registry, _, _, destination, dst, _ = fixture_servers
    rollback = t._rollback
    def partial(root, txn, journal):
        os.replace(root / "island", txn / "old" / "island")
        raise OSError("publish failure")
    def fail(*a):
        raise OSError("rollback failure")
    monkeypatch.setattr(t, "_publish_worlds", partial)
    monkeypatch.setattr(t, "_rollback", fail)
    before = tree(dst)
    with pytest.raises(OSError, match="rollback failure"):
        t.run("world-copy", "source", prepare(t, "world-copy", world_options()))
    assert (dst / ".fabricator-transfer" / "journal.json").exists()
    assert registry.start_server(destination)["status"] == "error"
    monkeypatch.setattr(t, "_rollback", rollback)
    t.recover()
    assert {k: v for k, v in tree(dst).items() if not k.startswith("backups/")} == before


def test_opposing_copies_use_deterministic_locks(fixture_servers):
    t, _, _, _, _, _, _, _ = fixture_servers
    a = prepare(t, "world-copy", world_options())
    reverse = world_options(destinationId="source")
    b = {**reverse, "previewToken": t.preview("world-copy", "destination", reverse)["previewToken"]}
    barrier = threading.Barrier(2)
    results = []
    def worker(source, data):
        barrier.wait(timeout=5)
        try:
            results.append(t.run("world-copy", source, data))
        except ValueError as exc:
            results.append(str(exc))
    threads = [threading.Thread(target=worker, args=("source", a)),
               threading.Thread(target=worker, args=("destination", b))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2 and sum(isinstance(result, dict) for result in results) == 1
    assert any("stale" in str(result) for result in results)


def test_fork_resets_managed_map_authority_and_listeners(fixture_servers):
    t, storage, _, _, src, _, _, _ = fixture_servers
    (src / "config" / "bluemap").mkdir(parents=True)
    (src / "config" / "bluemap" / "core.conf").write_text("accept-download: true\n")
    (src / "config" / "bluemap" / "webserver.conf").write_text("enabled: true\nport: 8800\n")
    (src / "config" / "fabricator-map.json").write_text('{"nonce":"source-secret"}')
    (src / ".fabricator-map.json").write_text('{"nonce":"source-secret","port":8800}')
    storage.update_server("source", {"map": {"enabled": True, "port": 8800}})
    before = tree(src)
    result = t.run("fork", "source", prepare(t, "fork", fork_options()))
    record = storage.get_server(result["serverId"])
    clone = Path(record["installPath"])
    assert "map" not in record
    assert not (clone / ".fabricator-map.json").exists()
    assert not (clone / "config" / "fabricator-map.json").exists()
    assert (clone / "config" / "bluemap" / "webserver.conf").read_text() == "enabled: false\n"
    assert "accept-download: false" in (clone / "config" / "bluemap" / "core.conf").read_text()
    assert storage.port_values({"map": {"port": 8800}}) == {8800}
    assert tree(src) == before
