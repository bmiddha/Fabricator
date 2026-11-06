import copy
import hashlib
import importlib
import io
import json
import zipfile

import pytest

from backend.modrinth import client_pack as packs, mrpack
from backend.server import storage


class Catalog:
    def __init__(self):
        self.versions = {}
        self.projects = {}
        self.bytes = {}

    def add(self, project, version=None, environment="client_and_server", dependencies=None,
            filename=None, data=None):
        version = version or project + "V1"
        data = data or version.encode()
        filename = filename or f"{project}.jar"
        hashes = {algorithm: hashlib.new(algorithm, data).hexdigest()
                  for algorithm in ("sha1", "sha512")}
        self.projects[project] = {
            "id": project, "title": project, "project_type": "mod",
            "environment": [environment],
        }
        self.versions[version] = {
            "id": version, "project_id": project, "version_number": version,
            "game_versions": ["1.21.1"], "loaders": ["fabric"],
            "dependencies": dependencies or [],
            "files": [{
                "filename": filename, "hashes": hashes, "primary": True,
                "url": f"https://cdn.modrinth.com/data/{project}/{version}/{filename}",
                "size": len(data),
            }],
        }
        self.bytes[version] = data
        return self.versions[version]

    def get_version(self, version):
        return copy.deepcopy(self.versions[version])

    def get_project(self, project):
        return self.projects[project]

    def get_versions_by_hashes(self, hashes, algorithm="sha1"):
        return {
            file["hashes"][algorithm]: copy.deepcopy(version)
            for version in self.versions.values() for file in version["files"]
            if file["hashes"][algorithm] in hashes
        }

    def get_projects(self, ids):
        return [self.projects[project] for project in ids]

    def get_project_versions(self, project, **kwargs):
        return [copy.deepcopy(value) for value in self.versions.values()
                if value["project_id"] == project]

    def pick_best_version(self, versions):
        return versions[-1] if versions else None


@pytest.fixture(autouse=True)
def _fresh_modules():
    # App-factory tests deliberately reload backend modules.
    global packs, mrpack, storage
    packs = importlib.import_module("backend.modrinth.client_pack")
    mrpack = importlib.import_module("backend.modrinth.mrpack")
    storage = importlib.import_module("backend.server.storage")


@pytest.fixture
def pack_server(tmp_servers_root):
    root = tmp_servers_root / "servers" / "instance"
    (root / "mods").mkdir(parents=True)
    server = storage.create_server({
        "id": "srv_pack", "name": "Test server", "loader": "fabric",
        "version": "1.21.1", "loaderVersion": "0.16.9", "installPath": str(root),
    })
    return server, root, Catalog()


def install(root, catalog, version, name=None):
    file = version["files"][0]
    (root / "mods" / (name or file["filename"])).write_bytes(catalog.bytes[version["id"]])


def save(server, **updates):
    value = packs.configuration(server)
    value.update(updates)
    return packs.save_configuration(server, value)


def entry(version, client="required", server="unsupported"):
    file = version["files"][0]
    return {
        "path": f'mods/{file["filename"]}', "hashes": file["hashes"],
        "downloads": [file["url"]], "fileSize": file["size"],
        "env": {"client": client, "server": server},
    }


def archive(path, files=None, overrides=None):
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": "source",
        "name": "Original", "files": files or [],
        "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.16.9"},
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index))
        for name, value in (overrides or {}).items():
            zf.writestr(name, value)
    return path


def assert_spec(index):
    """Independent assertions against the official version-1 format."""
    assert index["formatVersion"] == 1
    assert index["game"] == "minecraft"
    assert isinstance(index["versionId"], str) and index["versionId"]
    assert isinstance(index["name"], str) and index["name"]
    assert set(index["dependencies"]) <= {"minecraft", "fabric-loader", "quilt-loader", "forge", "neoforge"}
    assert len({file["path"].casefold() for file in index["files"]}) == len(index["files"])
    for file in index["files"]:
        assert len(file["hashes"]["sha1"]) == 40
        assert len(file["hashes"]["sha512"]) == 128
        assert type(file["fileSize"]) is int
        assert set(file["env"].values()) <= {"required", "optional", "unsupported"}
        assert file["downloads"] and all(url.startswith("https://") for url in file["downloads"])
        assert not file["path"].startswith("/") and ".." not in file["path"].split("/")


def test_combined_pack_exact_hash_pin_optional_and_reopen(pack_server):
    server, root, catalog = pack_server
    old = catalog.add("shared", version="sharedOld")
    install(root, catalog, old, "renamed.jar")
    catalog.add("shared", version="sharedLatest")
    catalog.add("servermod", environment="server_only")
    install(root, catalog, catalog.versions["servermodV1"])
    catalog.add("disabled")
    install(root, catalog, catalog.versions["disabledV1"], "disabled.jar.disabled")
    catalog.add("sodium", environment="client_only")
    save(server, selections=[{
        "key": "client:sodium", "version_id": "sodiumV1", "requirement": "optional",
    }])
    before = {path.name: path.read_bytes() for path in (root / "mods").iterdir()}
    plan = packs.plan(server, root, catalog)
    assert not plan["errors"]
    assert plan["file_count"] == 2
    assert next(file for file in plan["files"] if file["path"] == "mods/renamed.jar")["hashes"] == old["files"][0]["hashes"]
    artifact = packs.build(server, root, catalog, plan["fingerprint"])
    path, _ = packs.artifact_path(server, artifact["id"])
    index = mrpack.read_index(path)
    assert_spec(index)
    assert index["dependencies"] == {"minecraft": "1.21.1", "fabric-loader": "0.16.9"}
    sodium = next(file for file in index["files"] if file["path"] == "mods/sodium.jar")
    assert sodium["env"] == {"client": "optional", "server": "unsupported"}
    assert {path.name: path.read_bytes() for path in (root / "mods").iterdir()} == before


def test_unknown_jar_requires_explicit_exclusion(pack_server):
    server, root, catalog = pack_server
    (root / "mods" / "unknown.jar").write_bytes(b"not registered")
    plan = packs.plan(server, root, catalog)
    assert any("Unknown/non-Modrinth" in error for error in plan["errors"])
    with pytest.raises(packs.PackError):
        packs.build(server, root, catalog, plan["fingerprint"])
    save(server, selections=[{"key": "server:mods/unknown.jar", "requirement": "excluded"}])
    assert not packs.plan(server, root, catalog)["errors"]


@pytest.mark.parametrize("destination", ["config/client.jar", "mods/nested/client.jar", "mods/client.zip"])
def test_imported_mod_requires_loadable_top_level_jar_destination(pack_server, tmp_path, destination):
    server, root, catalog = pack_server
    version = catalog.add("client", environment="client_only")
    file = entry(version)
    file["path"] = destination
    packs.capture_source(archive(tmp_path / "source.mrpack", files=[file]), root)
    plan = packs.plan(server, root, catalog)
    assert any(destination in error for error in plan["errors"])
    assert plan["file_count"] == 0
    with pytest.raises(packs.PackError):
        packs.build(server, root, catalog, plan["fingerprint"])


def test_required_transitive_dependencies_and_optional_upgrade(pack_server):
    server, root, catalog = pack_server
    catalog.add("leaf", environment="client_only")
    catalog.add("middle", environment="client_only", dependencies=[{
        "project_id": "leaf", "dependency_type": "required",
    }])
    catalog.add("parent", environment="client_only", dependencies=[{
        "project_id": "middle", "dependency_type": "required",
    }])
    save(server, selections=[
        {"key": "client:leaf", "version_id": "leafV1", "requirement": "optional"},
        {"key": "client:parent", "version_id": "parentV1", "requirement": "required"},
    ])
    plan = packs.plan(server, root, catalog)
    assert not plan["errors"]
    assert len(plan["files"]) == 3
    assert all(file["env"]["client"] == "required" for file in plan["files"])


def test_pinned_dependency_conflict_never_upgrades_shared(pack_server):
    server, root, catalog = pack_server
    old = catalog.add("shared", version="old")
    install(root, catalog, old)
    catalog.add("shared", version="new")
    catalog.add("client", environment="client_only", dependencies=[{
        "project_id": "shared", "version_id": "new", "dependency_type": "required",
    }])
    save(server, selections=[{"key": "client:client", "version_id": "clientV1", "requirement": "required"}])
    plan = packs.plan(server, root, catalog)
    assert any("conflicting with pinned old" in error for error in plan["errors"])
    assert (root / "mods" / "shared.jar").read_bytes() == catalog.bytes["old"]


def test_imported_client_intent_and_safe_override_layers(pack_server, tmp_path):
    server, root, catalog = pack_server
    shared = catalog.add("shared")
    client = catalog.add("client", environment="client_only")
    install(root, catalog, shared)
    path = archive(tmp_path / "source.mrpack", [
        entry(shared, client="optional", server="required"), entry(client),
    ], {
        "overrides/config/sodium-options.json": '{"quality":1}',
        "client-overrides/config/sodium-options.json": '{"quality":2}',
        "server-overrides/config/sodium-options.json": '{"secret":"never"}',
        "overrides/server.properties": "rcon.password=secret",
        "client-overrides/config/unknown.json": '{"token":"never"}',
        "client-overrides/mods/unlicensed.jar": "never redistribute",
    })
    packs.capture_source(path, root, server_files=["mods/shared.jar"])
    save(server, overrides=["imported:config/sodium-options.json"], selections=[{
        "key": "imported-override:client-overrides/mods/unlicensed.jar", "requirement": "excluded",
    }])
    plan = packs.plan(server, root, catalog)
    assert not plan["errors"]
    assert len(plan["excluded_imported_overrides"]) == 3
    assert next(file for file in plan["files"] if file["path"] == "mods/shared.jar")["env"]["client"] == "optional"
    assert plan["_overrides"]["config/sodium-options.json"] == b'{"quality":2}'
    artifact = packs.build(server, root, catalog, plan["fingerprint"])
    target, _ = packs.artifact_path(server, artifact["id"])
    with zipfile.ZipFile(target) as zf:
        assert set(zf.namelist()) == {"modrinth.index.json", "client-overrides/config/sodium-options.json"}
        assert b"never" not in zf.read("client-overrides/config/sodium-options.json")
    (root / "mods" / "shared.jar").rename(root / "mods" / "shared.jar.disabled")
    assert packs.plan(server, root, catalog)["file_count"] == 1


@pytest.mark.parametrize("environment,legacy,supported", [
    ("client_only", None, True), ("client_or_server", None, True),
    ("server_only_client_optional", None, True), ("singleplayer_only", None, False),
    ("dedicated_server_only", None, False), ("server_only", None, False),
    ("unknown", "optional", True), ("unknown", None, False),
])
def test_client_environment_not_server_verdict(pack_server, environment, legacy, supported):
    server, root, catalog = pack_server
    version = catalog.add("example", environment=environment)
    catalog.projects["example"]["client_side"] = legacy
    save(server, selections=[{"key": "client:example", "version_id": version["id"], "requirement": "required"}])
    assert (not packs.plan(server, root, catalog)["errors"]) == supported


@pytest.mark.parametrize("loader,key", [("fabric", "fabric-loader"), ("quilt", "quilt-loader"),
                                       ("forge", "forge"), ("neoforge", "neoforge")])
def test_loader_keys(pack_server, loader, key):
    server, root, catalog = pack_server
    server["loader"] = loader
    plan = packs.plan(server, root, catalog)
    assert not plan["errors"]
    assert set(plan["dependencies"]) == {"minecraft", key}


@pytest.mark.parametrize("loader", ["vanilla", "paper", "velocity"])
def test_unsupported_loaders(pack_server, loader):
    server, root, catalog = pack_server
    server["loader"] = loader
    assert packs.plan(server, root, catalog)["errors"]


def test_settings_paths_symlinks_and_revision(pack_server, tmp_path):
    server, root, catalog = pack_server
    for path in ["server:server.properties", "server:config/../../auth.json",
                 "server:config/auth.json", "server:world/level.dat"]:
        with pytest.raises(packs.PackError):
            save(server, overrides=[path])
    (root / "config").mkdir()
    secret = tmp_path / "secret.json"
    secret.write_text('{"password":"secret"}')
    (root / "config" / "sodium-options.json").symlink_to(secret)
    save(server, overrides=["server:config/sodium-options.json"])
    assert packs.plan(server, root, catalog)["errors"]
    stale = packs.configuration(server)
    save(server, name="New name")
    with pytest.raises(packs.PackError, match="edited elsewhere"):
        packs.save_configuration(server, stale)
    (root / "mods" / "evil.jar").symlink_to(secret)
    with pytest.raises(OSError):
        packs.plan(server, root, catalog)


def test_changed_sources_expiry_and_immutable_archives(pack_server, monkeypatch):
    server, root, catalog = pack_server
    install(root, catalog, catalog.add("shared"))
    plan = packs.plan(server, root, catalog)
    one = packs.build(server, root, catalog, plan["fingerprint"])
    one_path, _ = packs.artifact_path(server, one["id"])
    before = one_path.read_bytes()
    save(server, name="Changed")
    with pytest.raises(packs.PackError, match="changed since review"):
        packs.build(server, root, catalog, plan["fingerprint"])
    updated = packs.plan(server, root, catalog)
    two = packs.build(server, root, catalog, updated["fingerprint"])
    assert two["id"] != one["id"]
    assert one_path.read_bytes() == before
    import os
    os.utime(one_path, (0, 0))
    with pytest.raises(FileNotFoundError):
        packs.artifact_path(server, one["id"])


def test_routes_import_no_install_and_download_scoping(client, pack_server, monkeypatch, tmp_path):
    server, root, catalog = pack_server
    import backend.modrinth.client_pack_routes as routes
    monkeypatch.setattr(routes, "modrinth_client", catalog)
    version = catalog.add("client", environment="client_only")
    path = archive(tmp_path / "reimport.mrpack", [entry(version)])
    base = f'/api/servers/{server["id"]}/client-pack'
    assert client.post(base + "/import", data=path.read_bytes()).status_code == 200
    assert list((root / "mods").iterdir()) == []
    preview = client.get(base).get_json()
    assert not preview["errors"]
    response = client.post(base + "/exports", json={"fingerprint": preview["fingerprint"]})
    assert response.status_code == 201
    artifact = response.get_json()
    download = client.get(base + f'/exports/{artifact["id"]}/download')
    assert download.status_code == 200
    assert download.mimetype == "application/x-modrinth-modpack+zip"
    assert "private" in download.headers["Cache-Control"]
    assert_spec(json.loads(zipfile.ZipFile(io.BytesIO(download.data)).read("modrinth.index.json")))
    other = storage.create_server({"id": "other", "installPath": str(root.parent / "other")})
    assert client.get(f'/api/servers/{other["id"]}/client-pack/exports/{artifact["id"]}/download').status_code == 404
    assert client.get(base + "/exports/../../servers.json/download").status_code == 404


def test_download_requires_login(auth_client, pack_server):
    server, _, _ = pack_server
    assert auth_client.get(f'/api/servers/{server["id"]}/client-pack/exports').status_code == 401


def test_bulk_hash_payloads_avoid_per_jar_version_requests(pack_server, monkeypatch):
    server, root, catalog = pack_server
    for index in range(150):
        install(root, catalog, catalog.add(f"mod{index}"))
    monkeypatch.setattr(catalog, "get_version", lambda *args: pytest.fail("No per-jar version GET"))
    value = packs.plan(server, root, catalog)
    assert not value["errors"]
    assert value["file_count"] == 150
    assert packs.build(server, root, catalog, value["fingerprint"])["file_count"] == 150


@pytest.mark.parametrize("text", [
    '{"token":"secret"}', 'password=secret', 'lastServer:192.0.2.1',
    "-----BEGIN PRIVATE KEY-----", '{"username":"player"}',
])
def test_sensitive_fields_are_not_exported(pack_server, text):
    server, root, catalog = pack_server
    (root / "options.txt").write_text(text)
    save(server, overrides=["server:options.txt"])
    assert any("sensitive" in error for error in packs.plan(server, root, catalog)["errors"])


def test_portable_file_identity_guard(pack_server, monkeypatch, tmp_path):
    _, root, _ = pack_server
    (root / "options.txt").write_bytes(b"fov:90")
    monkeypatch.setattr(packs.os, "supports_dir_fd", set())
    assert packs._safe_read(root, "options.txt") == b"fov:90"
    (tmp_path / "secret").write_bytes(b"do not export")
    (root / "options.txt").unlink()
    (root / "options.txt").symlink_to(tmp_path / "secret")
    with pytest.raises(packs.PackError, match="Symlinks"):
        packs._safe_read(root, "options.txt")


def test_sources_changed_during_generation_leave_no_partial_artifact(pack_server, monkeypatch):
    server, root, catalog = pack_server
    version = catalog.add("shared")
    install(root, catalog, version)
    original_plan = packs.plan
    reviewed = original_plan(server, root, catalog)
    calls = 0

    def changing_plan(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "mods" / "shared.jar").rename(root / "mods" / "shared.jar.disabled")
        return original_plan(*args, **kwargs)

    monkeypatch.setattr(packs, "plan", changing_plan)
    with pytest.raises(packs.PackError, match="changed during generation"):
        packs.build(server, root, catalog, reviewed["fingerprint"])
    assert not list((packs._directory(server) / "exports").iterdir())


def test_path_collision_duplicate_project_and_incompatible_dependency(pack_server):
    server, root, catalog = pack_server
    a = catalog.add("alpha", filename="same.jar", dependencies=[{
        "project_id": "beta", "dependency_type": "incompatible",
    }])
    b = catalog.add("beta", filename="SAME.jar")
    save(server, selections=[
        {"key": "client:alpha", "version_id": a["id"], "requirement": "required"},
        {"key": "client:beta", "version_id": b["id"], "requirement": "required"},
    ])
    assert any("Colliding pack path" in error for error in packs.plan(server, root, catalog)["errors"])
    b["files"][0]["filename"] = "beta.jar"
    from backend.modrinth import installed
    installed.clear_caches()
    assert any("Incompatible projects" in error for error in packs.plan(server, root, catalog)["errors"])
    install(root, catalog, a)
    assert any("duplicates an installed server project" in error for error in packs.plan(server, root, catalog)["errors"])


def test_metadata_reimport_absent_shared_requires_explicit_review(pack_server, tmp_path):
    server, root, catalog = pack_server
    version = catalog.add("shared")
    path = archive(tmp_path / "original.mrpack", [entry(version, server="required")])
    packs.capture_source(path, root)
    value = packs.plan(server, root, catalog)
    assert any("absent/disabled" in error for error in value["errors"])
    save(server, selections=[{"key": "imported:mods/shared.jar", "requirement": "excluded"}])
    assert not packs.plan(server, root, catalog)["errors"]
    assert list((root / "mods").iterdir()) == []


def test_reopened_export_installs_shared_but_not_client_entries(pack_server, monkeypatch, tmp_path):
    server, root, catalog = pack_server
    shared = catalog.add("shared")
    install(root, catalog, shared)
    catalog.add("client", environment="client_only")
    save(server, selections=[{"key": "client:client", "version_id": "clientV1", "requirement": "optional"}])
    reviewed = packs.plan(server, root, catalog)
    artifact = packs.build(server, root, catalog, reviewed["fingerprint"])
    path, _ = packs.artifact_path(server, artifact["id"])
    from backend.modrinth.client import ModrinthClient
    importer = ModrinthClient()
    monkeypatch.setattr(importer, "get_versions_by_hashes", catalog.get_versions_by_hashes)
    monkeypatch.setattr(importer, "get_projects", catalog.get_projects)
    monkeypatch.setattr(importer, "_probe_download_url", lambda url: "")
    def download(url, target, hashes, error_context):
        value = next(data for version, data in catalog.bytes.items() if f"/{version}/" in url)
        assert hashlib.sha1(value).hexdigest() == hashes["sha1"]
        target.write_bytes(value)
    monkeypatch.setattr(importer, "_download_and_verify", download)
    destination = tmp_path / "reopened"
    result = importer.install_mrpack_archive(path, destination, loader="fabric")
    assert "mods/shared.jar" in result["files_installed"]
    assert "mods/client.jar" in result["files_skipped"]
    assert (destination / "mods" / "shared.jar").read_bytes() == catalog.bytes["sharedV1"]
    assert not (destination / "mods" / "client.jar").exists()
    retained = packs.source(destination)
    assert len(retained["index"]["files"]) == 2


@pytest.mark.parametrize("url", ["http://cdn.modrinth.com/test.jar", "https://evil.example/mod.jar",
                               "https://cdn.modrinth.com/bad file.jar"])
def test_disallowed_download_hosts_and_urls_fail(pack_server, url):
    server, root, catalog = pack_server
    version = catalog.add("client", environment="client_only")
    version["files"][0]["url"] = url
    save(server, selections=[{"key": "client:client", "version_id": version["id"], "requirement": "required"}])
    assert packs.plan(server, root, catalog)["errors"]


def test_retention_is_bounded_and_configuration_survives_reload(pack_server):
    server, root, catalog = pack_server
    install(root, catalog, catalog.add("shared"))
    saved = save(server, name="Durable pack")
    assert packs.configuration(server) == saved
    value = packs.plan(server, root, catalog)
    ids = [packs.build(server, root, catalog, value["fingerprint"])["id"] for _ in range(7)]
    exports = packs.artifacts(server)
    assert len(exports) == packs.MAX_ARTIFACTS
    assert ids[0] not in {artifact["id"] for artifact in exports}


def test_excluded_shared_dependency_is_not_silently_readded(pack_server):
    server, root, catalog = pack_server
    install(root, catalog, catalog.add("library"))
    catalog.add("client", environment="client_only", dependencies=[{
        "project_id": "library", "dependency_type": "required",
    }])
    save(server, selections=[
        {"key": "server:mods/library.jar", "requirement": "excluded"},
        {"key": "client:client", "version_id": "clientV1", "requirement": "required"},
    ])
    assert any("contradictory" in error for error in packs.plan(server, root, catalog)["errors"])


def test_server_deletion_cleans_client_state_without_touching_external_folder(client, pack_server):
    server, root, catalog = pack_server
    install(root, catalog, catalog.add("shared"))
    storage.update_server(server["id"], {"external": True})
    save(server, name="Cleanup test")
    reviewed = packs.plan(server, root, catalog)
    packs.build(server, root, catalog, reviewed["fingerprint"])
    response = client.delete(f'/api/servers/{server["id"]}')
    assert response.status_code == 200
    assert root.exists()
    assert not packs._directory(server).exists()


def test_invalid_client_metadata_is_rejected_before_clean_install_mutates_server(pack_server, tmp_path):
    _, root, catalog = pack_server
    version = catalog.add("shared")
    install(root, catalog, version)
    invalid = entry(version)
    invalid["env"] = {"client": ["malformed"]}
    path = archive(tmp_path / "malformed.mrpack", [invalid])
    from backend.modrinth.client import ModrinthClient
    with pytest.raises(packs.PackError, match="malformed"):
        ModrinthClient().install_mrpack_archive(path, root, loader="fabric", clean_install=True)
    assert (root / "mods" / "shared.jar").read_bytes() == catalog.bytes["sharedV1"]


def test_deleted_client_version_remains_editable_with_blocking_error(pack_server, monkeypatch):
    server, root, catalog = pack_server
    save(server, selections=[{"key": "client:deleted", "version_id": "gone", "requirement": "required"}])
    from backend.modrinth.client import ModrinthApiError
    def missing(_):
        raise ModrinthApiError("Version removed", status_code=404)
    monkeypatch.setattr(catalog, "get_version", missing)
    value = packs.plan(server, root, catalog)
    assert any("no longer available" in message for message in value["errors"])
    assert value["config"]["selections"][0]["key"] == "client:deleted"


def test_required_dependency_cycle_terminates_and_promotes_requirements(pack_server):
    server, root, catalog = pack_server
    catalog.add("one", environment="client_only", dependencies=[{
        "project_id": "two", "dependency_type": "required",
    }])
    catalog.add("two", environment="client_only", dependencies=[{
        "project_id": "one", "dependency_type": "required",
    }])
    save(server, selections=[
        {"key": "client:one", "version_id": "oneV1", "requirement": "optional"},
        {"key": "client:two", "version_id": "twoV1", "requirement": "required"},
    ])
    value = packs.plan(server, root, catalog)
    assert not value["errors"]
    assert len(value["files"]) == 2
    assert all(file["env"]["client"] == "required" for file in value["files"])


@pytest.mark.parametrize("field,value", [
    ("game_versions", ["1.20.1"]), ("loaders", ["neoforge"]),
])
def test_incompatible_client_versions_block_export(pack_server, field, value):
    server, root, catalog = pack_server
    version = catalog.add("client", environment="client_only")
    version[field] = value
    save(server, selections=[{"key": "client:client", "version_id": version["id"], "requirement": "required"}])
    assert any("not declared compatible" in error for error in packs.plan(server, root, catalog)["errors"])


def test_installed_sha512_or_size_mismatch_is_not_exported(pack_server):
    server, root, catalog = pack_server
    version = catalog.add("shared")
    install(root, catalog, version)
    version["files"][0]["hashes"]["sha512"] = "0" * 128
    assert any("hashes/size" in error for error in packs.plan(server, root, catalog)["errors"])
