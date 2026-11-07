"""Fixture-backed history, loader, capture lifecycle and authorization coverage."""
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.diagnostics import service, storage
from backend.server import storage as servers
from backend.server.manager import ServerManager
from backend.server import tps


@pytest.fixture
def record(tmp_servers_root):
    value = {"id": "a", "name": "a", "loader": "paper", "version": "1.21.1"}
    servers.save_servers([value])
    return value


@pytest.fixture
def manager(monkeypatch, record):
    value = ServerManager(str(record["id"]), loader="paper", version="1.21.1")
    value._process = SimpleNamespace(pid=1234, poll=lambda: None)
    value._ready_event.set()
    value._tps_probe = tps.probe_for("paper", "1.21.1")
    value.send_command = MagicMock(return_value={"success": True})
    registry = SimpleNamespace(get_manager=lambda sid: value,
                               resolve_install_path=lambda server: servers._resolve_servers_file().parent)
    monkeypatch.setattr(service, "get_server_process_registry", lambda: registry)
    monkeypatch.setattr(value, "_get_cpu_percent", lambda: 25.0)
    monkeypatch.setattr(value, "_get_rss_usage_bytes", lambda: 4096)
    return value


@pytest.fixture(autouse=True)
def clear_active():
    global service, storage, servers, ServerManager, tps
    from backend.diagnostics import service, storage
    from backend.server import storage as servers, tps
    from backend.server.manager import ServerManager
    service._active.clear()
    yield
    service._active.clear()


@pytest.mark.parametrize("loader,version,supported", [
    ("paper", "1.21", True), ("paper", "1.21.11", True),
    ("paper", "1.20.6", False), ("paper", "26.1", False),
    ("paper", "1.21-pre1", False), ("paper", "unknown", False),
    ("purpur", "1.21.1", False), ("fabric", "1.21.1", False),
    ("forge", "1.21.1", False), ("velocity", "3.4", False),
])
def test_capability_is_conservative(loader, version, supported):
    assert service.capability({"loader": loader, "version": version})["supported"] is supported


def test_jstat_units_and_unsupported_measurements():
    data = service.parse_jstat("S0U S1U EU OU YGC FGC CGC GCT\n1 2 3 4 5 6 7 0.125")
    assert data == {"heapUsedBytes": 10240, "gcCollections": 18, "gcTimeMs": 125}
    assert service.parse_jstat("S0U S1U EU OU YGC FGC CGC GCT\n- - - - 0 0 - -") == {}
    assert service.parse_jstat("GCT\nNaN") == {}
    assert service.parse_jstat("GCT\nInfinity") == {}
    assert service.parse_jstat("garbage") == {}
    assert service.parse_jstat("YGC FGC GCT\n0 0 0") == {"gcTimeMs": 0, "gcCollections": 0}


def test_sample_uses_existing_metrics_and_does_not_guess_mspt(record, manager):
    manager._tps, manager._tps_at = 19.5, time.monotonic()
    result = service.sample(record, now=123)
    assert result["ts"] == 123 and result["tps"] == 19.5
    assert result["rssBytes"] == 4096 and result["cpu"] == 25
    assert "mspt" not in result and "heapUsedBytes" not in result
    assert result["jvmUnavailable"] == "sampling-disabled"
    assert result["volumeTotalBytes"] > 0
    assert "worldBytes" not in result
    manager.send_command.assert_not_called()


def test_stopped_and_missing_tools_are_not_zero(record, manager, monkeypatch):
    manager._process = None
    result = service.sample(record)
    assert result["running"] is False
    assert not {"rssBytes", "cpu", "tps", "mspt", "gcTimeMs"} & result.keys()
    manager._process = SimpleNamespace(pid=1234, poll=lambda: None)
    record["diagnostics"] = {"jvmSampling": True}
    monkeypatch.setattr(service.shutil, "which", lambda _: None)
    result = service.sample(record)
    assert result["jvmUnavailable"] == "jstat-unavailable"
    assert "gcTimeMs" not in result


def test_process_change_during_sampling_discards_mixed_runtime_measurements(record, manager, monkeypatch):
    def changed():
        manager._process = SimpleNamespace(pid=5678, poll=lambda: None)
        return {"tps": 20, "ram": {"rssBytes": 999}}
    monkeypatch.setattr(manager, "status", changed)
    result = service.sample(record)
    assert not {"tps", "rssBytes", "run"} & result.keys()
    assert result["jvmUnavailable"] == "process-changed"


def test_jstat_subprocess_is_bounded_and_uses_running_java(record, manager, monkeypatch, tmp_path):
    java = tmp_path / "java"
    jstat = tmp_path / "jstat"
    jstat.touch()
    monkeypatch.setattr(manager, "_java_executable", lambda: str(java))
    run = MagicMock(return_value=SimpleNamespace(stdout="GCT\n0.25"))
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service.jvm_sample(manager) == ({"gcTimeMs": 250}, None)
    args, kwargs = run.call_args
    assert args[0] == [str(jstat), "-gc", "1234"]
    assert kwargs["timeout"] == 2 and kwargs["check"] is True
    assert "shell" not in kwargs


def test_actual_mspt_is_preserved_and_expires(manager):
    manager._consume_tps_line("[12:00:00] [Server thread/INFO]: TPS from last 1m, 5m, 15m: 19.5, 20.0, 20.0")
    manager._consume_tps_line("Average time per tick: 12.5ms (Target: 50.0ms)")
    assert manager.status()["mspt"] == 12.5
    assert manager.status()["tps"] == 19.5  # Do not overwrite Paper's rolling TPS.
    manager._mspt_at -= manager.TPS_STALE_AFTER_SEC + 1
    assert "mspt" not in manager.status()
    manager._process = None
    assert "mspt" not in manager.status()


def test_forge_overall_mspt_not_dimension_mspt(record):
    manager = ServerManager(".", loader="forge", version="1.20.1")
    manager._tps_probe = tps.probe_for("forge", "1.20.1")
    manager._consume_tps_line("Overall: Mean tick time: 23.45 ms. Mean TPS: 20.000")
    assert manager._mspt == 23.45
    manager._consume_tps_line("Dim minecraft:the_nether: Mean tick time: 1.0 ms. Mean TPS: 20.000")
    assert manager._mspt == 23.45


def test_history_is_persistent_bounded_server_scoped_and_cleaned(record, monkeypatch):
    now = int(time.time())
    monkeypatch.setattr(storage, "MAX_SAMPLES", 3)
    for offset in range(5):
        storage.append("a", {"ts": now - offset * 30, "tps": offset})
    storage.append("b", {"ts": now, "rssBytes": 999})
    assert len(storage.history("a", 3600)) == 3
    assert all("rssBytes" not in value for value in storage.history("a", 3600))
    storage.append("a", {"ts": now - 90000, "tps": 999})
    assert len(storage.history("a", 100000)) == 3
    storage.cleanup({"a"})
    assert storage.history("b", 3600) == []
    storage.delete("a")
    assert storage.history("a", 3600) == []


def test_capture_retention_is_bounded(record, monkeypatch):
    monkeypatch.setattr(storage, "MAX_CAPTURES", 2)
    for offset in range(3):
        storage.save_capture("a", {"id": str(offset), "created": time.time() - offset, "status": "ready"})
    assert len(storage.captures("a")) == 2
    storage.save_capture("b", {"id": "old", "created": time.time() - 90000, "status": "ready"})
    assert not storage.captures("b")
    storage.delete("a")
    assert not storage.captures("a")


def test_storage_connections_are_closed_after_each_transaction(record):
    with storage.database() as db:
        db.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_finishing_tracker_cannot_resurrect_deleted_references(record):
    capture = {"id": "test", "created": time.time(), "status": "running"}
    service._active["a"] = "test"
    service._persist_capture("a", capture)
    service.delete_state("a")
    capture["status"] = "ready"
    service._persist_capture("a", capture)
    assert storage.captures("a") == []
    assert not service._active


def test_routes_validate_window_and_persist_explicit_sampling(client, record):
    endpoint = "/api/servers/a/diagnostics"
    assert client.get(endpoint + "?window=forever").status_code == 400
    assert client.get("/api/servers/missing/diagnostics").status_code == 404
    response = client.get(endpoint)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["samples"] == []
    for data in ({}, {"jvmSampling": 1}, [], {"jvmSampling": "true"}):
        assert client.put(endpoint + "/sampling", json=data).status_code == 400
    assert client.put(endpoint + "/sampling", json={"jvmSampling": True}).status_code == 200
    assert servers.get_server("a")["diagnostics"]["jvmSampling"] is True


@pytest.mark.parametrize("payload", [
    {}, {"acceptUpload": False}, {"acceptUpload": "true"},
    {"acceptUpload": True, "durationSeconds": "60\nstop"},
    {"acceptUpload": True, "durationSeconds": True},
    {"acceptUpload": True, "durationSeconds": 999},
])
def test_capture_rejects_missing_consent_and_injection(record, manager, payload):
    with pytest.raises(service.DiagnosticError):
        service.start_capture(record, payload)
    manager.send_command.assert_not_called()


def test_capture_request_is_not_success_until_acknowledged(record, manager, monkeypatch):
    worker = MagicMock()
    monkeypatch.setattr(service.threading, "Thread", worker)
    result = service.start_capture(record, {"acceptUpload": True, "durationSeconds": 60})
    assert result["status"] == "requested"
    manager.send_command.assert_called_once_with("spark profiler start --timeout 60")
    assert storage.captures("a")[0]["status"] == "requested"
    with pytest.raises(service.DiagnosticError, match="already"):
        service.start_capture(record, {"acceptUpload": True})
    worker.return_value.start.assert_called_once()


@pytest.mark.parametrize("mode", ["unsupported", "not-ready", "changed-runtime", "stopped", "send-failed"])
def test_capture_checks_runtime_and_command_failure(record, manager, mode):
    if mode == "unsupported":
        record["loader"] = "fabric"
    elif mode == "not-ready":
        manager._ready_event.clear()
    elif mode == "changed-runtime":
        manager.version = "1.20.6"
    elif mode == "stopped":
        manager._process = None
    else:
        manager.send_command.return_value = {"success": False}
    with pytest.raises(service.DiagnosticError):
        service.start_capture(record, {"acceptUpload": True})
    assert not service._active


def test_capture_parser_rejects_chat_links_and_untrusted_urls(manager):
    prefix = "[12:00:00] [Server thread/INFO]: "
    for body in ("<Alex> https://spark.lucko.me/abc123",
                 "[Not Secure] <Alex> Profiler is now running!",
                 "* Alex https://spark.lucko.me/abc123",
                 "System chat: <Alex> https://spark.lucko.me/abc123"):
        assert service.console_body(manager, prefix + body) is None
    capture = {"status": "requested"}
    service.apply_capture_line(capture, "https://spark.lucko.me/abc123")
    assert capture["status"] == "requested"
    service.apply_capture_line(capture, "[spark] Profiler is now running!")
    assert capture["status"] == "running"
    service.apply_capture_line(capture, "[OtherPlugin] Failed to load its config")
    assert capture["status"] == "running"
    for url in ("https://spark.lucko.me.evil/abc123", "http://spark.lucko.me/abc123",
                "https://spark.lucko.me/abc123?secret=x", "https://spark.lucko.me/abc123/evil",
                "<Alex> https://spark.lucko.me/abc123", "Click https://spark.lucko.me/abc123"):
        service.apply_capture_line(capture, url)
        assert "reportUrl" not in capture
    service.apply_capture_line(capture, "https://spark.lucko.me/abc123")
    assert capture["status"] == "ready" and capture["reportUrl"].endswith("abc123")
    assert service.console_body(manager, prefix + "\u00a77https://spark.lucko.me/abc123\u00a7r") == "https://spark.lucko.me/abc123"


def test_capture_worker_consumes_new_output_and_persists_reference_only(record, manager):
    capture = {"id": "test", "created": time.time(), "durationSeconds": 60, "status": "requested"}
    manager.console_since = lambda cursor: (5, [
        "[12:00:00] [Server thread/INFO]: [spark] Profiler is now running!",
        "[12:01:00] [Server thread/INFO]: https://spark.lucko.me/abc123",
    ])
    service._active["a"] = "test"
    service._watch_capture("a", manager, manager._process, capture, 0)
    stored = storage.captures("a")[0]
    assert stored["status"] == "ready"
    assert "stdout" not in stored and "[Server thread" not in json.dumps(stored)
    assert not service._active


def test_capture_worker_reports_interruption_and_timeout(record, manager, monkeypatch):
    capture = {"id": "test", "created": time.time(), "durationSeconds": 60, "status": "requested"}
    service._active["a"] = "test"
    service._watch_capture("a", manager, object(), capture, 0)
    assert storage.captures("a")[0]["status"] == "interrupted"
    capture["status"] = "requested"
    service._active["a"] = "test"
    clock = iter((0, 0, 200))
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    service._watch_capture("a", manager, manager._process, capture, 0)
    assert storage.captures("a")[0]["status"] == "failed"


def test_sampler_does_not_require_a_browser(record, manager):
    service.sample_all()
    assert storage.history("a", 3600)[0]["rssBytes"] == 4096


def test_capture_readiness_does_not_depend_on_retained_samples(record, manager):
    data = service.overview(record, 3600)
    assert data["samples"] == [] and data["captureReady"] is True
    manager._ready_event.clear()
    assert service.overview(record, 3600)["captureReady"] is False


def test_disabled_sampler_still_recovers_abandoned_capture_on_read(record, manager):
    storage.save_capture("a", {"id": "test", "created": time.time(), "status": "running"})
    assert service.overview(record, 3600)["captures"][0]["status"] == "interrupted"


def test_thread_start_failure_does_not_leave_an_active_lease(record, manager, monkeypatch):
    worker = MagicMock()
    worker.return_value.start.side_effect = RuntimeError("thread unavailable")
    monkeypatch.setattr(service.threading, "Thread", worker)
    with pytest.raises(service.DiagnosticError, match="tracking"):
        service.start_capture(record, {"acceptUpload": True})
    assert not service._active
    assert storage.captures("a")[0]["status"] == "failed"


def test_restart_recovers_unfinished_capture_and_starts_one_sampler(record, monkeypatch):
    storage.save_capture("a", {"id": "test", "created": time.time(), "status": "running"})
    monkeypatch.setenv("FABRICATOR_DISABLE_DIAGNOSTICS", "0")
    monkeypatch.setattr(service, "_thread", None)
    thread = MagicMock()
    monkeypatch.setattr(service.threading, "Thread", thread)
    service.init(SimpleNamespace(testing=False))
    assert storage.captures("a")[0]["status"] == "interrupted"
    service.init(SimpleNamespace(testing=False))
    thread.assert_called_once()


def test_console_cursor_never_reads_old_run_or_chat_as_report(record, manager):
    manager._prev_stdout_buffer = [("old", "https://spark.lucko.me/old123")]
    manager._stdout_buffer = [("now", "new line")]
    manager._stdout_total = 10
    assert manager.console_since() == (10, [])
    assert manager.console_since(9) == (10, ["new line"])
    assert manager.console_since(10) == (10, [])


def test_permission_gate_covers_server_scope_and_manage(app, record, monkeypatch):
    from flask import g
    from backend.auth.permissions import Principal, authorize
    def gate():
        return authorize(g.test_user)
    # Exercise the actual catalog/resolver without mocking diagnostics handlers.
    app.before_request_funcs[None].append(gate)
    viewer = Principal("viewer", "issuer", (), frozenset(),
                       {"a": frozenset({"server.view", "server.metrics.read"})}, {"a": ()})
    # This hook must run before the catalog gate.
    app.before_request_funcs[None].insert(0, lambda: setattr(g, "test_user", viewer))
    client = app.test_client()
    assert client.get("/api/servers/a/diagnostics").status_code == 200
    assert client.post("/api/servers/a/diagnostics/captures", json={"acceptUpload": True}).status_code == 403
    assert client.put("/api/servers/a/diagnostics/sampling", json={"jvmSampling": True}).status_code == 403
    assert client.get("/api/servers/b/diagnostics").status_code == 404


def test_capture_routes_are_not_accessible_to_mcp_tokens():
    from backend.auth.buckets import BUCKETS
    for method, suffix in (("GET", ""), ("PUT", "/sampling"), ("POST", "/captures")):
        assert BUCKETS[method, "/api/servers/<server_id>/diagnostics" + suffix] == "never"
