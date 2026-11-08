"""Independent source/target recovery using only an isolated S3 emulator."""
import json
import gzip
from io import BytesIO
from pathlib import Path
import shutil
import tarfile
import time
import zipfile

import boto3
import pytest

from tests.test_transfers import nbt

moto = pytest.importorskip("moto", reason="Install tests/requirements-s3.txt")


@pytest.fixture
def migration(app, tmp_servers_root, monkeypatch):
    from backend.backups import archive, destinations, portable, recovery
    from backend.server import storage, transfers
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "source": {"buckets": ["recovery-tests"], "serverIds": ["srv_source"]},
        "target": {"buckets": ["recovery-tests"], "serverIds": ["srv_local"], "allowImport": True},
    }))
    monkeypatch.setattr(storage, "_request_dns_sync", lambda: None)
    source = tmp_servers_root / "independent-source"
    source.mkdir()
    (source / "world" / "region").mkdir(parents=True)
    (source / "world" / "level.dat").write_bytes(nbt())
    (source / "world" / "region" / "r.0.0.mca").write_bytes(b"independent-world")
    (source / "world" / "playerdata").mkdir()
    (source / "world" / "playerdata" / "fixture.dat").write_bytes(b"player-state")
    (source / "config").mkdir()
    (source / "config" / "mod.properties").write_text("feature=true\n")
    (source / "server.properties").write_text("level-name=world\nserver-port=25565\nrcon.password=fixture-only\n")
    (source / "eula.txt").write_text("eula=true\n")
    with zipfile.ZipFile(source / "server.jar", "w") as jar:
        jar.writestr("version.json", '{"id":"1.21.1","name":"1.21.1"}')
    server = {
        "id": "srv_source", "name": "Source", "loader": "vanilla", "version": "1.21.1",
        "launch": {"type": "jar", "jar": "server.jar", "jvm_args": [], "program_args": ["nogui"]},
        "memory": 4, "memoryMin": 1, "memoryUnit": "GB", "jvmArgs": "-XX:+UseG1GC",
        "autoStart": "always", "dns": {"name": "source-only"}, "javaPath": "/source/java",
        "map": {"enabled": True, "nonce": "source-only"}, "rconPassword": "fixture-only",
    }
    config = {"id": "cfg_source", "name": "Portable", "destination": {
        "type": "s3", "profile": "source", "bucket": "recovery-tests", "prefix": "migration",
    }}
    target = {**config["destination"], "profile": "target"}
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="recovery-tests")
        def publish(legacy=False, raw=None):
            path = tmp_servers_root / "source.tar"
            if raw is None:
                assert not archive.write_tree_tar(source, path)
            else:
                path.write_bytes(raw)
            snapshot = {"serverId": server["id"], "fileName": "source.tar",
                        "sizeBytes": path.stat().st_size, "createdAt": "2026-10-01T00:00:00Z",
                        "configId": config["id"], "filePath": str(path)}
            return destinations.publish(path, snapshot, config, None,
                                        recovery=None if legacy else portable.manifest(server))
        snapshot = publish()
        yield {
            "app": app, "client": client, "source": source, "server": server, "snapshot": snapshot,
            "destination": target, "publish": publish, "recovery": recovery,
            "storage": storage, "transfers": transfers, "root": tmp_servers_root,
        }


def options(fixture, **extra):
    return {"destination": fixture["destination"], "objectKey": fixture["snapshot"]["objectKey"],
            "name": "Recovered", **extra}


def confirm(preview, **extra):
    return {"previewId": preview["previewId"], "confirmName": preview["name"],
            "acknowledgeRisks": True, **extra}


def test_independent_recovery_without_source_registry_or_index(migration):
    m = migration
    shutil.rmtree(m["source"])
    assert not m["storage"].get_all_servers()
    discovery = m["recovery"].discover(m["destination"])
    assert len(discovery["snapshots"]) == 1 and not discovery["snapshots"][0]["legacy"]
    preview = m["recovery"].prepare(options(m))
    assert preview["worlds"] == ["world"] and preview["java"]["required_java"] == 21
    assert not preview["secrets"]["suspectedCount"]
    assert "fixture-only" not in json.dumps(preview)
    result = m["recovery"].import_instance(confirm(preview))
    record = m["storage"].get_server(result["serverId"])
    root = Path(record["installPath"])
    assert record["id"] != "srv_source" and record["name"] == "Recovered"
    assert record["status"] == "stopped" and record["autoStart"] == "never"
    assert len({record[k] for k in ("port", "queryPort", "rconPort")}) == 3
    assert record["serverIp"] == "127.0.0.1" and not record["enableRcon"] and not record["enableQuery"]
    assert record["rconPassword"] == "" and record["jvmArgs"] == "-XX:+UseG1GC"
    assert not {"javaPath", "dns", "map", "nonce", "schedules", "forwardingProxyId"} & record.keys()
    assert (root / "world" / "region" / "r.0.0.mca").read_bytes() == b"independent-world"
    assert (root / "world" / "playerdata" / "fixture.dat").read_bytes() == b"player-state"
    assert not list(root.parent.glob(".s3-preview-*")) and not list(root.parent.glob(".fork-*"))
    assert m["client"].head_object(Bucket="recovery-tests", Key=m["snapshot"]["objectKey"])


def test_explicit_profile_access_no_original_credentials_required(migration, monkeypatch):
    m = migration
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "target": {"buckets": ["recovery-tests"], "serverIds": ["not-source"], "allowImport": True},
    }))
    assert m["recovery"].prepare(options(m))["version"] == "1.21.1"
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", '{"target":{"buckets":["recovery-tests"],"serverIds":["not-source"]}}')
    with pytest.raises(RuntimeError, match="allowImport"):
        m["recovery"].discover(m["destination"])


def test_legacy_requires_explicit_launch_and_loader(migration):
    m = migration
    snapshot = m["publish"](legacy=True)
    data = options(m, objectKey=snapshot["objectKey"])
    with pytest.raises(ValueError, match="Legacy"):
        m["recovery"].prepare(data)
    with pytest.raises(ValueError, match="loader"):
        m["recovery"].prepare({**data, "legacy": {"version": "1.21.1"}})
    preview = m["recovery"].prepare({**data, "legacy": m["server"] | {"command": None}})
    assert preview["legacy"]
    assert m["recovery"].import_instance(confirm(preview))["status"] == "stopped"


def test_config_secrets_known_authority_and_map_consent_reset(migration):
    m = migration
    src = m["source"]
    (src / ".env").write_text("INSTALLATION_SECRET=fixture-env\n")
    (src / "auth.json").write_text('{"password":"fixture-auth"}')
    (src / "ops.json").write_text('[{"level":4}]')
    (src / ".fabricator-map.json").write_text('{"nonce":"fixture-nonce"}')
    (src / "config" / "fabricator-map.json").write_text('{"nonce":"fixture-map-nonce"}')
    (src / "config" / "bluemap").mkdir()
    (src / "config" / "bluemap" / "core.conf").write_text("accept-download: true")
    (src / "config" / "bluemap" / "webserver.conf").write_text("enabled: true")
    (src / "config" / "plugin.yml").write_text("api_key: fixture-plugin-key\n")
    snapshot = m["publish"]()
    preview = m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
    assert preview["secrets"]["suspectedFiles"] == ["config/plugin.yml"]
    assert "fixture-plugin-key" not in json.dumps(preview)
    with pytest.raises(ValueError, match="secrets"):
        m["recovery"].import_instance(confirm(preview))
    result = m["recovery"].import_instance(confirm(preview, retainEmbeddedSecrets=True))
    target = Path(m["storage"].get_server(result["serverId"])["installPath"])
    for path in (".env", "auth.json", "ops.json", ".fabricator-map.json", "config/fabricator-map.json"):
        assert not (target / path).exists()
    assert "accept-download: false" in (target / "config/bluemap/core.conf").read_text()
    assert "enabled: false" in (target / "config/bluemap/webserver.conf").read_text()
    assert "fixture-plugin-key" in (target / "config/plugin.yml").read_text()


def malicious_tar(kind):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tf:
        info = tarfile.TarInfo("../escape" if kind == "traversal" else "file")
        if kind in {"symlink", "hardlink"}:
            info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            info.linkname = "server.jar"
        elif kind == "device":
            info.type = tarfile.CHRTYPE
        elif kind == "huge":
            info.size = 100 * 1024**3 + 1
        else:
            info.size = 1
        if kind == "huge":
            # A header alone is enough: the limit must reject before reading the declared body.
            stream.write(info.tobuf())
        else:
            tf.addfile(info, BytesIO(b"x") if info.isfile() else None)
        if kind in {"duplicate", "case"}:
            again = tarfile.TarInfo("File" if kind == "case" else "file")
            again.size = 1
            tf.addfile(again, BytesIO(b"x"))
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["traversal", "symlink", "hardlink", "device", "duplicate", "case", "huge"])
def test_unsafe_archives_rejected_and_staging_cleaned(migration, kind):
    m = migration
    snapshot = m["publish"](raw=malicious_tar(kind))
    with pytest.raises(ValueError):
        m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
    assert not m["storage"].get_all_servers()
    assert not list(m["transfers"].registry().base_dir.glob(".s3-preview-*"))
    assert not (m["root"] / "escape").exists()


def test_checksum_corruption_and_sidecar_mismatch(migration):
    m = migration
    snap = m["snapshot"]
    head = m["client"].head_object(Bucket="recovery-tests", Key=snap["objectKey"])
    m["client"].put_object(Bucket="recovery-tests", Key=snap["objectKey"],
                           Body=b"corrupt".ljust(snap["sizeBytes"], b"!"), Metadata=head["Metadata"])
    with pytest.raises(RuntimeError, match="checksum"):
        m["recovery"].prepare(options(m))
    assert not list(m["transfers"].registry().base_dir.glob(".s3-preview-*"))
    sidecar = json.loads(m["client"].get_object(Bucket="recovery-tests",
                         Key=snap["objectKey"] + ".fabricator.json")["Body"].read())
    sidecar["snapshot"]["sha256"] = "0" * 64
    m["client"].put_object(Bucket="recovery-tests", Key=snap["objectKey"] + ".fabricator.json",
                           Body=json.dumps(sidecar))
    with pytest.raises(ValueError, match="sidecar"):
        m["recovery"].prepare(options(m))


def test_arbitrary_objects_not_discovered_and_sidecar_loss_is_explicit_legacy(migration):
    m = migration
    m["client"].put_object(Bucket="recovery-tests", Key="migration/fabricator/other/id/arbitrary.tar", Body=b"x")
    assert m["recovery"].discover(m["destination"])["rejectedObjects"] == 1
    m["client"].delete_object(Bucket="recovery-tests", Key=m["snapshot"]["objectKey"] + ".fabricator.json")
    assert m["recovery"].discover(m["destination"])["snapshots"][0]["legacy"]
    with pytest.raises(ValueError, match="Legacy"):
        m["recovery"].prepare(options(m))


def test_owner_binding_expiry_integrity_and_explicit_confirmation(migration):
    m = migration
    owner = {"issuer": "fixture", "sub": "one"}
    preview = m["recovery"].prepare(options(m), owner)
    with pytest.raises(ValueError, match="not found"):
        m["recovery"].import_instance(confirm(preview), {"issuer": "fixture", "sub": "two"})
    with pytest.raises(ValueError, match="Confirm"):
        m["recovery"].import_instance(confirm(preview, confirmName="wrong"), owner)
    root = m["recovery"]._preview_path(preview["previewId"])
    (root / "instance" / "world" / "region" / "r.0.0.mca").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        m["recovery"].import_instance(confirm(preview), owner)
    receipt = json.loads((root / "receipt.json").read_text())
    receipt["expiresAt"] = time.time() - 1
    (root / "receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="expired"):
        m["recovery"].preview_status(preview["previewId"], owner)
    m["recovery"].recover()
    assert not root.exists()


def test_registration_failure_retry_and_startup_recovery(migration, monkeypatch):
    m = migration
    preview = m["recovery"].prepare(options(m))
    register = m["storage"].register_fork
    def fail(record):
        register(record)
        raise OSError("fixture registration interruption")
    monkeypatch.setattr(m["storage"], "register_fork", fail)
    with pytest.raises(OSError):
        m["recovery"].import_instance(confirm(preview))
    assert not m["storage"].get_all_servers()
    assert m["recovery"].preview_status(preview["previewId"], None)
    monkeypatch.setattr(m["storage"], "register_fork", register)
    result = m["recovery"].import_instance(confirm(preview))
    server_id = result["serverId"]
    txn = m["transfers"].registry().base_dir / (".fork-" + server_id)
    txn.mkdir()
    (txn / "journal.json").write_text(json.dumps({"id": server_id, "committed": False}))
    assert "recovery" in m["transfers"].registry()._start_server_locked(
        m["storage"].get_server(server_id))["message"].lower()
    m["transfers"].recover()
    assert not m["storage"].get_server(server_id) and not txn.exists()


def test_space_version_and_identity_collisions(migration, monkeypatch):
    m = migration
    def no_space(*_):
        raise ValueError("Insufficient disk space")
    space = m["transfers"]._space
    monkeypatch.setattr(m["transfers"], "_space", no_space)
    with pytest.raises(ValueError, match="space"):
        m["recovery"].prepare(options(m))
    monkeypatch.setattr(m["transfers"], "_space", space)
    with pytest.raises(ValueError, match="version"):
        m["recovery"].prepare(options(m, legacy=m["server"] | {"version": "1.20.1"}))
    preview = m["recovery"].prepare(options(m))
    m["storage"].create_server({"id": "existing", "name": "Recovered", "port": 25565})
    with pytest.raises(ValueError, match="name"):
        m["recovery"].import_instance(confirm(preview))
    assert m["storage"].get_server("existing")


@pytest.mark.parametrize("unsafe", [False, True])
def test_hybrid_archives_validate_both_layers(migration, unsafe):
    m = migration
    data, world, outer = BytesIO(), BytesIO(), BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tf:
        for path in m["source"].iterdir():
            if path.name != "world":
                tf.add(path, arcname=path.name)
    with tarfile.open(fileobj=world, mode="w") as tf:
        tf.add(m["source"] / "world", arcname="world")
    with tarfile.open(fileobj=outer, mode="w") as tf:
        for name, raw in (("data.tar.gz", gzip.compress(malicious_tar("traversal")) if unsafe else data.getvalue()),
                          ("worlds.tar", world.getvalue())):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            tf.addfile(info, BytesIO(raw))
    snapshot = m["publish"](raw=outer.getvalue())
    if unsafe:
        with pytest.raises(ValueError, match="relative"):
            m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
    else:
        preview = m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
        result = m["recovery"].import_instance(confirm(preview))
        assert (Path(m["storage"].get_server(result["serverId"])["installPath"]) / "world/level.dat").read_bytes() == (m["source"] / "world/level.dat").read_bytes()


def test_unsafe_custom_launch_metadata_is_explicit_legacy(migration):
    m = migration
    m["server"]["jvmArgs"] = "-javaagent:/source/custom-agent.jar"
    snapshot = m["publish"]()
    raw = m["client"].get_object(Bucket="recovery-tests", Key=snapshot["objectKey"] + ".fabricator.json")["Body"].read()
    assert b"/source/custom-agent.jar" not in raw
    assert json.loads(raw)["recovery"]["requiresOperatorMetadata"]
    with pytest.raises(ValueError, match="Legacy"):
        m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))


def test_platform_argument_files_and_host_paths(migration):
    m = migration
    m["server"].update(loader="forge", loaderVersion="52.0.1",
                       launch={"type": "args_file", "args_file": "libraries/unix_args.txt"})
    (m["source"] / "libraries").mkdir()
    (m["source"] / "libraries/unix_args.txt").write_text("-cp libraries/fixture.jar\nfixture.Main\n")
    snapshot = m["publish"]()
    key = snapshot["objectKey"] + ".fabricator.json"
    sidecar = json.loads(m["client"].get_object(Bucket="recovery-tests", Key=key)["Body"].read())
    sidecar["recovery"]["platform"] = "windows"
    m["client"].put_object(Bucket="recovery-tests", Key=key, Body=json.dumps(sidecar))
    with pytest.raises(ValueError, match="platform-specific"):
        m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
    assert m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"], legacy=m["server"]))["legacy"]
    (m["source"] / "libraries/unix_args.txt").write_text("-cp /original/host/library.jar\n")
    snapshot = m["publish"]()
    with pytest.raises(ValueError, match="host paths"):
        m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"], legacy=m["server"]))


def test_post_commit_cleanup_failure_preserves_recovery_journal_and_reports_publication(migration, monkeypatch):
    m = migration
    from backend.backups import progress
    preview = m["recovery"].prepare(options(m))
    remove = m["transfers"]._remove
    def fail(path):
        if path.name.startswith(".fork-"):
            raise OSError("fixture cleanup failure")
        return remove(path)
    monkeypatch.setattr(m["transfers"], "_remove", fail)
    with pytest.raises(OSError):
        m["recovery"].import_instance(confirm(preview), job_id="cleanup-test")
    state = progress.get("cleanup-test")
    assert state["published"]
    record = m["storage"].get_server(state["result"]["serverId"])
    assert record["status"] == "stopped"
    monkeypatch.setattr(m["transfers"], "_remove", remove)
    m["transfers"].recover()
    assert m["storage"].get_server(record["id"])
    assert not list(m["transfers"].registry().base_dir.glob(".fork-*"))


def test_endpoint_errors_are_sanitized_and_no_new_permissions_or_credentials_leak(migration, monkeypatch):
    from botocore.client import BaseClient
    from botocore.exceptions import EndpointConnectionError
    m = migration
    def fail(*_):
        raise EndpointConnectionError(endpoint_url="https://fixture/?credential=provider-secret-canary")
    monkeypatch.setattr(BaseClient, "_make_api_call", fail)
    response = m["app"].test_client().post("/api/s3-recovery/discover", json={"destination": m["destination"]})
    assert response.status_code == 400
    assert "provider-secret-canary" not in response.get_data(as_text=True)
    assert "credentials" in response.get_json()["error"]


def test_recovery_cleans_interrupted_preview_but_keeps_ready_receipts(migration):
    m = migration
    preview = m["recovery"].prepare(options(m))
    interrupted = m["transfers"].registry().base_dir / (".s3-preview-" + "f" * 32)
    interrupted.mkdir()
    (interrupted / "partial.tar").write_bytes(b"partial")
    m["recovery"].recover()
    assert not interrupted.exists()
    assert m["recovery"].preview_status(preview["previewId"], None)


def test_invalid_configuration_does_not_log_embedded_secret_values(migration, monkeypatch, caplog):
    m = migration
    (m["source"] / "spigot.yml").write_text("settings: {password: fixture-yaml-secret-canary\n")
    snapshot = m["publish"]()
    class InlineThread:
        def __init__(self, target, **_):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(m["recovery"].threading, "Thread", InlineThread)
    job = m["recovery"].enqueue("s3-preview", options(m, objectKey=snapshot["objectKey"]), None)
    from backend.backups import progress
    state = progress.get(job)
    assert state["phase"] == "failed" and "YAML" in state["error"]
    assert "fixture-yaml-secret-canary" not in state["error"] + caplog.text
    assert not m["storage"].get_all_servers()


def test_large_control_files_are_bounded_before_parsing(migration):
    m = migration
    (m["source"] / "bukkit.yml").write_bytes(b"x" * (1024 * 1024 + 1))
    snapshot = m["publish"]()
    with pytest.raises(ValueError, match="1 MiB"):
        m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))


def test_implicit_directories_count_towards_archive_entry_limit(tmp_path, monkeypatch):
    from backend.backups import portable
    monkeypatch.setattr(portable, "MAX_ENTRIES", 4)
    path = tmp_path / "deep.tar"
    with tarfile.open(path, "w") as tf:
        entry = tarfile.TarInfo("a/b/c/d/file")
        entry.size = 1
        tf.addfile(entry, BytesIO(b"x"))
    with pytest.raises(ValueError, match="entry"):
        portable.inspect_archive(path)


@pytest.mark.parametrize("names", [
    ("World/a", "world/b"),
    ("World/a", "world"),
])
def test_case_colliding_implicit_directories_are_rejected(tmp_path, names):
    from backend.backups import portable
    path = tmp_path / "case.tar"
    with tarfile.open(path, "w") as tf:
        for name in names:
            entry = tarfile.TarInfo(name)
            if name == "world":
                entry.type = tarfile.DIRTYPE
                tf.addfile(entry)
            else:
                entry.size = 1
                tf.addfile(entry, BytesIO(b"x"))
    with pytest.raises(ValueError, match="case-colliding"):
        portable.inspect_archive(path)


@pytest.mark.parametrize("compressed", [False, True])
def test_large_pax_metadata_rejected_before_unbounded_read(tmp_path, compressed):
    from backend.backups import portable
    metadata = tarfile.TarInfo("metadata")
    metadata.type = tarfile.XHDTYPE
    metadata.size = 2 * 1024 * 1024
    raw = metadata.tobuf() + b"\0" * 1024
    path = tmp_path / "pax.tar"
    if compressed:
        stream = BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as outer:
            for name, body in (("data.tar.gz", gzip.compress(raw)), ("worlds.tar", b"\0" * 10240)):
                info = tarfile.TarInfo(name)
                info.size = len(body)
                outer.addfile(info, BytesIO(body))
        raw = stream.getvalue()
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="metadata"):
        portable.inspect_archive(path)


def test_multipart_sized_archive_import_streams_independent_bytes(migration):
    m = migration
    raw = b"fixture-world-data-" * (1024 * 1024)
    (m["source"] / "plugin-data.bin").write_bytes(raw)
    snapshot = m["publish"]()
    assert snapshot["sizeBytes"] > 16 * 1024 * 1024
    preview = m["recovery"].prepare(options(m, objectKey=snapshot["objectKey"]))
    result = m["recovery"].import_instance(confirm(preview))
    target = Path(m["storage"].get_server(result["serverId"])["installPath"])
    assert (target / "plugin-data.bin").read_bytes() == raw
