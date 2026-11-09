"""Durable, permission-scoped task status and honest archive availability."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.backups import destinations, progress
from backend.events import store


@pytest.fixture(autouse=True)
def isolated_progress():
    global destinations, progress, store
    from backend.backups import destinations, progress
    from backend.events import store
    progress.reset_for_tests()
    destinations._availability_cache.clear()
    yield
    progress.reset_for_tests()
    destinations._availability_cache.clear()


def test_task_progress_is_durable_sanitized_and_interrupted_on_recovery():
    progress.start("bjb_status", server_id="a", kind="backup", config_id="cfg_generic")
    progress.update("bjb_status", phase="uploading", total_bytes=100, transferred_bytes=25,
                    error="secret-canary", file_name="private-canary", destination={"password": "secret-canary"})
    task = store.tasks("a")["items"][0]
    assert task["phase"] == "uploading" and task["active"]
    assert task["total_bytes"] == 100 and task["transferred_bytes"] == 25
    progress.update("bjb_status", transferred_bytes=50)
    assert store.tasks("a")["items"][0]["transferred_bytes"] == 50
    assert "canary" not in json.dumps(task)
    assert b"canary" not in store.db_path().read_bytes()
    assert "actor" not in task
    progress.reset_for_tests()
    store.recover()
    status = progress.get_status("bjb_status")
    assert status["phase"] == "interrupted" and status["active"] is False
    assert status["elapsed_seconds"] >= 0


def test_job_claim_is_atomic_and_terminal_jobs_release_it(monkeypatch):
    progress.start("bjb_first", server_id="a", kind="backup")
    with pytest.raises(progress.JobBusyError):
        progress.start("bjr_second", server_id="a", kind="restore")
    assert progress.get("bjr_second") == {}
    progress.start("bjb_other", server_id="b", kind="backup")
    progress.update("bjb_first", phase="done")
    progress.start("bjr_second", server_id="a", kind="restore")
    thread = Mock()
    thread.start.side_effect = RuntimeError("worker limit")
    monkeypatch.setattr(progress.threading, "Thread", lambda **kw: thread)
    with pytest.raises(RuntimeError):
        progress.launch("bjr_second", lambda: None, "isolated-worker")
    assert not progress.is_active("bjr_second")
    assert store.task("backup:bjr_second")["phase"] == "failed"


def test_old_job_schema_is_migrated_without_discarding_records():
    import sqlite3
    import time
    path = store.db_path()
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs(key TEXT PRIMARY KEY,run_id TEXT,server_id TEXT,"
                   "action TEXT,actor TEXT,at REAL,terminal INTEGER)")
        db.execute("INSERT INTO jobs VALUES('backup:old','original','a','worlds.restore','{}',?,0)", (time.time(),))
    store.recover()
    assert store.task("backup:old")["phase"] == "interrupted"
    assert progress.get_status("old")["kind"] == "restore"


def test_stale_status_is_not_marked_completed():
    progress.start("bjb_stale", server_id="a", kind="backup")
    with store.connection() as db:
        db.execute("UPDATE jobs SET updated=updated-121")
    task = store.tasks("a")["items"][0]
    assert task["active"] and task["stale"] and task["phase"] == "queued"


def test_task_route_scopes_operations_and_related_servers(client, tmp_servers_root, monkeypatch):
    from backend.events import routes
    (Path(tmp_servers_root) / "servers.json").write_text(json.dumps([
        {"id": "a", "name": "Generic server", "loader": "fabric", "version": "1.21.1"},
    ]))
    progress.start("bjb_allowed", server_id="a", kind="backup")
    progress.update("bjr_hidden", phase="starting", server_id="a", kind="restore")
    progress.update("transfer_private", phase="starting", sourceId="b", destinationId="a", kind="world-copy")
    grants = {("server.view", "a"), ("backups.create", "a")}
    monkeypatch.setattr(routes, "can", lambda permission, sid: (permission, sid) in grants)
    response = client.get("/api/servers/a/tasks")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [task["job_id"] for task in response.json["items"]] == ["bjb_allowed"]
    assert response.json["items"][0]["server_name"] == "Generic server"
    grants.add(("activity.read", "a"))
    assert {task["job_id"] for task in client.get("/api/servers/a/tasks").json["items"]} == {
        "bjb_allowed", "bjr_hidden",
    }
    grants.clear()
    assert client.get("/api/servers/a/tasks").json["items"] == []
    assert client.get("/api/servers/absent/tasks").status_code == 404


def test_local_absence_is_distinct_from_permission_failure(tmp_path, monkeypatch):
    path = tmp_path / "generic.tar"
    snapshot = {"filePath": str(path)}
    assert destinations.availability(snapshot)[0] == "missing"
    path.write_bytes(b"synthetic archive")
    assert destinations.availability(snapshot) == ("available", None)
    original = Path.stat
    def denied(current, *args, **kwargs):
        if current == path:
            raise PermissionError("private-canary")
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", denied)
    state, error = destinations.availability(snapshot)
    assert state == "unavailable" and "canary" not in error
    assert destinations.availability({}) == ("none", None)


@pytest.fixture
def remote(monkeypatch):
    import boto3
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", json.dumps({
        "default": {"buckets": ["isolated"], "serverIds": ["a"]},
    }))
    client = Mock()
    client.head_object.return_value = {"ContentLength": 10, "Metadata": {"sha256": "hash"}}
    monkeypatch.setattr(boto3, "Session", lambda **kw: Mock(client=Mock(return_value=client)))
    def snapshot(index=0):
        sid = f"snapshot{index}"
        return {
            "serverId": "a", "id": sid, "fileName": "generic.tar", "sizeBytes": 10,
            "sha256": "hash", "destination": {"type": "s3", "profile": "default", "bucket": "isolated"},
            "objectKey": f"fabricator/a/{sid}/generic.tar", "filePath": None,
        }
    return client, snapshot


@pytest.mark.parametrize("code,state", [("404", "missing"), ("NoSuchKey", "missing"), ("AccessDenied", "unavailable")])
def test_remote_absence_is_not_credentials_or_provider_failure(remote, code, state):
    from botocore.exceptions import ClientError
    client, snapshot = remote
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "credential-canary"}}, "HeadObject",
    )
    actual, error = destinations.availability(snapshot(), check_remote=True)
    assert actual == state
    assert error and "canary" not in error


def test_remote_checks_are_bounded_cached_and_revalidate_profile_grants(remote, monkeypatch):
    client, snapshot = remote
    assert destinations.availability(snapshot())[0] == "unchecked"
    client.head_object.assert_not_called()
    budget = [20]
    states = [destinations.availability(snapshot(i), check_remote=True, budget=budget)[0] for i in range(25)]
    assert states == ["available"] * 20 + ["unchecked"] * 5
    assert client.head_object.call_count == 20
    assert destinations.availability(snapshot())[0] == "available"
    monkeypatch.setenv("FABRICATOR_BACKUP_S3_PROFILES", "{}")
    assert destinations.availability(snapshot(), check_remote=True)[0] == "unavailable"
    assert client.head_object.call_count == 20


def test_indexed_missing_entries_remain_listed_and_download_fails_honestly(client, tmp_servers_root, monkeypatch):
    from backend.backups import storage
    root = Path(tmp_servers_root)
    (root / "servers.json").write_text(json.dumps([{"id": "a", "name": "Generic"}]))
    monkeypatch.setenv("BACKUPS_DIR", str(root / "archive-index"))
    snapshot = storage.record_snapshot("a", {"filePath": str(root / "missing.tar"), "fileName": "missing.tar"})
    response = client.get("/api/servers/a/snapshots")
    assert response.json[0]["id"] == snapshot["id"]
    assert response.json[0]["availability"] == "missing"
    assert storage.get_snapshot("a", snapshot["id"]) is not None
    assert client.get(f"/api/servers/a/snapshots/{snapshot['id']}/download").status_code == 404


def test_duplicate_http_submission_is_a_conflict_without_starting_another_worker(client, tmp_servers_root):
    (Path(tmp_servers_root) / "servers.json").write_text(json.dumps([{"id": "a", "name": "Generic"}]))
    progress.start("bjb_existing", server_id="a", kind="backup")
    response = client.post("/api/servers/a/backup-quick", json={})
    assert response.status_code == 409
    assert "already queued or running" in response.json["error"]
    assert len(store.tasks("a")["items"]) == 1


@pytest.mark.parametrize("phase,outcome", [
    ("completed", "succeeded"), ("error", "failed"),
    ("cancelled", "skipped"), ("interrupted", "interrupted"),
])
def test_all_known_terminal_states_stop_polling_and_release_submission(phase, outcome):
    progress.start("bjb_terminal", server_id="a", kind="backup")
    progress.update("bjb_terminal", phase=phase)
    assert not progress.is_active("bjb_terminal")
    assert progress.get_status("bjb_terminal")["active"] is False
    assert store.task("backup:bjb_terminal")["phase"] == phase
    assert store.history("a")["items"][0]["outcome"] == outcome
    progress.start("bjb_next", server_id="a", kind="backup")
