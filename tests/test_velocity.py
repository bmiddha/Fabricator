"""Proxy lifecycle, lossless routing, protected secrets and explicit forwarding."""
from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tomlkit
from ruamel.yaml import YAML


@pytest.fixture
def velocity(app):
    service = importlib.import_module("backend.velocity.service")
    storage = importlib.import_module("backend.server.storage")
    routes = importlib.import_module("backend.velocity.routes")
    if "velocity" not in app.blueprints:
        app.register_blueprint(routes.velocity_bp, url_prefix="/api")
    proxy = storage.create_server({
        "name": "Gateway", "loader": "velocity", "version": "3.4.0-SNAPSHOT",
        "port": 25565, "installPath": "gateway", "memory": 1,
    })
    backend = storage.create_server({
        "name": "Lobby", "loader": "paper", "version": "1.21.4",
        "port": 25566, "installPath": "lobby",
    })
    root = service._root(backend)
    root.mkdir(parents=True, exist_ok=True)
    (root / "server.properties").write_text("# original properties\nonline-mode=true\nmotd=Keep me\n")
    (root / "config").mkdir()
    (root / "config" / "paper-global.yml").write_text("# original YAML\nunrelated: 'preserve me'\n")
    (root / "spigot.yml").write_text("settings:\n  # keep this comment\n  bungeecord: true\n")
    service.initialize(proxy)
    config = {
        "listen": {"host": "0.0.0.0", "port": 25565},
        "onlineMode": True, "forwardingMode": "modern",
        "backends": [{"name": "lobby", "serverId": backend["id"]}],
        "try": ["lobby"], "forcedHosts": {"play.example.com": ["lobby"]},
    }
    return SimpleNamespace(service=service, storage=storage, proxy=proxy, backend=backend,
                           root=root, config=config, client=app.test_client())


def _url(v, suffix="config"):
    return f"/api/servers/{v.proxy['id']}/velocity/{suffix}"


def test_velocity_installer_has_proxy_launch_and_no_game_files(tmp_path, monkeypatch, tmp_servers_root):
    installers = importlib.import_module("backend.server.installer")
    papermc = importlib.import_module("backend.server.installer.papermc")
    storage = importlib.import_module("backend.server.storage")
    server = storage.create_server({"loader": "velocity", "version": "3.4.0-SNAPSHOT", "port": 25565})
    installer = installers.get_installer_for("velocity", tmp_path / "proxy")
    assert installer.loader_name == "velocity"
    assert installer.modrinth_loader_facets == ["velocity"]
    monkeypatch.setattr(installer, "_select_build", lambda version: {
        "id": 99, "downloads": {"server:default": {"url": "https://example.test/velocity.jar",
                                                  "checksums": {"sha256": "abc"}}},
    })
    def download(url, target, **kwargs):
        assert kwargs["sha256"] == "abc"
        target.write_bytes(b"proxy")
    monkeypatch.setattr(papermc, "download_with_hash_verify", download)
    result = installer.install_with_config("3.4.0-SNAPSHOT", server)
    assert result.success
    assert result.launch.program_args == []
    assert result.launch.type == "jar"
    assert (installer.install_path / "velocity.toml").exists()
    assert not (installer.install_path / "server.properties").exists()
    assert not (installer.install_path / "eula.txt").exists()


def test_proxy_java_requirement_not_minecraft_version():
    compat = importlib.import_module("backend.server.java_compat")
    assert compat.resolve_required_java("3.4.0-SNAPSHOT", "velocity").required_java == 21
    assert compat.resolve_required_java("1.16.5").required_java == 8
    assert compat.resolve_required_java("1.21.4").required_java == 21


def test_routing_roundtrip_preserves_comments_secret_and_references(velocity):
    v = velocity
    path = v.service._config_path(v.proxy)
    path.write_text(path.read_text() + '\n# custom settings\n[advanced]\ncompression-threshold = 512\n')
    secret = v.service._secret(v.proxy)
    result = v.client.put(_url(v), json=v.config)
    assert result.status_code == 200, result.json
    assert result.json["backends"] == [{"name": "lobby", "serverId": v.backend["id"], "address": "127.0.0.1:25566"}]
    assert result.json["forcedHosts"] == {"play.example.com": ["lobby"]}
    assert result.json["forwardingSecretConfigured"] is True
    assert secret not in result.get_data(as_text=True)
    assert "# custom settings" in path.read_text()
    assert tomlkit.parse(path.read_text())["advanced"]["compression-threshold"] == 512
    assert v.service._secret(v.proxy) == secret
    assert v.service._secret_path(v.proxy).stat().st_mode & 0o777 == 0o600
    again = v.client.put(_url(v), json=result.json)
    assert again.status_code == 200
    assert v.service._secret(v.proxy) == secret
    assert list(path.parent.glob("velocity.toml.fabricator-backup-*"))
    assert v.client.delete(f"/api/servers/{v.backend['id']}").status_code == 409
    with pytest.raises(ValueError, match="referenced"):
        v.storage.delete_server(v.backend["id"])


@pytest.mark.parametrize("change", [
    {"forwardingMode": "legacy"}, {"listen": {"host": "0.0.0.0", "port": True}},
    {"listen": {"host": "0.0.0.0", "port": 25566}},
    {"backends": [{"name": "self", "serverId": "__self__"}]},
    {"try": ["missing"]}, {"try": []}, {"forcedHosts": {"*.example.com": ["lobby"]}},
    {"forcedHosts": {"play.example.com": ["missing"]}},
    {"backends": [{"name": "try", "serverId": "missing"}]},
    {"onlineMode": "false"},
])
def test_invalid_config_never_writes(velocity, change):
    v = velocity
    original = v.service._config_path(v.proxy).read_bytes()
    change = {**change}
    if "__self__" in str(change):
        change["backends"][0]["serverId"] = v.proxy["id"]
    response = v.client.put(_url(v), json={**v.config, **change})
    assert response.status_code == 400
    assert v.service._config_path(v.proxy).read_bytes() == original


def test_running_proxy_or_backend_rejects_writes(velocity, monkeypatch):
    v = velocity
    monkeypatch.setattr(v.service._registry(), "get_status", lambda sid: {"status": "running"})
    assert v.client.put(_url(v), json=v.config).status_code == 409


def test_forwarding_preview_apply_requires_confirmation_and_fresh_preview(velocity):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    payload = {"serverIds": [v.backend["id"]]}
    response = v.client.post(_url(v, "forwarding/preview"), json=payload)
    assert response.status_code == 200, response.json
    assert (v.root / "server.properties").read_text().find("online-mode=true") != -1
    secret = v.service._secret(v.proxy)
    assert secret not in response.get_data(as_text=True)
    payload["previewToken"] = response.json["previewToken"]
    assert v.client.post(_url(v, "forwarding/apply"), json=payload).status_code == 400
    payload["confirmOfflineMode"] = True
    (v.root / "server.properties").write_text((v.root / "server.properties").read_text() + "unrelated=added\n")
    assert v.client.post(_url(v, "forwarding/apply"), json=payload).status_code == 400
    response = v.client.post(_url(v, "forwarding/preview"), json=payload)
    payload["previewToken"] = response.json["previewToken"]
    result = v.client.post(_url(v, "forwarding/apply"), json=payload)
    assert result.status_code == 200, result.json
    assert secret not in result.get_data(as_text=True)
    paper = v.root / "config" / "paper-global.yml"
    text = paper.read_text()
    assert "# original YAML" in text
    assert "unrelated: 'preserve me'" in text
    parsed = YAML().load(text)
    assert parsed["proxies"]["velocity"] == {"enabled": True, "online-mode": True, "secret": secret}
    assert "online-mode=false" in (v.root / "server.properties").read_text()
    assert "motd=Keep me" in (v.root / "server.properties").read_text()
    assert "unrelated=added" in (v.root / "server.properties").read_text()
    assert "# keep this comment" in (v.root / "spigot.yml").read_text()
    assert YAML().load((v.root / "spigot.yml").read_text())["settings"]["bungeecord"] is False
    assert v.storage.get_server(v.backend["id"])["onlineMode"] is False
    assert len(result.json["backups"]) == 3
    assert v.client.get(f"/api/servers/{v.backend['id']}/files/content?path=config/paper-global.yml").status_code == 403
    assert v.client.get(f"/api/servers/{v.proxy['id']}/files/content?path=velocity.toml").status_code == 403


@pytest.mark.parametrize("loader,version", [("fabric", "1.21.4"), ("vanilla", "1.21.4"), ("paper", "1.12.2"), ("paper", "1.21-pre1")])
def test_forwarding_rejects_unsupported_backends(velocity, loader, version):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    v.storage.update_server(v.backend["id"], {"loader": loader, "version": version})
    result = v.client.post(_url(v, "forwarding/preview"), json={"serverIds": [v.backend["id"]]})
    assert result.status_code == 400
    assert "supported only" in result.json["error"]


def test_legacy_paper_yaml_preserved(velocity):
    v = velocity
    v.storage.update_server(v.backend["id"], {"version": "1.18.2"})
    (v.root / "paper.yml").write_text("# old Paper\nsettings:\n  unrelated: true\n")
    assert v.client.put(_url(v), json=v.config).status_code == 200
    payload = {"serverIds": [v.backend["id"]]}
    preview = v.client.post(_url(v, "forwarding/preview"), json=payload)
    assert preview.status_code == 200
    payload.update(confirmOfflineMode=True, previewToken=preview.json["previewToken"])
    assert v.client.post(_url(v, "forwarding/apply"), json=payload).status_code == 200
    text = (v.root / "paper.yml").read_text()
    assert "# old Paper" in text
    assert YAML().load(text)["settings"]["unrelated"] is True
    assert YAML().load(text)["settings"]["velocity-support"]["enabled"] is True


def test_proxy_creation_managed_rejection_and_capabilities(client, monkeypatch):
    payload = {"name": "Gateway", "loader": "velocity", "version": "3.4.0-SNAPSHOT",
               "port": 25565, "installPath": "gateway", "memoryMin": 1, "memory": 1,
               "javaPath": ""}
    monkeypatch.setenv("FABRICATOR_MANAGED", "1")
    assert client.post("/api/servers", json=payload).status_code == 403
    monkeypatch.delenv("FABRICATOR_MANAGED")
    response = client.post("/api/servers", json=payload)
    assert response.status_code == 201
    assert response.json["kind"] == "proxy"
    assert response.json["capabilities"]["serverProperties"] is False
    assert response.json["javaRequirement"]["required_java"] == 21
    sid = response.json["id"]
    assert client.put(f"/api/servers/{sid}/settings", json={"motd": "no game settings"}).status_code == 400
    assert client.put(f"/api/servers/{sid}/settings", json={"name": "Renamed"}).status_code == 200
    service = importlib.import_module("backend.velocity.service")
    assert not (service._root(response.json) / "server.properties").exists()


def test_velocity_stop_sends_shutdown(tmp_path):
    manager_mod = importlib.import_module("backend.server.manager")
    manager = manager_mod.ServerManager(str(tmp_path), loader="velocity")
    proc = Mock()
    proc.poll.return_value = None
    proc.stdin.closed = False
    manager._process = proc
    result = manager.stop()
    assert result["status"] == "stopped"
    proc.stdin.write.assert_called_once_with("shutdown\n")


def test_velocity_has_no_tps_probe():
    tps = importlib.import_module("backend.server.tps")
    assert tps.probe_for("velocity", "3.4.0-SNAPSHOT") is None


def test_missing_secret_fails_closed_instead_of_rotating(velocity):
    v = velocity
    v.service._secret_path(v.proxy).unlink()
    with pytest.raises(v.service.ConfigError, match="missing"):
        v.service.initialize(v.proxy)
    assert not v.service._secret_path(v.proxy).exists()


def test_backend_port_refreshed_before_start(velocity):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    v.storage.update_server(v.backend["id"], {"port": 25577})
    proxy = v.storage.get_server(v.proxy["id"])
    v.service.refresh_addresses(proxy)
    assert v.service.get_config(v.storage.get_server(proxy["id"]))["backends"][0]["address"] == "127.0.0.1:25577"


def test_delete_backend_after_route_removed(velocity):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    v.config.update(backends=[], **{"try": []}, forcedHosts={})
    assert v.client.put(_url(v), json=v.config).status_code == 200
    assert v.client.delete(f"/api/servers/{v.backend['id']}").status_code == 200


def test_forwarding_never_applied_on_config_write(velocity):
    v = velocity
    before = (v.root / "server.properties").read_bytes()
    assert v.client.put(_url(v), json=v.config).status_code == 200
    assert (v.root / "server.properties").read_bytes() == before
    assert "secret:" not in (v.root / "config" / "paper-global.yml").read_text()


def test_forwarding_rejects_running_backend_and_unreferenced_backend(velocity, monkeypatch):
    v = velocity
    payload = {"serverIds": [v.backend["id"]]}
    assert v.client.post(_url(v, "forwarding/preview"), json=payload).status_code == 400
    assert v.client.put(_url(v), json=v.config).status_code == 200
    monkeypatch.setattr(v.service._registry(), "get_status",
                        lambda sid: {"status": "running" if sid == v.backend["id"] else "stopped"})
    assert v.client.post(_url(v, "forwarding/preview"), json=payload).status_code == 409


def test_malformed_yaml_rejected_without_leaking_parser_content(velocity):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    path = v.root / "config" / "paper-global.yml"
    path.write_text("proxies:\n  velocity: [bad: syntax\n  secret: never-leak-this\n")
    response = v.client.post(_url(v, "forwarding/preview"), json={"serverIds": [v.backend["id"]]})
    assert response.status_code == 400
    assert "never-leak-this" not in response.get_data(as_text=True)
    assert "online-mode=true" in (v.root / "server.properties").read_text()


def test_forwarding_rejects_config_directory_symlink(velocity, tmp_path):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    outside = tmp_path / "outside"
    (v.root / "config").rename(outside)
    (v.root / "config").symlink_to(outside, target_is_directory=True)
    response = v.client.post(_url(v, "forwarding/preview"), json={"serverIds": [v.backend["id"]]})
    assert response.status_code == 400
    assert "outside" in response.json["error"]
    assert "secret:" not in (outside / "paper-global.yml").read_text()


def test_atomic_file_group_rolls_back_on_replace_failure(velocity, monkeypatch):
    v = velocity
    first, second = v.root / "first.yml", v.root / "second.yml"
    first.write_text("first original")
    second.write_text("second original")
    replace = v.service.os.replace
    def fail_second(source, target):
        if target == second:
            raise OSError("replacement failed")
        return replace(source, target)
    monkeypatch.setattr(v.service.os, "replace", fail_second)
    with pytest.raises(OSError):
        v.service.write_files({first: "first changed", second: "second changed"})
    assert first.read_text() == "first original"
    assert second.read_text() == "second original"
    assert not list(v.root.glob("*.fabricator-write-*"))


def test_raw_proxy_config_update_upload_delete_are_blocked(velocity):
    v = velocity
    base = f"/api/servers/{v.proxy['id']}/files"
    response = v.client.put(base + "/content", json={"path": "velocity.toml", "content": "broken"})
    assert response.status_code == 403
    response = v.client.post(base + "/upload?filename=velocity.toml&overwrite=true",
                             data=b"broken", content_type="application/octet-stream")
    assert response.status_code == 403
    response = v.client.delete(base, json={"paths": ["velocity.toml"]})
    assert response.status_code == 200
    assert response.json["deleted"] == []
    assert response.json["errors"]
    assert v.service._config_path(v.proxy).exists()


def test_versions_are_velocity_project_versions_not_mc_versions(tmp_path, monkeypatch):
    cls = importlib.import_module("backend.server.installer.papermc").VelocityInstaller
    installer = cls(tmp_path / "proxy")
    urls = []
    def get_json(url):
        urls.append(url)
        return {"versions": {"3.4": ["3.4.0-SNAPSHOT"], "3.3": ["3.3.0"]}}
    monkeypatch.setattr(installer, "_get_json", get_json)
    versions = {v["version"]: v for v in installer.get_minecraft_versions()}
    assert versions["3.4.0-SNAPSHOT"]["stable"] is False
    assert versions["3.3.0"]["stable"] is True
    assert urls == ["https://fill.papermc.io/v3/projects/velocity"]


def test_velocity_refuses_download_without_hash(tmp_path, monkeypatch):
    cls = importlib.import_module("backend.server.installer.papermc").VelocityInstaller
    installer = cls(tmp_path / "proxy")
    monkeypatch.setattr(installer, "_select_build", lambda version: {
        "id": 1, "downloads": {"server:default": {"url": "https://example.test/velocity.jar"}},
    })
    result = installer.install("3.4.0-SNAPSHOT")
    assert not result.success
    assert "SHA-256" in result.message
    assert not (installer.install_path / "server.jar").exists()


def test_velocity_start_does_not_create_eula(tmp_path, monkeypatch):
    cls = importlib.import_module("backend.server.manager").ServerManager
    manager = cls(str(tmp_path / "proxy"), loader="velocity")
    monkeypatch.setattr(manager, "probe_java", lambda: {"available": True, "major_version": 21})
    monkeypatch.setattr(manager, "_spawn_process", lambda command: (True, "started"))
    assert manager.start(required_java_major=21)["status"] == "running"
    assert not (tmp_path / "proxy" / "eula.txt").exists()


def test_velocity_registry_selects_java21_and_launch_without_nogui(tmp_path, monkeypatch):
    registry = importlib.import_module("backend.server.registry")
    reg = registry.ServerProcessRegistry(tmp_path / "servers")
    find = Mock(return_value="/java21/bin/java")
    monkeypatch.setattr(registry.java_manager, "find_compatible_java", find)
    server = {"id": "srv_proxy", "loader": "velocity", "version": "3.4.0-SNAPSHOT"}
    assert reg._build_command(server) == ["/java21/bin/java", "-Xms4G", "-Xmx4G", "-jar", "server.jar"]
    find.assert_called_once_with(21, None)


def test_proxy_deletion_removes_secret_but_not_backend_authentication(velocity):
    v = velocity
    path = v.service._secret_path(v.proxy)
    assert path.exists()
    assert v.client.delete(f"/api/servers/{v.proxy['id']}").status_code == 200
    assert not path.exists()
    assert v.storage.get_server(v.backend["id"]) is not None


@pytest.mark.parametrize("method,suffix,payload", [
    ("get", "/players/state", None),
    ("get", "/players/online", None),
    ("post", "/players/ops", {"name": "Player"}),
    ("post", "/players/whitelist", {"name": "Player"}),
    ("post", "/players/bans", {"name": "Player"}),
    ("post", "/players/kick", {"name": "Player"}),
    ("post", "/world-import", None),
    ("post", "/upgrade", {"targetVersion": "3.5.0"}),
])
def test_game_only_api_operations_reject_proxy_targets(velocity, method, suffix, payload):
    v = velocity
    response = getattr(v.client, method)(f"/api/servers/{v.proxy['id']}{suffix}", json=payload)
    assert response.status_code == 400
    assert response.json["error"]
    assert not (v.service._root(v.proxy) / "server.properties").exists()


@pytest.mark.parametrize("path", [
    "/api/modrinth/modpack/example/install",
    "/api/modrinth/modpack/upload/example/install",
])
def test_modpack_api_rejects_proxy_targets(velocity, path):
    v = velocity
    response = v.client.post(path, json={"server_id": v.proxy["id"], "mc_version": "1.21.4", "force": True})
    assert response.status_code == 400
    assert "Velocity" in response.json["error"]


def test_velocity_plugin_install_ignores_proxy_version_as_minecraft_filter(velocity, monkeypatch):
    v = velocity
    routes = importlib.import_module("backend.modrinth.routes")
    resolve = Mock(return_value={"url": "https://example.invalid/plugin.jar", "hashes": {}})
    download = Mock(return_value=Path("plugin.jar"))
    monkeypatch.setattr(routes.modrinth_client, "get_project_download_url", resolve)
    monkeypatch.setattr(routes.modrinth_client, "download_mod", download)
    response = v.client.post("/api/modrinth/mod/plugin/install", json={
        "server_id": v.proxy["id"], "mc_version": "3.4.0-SNAPSHOT", "loader": "paper",
    })
    assert response.status_code == 200
    assert resolve.call_args.kwargs["mc_version"] is None
    assert resolve.call_args.kwargs["loaders"] == ["velocity"]
    assert download.call_args.args[1].name == "plugins"


def test_velocity_plugin_search_and_versions_omit_minecraft_filter(velocity, monkeypatch):
    v = velocity
    routes = importlib.import_module("backend.modrinth.routes")
    search, versions = Mock(return_value={"hits": []}), Mock(return_value=[])
    monkeypatch.setattr(routes.modrinth_client, "search", search)
    monkeypatch.setattr(routes.modrinth_client, "get_project_versions", versions)
    assert v.client.get("/api/modrinth/search?loader=velocity&mc_version=3.4.0-SNAPSHOT&project_type=plugin").status_code == 200
    assert search.call_args.kwargs["mc_version"] is None
    assert v.client.get("/api/modrinth/project/plugin/versions?loaders=velocity&game_versions=3.4.0-SNAPSHOT").status_code == 200
    assert versions.call_args.kwargs["game_versions"] is None


def test_velocity_resolution_allows_no_minecraft_version(velocity, monkeypatch):
    routes = importlib.import_module("backend.modrinth.routes")
    resolve = Mock(return_value={"version": {}, "download_url": "https://example.invalid/plugin.jar"})
    monkeypatch.setattr(routes.modrinth_client, "resolve_project_version", resolve)
    response = velocity.client.get("/api/modrinth/project/plugin/resolve-version?loader=velocity")
    assert response.status_code == 200
    assert resolve.call_args.kwargs["mc_version"] is None


def test_forwarding_rolls_back_files_if_record_persistence_fails(velocity, monkeypatch):
    v = velocity
    assert v.client.put(_url(v), json=v.config).status_code == 200
    payload = {"serverIds": [v.backend["id"]]}
    preview = v.client.post(_url(v, "forwarding/preview"), json=payload)
    payload.update(confirmOfflineMode=True, previewToken=preview.json["previewToken"])
    paths = [v.root / "server.properties", v.root / "spigot.yml",
             v.root / "config" / "paper-global.yml"]
    before = {path: path.read_bytes() for path in paths}
    def failed_save(updates):
        raise OSError("index write failed")
    monkeypatch.setattr(v.storage, "update_servers", failed_save)
    result = v.client.post(_url(v, "forwarding/apply"), json=payload)
    assert result.status_code == 500
    assert all(path.read_bytes() == before[path] for path in paths)
    assert v.storage.get_server(v.backend["id"]).get("forwardingProxyId") is None


def test_legacy_string_backend_ports_are_resolved_and_reserved(velocity):
    v = velocity
    v.storage.update_server(v.backend["id"], {"port": "25566"})
    assert v.client.put(_url(v), json=v.config).status_code == 200
    v.config["listen"]["port"] = 25566
    response = v.client.put(_url(v), json=v.config)
    assert response.status_code == 400
    assert "already in use" in response.json["error"]
