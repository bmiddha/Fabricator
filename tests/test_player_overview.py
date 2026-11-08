"""Bounded, UUID-attributed online preview and per-server authorization."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from flask import g

from backend.auth import permissions
from backend.server import storage
from backend.server.manager import OnlinePlayer, ServerManager
from backend.server.players import files, live, overview_routes

UID = "12345678-1234-4234-8234-123456789abc"


def snapshot_registry(tmp_path, count=13):
    manager = ServerManager(str(tmp_path))
    manager._players = {
        f"Player{i}": OnlinePlayer(f"Player{i}", datetime.now(timezone.utc),
                                 uuid=UID, observed_uuid=UID if i == 0 else None)
        for i in range(count)
    }
    registry = MagicMock()
    registry.get_status.return_value = {"status": "running"}
    registry.get_manager.return_value = manager
    return registry, manager


def test_preview_caps_at_ten_with_remainder_and_never_resolves_name(tmp_path, monkeypatch):
    registry, _ = snapshot_registry(tmp_path)
    lookup = MagicMock(side_effect=AssertionError("No name resolution"))
    monkeypatch.setattr(live.mojang, "lookup_cached", lookup)
    result = live.preview_online(registry, {"id": "a", "onlineMode": True})
    assert len(result["players"]) == 10
    assert result["total"] == 13
    assert result["status"] == "partial"
    assert result["players"][0]["id"] == f"minecraft:{UID}"
    assert result["players"][1]["uuid"] is None  # Legacy cached UUID is not evidence.
    lookup.assert_not_called()
    registry.resolve_install_path.assert_not_called()
    registry.send_command.assert_not_called()


def test_offline_synthetic_uuid_and_unknown_namespace_remain_distinct(tmp_path):
    registry, manager = snapshot_registry(tmp_path, 3)
    manager._players["Player1"].observed_uuid = str(files.offline_uuid("Player1"))
    manager._players["Player2"].observed_uuid = "12345678-1234-1234-8234-123456789abc"
    result = live.preview_online(registry, {"id": "a", "onlineMode": False})
    assert result["offlineMode"] is True
    assert result["players"][1]["namespace"] == "offline:a"
    assert result["players"][2]["namespace"] == "unknown:a"
    result_b = live.preview_online(registry, {"id": "b", "onlineMode": False})
    assert result_b["players"][1]["id"] != result["players"][1]["id"]


def test_preview_empty_stopped_starting_proxy_and_untracked_are_explicit(tmp_path):
    registry, _ = snapshot_registry(tmp_path, 0)
    assert live.preview_online(registry, {"id": "a"})["status"] == "partial"
    registry.get_status.return_value = {"status": "stopped"}
    assert live.preview_online(registry, {"id": "a"})["status"] == "offline"
    registry.get_status.return_value = {"status": "starting"}
    assert live.preview_online(registry, {"id": "a"})["status"] == "unavailable"
    registry.get_status.return_value = {"status": "running"}
    registry.get_manager.return_value = None
    assert live.preview_online(registry, {"id": "a"})["status"] == "unavailable"
    assert live.preview_online(registry, {"id": "a", "loader": "velocity"})["players"] == []


def test_real_preview_route_requires_player_read_before_runtime_access(app, client, tmp_path, monkeypatch):
    with app.app_context():
        record = storage.create_server({"name": "A", "loader": "vanilla", "version": "1.21"})
    sid = record["id"]
    registry, _ = snapshot_registry(tmp_path)
    monkeypatch.setattr(overview_routes, "get_server_process_registry", lambda: registry)
    user = permissions.Principal("viewer", "https://oidc.example", (), frozenset(),
                                 {sid: frozenset({"server.view"})}, {})

    @app.before_request
    def scope():
        g.principal = user
        return permissions.authorize(user)

    assert client.get(f"/api/servers/{sid}/players/preview").status_code == 403
    assert client.get("/api/servers/hidden/players/preview").status_code == 404
    registry.get_status.assert_not_called()
    user = permissions.Principal("reader", "https://oidc.example", (), frozenset(),
                                 {sid: frozenset({"server.view", "players.read"})}, {})
    response = client.get(f"/api/servers/{sid}/players/preview")
    assert response.status_code == 200
    assert len(response.get_json()["players"]) == 10
    assert response.get_json()["total"] == 13
    registry.send_command.assert_not_called()
