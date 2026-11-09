"""Synthetic archives exercising extraction through final side reconciliation."""
from __future__ import annotations

import hashlib
import importlib
import io
import json
from types import SimpleNamespace
import zipfile

import pytest


def universal_metadata(mod_id, depends=()):
    return {
        "id": mod_id,
        "version": "1.0",
        "environment": "*",
        "entrypoints": {"main": ["example.Common"], "client": ["example.Client"]},
        "depends": {dependency: "*" for dependency in depends},
    }


@pytest.fixture
def pipeline(tmp_path, monkeypatch, request):
    module = importlib.import_module("backend.modrinth.client")
    client = module.ModrinthClient()
    counter = 0

    def install(files, *, catalog=None, server_env=None, side_overrides=None,
                loader="fabric", extra_metadata=None):
        nonlocal counter
        counter += 1
        directory = tmp_path / str(counter)
        directory.mkdir()
        destination = directory / "server"
        destination.mkdir()
        payloads, entries, embedded, versions, projects = {}, [], {}, {}, []
        for path, metadata in files.items():
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as jar:
                jar.writestr("fabric.mod.json", json.dumps(metadata))
                jar.writestr("example/Common.class", b"\xca\xfe\xba\xbejava/lang/Object")
                jar.writestr("example/Client.class", b"\xca\xfe\xba\xbeorg/lwjgl/glfw/GLFW")
                jar.writestr("example/ClientMixin.class", b"\xca\xfe\xba\xbenet/minecraft/client/gui/Gui")
                jar.writestr("example.mixins.json", json.dumps({"client": ["ClientMixin"]}))
                jar.writestr("fixture.txt", f"{request.node.nodeid}:{counter}:{path}")
                for name, value in (extra_metadata or {}).get(path, {}).items():
                    jar.writestr(name, value)
            body = buffer.getvalue()
            hashes = {algorithm: hashlib.new(algorithm, body).hexdigest()
                      for algorithm in ("sha1", "sha512")}
            if path.startswith(("overrides/", "server-overrides/")):
                embedded[path] = body
            else:
                url = f"https://example.invalid/{path}"
                payloads[url] = body
                entries.append({
                    "path": path, "downloads": [url], "hashes": hashes,
                    "fileSize": len(body),
                    "env": {"client": "required", "server": (server_env or {}).get(path, "required")},
                })
            tag = (catalog or {}).get(path)
            if tag is not None:
                project_id = f"fixture:{counter}:{path}"
                versions[hashes["sha1"]] = {"id": project_id, "project_id": project_id}
                fields = tag if isinstance(tag, dict) else {"environment": [tag]}
                projects.append({"id": project_id, **fields})

        archive = directory / "source.mrpack"
        with zipfile.ZipFile(archive, "w") as pack:
            pack.writestr("modrinth.index.json", json.dumps({
                "formatVersion": 1, "game": "minecraft", "name": "Generic pack",
                "versionId": "1.0", "files": entries,
                "dependencies": {"minecraft": "1.21.1", "fabric-loader": "0.18.4"},
            }))
            for path, body in embedded.items():
                pack.writestr(path, body)

        downloaded = []

        def download(url, target, hashes, error_context):
            body = payloads[url]
            assert all(hashlib.new(algorithm, body).hexdigest() == expected
                       for algorithm, expected in hashes.items())
            target.write_bytes(body)
            downloaded.append(url)

        monkeypatch.setattr(client, "_download_and_verify", download)
        monkeypatch.setattr(client, "_probe_download_url", lambda url: "")
        monkeypatch.setattr(client, "get_versions_by_hashes", lambda hashes, algorithm="sha1": {
            digest: versions[digest] for digest in hashes if digest in versions
        })
        monkeypatch.setattr(client, "get_projects", lambda ids: [
            project for project in projects if project["id"] in ids
        ])
        before_sweep = {}

        def progress_callback(stage, **kwargs):
            if stage == "verifying_mod_sides":
                before_sweep.update({
                    path: (destination / client._strip_override_prefix(path)).is_file()
                    for path in files
                })

        result = client.install_mrpack_archive(
            archive, destination, loader=loader, mod_side_overrides=side_overrides,
            progress_callback=progress_callback,
        )
        return SimpleNamespace(root=destination, result=result, before_sweep=before_sweep,
                               downloaded=downloaded)

    return install


@pytest.mark.parametrize("prefix", ["overrides/", "server-overrides/"])
@pytest.mark.parametrize("explicit_environment", [True, False])
def test_universal_embedded_mod_survives_both_install_phases(pipeline, prefix, explicit_environment):
    path = prefix + "mods/universal.jar"
    metadata = universal_metadata("universal")
    if not explicit_environment:
        del metadata["environment"]
    outcome = pipeline({path: metadata})
    assert outcome.before_sweep[path]
    assert (outcome.root / "mods/universal.jar").is_file()
    assert outcome.result["files_installed"] == [path]
    assert outcome.result["files_skipped"] == []
    assert outcome.result["uncertain_mod_files"] == []


@pytest.mark.parametrize("library_path,library_env", [
    ("mods/library.jar", "required"),
    ("mods/library.jar", "optional"),
    ("overrides/mods/library.jar", "required"),
])
def test_required_universal_dependency_survives_catalog_conflict(pipeline, caplog,
                                                               library_path, library_env):
    application = "mods/application.jar"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library_path: universal_metadata("library"),
    }, catalog={application: "client_and_server", library_path: "client_only"},
        server_env={library_path: library_env})
    assert all(outcome.before_sweep.values())
    assert (outcome.root / application).is_file()
    assert (outcome.root / "mods/library.jar").is_file()
    assert set(outcome.result["files_installed"]) == {application, library_path}
    assert outcome.result["files_skipped"] == []
    assert outcome.result["uncertain_mod_files"] == []
    assert "conflicting" in caplog.text


@pytest.mark.parametrize("server_env,kept", [("required", True), ("optional", False)])
def test_universal_catalog_conflict_respects_index_requirement(pipeline, server_env, kept):
    path = "mods/library.jar"
    outcome = pipeline({path: universal_metadata("library")},
                       catalog={path: "client_only"}, server_env={path: server_env})
    assert outcome.before_sweep[path]
    assert (outcome.root / path).is_file() == kept
    assert (path in outcome.result["files_installed"]) == kept
    assert (path in outcome.result["files_skipped"]) != kept


def test_optional_dependency_does_not_rescue_optional_catalog_candidate(pipeline):
    application, library = "mods/application.jar", "mods/library.jar"
    metadata = universal_metadata("application")
    metadata["recommends"] = {"library": "*"}
    outcome = pipeline({
        application: metadata, library: universal_metadata("library"),
    }, catalog={application: "client_and_server", library: "client_only"},
        server_env={library: "optional"})
    assert (outcome.root / application).is_file()
    assert not (outcome.root / library).exists()
    assert outcome.result["files_skipped"] == [library]


@pytest.mark.parametrize("environment,common", [("client", True), ("*", False)])
def test_genuine_client_dependency_still_removes_dependents(pipeline, caplog, environment, common):
    caplog.set_level("INFO", logger="backend.modrinth.client")
    application, library = "mods/application.jar", "mods/library.jar"
    metadata = universal_metadata("library")
    metadata["environment"] = environment
    if not common:
        del metadata["entrypoints"]["main"]
    outcome = pipeline({
        application: universal_metadata("application", ["library"]), library: metadata,
    }, catalog={application: "client_and_server", library: "client_only"})
    assert all(outcome.before_sweep.values())
    assert not (outcome.root / application).exists()
    assert not (outcome.root / library).exists()
    assert set(outcome.result["files_skipped"]) == {application, library}
    assert "Required dependencies removed: library" in caplog.text


@pytest.mark.parametrize("loader", ["forge", "neoforge", "quilt"])
def test_native_loader_metadata_is_not_overruled_by_fabric_metadata(pipeline, loader):
    application, library = "mods/application.jar", "mods/library.jar"
    extra = {}
    if loader == "quilt":
        extra = {library: {"quilt.mod.json": json.dumps({
            "quilt_loader": {"id": "library", "minecraft": {"environment": "client"}}
        })}}
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library"),
    }, catalog={application: "client_and_server", library: "client_only"},
        loader=loader, extra_metadata=extra)
    assert not (outcome.root / application).exists()
    assert not (outcome.root / library).exists()


@pytest.mark.parametrize("tag", ["client_only", "client_only_server_optional"])
def test_forced_server_dependent_still_protects_compatible_dependencies(pipeline, tag):
    application, library = "mods/application.jar", "mods/library.jar"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library"),
    }, catalog={library: tag}, server_env={library: "optional"},
        side_overrides={application: "server"})
    assert (outcome.root / application).is_file()
    assert (outcome.root / library).is_file()
    assert outcome.result["files_skipped"] == []


def test_forced_server_dependent_reports_incompatible_dependency(pipeline):
    application, library = "mods/application.jar", "mods/library.jar"
    metadata = universal_metadata("library")
    metadata["environment"] = "client"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]), library: metadata,
    }, catalog={library: "client_only"}, side_overrides={application: "server"})
    assert (outcome.root / application).is_file()
    assert not (outcome.root / library).exists()
    assert outcome.result["uncertain_mod_files"] == [{
        "path": application, "reason": "Required dependencies removed: library",
    }]


def test_multiplayer_incompatible_catalog_tag_remains_hard(pipeline):
    path = "mods/library.jar"
    outcome = pipeline({path: universal_metadata("library")}, catalog={path: "singleplayer_only"})
    assert not (outcome.root / path).exists()
    assert outcome.result["files_skipped"] == [path]


def test_explicit_client_override_still_wins_over_universal_metadata(pipeline):
    path = "overrides/mods/universal.jar"
    outcome = pipeline({path: universal_metadata("universal")}, side_overrides={path: "client"})
    assert not outcome.before_sweep[path]
    assert not (outcome.root / "mods/universal.jar").exists()
    assert outcome.result["files_skipped"] == [path]


def test_unsupported_index_environment_still_wins_over_server_override(pipeline):
    path = "mods/disabled.jar"
    outcome = pipeline({path: universal_metadata("disabled")},
                       server_env={path: "unsupported"}, side_overrides={path: "server"})
    assert not outcome.downloaded
    assert not (outcome.root / path).exists()
    assert outcome.result["files_skipped"] == [path]


def test_disabled_override_file_is_not_enabled_by_classification(pipeline):
    path = "overrides/mods/disabled.jar.disabled"
    outcome = pipeline({path: universal_metadata("disabled")})
    assert (outcome.root / "mods/disabled.jar.disabled").is_file()
    assert not (outcome.root / "mods/disabled.jar").exists()


def test_required_dependency_rescue_is_transitive(pipeline):
    application, library, support = (
        "mods/application.jar", "mods/library.jar", "overrides/mods/support.jar",
    )
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library", ["support"]),
        support: universal_metadata("support"),
    }, catalog={application: "client_and_server", library: "client_only", support: "client_only"},
        server_env={library: "optional"})
    assert (outcome.root / application).is_file()
    assert (outcome.root / library).is_file()
    assert (outcome.root / "mods/support.jar").is_file()
    assert outcome.result["files_skipped"] == []


def test_required_universal_file_cannot_shield_incompatible_hard_dependency(pipeline, caplog):
    caplog.set_level("INFO", logger="backend.modrinth.client")
    application, library, visual = (
        "mods/application.jar", "mods/library.jar", "mods/visual.jar",
    )
    client_metadata = universal_metadata("visual")
    client_metadata["environment"] = "client"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library", ["visual"]),
        visual: client_metadata,
    }, catalog={application: "client_and_server", library: "client_only", visual: "client_only"})
    assert all(outcome.before_sweep.values())
    assert outcome.result["files_installed"] == []
    assert set(outcome.result["files_skipped"]) == {application, library, visual}
    assert not any((outcome.root / path).exists() for path in (application, library, visual))
    assert "Required dependencies removed: visual" in caplog.text


def test_quilt_without_native_manifest_uses_fabric_common_entrypoint(pipeline):
    application, library = "mods/application.jar", "overrides/mods/library.jar"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library"),
    }, catalog={library: "client_only"}, loader="quilt")
    assert (outcome.root / application).is_file()
    assert (outcome.root / "mods/library.jar").is_file()


def test_legacy_catalog_conflict_preserves_required_universal_dependency(pipeline):
    application, library = "mods/application.jar", "overrides/mods/library.jar"
    outcome = pipeline({
        application: universal_metadata("application", ["library"]),
        library: universal_metadata("library"),
    }, catalog={library: {"server_side": "unsupported"}})
    assert (outcome.root / application).is_file()
    assert (outcome.root / "mods/library.jar").is_file()
