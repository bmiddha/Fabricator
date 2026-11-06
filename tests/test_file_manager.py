"""HTTP-surface tests for the file-manager write routes (issue #73).

Covers upload, delete and folder creation: containment, the filename rules that
keep mod jars intact, the size cap, overwrite handling, symlink refusal, and the
recursive-delete guard.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import quote

import pytest


def _seed_server(tmp_root: Path, server_id: str = "srv1") -> Path:
    """Write a stopped server record to servers.json and return its install path."""
    record = {
        "id": server_id,
        "name": "Test",
        "version": "1.21.1",
        "loader": "neoforge",
        "port": 25565,
        "installPath": server_id,   # relative → resolved under servers_root
        "status": "stopped",
    }
    index = tmp_root / "servers.json"
    existing = json.loads(index.read_text()) if index.exists() else []
    existing.append(record)
    index.write_text(json.dumps(existing), encoding="utf-8")

    install = tmp_root / "servers" / server_id
    install.mkdir(parents=True, exist_ok=True)
    return install


@pytest.fixture
def seeded(tmp_servers_root):
    install = _seed_server(tmp_servers_root)
    (install / "mods").mkdir()
    (install / "server.properties").write_text("motd=hi\n", encoding="utf-8")
    return install


def _upload(client, body: bytes, *, name: str, path: str = "", overwrite: bool = False):
    # quote() the way the real client does: an unencoded "+" in a query string
    # decodes to a space, which would quietly rename every Fabric jar.
    query = f"?path={quote(path)}&filename={quote(name)}"
    if overwrite:
        query += "&overwrite=true"
    return client.post(
        f"/api/servers/srv1/files/upload{query}",
        data=body,
        content_type="application/octet-stream",
    )


# ---------------------------------------------------------------------------
# Text editing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "original", "edited"),
    [
        (
            "mod.json5",
            "{\n  // Keep this comment\n  unquoted: 'old',\n}\n",
            "{\n  // Keep this comment\n  unquoted: 'caf\u00e9',\n"
            "  /* Keep block comments too. */\n  values: [0x10, +2, .5,],\n}\n",
        ),
        ("Mod.JSON5", "{enabled: true,}", "{enabled: false, /* keep me */}\n"),
        ("mod.json", '{ "enabled": true }\n', '{ "enabled": false, "values": [1, 2] }\n'),
    ],
)
def test_text_file_browse_search_open_and_save_preserves_content(
    client, seeded, name, original, edited,
):
    config = seeded / "config"
    config.mkdir()
    target = config / name
    target.write_text(original, encoding="utf-8")
    path = str(Path("config") / name)

    listing = client.get("/api/servers/srv1/files?path=config")
    assert listing.status_code == 200
    assert listing.get_json()["entries"][0]["relativePath"] == path
    search = client.get(f"/api/servers/srv1/files/search?q={name}")
    assert search.status_code == 200
    assert search.get_json()["results"][0]["relativePath"] == path

    endpoint = "/api/servers/srv1/files/content"
    opened = client.get(endpoint, query_string={"path": path})
    assert opened.status_code == 200
    assert opened.get_json() == {"path": path, "content": original}
    saved = client.put(endpoint, json={"path": path, "content": edited})
    assert saved.status_code == 200
    assert saved.get_json() == {"success": True}
    assert target.read_bytes() == edited.encode("utf-8")
    reopened = client.get(endpoint, query_string={"path": path})
    assert reopened.get_json()["content"] == edited


@pytest.mark.parametrize("extension", ["json", "json5"])
@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_text_editor_rejects_traversal_and_external_symlinks(
    client, seeded, extension, method,
):
    outside = seeded.parent / f"outside.{extension}"
    outside.write_text("keep me", encoding="utf-8")
    (seeded / f"link.{extension}").symlink_to(outside)
    endpoint = "/api/servers/srv1/files/content"
    for path in [f"../outside.{extension}", f"link.{extension}"]:
        response = (
            client.get(endpoint, query_string={"path": path})
            if method == "GET"
            else client.put(endpoint, json={"path": path, "content": "clobber"})
        )
        assert response.status_code == 400
    assert outside.read_text(encoding="utf-8") == "keep me"


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_json5_editing_requires_authentication(auth_client, seeded, method):
    target = seeded / "mod.json5"
    target.write_text("{enabled: true,}", encoding="utf-8")
    endpoint = "/api/servers/srv1/files/content"
    response = (
        auth_client.get(endpoint, query_string={"path": "mod.json5"})
        if method == "GET"
        else auth_client.put(endpoint, json={"path": "mod.json5", "content": "{}"})
    )
    assert response.status_code == 401
    assert target.read_text(encoding="utf-8") == "{enabled: true,}"


def test_json5_open_still_requires_utf8_text(client, seeded):
    (seeded / "mod.json5").write_bytes(b"\xff\x00")
    response = client.get("/api/servers/srv1/files/content?path=mod.json5")
    assert response.status_code == 400
    assert response.get_json()["error"] == "File is not UTF-8 text"


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def test_upload_writes_the_file_into_a_subdirectory(client, seeded):
    resp = _upload(client, b"jar-bytes", name="Create-1.21.1.jar", path="mods")
    assert resp.status_code == 201

    landed = seeded / "mods" / "Create-1.21.1.jar"
    assert landed.read_bytes() == b"jar-bytes"

    entry = resp.get_json()["entry"]
    assert entry["name"] == "Create-1.21.1.jar"
    assert entry["relativePath"] == str(Path("mods") / "Create-1.21.1.jar")
    assert entry["isDir"] is False
    assert entry["size"] == len(b"jar-bytes")


def test_upload_preserves_plus_in_fabric_jar_names(client, seeded):
    """secure_filename would turn this into fabric-api-0.102.01.21.jar."""
    name = "fabric-api-0.102.0+1.21.jar"
    assert _upload(client, b"x", name=name, path="mods").status_code == 201
    assert (seeded / "mods" / name).exists()


def test_upload_preserves_spaces_and_unicode(client, seeded):
    name = "Sodium Extra é.jar"
    assert _upload(client, b"x", name=name, path="mods").status_code == 201
    assert (seeded / "mods" / name).exists()


def test_upload_leaves_no_staging_file_behind(client, seeded):
    _upload(client, b"x", name="a.jar", path="mods")
    assert [p.name for p in (seeded / "mods").iterdir()] == ["a.jar"]


@pytest.mark.parametrize(
    "name",
    ["../escape.jar", "sub/nested.jar", "sub\\nested.jar", "..", ".", "evil.jar:ads"],
)
def test_upload_rejects_names_that_could_escape_the_directory(client, seeded, name):
    resp = _upload(client, b"x", name=name, path="mods")
    assert resp.status_code == 400
    assert not (seeded.parent / "escape.jar").exists()


def test_upload_rejects_a_traversing_directory(client, seeded):
    resp = _upload(client, b"x", name="a.jar", path="../..")
    assert resp.status_code == 400
    assert not (seeded.parent.parent / "a.jar").exists()


def test_upload_rejects_windows_device_names(client, seeded):
    assert _upload(client, b"x", name="CON.jar", path="mods").status_code == 400


def test_upload_rejects_a_trailing_dot(client, seeded):
    """Windows drops trailing dots, so the name on disk would not be the one
    that was validated."""
    assert _upload(client, b"x", name="mod.jar.", path="mods").status_code == 400


def test_upload_trims_surrounding_whitespace_from_the_name(client, seeded):
    assert _upload(client, b"x", name="  mod.jar  ", path="mods").status_code == 201
    assert (seeded / "mods" / "mod.jar").exists()


def test_upload_requires_a_filename(client, seeded):
    resp = client.post(
        "/api/servers/srv1/files/upload?path=mods",
        data=b"x",
        content_type="application/octet-stream",
    )
    assert resp.status_code == 400


def test_upload_accepts_the_filename_header(client, seeded):
    resp = client.post(
        "/api/servers/srv1/files/upload?path=mods",
        data=b"x",
        content_type="application/octet-stream",
        headers={"X-Filename": "header.jar"},
    )
    assert resp.status_code == 201
    assert (seeded / "mods" / "header.jar").exists()


def test_upload_rejects_an_empty_body(client, seeded):
    resp = _upload(client, b"", name="empty.jar", path="mods")
    assert resp.status_code == 400
    assert not (seeded / "mods" / "empty.jar").exists()


def test_upload_404s_for_a_missing_directory(client, seeded):
    assert _upload(client, b"x", name="a.jar", path="nope").status_code == 404


def test_upload_conflicts_when_the_file_exists(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"old")

    resp = _upload(client, b"new", name="a.jar", path="mods")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "file-exists"
    assert (seeded / "mods" / "a.jar").read_bytes() == b"old"


def test_upload_replaces_when_overwrite_is_set(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"old")

    resp = _upload(client, b"new", name="a.jar", path="mods", overwrite=True)
    assert resp.status_code == 201
    assert (seeded / "mods" / "a.jar").read_bytes() == b"new"


def test_upload_conflicts_with_an_existing_folder(client, seeded):
    resp = _upload(client, b"x", name="mods", path="")
    assert resp.status_code == 409


def test_upload_refuses_to_write_through_a_symlink(client, seeded):
    outside = seeded.parent / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    (seeded / "mods" / "link.jar").symlink_to(outside)

    resp = _upload(client, b"clobber", name="link.jar", path="mods", overwrite=True)
    assert resp.status_code in (400, 409)
    assert outside.read_text(encoding="utf-8") == "keep me"


def test_upload_over_the_cap_is_rejected_and_leaves_nothing(client, seeded, monkeypatch):
    monkeypatch.setenv("FABRICATOR_MAX_FILE_UPLOAD_BYTES", "16")

    resp = _upload(client, b"x" * 64, name="big.jar", path="mods")
    assert resp.status_code == 413
    assert list((seeded / "mods").iterdir()) == []


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def _delete(client, paths, *, recursive: bool = False):
    return client.delete(
        "/api/servers/srv1/files",
        json={"paths": paths, "recursive": recursive},
    )


def test_delete_removes_a_file(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"x")

    resp = _delete(client, ["mods/a.jar"])
    assert resp.status_code == 200
    assert resp.get_json() == {
        "success": True,
        "deleted": ["mods/a.jar"],
        "errors": [],
    }
    assert not (seeded / "mods" / "a.jar").exists()


def test_delete_removes_an_empty_folder_without_recursive(client, seeded):
    (seeded / "empty").mkdir()

    assert _delete(client, ["empty"]).get_json()["deleted"] == ["empty"]
    assert not (seeded / "empty").exists()


def test_delete_refuses_a_non_empty_folder_without_recursive(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"x")

    body = _delete(client, ["mods"]).get_json()
    assert body["success"] is False
    assert body["deleted"] == []
    assert body["errors"][0]["code"] == "not-empty"
    assert (seeded / "mods" / "a.jar").exists()


def test_delete_removes_a_non_empty_folder_with_recursive(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"x")

    assert _delete(client, ["mods"], recursive=True).get_json()["deleted"] == ["mods"]
    assert not (seeded / "mods").exists()


def test_delete_reports_per_entry_failures_without_sinking_the_batch(client, seeded):
    (seeded / "mods" / "a.jar").write_bytes(b"x")

    body = _delete(client, ["mods/a.jar", "mods/gone.jar"]).get_json()
    assert body["deleted"] == ["mods/a.jar"]
    assert body["errors"] == [{"path": "mods/gone.jar", "error": "Not found"}]
    assert body["success"] is False


@pytest.mark.parametrize("path", ["../servers.json", "../../etc/passwd", "/etc/passwd"])
def test_delete_rejects_paths_outside_the_install_dir(client, seeded, path):
    body = _delete(client, [path]).get_json()
    assert body["deleted"] == []
    assert body["errors"]
    assert (seeded.parent.parent / "servers.json").exists()


def test_delete_refuses_the_server_root(client, seeded):
    body = _delete(client, ["."], recursive=True).get_json()
    assert body["deleted"] == []
    assert body["errors"][0]["error"] == "Refusing to delete the server folder itself"
    assert seeded.exists()


def test_delete_refuses_a_dotdot_segment(client, seeded):
    """PurePath keeps ".." verbatim, so it is rejected rather than guessed at."""
    body = _delete(client, ["mods/.."], recursive=True).get_json()
    assert body["deleted"] == []
    assert body["errors"][0]["error"] == "Invalid path"
    assert seeded.exists()


def test_delete_removes_a_symlink_without_touching_its_target(client, seeded):
    """A world symlinked onto another disk must not be rmtree'd through."""
    real = seeded.parent / "real-world"
    real.mkdir()
    (real / "level.dat").write_bytes(b"\x00")
    (seeded / "world").symlink_to(real, target_is_directory=True)

    assert _delete(client, ["world"], recursive=True).get_json()["deleted"] == ["world"]
    assert not (seeded / "world").exists()
    assert (real / "level.dat").exists()


def test_delete_requires_a_non_empty_path_list(client, seeded):
    assert client.delete("/api/servers/srv1/files", json={"paths": []}).status_code == 400
    assert client.delete("/api/servers/srv1/files", json={}).status_code == 400


# ---------------------------------------------------------------------------
# Create folder
# ---------------------------------------------------------------------------


def test_create_folder(client, seeded):
    resp = client.post("/api/servers/srv1/files/folder", json={"path": "", "name": "config"})
    assert resp.status_code == 201
    assert resp.get_json()["entry"]["isDir"] is True
    assert (seeded / "config").is_dir()


def test_create_folder_nested(client, seeded):
    resp = client.post(
        "/api/servers/srv1/files/folder", json={"path": "mods", "name": "disabled"}
    )
    assert resp.status_code == 201
    assert (seeded / "mods" / "disabled").is_dir()


def test_create_folder_conflicts_when_it_exists(client, seeded):
    resp = client.post("/api/servers/srv1/files/folder", json={"path": "", "name": "mods"})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "file-exists"


@pytest.mark.parametrize("name", ["../escape", "a/b", "", "..", "NUL"])
def test_create_folder_rejects_unsafe_names(client, seeded, name):
    resp = client.post("/api/servers/srv1/files/folder", json={"path": "", "name": name})
    assert resp.status_code == 400
    assert not (seeded.parent / "escape").exists()


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
#
# The per-server lock is an RLock, so a same-thread acquire always succeeds by
# design (see backend/server/locks.py). Every probe below therefore runs on its
# own thread, which is also how contending requests actually arrive under
# Flask's threaded server.


def _lock_is_free(server_id: str) -> bool:
    """Whether the per-server lock can be taken from a *different* thread."""
    from backend.server.locks import try_acquire

    result = {}

    def probe():
        lock = try_acquire(server_id)
        result["free"] = lock is not None
        if lock is not None:
            lock.release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=5)
    return result["free"]


def _view_globals(app, endpoint: str) -> dict:
    """Return the module namespace the registered view actually resolves from.

    test_app_factory.py reloads ``backend.server.routes``, and ``core.app``
    keeps its pre-reload blueprint — so the app can be serving functions whose
    globals belong to a module object that is no longer the one
    ``import backend.server.routes`` hands back. Patching by module name would
    then silently miss. Unwrapping the registered view and using its own
    ``__globals__`` is correct whichever module object won.
    """
    view = app.view_functions[endpoint]
    while hasattr(view, "__wrapped__"):
        view = view.__wrapped__
    return view.__globals__


def test_upload_does_not_hold_the_server_lock_while_the_body_streams(client, seeded):
    """A slow upload must not 409 the Start it is being uploaded for.

    The lock is taken around the atomic commit only, so it stays free for the
    minutes a large jar spends on the wire. Probed by wrapping the streaming
    helper: the test client buffers ``data=`` before dispatch, so a probe fed
    through the request body would run outside the handler and prove nothing.
    """
    namespace = _view_globals(client.application, "server.upload_server_file")
    real_stream = namespace["stream_upload_to_temp"]
    observed = {}

    def probing_stream(stream, dest, *, max_bytes):
        observed["free_during_stream"] = _lock_is_free("srv1")
        return real_stream(stream, dest, max_bytes=max_bytes)

    namespace["stream_upload_to_temp"] = probing_stream
    try:
        resp = _upload(client, b"payload", name="slow.jar", path="mods")
    finally:
        namespace["stream_upload_to_temp"] = real_stream

    assert resp.status_code == 201
    assert observed["free_during_stream"] is True
    assert (seeded / "mods" / "slow.jar").read_bytes() == b"payload"
    # And it is handed back afterwards.
    assert _lock_is_free("srv1") is True


def test_upload_409s_when_another_operation_holds_the_lock(client, seeded):
    from backend.server.locks import try_acquire

    acquired = threading.Event()
    release = threading.Event()

    def holder():
        lock = try_acquire("srv1")
        if lock is None:
            return
        acquired.set()
        release.wait(timeout=5)
        lock.release()

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(timeout=5), "could not take the lock to set the test up"
        resp = _upload(client, b"x", name="blocked.jar", path="mods")
    finally:
        release.set()
        thread.join(timeout=5)

    assert resp.status_code == 409
    # Nothing committed, and no staging file orphaned in the destination.
    assert list((seeded / "mods").iterdir()) == []
