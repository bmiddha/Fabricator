"""Alert persistence, safe destinations, scoped reads and payload-free audit coverage."""
import json
import os
import socket
import shutil
import threading
import time
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.events import service, store, transport


@pytest.fixture(autouse=True)
def live_modules():
    global service, store, transport
    from backend.events import service, store, transport


def channel(url="https://hooks.example/secret-canary", **kwargs):
    return service.save_destination({"name": "Operations", "kind": "webhook", "url": url, **kwargs})


def test_encrypted_secret_never_returns_or_enters_audit(client):
    response = client.post("/api/notifications/destinations", json={
        "name": "Operations", "kind": "webhook", "url": "https://hooks.example/secret-canary",
    })
    assert response.status_code == 201
    body = client.get("/api/notifications").get_data(as_text=True)
    assert "secret-canary" not in body and "hooks.example" not in body
    assert b"secret-canary" not in store.db_path().read_bytes()
    if os.name != "nt":
        assert store.db_path().stat().st_mode & 0o777 == 0o600
        assert (store.db_path().parent / "webhooks.key").stat().st_mode & 0o777 == 0o600
    rows = store.history(instance=True)["items"]
    assert rows[0]["action"] == "notifications.configure"
    assert rows[0]["actor"]["kind"] == "unattributed"
    assert "secret-canary" not in json.dumps(rows)


def test_secret_preservation_replacement_and_queue_cancellation():
    dest = channel()
    store.emit("server.crashed", server_id="a", outcome="failed", alert="server.crashed")
    with store.connection() as db:
        secret = db.execute("SELECT secret FROM destinations").fetchone()[0]
    service.save_destination({"name": "Renamed", "kind": "webhook", "enabled": False}, dest)
    with store.connection() as db:
        assert db.execute("SELECT secret FROM destinations").fetchone()[0] == secret
    assert service.deliveries()[0]["state"] == "cancelled"
    assert service.delete_destination(dest)
    assert not service.destinations()


def test_retry_limit_survives_restart_and_reports_failure(monkeypatch):
    channel()
    sender = Mock(side_effect=transport.DeliveryError("http_503"))
    monkeypatch.setattr(transport, "deliver", sender)
    store.emit("backups.create", server_id="a", outcome="failed", alert="backup.failed")
    now = time.time() + 1
    for delay in (0, *service.RETRY_DELAYS):
        now += delay
        assert service.deliver_due(now)
        store.recover()
    row = service.deliveries()[0]
    assert row["attempts"] == 5 and row["state"] == "failed" and row["error"] == "http_503"
    assert not service.deliver_due(now + 100000)
    assert sender.call_count == 5


def test_claim_recovery_and_deduplication(monkeypatch):
    channel()
    store.emit("server.crashed", server_id="a", outcome="failed", alert="server.crashed", cooldown=300)
    store.emit("server.crashed", server_id="a", outcome="failed", alert="server.crashed", cooldown=300)
    assert len(service.deliveries()) == 1
    with store.connection() as db:
        db.execute("UPDATE deliveries SET state='sending',attempts=1")
    store.recover()
    sender = Mock()
    monkeypatch.setattr(transport, "deliver", sender)
    assert service.deliver_due(time.time() + 1)
    assert service.deliveries()[0]["state"] == "sent"
    sender.assert_called_once()


def test_tests_are_targeted_and_rate_limited(client):
    first, second = channel(), channel("https://other.example/secret")
    response = client.post(f"/api/notifications/destinations/{first}/test")
    assert response.status_code == 202
    assert client.post(f"/api/notifications/destinations/{first}/test").status_code == 400
    assert [r["destination_id"] for r in service.deliveries()] == [first]
    assert second != first


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254",
    "100.100.100.100", "0.0.0.0", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1", "224.0.0.1",
])
def test_default_blocks_nonpublic_dns(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))])
    with pytest.raises(transport.DeliveryError, match="target_blocked"):
        transport.resolve("https://hooks.example/path")


def test_rebinding_and_mixed_answers_fail_closed(monkeypatch):
    answers = iter([["8.8.8.8"], ["8.8.8.8", "10.1.2.3"]])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (ip, 443)) for ip in next(answers)])
    assert transport.resolve("https://hooks.example/path")[3] == ["8.8.8.8"]
    with pytest.raises(transport.DeliveryError):
        transport.resolve("https://hooks.example/path")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://hooks.example/path", "https://user:secret@hooks.example",
    "https://hooks.example:8443/path", "https://hooks.example/#secret", "https://hooks.example\\@localhost/",
    "https://hooks.example/\r\nbad",     "https://hooks.example:bad/", "https://hooks.example:0/",
])
def test_url_validation(url):
    with pytest.raises(ValueError):
        transport.target(url)


def test_invalid_destination_types_are_validation_errors(client):
    assert client.post("/api/notifications/destinations", json={"name": "Fixture", "kind": {}}).status_code == 400
    assert client.post("/api/notifications/destinations", data=b"x" * 17000, content_type="application/json").status_code == 413


def test_private_allowlist_is_exact_and_cannot_rebind(monkeypatch):
    monkeypatch.setenv("FABRICATOR_WEBHOOK_PRIVATE_TARGETS", '{"http://hooks.internal:8080":["10.1.2.3"]}')
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("10.1.2.4", 8080))])
    with pytest.raises(transport.DeliveryError):
        transport.resolve("http://hooks.internal:8080/path")
    with pytest.raises(ValueError):
        transport.resolve("http://other.internal:8080/path")


def test_checked_addresses_fall_back_without_resolving_again(monkeypatch):
    connected = Mock()
    connect = Mock(side_effect=[OSError("unreachable"), connected])
    monkeypatch.setattr(socket, "create_connection", connect)
    assert transport._connect_checked(["2001:4860:4860::8888", "8.8.8.8"], 443, 5) is connected
    assert [call.args[0] for call in connect.call_args_list] == [("2001:4860:4860::8888", 443), ("8.8.8.8", 443)]


def test_local_fixture_delivery_no_redirect_and_discord_no_mentions(monkeypatch):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(302 if self.path == "/redirect" else 204)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("FABRICATOR_WEBHOOK_PRIVATE_TARGETS", json.dumps({origin: ["127.0.0.1"]}))
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    event = {"id": 1, "path": "/server/a/activity", "alert": "job.failed", "action": "mods.install", "outcome": "failed"}
    try:
        transport.deliver(origin + "/ok", "webhook", event)
        transport.deliver(origin + "/discord", "discord", event)
        with pytest.raises(transport.DeliveryError, match="http_302"):
            transport.deliver(origin + "/redirect", "webhook", event)
        assert len(received) == 3
        assert received[0][1]["url"] == "/server/a/activity"
        assert received[1][1]["allowed_mentions"] == {"parse": []}
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_job_actor_is_preserved_and_payloads_are_excluded(app):
    from flask import g
    from backend.backups import progress
    with app.test_request_context("/"):
        g.audit_actor = {"kind": "oidc", "id": "alice", "issuer": "https://id.example"}
        progress.update("audit-test", phase="starting", kind="world_import", server_id="a")
    progress.update("audit-test", phase="failed", error="Bearer secret-canary console-sensitive")
    progress.update("audit-test", phase="failed")
    rows = store.history("a")["items"]
    assert len(rows) == 1
    assert rows[0]["actor"]["id"] == "alice"
    assert rows[0]["action"] == "worlds.import" and rows[0]["alert"] == "job.failed"
    assert "secret-canary" not in json.dumps(rows)


def test_interrupted_jobs_are_explicit_and_recovered_once():
    store.track_job("abandoned", "a", "backups.create", "starting")
    store.recover()
    store.recover()
    rows = store.history("a")["items"]
    assert len(rows) == 1 and rows[0]["outcome"] == "interrupted" and rows[0]["alert"] == "backup.failed"


def test_history_is_paginated_and_scoped():
    for _ in range(55):
        store.emit("configs.update", server_id="a")
    store.emit("configs.update", server_id="b")
    first = store.history("a")
    second = store.history("a", before=first["next"])
    assert len(first["items"]) == 50 and len(second["items"]) == 5
    assert second["next"] is None
    assert all(row["server_id"] == "a" for row in first["items"] + second["items"])


def test_retention_and_queue_overflow_are_bounded(monkeypatch):
    monkeypatch.setattr(store, "MAX_EVENTS", 3)
    monkeypatch.setattr(store, "MAX_PENDING", 1)
    channel()
    for i in range(6):
        store.emit("backups.create", server_id=f"a{i}", outcome="failed", alert="backup.failed")
    assert len(store.history(instance=True)["items"]) == 3
    assert sum(row["state"] == "pending" for row in service.deliveries()) == 1
    assert service.deliveries()[0]["error"] == "queue_capacity"
    service.deliver_due(time.time() + store.DELIVERY_TTL + 1)
    assert all(row["state"] == "failed" for row in service.deliveries())


def test_disk_hysteresis_scope_and_cooldown(monkeypatch):
    from backend.server import storage
    from backend.backups import storage as backups
    monkeypatch.setattr(storage, "get_all_servers", lambda: [{"id": "a", "installPath": "/server-data"}])
    monkeypatch.setattr(backups, "all_configs", lambda: [])
    free = 1024**3
    usage = SimpleNamespace(total=100 * 1024**3, used=99 * 1024**3, free=free)
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    service.sample_disks()
    service.sample_disks()
    assert [row["action"] for row in store.history("a")["items"]] == ["disk.server.pressure"]
    usage.used, usage.free = 87 * 1024**3, 13 * 1024**3
    service.sample_disks()
    assert len(store.history("a")["items"]) == 1
    usage.used, usage.free = 80 * 1024**3, 20 * 1024**3
    service.sample_disks()
    assert store.history("a")["items"][0]["alert"] == "disk.recovered"


def test_journal_failure_does_not_change_operation(monkeypatch, caplog):
    monkeypatch.setattr(store, "connection", Mock(side_effect=OSError("secret-canary")))
    assert store.emit("configs.update", server_id="a") is None
    assert "journal write failed" in caplog.text
    assert "secret-canary" not in caplog.text


def test_unexpected_exit_but_not_intentional_stop_alerts():
    from backend.server.manager import ServerManager
    manager = ServerManager("/unused")
    manager.server_id = "a"
    intentional = threading.Event()
    intentional.set()
    manager._watch_exit(Mock(), intentional)
    assert not store.history("a")["items"]
    manager._watch_exit(Mock(), threading.Event())
    manager._watch_exit(Mock(), threading.Event())
    assert len(store.history("a")["items"]) == 1
    assert store.history("a")["items"][0]["alert"] == "server.crashed"


def test_current_mutations_audit_actor_without_values(authed_client, tmp_servers_root):
    from tests.test_players_routes import _seed_server
    root = _seed_server(tmp_servers_root, "audit-server", {"loader": "fabric"})
    (root / "mods").mkdir()
    (root / "mods" / "example.jar").write_bytes(b"fixture")
    (root / "server.properties").write_text("motd=before\n")
    assert authed_client.patch("/api/servers/audit-server/mods", json={"filenames": ["example.jar"], "enabled": False}).status_code == 200
    assert authed_client.put("/api/servers/audit-server/settings", json={"motd": "secret-canary"}).status_code == 200
    assert authed_client.patch("/api/servers/audit-server/players/whitelist/enforce", json={"active": True}).status_code == 200
    items = store.history("audit-server")["items"]
    assert {row["action"] for row in items} == {"mods.toggle", "configs.update", "access.whitelist"}
    assert all(row["actor"]["kind"] == "shared-password" for row in items)
    assert "secret-canary" not in json.dumps(items)
    response = authed_client.get("/api/servers/audit-server/activity")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert authed_client.get("/api/servers/audit-server/activity?before=oops").status_code == 400


@pytest.mark.parametrize("kind,action", [
    ("backup", "backups.create"), ("restore", "worlds.restore"), ("world_import", "worlds.import"),
    ("world-copy", "worlds.copy"), ("fork", "server.fork"),
])
def test_existing_job_families_emit_once(kind, action):
    from backend.backups import progress
    progress.update("family-test", phase="starting", kind=kind, server_id="a", sourceId="a", destinationId="b")
    progress.update("family-test", phase="failed", error="secret-canary")
    progress.update("family-test", phase="failed")
    items = store.history("a")["items"]
    assert len(items) == 1 and items[0]["action"] == action
    assert items[0]["alert"] == ("backup.failed" if kind == "backup" else "job.failed")
    assert store.history("b")["items"][0]["action"] == action


def test_install_and_schedule_failures_are_alerts(monkeypatch):
    from backend.server import install_progress
    from backend.modrinth import routes
    from backend.schedules import service as schedules
    install_progress.update("a", phase="starting", kind="upgrade")
    install_progress.update("a", phase="failed", error="secret-canary")
    routes._update_install_progress("a", stage="starting")
    routes._update_install_progress("a", stage="failed")
    monkeypatch.setattr(schedules, "get_schedule", lambda *a: {"steps": []})
    monkeypatch.setattr(schedules, "_run_steps", lambda *a: ("error", "secret-canary"))
    schedules.run_schedule("a", "fixture")
    items = store.history("a")["items"]
    assert {row["action"] for row in items} == {"server.upgrade", "modpacks.install", "schedules.run"}
    assert all(row["alert"] == "job.failed" for row in items)
    assert "secret-canary" not in json.dumps(items)


def test_cooldown_does_not_hide_job_outcomes():
    channel()
    store.track_job("one", "a", "backups.create", "starting")
    store.track_job("one", "a", "backups.create", "failed")
    store.track_job("two", "a", "backups.create", "starting")
    store.track_job("two", "a", "backups.create", "failed")
    assert len(store.history("a")["items"]) == 2
    assert {row["state"] for row in service.deliveries()} == {"pending", "suppressed"}


def test_terminal_alert_uses_the_submitted_job_action():
    store.track_job("backup-run", "a", "backups.create", "starting")
    store.track_job("backup-run", "a", "worlds.restore", "failed")
    event = store.history("a")["items"][0]
    assert event["action"] == "backups.create"
    assert event["alert"] == "backup.failed"


@pytest.mark.parametrize("phase,outcome", [
    ("done", "succeeded"), ("failed", "failed"), ("cancelled", "skipped"),
])
def test_related_server_receives_the_same_job_outcome(phase, outcome):
    store.track_job("transfer-run", "a", "worlds.copy", "starting")
    store.track_job("transfer-run", "a", "worlds.copy", phase, related_server="b")
    source = store.history("a")["items"][0]
    destination = store.history("b")["items"][0]
    assert source["outcome"] == destination["outcome"] == outcome
    assert source["actor"] == destination["actor"]
    assert destination["alert"] is None


def test_resolver_wait_is_bounded(monkeypatch):
    done = threading.Event()
    def blocked_lookup(*args, **kwargs):
        done.wait(1)
        return []
    monkeypatch.setattr(socket, "getaddrinfo", blocked_lookup)
    monkeypatch.setattr(transport, "DNS_TIMEOUT", 0.01)
    try:
        with pytest.raises(transport.DeliveryError, match="dns_timeout"):
            transport.resolve("https://hooks.example/path")
    finally:
        done.set()


def test_restart_cannot_exceed_final_delivery_attempt(monkeypatch):
    channel()
    store.emit("server.crashed", server_id="a", outcome="failed", alert="server.crashed")
    with store.connection() as db:
        db.execute("UPDATE deliveries SET state='sending',attempts=5")
    store.recover()
    sender = Mock()
    monkeypatch.setattr(transport, "deliver", sender)
    assert not service.deliver_due()
    assert service.deliveries()[0]["state"] == "failed"
    assert service.deliveries()[0]["error"] == "delivery_interrupted"
    sender.assert_not_called()


def test_nested_loader_completion_does_not_finish_outer_job(app):
    from flask import g
    from backend.server import install_progress
    with app.test_request_context("/"):
        g.audit_actor = {"kind": "oidc", "id": "alice"}
        install_progress.update("a", phase="starting")
    install_progress.update("a", phase="done", _audit_terminal=False)
    assert store.history("a")["items"] == []
    install_progress.update("a", modpack_error="secret-canary")
    install_progress.update("a", phase="done")
    items = store.history("a")["items"]
    assert len(items) == 2 and all(row["actor"]["id"] == "alice" for row in items)
    assert {(row["action"], row["outcome"]) for row in items} == {("server.install", "succeeded"), ("modpacks.install", "failed")}


def test_real_process_lifecycle_distinguishes_stop_console_stop_and_exit(tmp_path):
    from backend.server.manager import ServerManager
    for console_stop in (False, True):
        manager = ServerManager(str(tmp_path), [sys.executable, "-u", "-c", "import sys; sys.stdin.readline()"])
        manager.server_id = "fixture"
        assert manager._spawn_process(manager.command)[0]
        if console_stop:
            assert manager.send_command("stop")["success"]
            manager._process.wait(timeout=3)
        else:
            manager.stop()
    assert store.history("fixture")["items"] == []
    crash = ServerManager(str(tmp_path), [sys.executable, "-c", "import time; time.sleep(0.7)"])
    crash.server_id = "fixture"
    assert crash._spawn_process(crash.command)[0]
    crash._process.wait(timeout=3)
    deadline = time.monotonic() + 2
    while not store.history("fixture")["items"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.history("fixture")["items"][0]["alert"] == "server.crashed"
