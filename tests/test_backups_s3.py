"""Isolated S3 protocol tests; install tests/requirements-s3.txt to run."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock
import zipfile
from io import BytesIO

import boto3
from botocore.client import BaseClient
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
import pytest

moto = pytest.importorskip("moto", reason="Install tests/requirements-s3.txt")


@pytest.fixture
def remote(tmp_servers_root, monkeypatch):
    from backend.backups import destinations, progress, restore, service, storage
    monkeypatch.setenv("BACKUPS_DIR", str(tmp_servers_root / "backup-state"))
    monkeypatch.setenv("FABRICATOR_DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "default": {"buckets": ["backup-tests"], "region": "us-east-1"},
    }))
    storage.reset_for_tests()
    install = tmp_servers_root / "servers" / "srv_s3"
    install.mkdir(parents=True)
    (install / "world").mkdir()
    (install / "world" / "level.dat").write_text("original")
    (install / "server.properties").write_text("level-name=world\n")
    server = {"id": "srv_s3", "name": "S3 test", "loader": "fabric",
              "installPath": "srv_s3", "status": "stopped", "levelName": "world"}
    (tmp_servers_root / "servers.json").write_text(json.dumps([server]))
    registry = MagicMock()
    registry.resolve_install_path.return_value = install
    registry.get_status.return_value = {"status": "stopped"}
    registry.stop_server.return_value = {"status": "stopped"}
    registry.start_server.return_value = {"status": "running"}
    for module in (service, restore, storage):
        monkeypatch.setattr(module, "get_server_process_registry", lambda: registry)
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="backup-tests")
        cfg = storage.create_config("srv_s3", {
            "name": "Remote", "destination": {
                "type": "s3", "bucket": "backup-tests", "prefix": "shared",
            }, "flush": False, "compress": False,
        })
        yield dict(s3=s3, cfg=cfg, install=install, registry=registry,
                   storage=storage, service=service, restore=restore,
                   destinations=destinations, progress=progress, root=tmp_servers_root)
    storage.reset_for_tests()


def _backup(remote):
    return remote["service"].run_backup(remote["cfg"]["id"])


def _deny_api(monkeypatch, operation, *, metadata_only=False):
    original = BaseClient._make_api_call

    def call(self, operation_name, params):
        if operation_name == operation and (
            not metadata_only or params.get("Key", "").endswith(".fabricator.json")
        ):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "SECRET-CANARY"}},
                operation_name,
            )
        return original(self, operation_name, params)
    monkeypatch.setattr(BaseClient, "_make_api_call", call)


@pytest.mark.parametrize("compress", [False, True])
@pytest.mark.parametrize("mode", ["in_place", "reset"])
def test_remote_roundtrip_metadata_config_edit_and_restore(remote, compress, mode):
    store = remote["storage"]
    store.update_config("srv_s3", remote["cfg"]["id"], {"compress": compress})
    snap = _backup(remote)
    assert snap["filePath"] is None and snap["sha256"]
    assert snap["destination"]["bucket"] == "backup-tests"
    assert snap["objectKey"].startswith("shared/fabricator/srv_s3/" + snap["id"])
    sidecar = json.loads(remote["s3"].get_object(
        Bucket="backup-tests", Key=snap["objectKey"] + ".fabricator.json",
    )["Body"].read())
    assert sidecar["format"] == "fabricator-backup" and sidecar["version"] == 1
    assert sidecar["snapshot"]["sha256"] == snap["sha256"]
    assert sidecar["snapshot"]["serverId"] == "srv_s3"
    assert sidecar["snapshot"]["configId"] == remote["cfg"]["id"]
    assert sidecar["snapshot"]["filePath"] is None
    assert "testing" not in json.dumps(sidecar)
    assert list((remote["root"] / "backup-state" / "transfer" / remote["cfg"]["id"]).iterdir()) == []
    with remote["destinations"].materialise(snap) as path:
        assert path.exists() and remote["destinations"].checksum(path) == snap["sha256"]
        temporary_path = path
    assert not temporary_path.exists()
    # Changing the saved destination cannot redirect an existing snapshot.
    store.update_config("srv_s3", remote["cfg"]["id"], {
        "destination": {"type": "s3", "bucket": "backup-tests", "prefix": "elsewhere"},
    })
    (remote["install"] / "world" / "level.dat").write_text("changed")
    remote["registry"].get_status.return_value = {"status": "running"}
    result = remote["restore"].run_restore(snap["id"], mode=mode)
    assert (remote["install"] / "world" / "level.dat").read_text() == "original"
    safety = store.get_snapshot("srv_s3", result["safetySnapshotId"])
    assert Path(safety["filePath"]).parent == remote["install"] / "backups"
    assert Path(safety["filePath"]).exists()
    remote["registry"].stop_server.assert_called_once()
    remote["registry"].start_server.assert_called_once()


def test_server_restarts_before_upload(remote, monkeypatch):
    remote["storage"].update_config("srv_s3", remote["cfg"]["id"], {"shutdown": True})
    registry = remote["registry"]
    registry.get_status.return_value = {"status": "running"}
    real_publish = remote["destinations"].publish

    def publish(*args):
        registry.start_server.assert_called_once()
        return real_publish(*args)
    monkeypatch.setattr(remote["destinations"], "publish", publish)
    _backup(remote)
    registry.start_server.assert_called_once()


@pytest.mark.parametrize("operation,metadata", [
    ("PutObject", False), ("GetObject", False), ("PutObject", True),
])
def test_transfer_failure_never_runs_retention_and_cleans_local_files(
    remote, monkeypatch, operation, metadata,
):
    old = _backup(remote)
    remote["storage"].update_config("srv_s3", remote["cfg"]["id"], {"maxSnapshots": 1})
    retain = MagicMock()
    monkeypatch.setattr(remote["service"], "_apply_retention", retain)
    _deny_api(monkeypatch, operation, metadata_only=metadata)
    with pytest.raises(remote["destinations"].DestinationError) as error:
        _backup(remote)
    assert "SECRET-CANARY" not in str(error.value)
    retain.assert_not_called()
    assert remote["storage"].get_snapshot("srv_s3", old["id"])
    assert list((remote["root"] / "backup-state" / "transfer" / remote["cfg"]["id"]).iterdir()) == []


def test_corrupt_download_does_not_stop_server_or_write_safety(remote):
    snap = _backup(remote)
    remote["s3"].put_object(Bucket="backup-tests", Key=snap["objectKey"], Body=b"corrupt")
    remote["registry"].get_status.return_value = {"status": "running"}
    with pytest.raises(remote["destinations"].DestinationError, match="checksum mismatch"):
        remote["restore"].run_restore(snap["id"], mode="reset")
    remote["registry"].stop_server.assert_not_called()
    assert not [s for s in remote["storage"].list_snapshots("srv_s3") if s["type"] == "safety"]
    assert (remote["install"] / "world" / "level.dat").read_text() == "original"


def test_remote_restore_safety_failure_leaves_live_data(remote, monkeypatch):
    snap = _backup(remote)
    (remote["install"] / "world" / "level.dat").write_text("live")
    monkeypatch.setattr(remote["restore"], "_write_safety_snapshot", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        remote["restore"].run_restore(snap["id"], mode="reset")
    assert (remote["install"] / "world" / "level.dat").read_text() == "live"


def test_retention_uses_original_keys_and_does_not_delete_other_objects(remote):
    old = _backup(remote)
    remote["s3"].put_object(Bucket="backup-tests", Key="shared/not-ours.tar", Body=b"other")
    remote["storage"].update_config("srv_s3", remote["cfg"]["id"], {
        "maxSnapshots": 1,
        "destination": {"type": "s3", "bucket": "backup-tests", "prefix": "new"},
    })
    new = _backup(remote)
    assert remote["storage"].get_snapshot("srv_s3", old["id"]) is None
    keys = [o["Key"] for o in remote["s3"].list_objects_v2(Bucket="backup-tests")["Contents"]]
    assert old["objectKey"] not in keys
    assert old["objectKey"] + ".fabricator.json" not in keys
    assert new["objectKey"] in keys and "shared/not-ours.tar" in keys


def test_retention_deletion_failure_keeps_record(remote, monkeypatch):
    old = _backup(remote)
    remote["storage"].update_config("srv_s3", remote["cfg"]["id"], {"maxSnapshots": 1})
    _deny_api(monkeypatch, "DeleteObject")
    with pytest.raises(RuntimeError, match="retention failed"):
        _backup(remote)
    assert remote["storage"].get_snapshot("srv_s3", old["id"])
    assert len([s for s in remote["storage"].list_snapshots("srv_s3") if s["status"] == "success"]) == 2


def test_multipart_upload_and_abort(remote, monkeypatch, tmp_path):
    path = tmp_path / "large.tar"
    path.write_bytes(b"x" * (17 * 1024 * 1024))
    data = {"serverId": "srv_s3", "fileName": "large.tar", "sizeBytes": path.stat().st_size,
            "configId": remote["cfg"]["id"], "createdAt": "2026-11-06T00:00:00Z"}
    real_call = BaseClient._make_api_call
    calls = []

    def call(self, operation, params):
        calls.append(operation)
        return real_call(self, operation, params)
    monkeypatch.setattr(BaseClient, "_make_api_call", call)
    remote["destinations"].publish(path, dict(data), remote["cfg"], "multipart")
    assert "CreateMultipartUpload" in calls and "CompleteMultipartUpload" in calls
    assert calls.count("UploadPart") == 2
    _deny_api(monkeypatch, "UploadPart")
    with pytest.raises(remote["destinations"].DestinationError):
        remote["destinations"].publish(path, dict(data), remote["cfg"], "multipart-fail")
    assert "AbortMultipartUpload" in calls
    assert remote["s3"].list_multipart_uploads(Bucket="backup-tests").get("Uploads", []) == []


@pytest.mark.parametrize("fmt", ["tar", "zip"])
def test_remote_download_and_temporary_cleanup(client, remote, monkeypatch, fmt):
    snap = _backup(remote)
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(remote["root"]))
    response = client.get(f"/api/servers/srv_s3/snapshots/{snap['id']}/download?format={fmt}")
    assert response.status_code == 200
    data = response.get_data()
    if fmt == "zip":
        with zipfile.ZipFile(BytesIO(data)) as archive:
            assert archive.read("world/level.dat") == b"original"
    else:
        import hashlib
        assert hashlib.sha256(data).hexdigest() == snap["sha256"]
    response.close()
    assert list(remote["root"].glob("fabricator-s3-download-*")) == []


def test_remote_config_delete_retains_access_and_snapshot_delete(client, remote):
    snap = _backup(remote)
    deleted = client.delete(f"/api/servers/srv_s3/backup-configs/{remote['cfg']['id']}")
    assert deleted.status_code == 200
    assert deleted.json["retained_paths"] == [remote["destinations"].location(snap)]
    assert remote["storage"].get_snapshot("srv_s3", snap["id"])
    response = client.get(f"/api/servers/srv_s3/snapshots/{snap['id']}/download")
    assert response.status_code == 200
    response.close()
    assert client.delete(f"/api/servers/srv_s3/snapshots/{snap['id']}").status_code == 200
    assert remote["storage"].get_snapshot("srv_s3", snap["id"]) is None
    assert remote["s3"].list_objects_v2(Bucket="backup-tests").get("Contents", []) == []


@pytest.mark.parametrize("purge", [False, True])
def test_deletion_failure_keeps_snapshot_and_config(client, remote, monkeypatch, purge):
    snap = _backup(remote)
    _deny_api(monkeypatch, "DeleteObject")
    url = (f"/api/servers/srv_s3/backup-configs/{remote['cfg']['id']}?purge=1"
           if purge else f"/api/servers/srv_s3/snapshots/{snap['id']}")
    response = client.delete(url)
    assert response.status_code == (502 if purge else 500)
    assert remote["storage"].get_snapshot("srv_s3", snap["id"])
    assert remote["storage"].get_config_record("srv_s3", remote["cfg"]["id"])
    assert "SECRET-CANARY" not in response.get_data(as_text=True)


def test_availability_and_corrupt_download_errors(client, remote):
    snap = _backup(remote)
    url = "/api/servers/srv_s3/snapshots"
    assert client.get(url).json[0]["availability"] == "unchecked"
    assert client.get(url + "?check_remote=1").json[0]["availability"] == "available"
    remote["s3"].put_object(Bucket="backup-tests", Key=snap["objectKey"], Body=b"bad")
    assert client.get(url + "?check_remote=1").json[0]["availability"] == "unavailable"
    assert client.get(url + f"/{snap['id']}/download").status_code == 502


@pytest.mark.parametrize("change", [
    {"endpointUrl": "https://untrusted.example"},
    {"profile": "not-approved"},
    {"bucket": "other-bucket"},
    {"accessKeyId": "SECRET-CANARY"},
    {"prefix": "../unsafe"},
])
def test_destination_validation(client, remote, change):
    response = client.post("/api/servers/srv_s3/backup-configs", json={
        "name": "bad", "destination": {**remote["cfg"]["destination"], **change},
    })
    assert response.status_code == 400
    assert "SECRET-CANARY" not in response.get_data(as_text=True)


def test_profile_server_and_endpoint_restrictions(client, remote, monkeypatch):
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "default": {"buckets": ["backup-tests"], "serverIds": ["different"]},
    }))
    response = client.post("/api/servers/srv_s3/backup-configs", json={
        "name": "bad", "destination": remote["cfg"]["destination"],
    })
    assert response.status_code == 400 and "server" in response.json["error"]
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "default": {"buckets": ["backup-tests"], "endpointUrl": "https://minio.example"},
    }))
    with pytest.raises(remote["destinations"].DestinationError, match="endpoint changed"):
        with remote["destinations"]._client(remote["cfg"]["destination"], "srv_s3"):
            pytest.fail("Changed endpoints must never be contacted")


@pytest.mark.parametrize("failure", [
    NoCredentialsError(),
    EndpointConnectionError(endpoint_url="https://SECRET-CANARY.example"),
])
def test_connection_error_is_actionable_without_raw_sdk_text(remote, monkeypatch, failure):
    monkeypatch.setattr(boto3, "Session", MagicMock(side_effect=failure))
    with pytest.raises(remote["destinations"].DestinationError, match="credentials"):
        with remote["destinations"]._client(remote["cfg"]["destination"], "srv_s3"):
            pytest.fail("Client should not be constructed")


def test_invalid_ownership_never_calls_s3(remote, monkeypatch):
    snap = _backup(remote)
    snap["objectKey"] = "shared/not-ours.tar"
    spy = MagicMock()
    monkeypatch.setattr(remote["destinations"], "_client", spy)
    with pytest.raises(remote["destinations"].DestinationError, match="ownership"):
        remote["destinations"].delete_archive(snap)
    spy.assert_not_called()


def test_sidecar_recovers_snapshot_after_index_loss(remote):
    original = _backup(remote)
    sidecar = json.loads(remote["s3"].get_object(
        Bucket="backup-tests", Key=original["objectKey"] + ".fabricator.json",
    )["Body"].read())
    store = remote["storage"]
    store.delete_server_file("srv_s3")
    # Follow the documented offline index recovery; the saved config is gone.
    index = remote["root"] / "backup-state" / "srv_s3.json"
    index.write_text(json.dumps({"configs": [], "snapshots": [sidecar["snapshot"]]}))
    store.reset_for_tests()
    (remote["install"] / "world" / "level.dat").write_text("live")
    result = remote["restore"].run_restore(original["id"], mode="reset")
    assert result["status"] == "success"
    assert (remote["install"] / "world" / "level.dat").read_text() == "original"
    assert Path(store.get_snapshot("srv_s3", result["safetySnapshotId"])["filePath"]).is_file()


def test_partial_delete_failure_can_be_retried(client, remote, monkeypatch):
    snap = _backup(remote)
    real_call = BaseClient._make_api_call

    def call(self, operation, params):
        if operation == "DeleteObject" and params["Key"] == snap["objectKey"]:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation)
        return real_call(self, operation, params)
    with monkeypatch.context() as patch:
        patch.setattr(BaseClient, "_make_api_call", call)
        assert client.delete(f"/api/servers/srv_s3/snapshots/{snap['id']}").status_code == 500
    keys = [o["Key"] for o in remote["s3"].list_objects_v2(Bucket="backup-tests")["Contents"]]
    assert snap["objectKey"] in keys and snap["objectKey"] + ".fabricator.json" not in keys
    assert remote["storage"].get_snapshot("srv_s3", snap["id"])
    assert client.delete(f"/api/servers/srv_s3/snapshots/{snap['id']}").status_code == 200


def test_async_transfer_jobs_are_server_scoped(remote, monkeypatch):
    snap = _backup(remote)
    monkeypatch.setattr(remote["service"].threading, "Thread", MagicMock())
    backup_job = remote["service"].run_backup_async(remote["cfg"]["id"])
    restore_job = remote["restore"].run_restore_async(snap["id"], mode="reset")
    quick_job = remote["service"].run_adhoc_backup_async("srv_s3")
    for job in (backup_job, restore_job, quick_job):
        assert remote["progress"].get(job)["server_id"] == "srv_s3"
