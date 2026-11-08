"""Directory identity, source updates and authorization regressions."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask, g

from backend.auth import permissions
from backend.server.manager import ServerManager
from backend.server.players import directory, directory_routes, files, observed, service

UID = "12345678-1234-4234-8234-123456789abc"
OTHER = "12345678-1234-4234-8234-123456789def"


def registry_for(tmp_path):
    registry = MagicMock()
    registry.resolve_install_path.side_effect = lambda s: tmp_path / s["id"]
    registry.get_status.return_value = {"status": "stopped"}
    registry.get_manager.return_value = None
    return registry


def server(sid):
    return {"id": sid, "name": f"Server {sid}", "loader": "vanilla", "onlineMode": True}


def seed(path, entries, filename="whitelist.json"):
    path.mkdir(parents=True, exist_ok=True)
    files.write_json_list(path, filename, entries)


def feed(manager, stdout, stderr=""):
    manager._process = SimpleNamespace(stdout=io.StringIO(stdout), stderr=io.StringIO(stderr))
    manager._start_log_streams()
    manager._stdout_thread.join(timeout=3)
    manager._stderr_thread.join(timeout=3)
    assert not manager._stdout_thread.is_alive()


def test_actual_stream_events_persist_names_join_leave_and_restarts(tmp_path):
    manager = ServerManager(str(tmp_path / "a"))
    seed(tmp_path / "a", [])
    feed(manager, f"[12:00:00] [User Authenticator #1/INFO]: UUID of player First is {UID}\n"
         "[12:00:01] [Server thread/INFO]: First joined the game\n")
    assert manager._players["First"].observed_uuid == UID
    reg = registry_for(tmp_path)
    reg.get_manager.return_value = manager
    reg.get_status.return_value = {"status": "running"}
    app = Flask(__name__)
    with app.test_request_context():
        result = directory.aggregate(reg, [server("a")])
        assert result["players"][0]["servers"][0]["online"] is True
        assert result["players"][0]["servers"][0]["joined"] is True
    feed(manager, "[12:00:02] [Server thread/INFO]: First left the game\n")
    assert not manager._players
    replacement = ServerManager(str(tmp_path / "a"))
    feed(replacement, f"[12:01:00] [User Authenticator #1/INFO]: UUID of player Second is {UID}\n"
         "[12:01:01] [Server thread/INFO]: Second joined the game\n"
         "[12:01:02] [Server thread/INFO]: Second left the game\n")
    history = files.read_json_list(tmp_path / "a", observed.HISTORY_FILE)
    assert len(history) == 1
    assert history[0]["names"] == ["First", "Second"]
    assert history[0]["lastSeen"] >= history[0]["lastJoined"]


def test_chat_stderr_and_legacy_name_enrichment_cannot_attribute_join(tmp_path):
    manager = ServerManager(str(tmp_path / "a"))
    seed(tmp_path / "a", [{"uuid": UID, "name": "Spoof"}], "usercache.json")
    feed(manager, f"[12:00:00] [Server thread/INFO]: <Spoof> UUID of player Spoof is {UID}\n"
         "[12:00:01] [Server thread/INFO]: Spoof joined the game\n",
         f"[12:00:00] [Server thread/INFO]: UUID of player Fake is {OTHER}\n"
         "[12:00:01] [Server thread/INFO]: Fake joined the game\n")
    manager._players["Spoof"].uuid = UID  # Legacy name cache enrichment.
    assert manager._players["Spoof"].observed_uuid is None
    assert "Fake" not in manager._players
    reg = registry_for(tmp_path)
    reg.get_manager.return_value = manager
    reg.get_status.return_value = {"status": "running"}
    with Flask(__name__).test_request_context():
        result = directory.aggregate(reg, [server("a")])
    assert result["players"][0]["servers"][0]["joined"] is None
    assert result["players"][0]["servers"][0]["online"] is None
    assert result["servers"][0]["online"] == "partial"
    assert not (tmp_path / "a" / observed.HISTORY_FILE).exists()


def test_whitelist_service_changes_are_current_and_never_join_evidence(tmp_path, monkeypatch):
    reg = registry_for(tmp_path)
    seed(tmp_path / "a", [])
    monkeypatch.setattr(service, "mojang_resolve_name", lambda *args: UID)
    service.add_to_whitelist(reg, server("a"), "First")
    with Flask(__name__).test_request_context():
        result = directory.aggregate(reg, [server("a")])
        assert result["players"][0]["servers"][0]["whitelisted"] is True
        assert result["players"][0]["servers"][0]["joined"] is None
        service.remove_from_whitelist(reg, server("a"), "First")
        assert directory.aggregate(reg, [server("a")])["players"] == []
    reg.send_command.assert_not_called()


def test_saved_playerdata_namespaces_collisions_and_unavailable_whitelist(tmp_path):
    offline = str(files.offline_uuid("First"))
    for sid in ("a", "b"):
        seed(tmp_path / sid, [{"uuid": UID, "name": "Same"}, {"uuid": offline, "name": "First"}])
        observed.record(tmp_path / sid, UID, f"Former{sid}", joined=True)
        folder = tmp_path / sid / "world" / "players" / "data"
        folder.mkdir(parents=True)
        (folder / f"{OTHER}.dat").write_bytes(b"saved")
    (tmp_path / "b" / "whitelist.json").unlink()
    with Flask(__name__).test_request_context():
        result = directory.aggregate(registry_for(tmp_path), [server("a"), server("b")])
    online = next(p for p in result["players"] if p["uuid"] == UID)
    assert len(online["servers"]) == 2
    assert {"Same", "Formera", "Formerb"} <= set(online["names"])
    offline_rows = [p for p in result["players"] if p["uuid"] == offline]
    assert len(offline_rows) == 1  # Only a has an offline entry.
    seed(tmp_path / "b", [{"uuid": offline, "name": "First"}, {"uuid": OTHER, "name": "Same"}])
    with Flask(__name__).test_request_context():
        result = directory.aggregate(registry_for(tmp_path), [server("a"), server("b")])
    assert len([p for p in result["players"] if p["uuid"] == offline]) == 2
    assert len([p for p in result["players"] if "Same" in p["names"]]) == 2
    assert next(p for p in result["players"] if p["uuid"] == OTHER)["servers"][0]["joined"] is True


@pytest.fixture
def directory_client(tmp_path, monkeypatch):
    reg = registry_for(tmp_path)
    for sid, name in (("a", "Visible"), ("b", "SECRET")):
        seed(tmp_path / sid, [{"uuid": UID if sid == "a" else OTHER, "name": name}])
    monkeypatch.setattr(directory_routes, "get_server_process_registry", lambda: reg)
    monkeypatch.setattr(directory_routes.storage, "get_all_servers", lambda: [server("a"), server("b")])
    app = Flask(__name__)
    app.register_blueprint(directory_routes.directory_bp)
    app.user = permissions.Principal("panel-user", "https://oidc.example", (), frozenset(),
                                     {"a": frozenset({"server.view", "players.read"}),
                                      "b": frozenset({"server.view"})}, {})

    @app.before_request
    def auth():
        return permissions.authorize(app.user)

    return app.test_client(), reg, app


def test_directory_filters_details_counts_and_reads_are_authorized(directory_client):
    client, reg, app = directory_client
    result = client.get("/api/players").get_json()
    assert result["total"] == 1
    assert [s["id"] for s in result["servers"]] == ["a"]
    assert all(call.args[0]["id"] == "a" for call in reg.resolve_install_path.call_args_list)
    assert client.get("/api/players?q=SECRET").get_json()["total"] == 0
    assert client.get("/api/players?server=b").get_json()["total"] == 0
    assert client.get("/api/players?relation=joined").get_json()["total"] == 0
    assert client.get("/api/players?relation=whitelisted").get_json()["total"] == 1
    assert client.get(f"/api/players/minecraft:{OTHER}").status_code == 404
    detail = client.get(f"/api/players/minecraft:{UID}").get_json()
    assert [s["id"] for s in detail["servers"]] == ["a"]
    app.user = permissions.Principal("panel-user", "https://oidc.example", (), frozenset(), {}, {})
    assert client.get("/api/players").get_json()["total"] == 0
    assert client.get(f"/api/players/minecraft:{UID}").status_code == 404
    assert client.post("/api/players").status_code in (403, 405)


def test_pagination_names_and_live_whitelist_file_updates(directory_client, tmp_path):
    client, _, _ = directory_client
    observed.record(tmp_path / "a", UID, "OldVisible", joined=True)
    observed.record(tmp_path / "a", UID, "Visible", joined=True)
    seed(tmp_path / "a", [{"uuid": UID, "name": "Visible"}, {"uuid": OTHER, "name": "Another"}])
    result = client.get("/api/players?limit=1").get_json()
    assert result["total"] == 2
    assert len(result["players"]) == 1
    second = client.get("/api/players?limit=1&offset=1").get_json()
    assert second["total"] == 2
    assert second["players"][0]["id"] != result["players"][0]["id"]
    assert client.get("/api/players?q=OldVisible").get_json()["total"] == 1
    seed(tmp_path / "a", [])
    result = client.get("/api/players?relation=whitelisted").get_json()
    assert result["total"] == 0
    detail = client.get(f"/api/players/minecraft:{UID}").get_json()
    assert detail["servers"][0]["whitelisted"] is False
    assert detail["servers"][0]["joined"] is True


def test_unattributed_or_wrong_version_uuid_is_not_minecraft_account():
    uid = "12345678-1234-1234-8234-123456789abc"
    assert directory.identity(uid, "a")[1] == "unknown:a"
    assert directory.identity(uid, "a")[0] != directory.identity(uid, "b")[0]
    assert directory.identity("panel-oidc-subject", "a") is None


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=no", "relation=banned"])
def test_invalid_filters_are_explicit(directory_client, query):
    client, reg, _ = directory_client
    assert client.get("/api/players?" + query).status_code == 400
    reg.resolve_install_path.assert_not_called()


def test_corrupt_missing_and_path_escape_sources_are_explicit(tmp_path):
    seed(tmp_path / "a", [{"uuid": UID, "name": "First"}], "usercache.json")
    reg = registry_for(tmp_path)
    with Flask(__name__).test_request_context():
        data = directory.aggregate(reg, [server("a")])
        assert data["players"][0]["servers"][0]["whitelisted"] is None
        (tmp_path / "a" / "whitelist.json").write_text("{}")
        data = directory.aggregate(reg, [server("a")])
        assert data["players"] == []
        assert data["servers"][0]["whitelist"] == "unavailable"
        seed(tmp_path / "a", [])
        (tmp_path / "a" / "server.properties").write_text("level-name=../../outside")
        assert directory.aggregate(reg, [server("a")])["servers"][0]["history"] == "unavailable"
        (tmp_path / "a" / "server.properties").unlink()
        seed(tmp_path / "a", [{"uuid": UID, "lastSeen": 42, "names": ["First"]}], observed.HISTORY_FILE)
        assert directory.aggregate(reg, [server("a")])["servers"][0]["history"] == "unavailable"


def test_real_app_registers_directory_and_read_does_not_grant_management(client, tmp_servers_root, app):
    root = Path(tmp_servers_root)
    (root / "servers.json").write_text(json.dumps([server("a")]))
    seed(root / "servers" / "a", [{"uuid": UID, "name": "First"}])

    @app.before_request
    def scoped_user():
        g.principal = permissions.Principal("First", "https://oidc.example", (), frozenset(),
                                            {"a": frozenset({"server.view", "players.read"})}, {})
        return permissions.authorize(g.principal)

    assert client.get("/api/players").status_code == 200
    assert client.get(f"/api/players/minecraft:{UID}").status_code == 200
    assert client.post("/api/servers/a/players/whitelist", json={"name": "Other"}).status_code == 403
    assert client.delete("/api/servers/a/players/whitelist", json={"name": "First"}).status_code == 403
