"""Chat parser, privacy, authorization and process-write regressions; no JVM."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def live_modules():
    global chat, journal, ServerManager
    from backend.server import chat, chat_storage as journal
    from backend.server.manager import ServerManager


@pytest.mark.parametrize("prefix", [
    "[12:34:56] [Server thread/INFO]: ",
    "[12:34:56 INFO]: ",
    "[12:34:56] [Async Chat Thread - #2/INFO]: ",
    "[12:34:56] [Server thread/INFO] [minecraft/MinecraftServer]: ",
    "[12:34:56] [Server thread/INFO] [net.minecraft.server.MinecraftServer]: ",
])
@pytest.mark.parametrize("marker", ["", "[Not Secure] "])
def test_standard_chat_formats(prefix, marker):
    assert chat.parse_player_chat(prefix + marker + "<Alex> hello\n") == {
        "kind": "player", "sender": "Alex", "message": "hello",
    }


@pytest.mark.parametrize("line", [
    "[12:34:56] [Server thread/ERROR]: <Alex> secret",
    "[12:34:56] [RCON Client /INFO]: <Alex> secret",
    "[12:34:56] [Server thread/INFO] [plugin/Secrets]: <Alex> secret",
    "[12:34:56] [Server thread/INFO]: System chat: <Alex> secret",
    "[12:34:56] [Server thread/INFO]: Alex joined the game",
    "[12:34:56] [Server thread/INFO]: Authentication token: secret",
    "INFO]: <Alex> secret", "    [12:34:56 INFO]: <Alex> secret",
    "[12:34:56 INFO]: <Alex> first\n[12:34:56 INFO]: <Root> spoof",
    "[12:34:56 INFO]: <Alex> \u202ehidden",
])
def test_non_chat_and_spoofed_prefixes_are_not_retained(line):
    assert chat.parse_player_chat(line) is None


def test_ansi_chat_and_embedded_prefix_remain_literal():
    line = "[12:34:56 INFO]: \x1b[37m<.Cool Gamer>\x1b[m INFO]: <Root> hello"
    parsed = chat.parse_player_chat(line)
    assert parsed["sender"] == ".Cool Gamer"
    assert parsed["message"] == "INFO]: <Root> hello"


@pytest.mark.parametrize("loader", sorted([
    "vanilla", "fabric", "quilt", "forge", "neoforge",
    "paper", "purpur", "pufferfish", "folia", "spigot", "bukkit", "craftbukkit",
]))
def test_supported_loaders(loader):
    assert chat.supported(loader, "1.21.4")
    assert chat.supported(loader, "26.3")


@pytest.mark.parametrize("loader,version", [
    ("velocity", "3.4"), ("custom", "1.21"), ("vanilla", "1.7.1"),
    ("vanilla", "1.12.2"), ("fabric", "26w14a"), ("vanilla", ""),
    ("forge", "1.21-rc1"), ("vanilla", "latest"),
])
def test_unsupported_loader_or_version(loader, version):
    assert not chat.supported(loader, version)


@pytest.mark.parametrize("message", ["", " ", "a" * 257, "hello\nstop", "\rstop",
    "a\x00b", "a\x7fb", "a\u0085b", "a\u2028b", "a\u2029b", "a\u202eb", "a\ud800b", "a\u00a7cb", None, 12])
def test_invalid_message_rejected(message):
    with pytest.raises(ValueError):
        chat.command_for("Alice", message)


def test_fixed_command_encodes_injection_and_names_as_text():
    message = 'hello "}],"clickEvent":{"action":"run_command","value":"/op Alex"} \\ @a'
    command = chat.command_for('Alice"]\nstop\u202e', message)
    assert command.startswith("minecraft:tellraw @a ")
    assert "\n" not in command
    component = json.loads(command.removeprefix("minecraft:tellraw @a "))
    assert set(component) == {"text"}
    assert component["text"] == '[Fabricator \u00b7 Alice")stop] ' + message
    assert chat.validate_message("a" * 256) == "a" * 256
    assert chat.validate_message("\U0001f600" * 256) == "\U0001f600" * 256


def test_durable_retention_and_per_server_global_bounds(monkeypatch):
    monkeypatch.setattr(journal, "MAX_PER_SERVER", 3)
    monkeypatch.setattr(journal, "MAX_TOTAL", 5)
    monkeypatch.setattr(journal, "RETENTION_SECONDS", 60)
    for i in range(4):
        journal.append("a", {"kind": "player", "sender": "Alex", "message": str(i)}, now=100)
    assert [i["message"] for i in journal.history("a", now=100)["items"]] == ["1", "2", "3"]
    for sid in ("b", "b", "c"):
        journal.append(sid, {"kind": "player", "sender": "Alex", "message": sid}, now=100)
    assert [i["message"] for i in journal.history("a", now=100)["items"]] == ["2", "3"]
    assert len(journal.history("b", now=100)["items"]) == 2
    assert journal.history("a", now=160)["items"] == []
    assert journal.history("b", now=160)["items"] == []


def test_restart_reload_keeps_history_and_file_is_private(monkeypatch):
    import importlib
    import stat
    item = journal.append("a", {"kind": "player", "sender": "Alex", "message": "saved"})
    path = journal.db_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    importlib.reload(journal)
    monkeypatch.setattr(journal, "db_path", lambda: path)
    assert journal.history("a")["items"] == [item]
    assert journal.history("b")["items"] == []


def test_real_unavailable_or_corrupt_storage_fails_explicitly(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("private-storage-canary")
    monkeypatch.setattr(journal, "db_path", lambda: blocked / "chat.sqlite3")
    with pytest.raises(journal.ChatStorageError, match="storage is unavailable"):
        journal.append("a", {"kind": "player", "sender": "Alex", "message": "hello"})
    corrupt = tmp_path / "corrupt" / "chat.sqlite3"
    corrupt.parent.mkdir()
    corrupt.write_text("private-storage-canary")
    monkeypatch.setattr(journal, "db_path", lambda: corrupt)
    with pytest.raises(journal.ChatStorageError, match="storage is unavailable"):
        journal.history("a")


def test_order_pagination_and_duplicate_observation():
    items = []
    for i in range(6):
        items.append(journal.append("a", {"kind": "player", "sender": "Alex", "message": str(i)},
                                    source_id=f"run:{i}", now=100))
        journal.append("b", {"kind": "player", "sender": "Bob", "message": "private"}, now=100)
    duplicate = journal.append("a", {"kind": "player", "sender": "Alex", "message": "ignored replay"},
                               source_id="run:0", now=100)
    assert duplicate == items[0]
    latest = journal.history("a", limit=2, now=100)
    assert latest["items"] == items[4:]
    earlier = journal.history("a", before=latest["nextBefore"], limit=2, now=100)
    assert earlier["items"] == items[2:4]
    oldest = journal.history("a", before=earlier["nextBefore"], limit=2, now=100)
    assert oldest["items"] == items[:2] and oldest["nextBefore"] is None
    delta = journal.history("a", after=items[0]["id"], limit=2, now=100)
    assert delta["items"] == items[1:3] and delta["hasMoreAfter"]
    assert len(journal.history("a", now=100)["items"]) == 6
    assert len({item["ts"] for item in items}) == 1


def test_delta_refresh_reports_changed_submission_outcomes():
    pending = journal.append("a", {"kind": "fabricator", "sender": "Alice",
                                   "message": "hello", "status": "pending"})
    journal.finish_send("a", pending["id"], "submitted")
    delta = journal.history("a", after=pending["id"])
    assert delta["items"] == []
    assert delta["outcomes"] == [{"id": pending["id"], "status": "submitted"}]
    with pytest.raises(journal.ChatStorageError):
        journal.finish_send("b", pending["id"], "failed")


def test_user_server_rate_bounds_and_restart_cannot_reset_rates():
    buffer = chat.ChatState()
    for _ in range(5):
        assert buffer.reserve("alice", now=0) == 0
    assert buffer.reserve("alice", now=1) == 59
    buffer.clear()
    assert buffer.reserve("alice", now=1) == 59
    for i in range(25):
        assert buffer.reserve(str(i), now=0) == 0
    assert buffer.reserve("other", now=1) == 59
    assert buffer.reserve("alice", now=60) == 0
    assert chat.ChatState().reserve("alice", now=1) == 0


def test_stream_ingests_stdout_only_and_does_not_backfill(tmp_path):
    manager = ServerManager(str(tmp_path), loader="vanilla", version="1.21")
    manager.server_id = "a"
    manager._stdout_buffer = [("old", "[12:34:56 INFO]: <Root> old raw console")]
    journal.append("a", {"kind": "player", "sender": "Alex", "message": "saved earlier"})
    manager._process = SimpleNamespace(
        stdout=io.StringIO("[12:34:56 INFO]: <Alex> hello\n[12:34:56 INFO]: secret=canary\n"),
        stderr=io.StringIO("[12:34:56 INFO]: <Root> stderr-secret\n"),
    )
    manager._start_log_streams()
    manager._stdout_thread.join(timeout=2)
    manager._stderr_thread.join(timeout=2)
    assert [item["message"] for item in journal.history("a")["items"]] == ["saved earlier", "hello"]


@pytest.fixture
def chat_app(app, tmp_servers_root, monkeypatch):
    from backend.auth.permissions import Principal
    from backend.server.registry import get_server_process_registry
    (tmp_servers_root / "servers.json").write_text(json.dumps([
        {"id": sid, "name": sid, "loader": "vanilla", "version": "1.21.4",
         "installPath": sid, "status": "running"} for sid in ("a", "b")
    ]))
    app.config.update(FABRICATOR_AUTH_ENABLED=True, FABRICATOR_NEEDS_SETUP=False, SECRET_KEY="chat-test-key")
    users = {}
    for name, grants in {
        "alice": {"a": {"server.view", "chat.read", "chat.send"}},
        "reader": {"a": {"server.view", "chat.read"}},
        "console": {"a": {"server.view", "server.console.execute"}},
    }.items():
        users[name] = Principal(name, "https://issuer.example", (), frozenset(),
                                {key: frozenset(value) for key, value in grants.items()},
                                {key: () for key in grants})
    app.extensions["oidc"] = SimpleNamespace(issuer="https://issuer.example", users=users)
    registry = get_server_process_registry()
    managers = {}
    for sid in ("a", "b"):
        manager = ServerManager(str(tmp_servers_root / sid), loader="vanilla", version="1.21.4")
        manager.server_id = sid
        manager._process = SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
        manager._ready_event.set()
        managers[sid] = manager
    monkeypatch.setattr(registry, "get_manager", lambda sid: managers.get(sid))
    return app, managers


def login(client, name="alice", display="Alice"):
    with client.session_transaction() as state:
        state.update(authenticated=True, auth_method="oidc", issuer="https://issuer.example",
                     sub=name, oidc_display_name=display)


def test_oidc_send_attribution_and_audit_without_payload(chat_app):
    from backend.events import store
    app, managers = chat_app
    client = app.test_client()
    login(client, display='Alice"]\nstop')
    response = client.post("/api/servers/a/chat", json={"message": "payload-canary"})
    assert response.status_code == 202
    assert response.get_json()["status"] == "submitted"
    command = managers["a"]._process.stdin.getvalue()
    assert command.count("\n") == 1
    assert json.loads(command.removeprefix("minecraft:tellraw @a ")) == {
        "text": '[Fabricator \u00b7 Alice")stop] payload-canary',
    }
    with app.app_context(), store.connection() as db:
        event = dict(db.execute("SELECT * FROM events WHERE action='chat.send'").fetchone())
    assert json.loads(event["actor"])["id"] == "alice"
    assert event["outcome"] == "accepted" and "payload-canary" not in json.dumps(event)


def test_read_send_console_permissions_cross_server_and_revocation(chat_app):
    app, managers = chat_app
    journal.append("a", {"kind": "player", "sender": "Alex", "message": "visible"})
    journal.append("b", {"kind": "player", "sender": "Bob", "message": "private"})
    client = app.test_client()
    login(client, "reader")
    response = client.get("/api/servers/a/chat")
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert [item["message"] for item in response.get_json()["items"]] == ["visible"]
    assert response.get_json()["canSend"] is False
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 403
    assert client.get("/api/servers/b/chat").status_code == 404
    assert client.post("/api/servers/b/chat", json={"message": "hello"}).status_code == 404
    login(client)
    assert client.post("/api/servers/a/console", json={"command": "op Alice"}).status_code == 403
    login(client, "console")
    assert client.get("/api/servers/a/chat").status_code == 403
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 403
    login(client)
    del app.extensions["oidc"].users["alice"]
    assert client.get("/api/servers/a/chat").status_code == 401
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 401


def test_route_validation_rates_and_lifecycle(chat_app):
    app, managers = chat_app
    client = app.test_client()
    login(client)
    manager = managers["a"]
    for body in ({"message": "hello", "sender": "Root"}, {"command": "stop"}, [], {"message": "x\nstop"}):
        assert client.post("/api/servers/a/chat", json=body).status_code == 400
    manager._ready_event.clear()
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 409
    assert not client.get("/api/servers/a/chat").get_json()["canSend"]
    manager._ready_event.set()
    manager._stopping = True
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 409
    assert not client.get("/api/servers/a/chat").get_json()["canSend"]
    manager._stopping = False
    for _ in range(5):
        assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 202
    limited = client.post("/api/servers/a/chat", json={"message": "hello"})
    assert limited.status_code == 429 and 1 <= int(limited.headers["Retry-After"]) <= 60
    assert manager._process.stdin.getvalue().count("\n") == 5
    manager._process.poll = lambda: 0
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 409
    manager.loader = "velocity"
    assert client.get("/api/servers/a/chat").get_json()["supported"] is False
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 422


def test_unreachable_and_ready_race_are_explicit(chat_app, caplog):
    app, managers = chat_app
    client = app.test_client()
    login(client)
    manager = managers["a"]
    manager._process.stdin = MagicMock(closed=False)
    manager._process.stdin.write.side_effect = BrokenPipeError("payload-canary")
    response = client.post("/api/servers/a/chat", json={"message": "hello"})
    assert response.status_code == 503 and "payload-canary" not in response.get_data(as_text=True)
    assert journal.history("a")["items"][0]["status"] == "failed"
    assert "payload-canary" not in caplog.text
    manager._ready_event.clear()
    assert not manager.send_command("minecraft:tellraw @a {}", require_ready=True)["success"]


def test_shared_admin_is_honest_and_auth_disabled_cannot_send(chat_app):
    app, managers = chat_app
    app.extensions["oidc"] = None
    client = app.test_client()
    with client.session_transaction() as state:
        state.update(authenticated=True, oidc_display_name="Invented person")
    assert client.get("/api/servers/a/chat").get_json()["sender"] == "Shared admin"
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 202
    assert "[Fabricator \u00b7 Shared admin]" in managers["a"]._process.stdin.getvalue()
    app.config["FABRICATOR_AUTH_ENABLED"] = False
    assert client.get("/api/servers/a/chat").get_json()["sender"] is None
    assert client.post("/api/servers/a/chat", json={"message": "hello"}).status_code == 403


def test_saved_history_survives_panel_restart_and_missing_manager(chat_app, monkeypatch):
    from backend.core.app import create_app
    from backend.server.registry import get_server_process_registry
    app, managers = chat_app
    item = journal.append("a", {"kind": "player", "sender": "Alex", "message": "persisted"})
    monkeypatch.setattr(get_server_process_registry(), "get_manager", lambda sid: None)
    client = app.test_client()
    login(client)
    result = client.get("/api/servers/a/chat").get_json()
    assert result["items"] == [item] and not result["ready"]
    restarted = create_app()
    assert restarted.test_client().get("/api/servers/a/chat").get_json()["items"] == [item]


def test_history_api_pagination_and_unsupported_prior_observations(chat_app):
    app, managers = chat_app
    for i in range(5):
        journal.append("a", {"kind": "player", "sender": "Alex", "message": str(i)})
    client = app.test_client()
    login(client)
    latest = client.get("/api/servers/a/chat?limit=2").get_json()
    assert [i["message"] for i in latest["items"]] == ["3", "4"]
    earlier = client.get(f"/api/servers/a/chat?limit=2&before={latest['nextBefore']}").get_json()
    assert [i["message"] for i in earlier["items"]] == ["1", "2"]
    for query in ("limit=201", "limit=0", "before=-1", "after=x", "before=1&after=2", "after=9223372036854775808"):
        assert client.get("/api/servers/a/chat?" + query).status_code == 400
    managers["a"].loader = "velocity"
    result = client.get("/api/servers/a/chat").get_json()
    assert not result["supported"] and len(result["items"]) == 5


def test_initial_persistence_failure_refuses_send(chat_app, monkeypatch):
    app, managers = chat_app
    monkeypatch.setattr(journal, "append", MagicMock(side_effect=journal.ChatStorageError("storage-secret")))
    client = app.test_client()
    login(client)
    result = client.post("/api/servers/a/chat", json={"message": "hello"})
    assert result.status_code == 503 and result.get_json()["submitted"] is False
    assert managers["a"]._process.stdin.getvalue() == ""
    assert "storage-secret" not in result.get_data(as_text=True)


def test_post_send_persistence_failure_preserves_uncertain_intent(chat_app, monkeypatch):
    app, managers = chat_app
    monkeypatch.setattr(journal, "finish_send", MagicMock(side_effect=journal.ChatStorageError("storage-secret")))
    client = app.test_client()
    login(client)
    result = client.post("/api/servers/a/chat", json={"message": "hello"})
    assert result.status_code == 503 and result.get_json()["submitted"] is True
    assert managers["a"]._process.stdin.getvalue().count("\n") == 1
    saved = journal.history("a")["items"]
    assert len(saved) == 1 and saved[0]["status"] == "pending"
    assert "Do not retry blindly" in result.get_json()["error"]


def test_storage_read_failure_never_falls_back_to_console(chat_app, monkeypatch):
    app, managers = chat_app
    managers["a"]._stdout_buffer = [("now", "[12:34:56 INFO]: <Alex> secret")]
    monkeypatch.setattr(journal, "history", MagicMock(side_effect=journal.ChatStorageError("storage-secret")))
    client = app.test_client()
    login(client)
    response = client.get("/api/servers/a/chat")
    assert response.status_code == 503
    assert "secret" not in response.get_data(as_text=True)


def test_player_capture_failure_is_visible_without_breaking_console(tmp_path, monkeypatch, caplog):
    manager = ServerManager(str(tmp_path), loader="vanilla", version="1.21")
    manager.server_id = "a"
    monkeypatch.setattr(journal, "append", MagicMock(side_effect=journal.ChatStorageError("storage-secret")))
    manager._process = SimpleNamespace(
        stdout=io.StringIO("[12:34:56 INFO]: <Alex> payload-secret\n[12:34:56 INFO]: Console remains available\n"),
        stderr=io.StringIO(""),
    )
    manager._start_log_streams()
    manager._stdout_thread.join(timeout=2)
    manager._stderr_thread.join(timeout=2)
    assert manager.chat.capture_errors == 1
    assert len(manager._stdout_buffer) == 2
    assert "persistence failed" in caplog.text
    assert "storage-secret" not in caplog.text and "payload-secret" not in caplog.text
