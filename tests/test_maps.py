"""Private map capabilities, bounded transport and explicit setup regressions."""
import json
import time
import zipfile
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def maps(app, tmp_path, monkeypatch):
    from backend.maps import service
    from backend.server import storage
    root = tmp_path / "servers/map-server"
    root.mkdir()
    (root / "mods").mkdir()
    server = {
        "id": "a", "name": "Test", "loader": "fabric", "loaderVersion": "0.18.4",
        "version": "1.21.1", "status": "stopped", "installPath": str(root),
    }
    other = {**server, "id": "b"}
    storage.save_servers([server, other])
    monkeypatch.setattr(service.get_server_process_registry(), "get_status",
                        lambda sid: {"status": storage.get_server(sid)["status"], "pid": 4321})
    with service._lock:
        service._tickets.clear()
    return service, storage, server, root


def dependencies(maps):
    service, _, _, root = maps
    for key, version in {**service.VERSIONS, "fabric-api": "0.116.7+1.21.1"}.items():
        with zipfile.ZipFile(root / "mods" / f"{key}.jar", "w") as archive:
            archive.writestr("fabric.mod.json", json.dumps({"id": key, "version": version}))


def setup(client, maps):
    dependencies(maps)
    response = client.post("/api/servers/a/map/setup", json={"accept_resource_download": True})
    assert response.status_code == 201, response.json
    maps[1].update_server("a", {"status": "running"})


def live(maps, monkeypatch):
    service, storage, _, _ = maps
    config = service.manifest(storage.get_server("a"))
    calls = []

    def upstream(config, path):
        calls.append(path)
        if path == "fabricator-status.json":
            return json.dumps({"schema": 1, "nonce": config["nonce"], "pid": 4321,
                               "updated": time.time(), "global_waystones": 1,
                               "global_markers": {"overworld": ["stable-id"]}}).encode()
        if path == "index.html":
            return b'<html><head></head><body><script src="assets/viewer.js"></script></body></html>'
        if path == "settings.json":
            return json.dumps({"maps": ["overworld"], "mapDataRoot": "https://evil.example",
                               "liveDataRoot": "/api", "scripts": ["/api/admin"], "useCookies": True}).encode()
        if path.endswith("/markers.json"):
            return json.dumps({service.LAYER: {"label": "Global waystones", "markers": {}},
                               "private-home": {"secret": "must-not-appear"}}).encode()
        if path.endswith("/players.json"):
            return b'{"players":[]}'
        if path.endswith(".json"):
            return b"{}"
        return b"fixture"

    monkeypatch.setattr(service, "upstream", upstream)
    return calls


def test_setup_is_explicit_stopped_version_scoped_and_persistent(client, maps):
    service, storage, server, root = maps
    assert client.get("/api/servers/a/map").json["enabled"] is False
    assert not (root / "config").exists()
    assert client.post("/api/servers/a/map/setup", json={}).status_code == 409
    assert client.post("/api/servers/a/map/setup", json={"accept_resource_download": True}).status_code == 409
    dependencies(maps)
    for changes in ({"loader": "quilt"}, {"version": "1.20.1"}, {"loaderVersion": "0.16.0"},
                    {"status": "running"}):
        storage.update_server("a", changes)
        assert client.post("/api/servers/a/map/setup", json={"accept_resource_download": True}).status_code == 409
        storage.update_server("a", server)
    response = client.post("/api/servers/a/map/setup", json={"accept_resource_download": True})
    assert response.status_code == 201
    assert response.json["restart_required"]
    config = service.manifest(storage.get_server("a"))
    listener = (root / "config/bluemap/webserver.conf").read_text()
    assert 'ip: "127.0.0.1"' in listener
    assert config["port"] == storage.get_server("a")["map"]["port"]
    assert storage.get_server("a")["status"] == "stopped"
    payload = client.get("/api/servers/a/map").get_data(as_text=True)
    assert str(root) not in payload and config["nonce"] not in payload
    assert client.post("/api/servers/a/map/setup", json={"accept_resource_download": True}).status_code == 409


def test_existing_operator_configs_are_never_overwritten(client, maps):
    dependencies(maps)
    root = maps[3]
    directory = root / "config/bluemap"
    directory.mkdir(parents=True)
    config = directory / "core.conf"
    original = b"# operator configuration\naccept-download: false\n"
    config.write_bytes(original)
    assert client.post("/api/servers/a/map/setup", json={"accept_resource_download": True}).status_code == 409
    assert config.read_bytes() == original
    assert not (directory / "webserver.conf").exists()


def test_dependencies_are_identified_by_manifest_not_filename(client, maps):
    dependencies(maps)
    service, _, server, root = maps
    assert service.preview(server)["ready_to_configure"]
    (root / "mods/waystones.jar").unlink()
    with zipfile.ZipFile(root / "mods/waystones-21.1.37.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "fwaystones", "version": "3.3.3"}))
    plan = service.preview(server)
    assert not plan["ready_to_configure"]
    assert next(d for d in plan["dependencies"] if d["id"] == "waystones")["installed"] is None


def test_config_drift_and_symlinks_fail_closed(client, maps):
    setup(client, maps)
    root = maps[3]
    config = root / "config/bluemap/webserver.conf"
    config.write_text('ip: "0.0.0.0"\nport: 8100\n')
    assert not client.get("/api/servers/a/map").json["ready"]
    assert client.post("/api/servers/a/map/session").status_code == 409
    assert client.put("/api/servers/a/map/enabled", json={"enabled": False}).status_code == 200
    assert maps[1].get_server("a")["map"]["enabled"] is False
    assert client.put("/api/servers/a/map/enabled", json={"enabled": True}).status_code == 409
    config.unlink()
    config.symlink_to(root / "server.properties")
    assert client.post("/api/servers/a/map/session").status_code == 409


def test_heartbeat_cannot_be_reused_after_process_restart(client, maps, monkeypatch):
    setup(client, maps)
    live(maps, monkeypatch)
    assert client.get("/api/servers/a/map").json["ready"] is True
    monkeypatch.setattr(maps[0].get_server_process_registry(), "get_status",
                        lambda sid: {"status": "running", "pid": 9999})
    assert client.get("/api/servers/a/map").json["ready"] is False
    assert client.post("/api/servers/a/map/session").status_code == 409


def test_sandbox_csp_uses_allowlisted_browser_origin_behind_dev_proxy(client, maps, monkeypatch):
    setup(client, maps)
    live(maps, monkeypatch)
    response = client.post("/api/servers/a/map/session", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 200
    csp = client.get(response.json["url"]).headers["Content-Security-Policy"]
    assert "http://localhost:3000/api/servers/a/map/view/" in csp
    assert client.post("/api/servers/a/map/session",
                       headers={"Origin": "https://not-allowed.example"}).status_code == 409


def test_every_asset_and_live_feed_requires_scoped_expiring_capability(client, maps, monkeypatch):
    setup(client, maps)
    calls = live(maps, monkeypatch)
    url = client.post("/api/servers/a/map/session").json["url"]
    for path in ("", "assets/viewer.js", "maps/overworld/settings.json",
                 "maps/overworld/live/players.json", "maps/overworld/live/markers.json",
                 "maps/overworld/hires/0_0.prbm"):
        response = client.get(url + path, headers={"Origin": "null", "Cookie": "secret=canary"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Access-Control-Allow-Origin"] == "null"
        assert "Access-Control-Allow-Credentials" not in response.headers
        assert "sandbox allow-scripts" in response.headers["Content-Security-Policy"]
        assert "allow-same-origin" not in response.headers["Content-Security-Policy"]
    assert client.get(url.replace("/a/", "/b/")).status_code == 401
    assert b"fabricator-sandbox.js" in client.get(url).data
    assert b"configurable: false" in client.get(url + "fabricator-sandbox.js").data
    assert client.get("/api/servers/a/map/view/bogus/settings.json").status_code == 401
    calls.clear()
    for path in ("../server.properties", "maps/%252e%252e/secret.json", "maps/a/../../config.json",
                 "fabricator-status.json", "server.properties", "maps/a/live/markers.json.gz"):
        assert client.get(url + path).status_code >= 400
    assert calls == []
    ticket = url.rstrip("/").rsplit("/", 1)[1]
    maps[0]._tickets[ticket]["expires"] = 0
    assert client.get(url + "assets/viewer.js").status_code == 401


def test_settings_are_local_and_only_global_marker_layer_crosses_boundary(client, maps, monkeypatch):
    setup(client, maps)
    live(maps, monkeypatch)
    url = client.post("/api/servers/a/map/session").json["url"]
    settings = client.get(url + "settings.json").json
    assert settings["mapDataRoot"] == settings["liveDataRoot"] == "maps"
    assert settings["scripts"] == settings["styles"] == [] and settings["useCookies"] is False
    markers = client.get(url + "maps/overworld/live/markers.json").json
    assert set(markers) == {maps[0].LAYER}
    assert "must-not-appear" not in json.dumps(markers)
    maps[1].update_server("a", {"map": {"enabled": False}})
    assert client.get(url + "maps/overworld/live/players.json").status_code == 404


def test_stale_heartbeat_and_offline_server_do_not_serve_positions(client, maps, monkeypatch):
    setup(client, maps)
    live(maps, monkeypatch)
    url = client.post("/api/servers/a/map/session").json["url"]
    service = maps[0]
    monkeypatch.setattr(service, "upstream", lambda config, path: json.dumps(
        {"schema": 1, "nonce": config["nonce"], "updated": time.time() - 21}).encode())
    assert client.get(url + "maps/overworld/live/players.json").status_code == 409
    assert client.get("/api/servers/a/map").json["ready"] is False
    maps[1].update_server("a", {"status": "stopped"})
    assert client.post("/api/servers/a/map/session").status_code == 409


def test_persisted_global_markers_are_filtered_against_current_snapshot_after_restart(maps):
    service = maps[0]
    raw = json.dumps({service.LAYER: {"markers": {
        "deleted": {"label": "deleted before restart"},
        "privatized": {"label": "now private"},
        "moved": {"label": "now in another dimension"},
        "still-global": {"label": "public"},
    }}, "private-homes": {"markers": {"home": {}}}}).encode()
    current = {"overworld": ["still-global"], "nether": ["moved"]}
    overworld = json.loads(service.filter_json("maps/overworld/live/markers.json", raw, current))
    assert set(overworld) == {service.LAYER}
    assert set(overworld[service.LAYER]["markers"]) == {"still-global"}
    nether = json.loads(service.filter_json("maps/nether/live/markers.json", raw, current))
    assert set(nether[service.LAYER]["markers"]) == {"moved"}
    unknown = json.loads(service.filter_json("maps/unknown/live/markers.json", raw, current))
    assert unknown[service.LAYER]["markers"] == {}


def test_transport_does_not_forward_auth_cookies_proxy_redirects_or_arbitrary_urls(maps, monkeypatch):
    service = maps[0]
    session = MagicMock()
    response = session.__enter__.return_value.get.return_value.__enter__.return_value
    response.status_code = 200
    response.iter_content.return_value = [b"asset"]
    monkeypatch.setattr(service.requests, "Session", lambda: session)
    assert service.upstream({"port": 8800}, "maps/world/live/players.json") == b"asset"
    get = session.__enter__.return_value.get
    assert get.call_args.args == ("http://127.0.0.1:8800/maps/world/live/players.json",)
    options = get.call_args.kwargs
    assert options["allow_redirects"] is False and options["headers"] == {"Accept-Encoding": "identity"}
    assert session.__enter__.return_value.trust_env is False
    response.status_code = 302
    with pytest.raises(service.MapError):
        service.upstream({"port": 8800}, "settings.json")
    response.status_code = 200
    response.iter_content.return_value = [b"x" * (service.MAX_BODY + 1)]
    with pytest.raises(service.MapError):
        service.upstream({"port": 8800}, "assets/test.js")


def test_pinned_install_requires_stopped_compatible_server_and_verifies_digest(client, maps, monkeypatch):
    from backend.modrinth.client import ModrinthClient, ModrinthApiError
    download = MagicMock()
    monkeypatch.setattr(ModrinthClient, "download_mod", download)
    response = client.post("/api/servers/a/map/install-bluemap")
    assert response.status_code == 201
    assert download.call_args.kwargs["hashes"] == {"sha512": maps[0].BLUE_MAP["sha512"]}
    assert download.call_args.args[0] == maps[0].BLUE_MAP["url"]
    download.side_effect = ModrinthApiError("bad hash")
    assert client.post("/api/servers/a/map/install-bluemap").status_code == 409
    maps[1].update_server("a", {"status": "running"})
    download.reset_mock()
    assert client.post("/api/servers/a/map/install-bluemap").status_code == 409
    download.assert_not_called()


def test_no_map_public_auth_bypass(auth_client):
    for path in ("/api/servers/a/map", "/api/servers/a/map/view/not-a-ticket/",
                 "/api/servers/a/map/view/not-a-ticket/maps/world/live/players.json"):
        assert auth_client.get(path).status_code == 401
    for path in ("setup", "session", "install-bluemap"):
        assert auth_client.post("/api/servers/a/map/" + path).status_code == 401
