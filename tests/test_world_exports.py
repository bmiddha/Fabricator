"""Fixture-only exports: real archive bytes, exclusion boundaries and grants."""
import hashlib
import io
import json
import os
import tarfile
import time

import pytest

from tests.test_transfers import fixture_servers
from tests.test_oidc import oidc_app, sign_in, ISSUER


def endpoint(server="source"):
    return f"/api/servers/{server}/world-exports"


def export(client):
    response = client.post(endpoint(), json={"world_id": "island"})
    assert response.status_code == 201, response.json
    return response.json


def share(client, artifact):
    response = client.post(endpoint() + f'/{artifact["id"]}/shares', json={
        "expiry_seconds": 3600, "acknowledge_privacy": True,
    })
    assert response.status_code == 201, response.json
    return response.json


def redeem(client, link):
    return client.post("/api/world-shares/download", json={"token": link["url"].split("#")[1]})


@pytest.mark.parametrize("loader", ["vanilla", "fabric", "quilt", "forge", "neoforge",
                                   "paper", "purpur", "pufferfish", "folia"])
def test_layout_exact_bytes_boundaries_and_immutability(fixture_servers, client, loader):
    _, storage, _, _, root, _, _, _ = fixture_servers
    from backend.server import world_exports as exports, worlds
    storage.update_server("source", {"loader": loader})
    if loader in worlds.PAPER:
        dimension = root / "island_nether" / "DIM-1"
    else:
        dimension = root / "island" / "dimensions" / "custom" / "moon"
    dimension.mkdir(parents=True)
    (dimension / "region.mca").write_bytes(b"dimension-fixture")
    (root / "island" / "data").mkdir()
    (root / "island" / "data" / "mod-state.dat").write_bytes(b"integral-mod-state")
    (root / "island" / "datapacks").mkdir()
    (root / "island" / "datapacks" / "pack.zip").write_bytes(b"required-world-datapack")
    (root / "island" / "session.lock").write_bytes(b"excluded-lock")
    for folder in ("config", "logs", "mods", "plugins"):
        (root / folder).mkdir()
        (root / folder / "private.txt").write_bytes(b"excluded-server-content")
        (root / "island" / folder).mkdir()
        (root / "island" / folder / "private.txt").write_bytes(b"excluded-nested-server-content")
    (root / "island" / "rogue.jar").write_bytes(b"excluded-binary")
    (root / ".env").write_bytes(b"excluded-secret")
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    metadata = export(client)
    response = client.get(endpoint() + f'/{metadata["id"]}/download')
    blob = response.data
    response.close()
    assert hashlib.sha256(blob).hexdigest() == metadata["sha256"]
    assert len(blob) == metadata["size"]
    assert response.headers["X-Archive-SHA256"] == metadata["sha256"]
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = archive.getmembers()
        assert all(m.isdir() or m.isfile() for m in members)
        manifest = json.load(archive.extractfile("fabricator-world.json"))
        assert manifest["world"]["layout"] == ("bukkit-split" if loader in worlds.PAPER else "java-nested")
        for member in members:
            if member.isfile() and member.name != "fabricator-world.json":
                data = archive.extractfile(member).read()
                assert data == before[member.name]
                assert hashlib.sha256(data).hexdigest() == manifest["files"][member.name]
        names = {m.name for m in members}
        assert "island/playerdata/player.dat" in names
        assert "island/data/mod-state.dat" in names
        assert "island/datapacks/pack.zip" in names
        assert (dimension / "region.mca").relative_to(root).as_posix() in names
        assert not any("private.txt" in name or name.endswith((".jar", "session.lock")) for name in names)
        assert "server.properties" not in names and "server.jar" not in names
    assert before == {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / "island" / "region" / "r.0.0.mca").write_bytes(b"changed-live-world")
    link = share(client, metadata)
    response = redeem(client.application.test_client(use_cookies=False), link)
    assert response.data == blob
    response.close()
    assert str(exports._directory({"id": "source"})).startswith(str(storage._resolve_servers_file().parent))


def test_share_expiry_revocation_deletion_and_private_auth(fixture_servers, client, monkeypatch):
    from backend.server import world_exports as exports
    from backend.modrinth import client_pack_shares as shares
    metadata = export(client)
    assert client.post(endpoint() + f'/{metadata["id"]}/shares', json={"expiry_seconds": 3600}).status_code == 400
    link = share(client, metadata)
    token = link["url"].split("#")[1]
    records = exports._read(exports._directory({"id": "source"}) / "shares.json", [])
    assert token not in json.dumps(records) and records[0]["token_hash"] == exports._key(token)
    client.application.config.update(FABRICATOR_AUTH_ENABLED=True, FABRICATOR_NEEDS_SETUP=False)
    anonymous = client.application.test_client(use_cookies=False)
    assert anonymous.get(endpoint()).status_code == 401
    assert anonymous.get(endpoint() + f'/{metadata["id"]}/download').status_code == 401
    response = redeem(anonymous, link)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "Set-Cookie" not in response.headers
    response.close()
    assert anonymous.post(endpoint(), json={"world_id": "island"}).status_code == 401
    assert anonymous.post("/api/servers/source/start", json={"token": token}).status_code == 401
    with monkeypatch.context() as context:
        context.setattr(shares.time, "time", lambda: records[0]["expires_at"] + 1)
        assert redeem(anonymous, link).status_code == 404
    client.application.config["FABRICATOR_AUTH_ENABLED"] = False
    link = share(client, metadata)
    assert client.delete(f'/api/servers/source/world-export-shares/{link["id"]}').status_code == 200
    assert redeem(anonymous, link).status_code == 404
    link = share(client, metadata)
    assert client.delete(endpoint() + "/" + metadata["id"]).status_code == 409
    assert client.delete(endpoint() + "/" + metadata["id"], json={"confirm_shared": True}).status_code == 200
    assert redeem(anonymous, link).status_code == 404
    directory = exports._directory({"id": "source"})
    assert not (directory / f'{metadata["id"]}.json').exists()
    assert not (directory / f'{metadata["id"]}.tar.gz').exists()
    metadata = export(client)
    link = share(client, metadata)
    from backend.server import storage
    storage.delete_server("source")
    assert redeem(anonymous, link).status_code == 404


@pytest.mark.parametrize("case", ["running", "symlink", "hardlink", "unsupported", "dimension", "traversal", "space", "quota", "limit"])
def test_fail_closed_before_publication(fixture_servers, client, monkeypatch, case):
    _, storage, _, _, root, _, _, _ = fixture_servers
    from backend.server import world_exports as exports
    if case == "running":
        storage.update_server("source", {"status": "running"})
    elif case == "symlink":
        (root / "island" / "steal").symlink_to(root / "server.properties")
    elif case == "hardlink":
        os.link(root / "server.properties", root / "island" / "steal")
    elif case == "unsupported":
        (root / "plugin-world").mkdir()
        (root / "plugin-world" / "level.dat").write_bytes(b"extra-world")
    elif case == "dimension":
        (root / "island" / "dimensions" / "config" / "moon").mkdir(parents=True)
    elif case == "traversal":
        (root / "island" / "bad\\path").write_bytes(b"unsafe-path")
    elif case == "space":
        monkeypatch.setattr(exports.transfers.shutil, "disk_usage", lambda _: type("Space", (), {"free": 1})())
    elif case == "quota":
        monkeypatch.setenv("FABRICATOR_WORLD_EXPORT_STORAGE_BYTES", "1")
    else:
        monkeypatch.setenv("FABRICATOR_MAX_WORLD_EXPORT_BYTES", "1")
    response = client.post(endpoint(), json={"world_id": "island"})
    assert response.status_code == 409, response.json
    assert not list(exports._root().glob("*/*.tar.gz"))
    assert not list(exports._root().glob("*/*.tmp"))


def test_changed_source_and_directory_swap_abort(fixture_servers, client, monkeypatch):
    from backend.server import world_exports as exports
    _, _, _, _, root, _, _, _ = fixture_servers
    original_hash = exports._hash
    calls = 0
    def mutate(handle):
        nonlocal calls
        calls += 1
        result = original_hash(handle)
        if calls == 4:
            (root / "island" / "region" / "r.0.0.mca").write_bytes(b"external-writer")
        return result
    monkeypatch.setattr(exports, "_hash", mutate)
    response = client.post(endpoint(), json={"world_id": "island"})
    assert response.status_code == 409
    assert not list(exports._root().glob("*/*.tar.gz"))
    assert not list(exports._root().glob("*/*.tmp"))


def test_large_streaming_retention_pins_and_recovery(fixture_servers, client, monkeypatch):
    from backend.server import world_exports as exports
    _, _, _, _, root, _, _, _ = fixture_servers
    # Incompressible fixture is larger than the client-pack archive cap.
    with (root / "island" / "region" / "large.mca").open("wb") as handle:
        for _ in range(18):
            handle.write(os.urandom(1024 * 1024))
    metadata = export(client)
    assert metadata["size"] > 16 * 1024 * 1024
    path, _ = exports.artifact_path({"id": "source"}, metadata["id"])
    response = client.get(endpoint() + f'/{metadata["id"]}/download', buffered=False)
    digest = hashlib.sha256()
    count = 0
    for chunk in response.response:
        assert len(chunk) <= 1024 * 1024
        digest.update(chunk)
        count += len(chunk)
    response.close()
    assert count == metadata["size"] and digest.hexdigest() == metadata["sha256"]
    link = share(client, metadata)
    os.utime(path, (time.time() - 90000,) * 2)
    assert client.get(endpoint()).json["exports"][0]["active_shares"] == 1
    directory = exports._directory({"id": "source"})
    partial = directory / ("a" * 32 + ".tmp")
    orphan = directory / ("b" * 32 + ".json")
    json_partial = directory / (".shares.json-" + "c" * 32 + ".tmp")
    partial.write_bytes(b"partial")
    orphan.write_bytes(b"orphan")
    json_partial.write_bytes(b"partial-metadata")
    exports.recover()
    assert not partial.exists() and not orphan.exists() and not json_partial.exists() and path.exists()
    client.delete(f'/api/servers/source/world-export-shares/{link["id"]}')
    assert client.get(endpoint()).json["exports"] == []


def test_download_concurrency_and_rate_limit_release(fixture_servers, client):
    from backend.server import world_exports as exports
    metadata = export(client)
    link = share(client, metadata)
    app = client.application
    held = [client.get(endpoint() + f'/{metadata["id"]}/download', buffered=False) for _ in range(4)]
    assert all(response.status_code == 200 for response in held)
    assert redeem(client, link).status_code == 503
    held[0].close()
    response = redeem(client, link)
    assert response.status_code == 200
    response.close()
    for response in held[1:]:
        response.close()
    for _ in range(30):
        app.extensions["world_share_limiter"].reset()
        assert client.post("/api/world-shares/download", data=b"x" * 513).status_code == 404
    app.extensions["world_share_limiter"].reset()
    for _ in range(30):
        assert client.post("/api/world-shares/download", json={"token": "invalid"}).status_code == 404
    assert client.post("/api/world-shares/download", json={"token": "invalid"}).status_code == 429


def test_build_concurrency_is_bounded(fixture_servers, client):
    from backend.server import world_exports as exports
    assert exports._BUILD.acquire(blocking=False)
    try:
        assert client.post(endpoint(), json={"world_id": "island"}).status_code == 409
    finally:
        exports._BUILD.release()


def test_in_flight_bytes_survive_revoke_and_other_artifacts_stay_scoped(fixture_servers, client):
    first = export(client)
    second = export(client)
    link = share(client, first)
    token = link["url"].split("#")[1]
    held = redeem(client, link)
    assert client.delete(f'/api/servers/source/world-export-shares/{link["id"]}').status_code == 200
    assert hashlib.sha256(held.data).hexdigest() == first["sha256"]
    held.close()
    assert redeem(client, link).status_code == 404
    assert client.get(endpoint("destination") + f'/{second["id"]}/download').status_code == 404
    assert client.post("/api/client-pack-shares/download", json={"token": token}).status_code == 404
    link = share(client, first)
    response = client.post("/api/world-shares/download", json={
        "token": link["url"].split("#")[1], "artifact_id": second["id"], "server_id": "destination",
    })
    assert hashlib.sha256(response.data).hexdigest() == first["sha256"]
    response.close()


def test_server_route_deletion_cleans_exports_and_invalidates_grants(fixture_servers, client):
    from backend.server import world_exports as exports
    metadata = export(client)
    link = share(client, metadata)
    response = client.delete("/api/servers/source")
    assert response.status_code == 200, response.json
    assert not exports._directory({"id": "source"}).exists()
    assert redeem(client, link).status_code == 404


def test_publication_io_failure_leaves_no_archive_and_releases_capacity(fixture_servers, client, monkeypatch):
    from backend.server import world_exports as exports
    with monkeypatch.context() as context:
        def fail(_):
            raise OSError("fixture-fsync-failure")
        context.setattr(exports.transfers, "_sync_dir", fail)
        assert client.post(endpoint(), json={"world_id": "island"}).status_code == 500
    assert not list(exports._root().glob("*/*.tar.gz"))
    assert not list(exports._root().glob("*/*.tmp"))
    assert export(client)["id"]


def test_metadata_corruption_and_pinned_capacity_fail_explicitly(fixture_servers, client):
    from backend.server import world_exports as exports
    pinned = []
    for _ in range(5):
        metadata = export(client)
        pinned.append(share(client, metadata))
    assert client.post(endpoint(), json={"world_id": "island"}).status_code == 409
    assert len(client.get(endpoint()).json["exports"]) == 5
    path = exports._directory({"id": "source"}) / f'{metadata["id"]}.json'
    path.write_text("[]")
    assert client.get(endpoint()).status_code == 409
    assert redeem(client, pinned[-1]).status_code == 503


@pytest.mark.parametrize("method,suffix,permission", [
    ("post", "", "worlds.export"),
    ("get", "", "worlds.download"),
    ("get", "/fake/download", "worlds.download"),
    ("post", "/fake/shares", "worlds.share"),
    ("delete", "/fake", "worlds.share"),
])
def test_oidc_world_access_scoped_before_storage(oidc_app, monkeypatch, method, suffix, permission):
    from backend.auth import permissions
    from backend.server import world_exports as exports
    from unittest.mock import MagicMock
    spy = MagicMock(side_effect=AssertionError("World bytes must not be accessed"))
    monkeypatch.setattr(exports, "artifact_path", spy)
    monkeypatch.setattr(exports, "build", spy)
    monkeypatch.setattr(exports, "artifacts", spy)
    user = permissions.Principal("world-reader", ISSUER, (), frozenset(),
                                 {"a": frozenset({"server.view", "worlds.read"})}, {"a": ()})
    oidc_app.extensions["oidc"].users[user.subject] = user
    client = oidc_app.test_client()
    sign_in(client, user.subject)
    assert getattr(client, method)(endpoint("a") + suffix, json={}).status_code == 403
    assert getattr(client, method)(endpoint("b") + suffix, json={}).status_code == 404
    spy.assert_not_called()
    assert permission in permissions.CATALOG
