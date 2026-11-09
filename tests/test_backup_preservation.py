"""Archive and index retention across isolated directory replacements."""
import errno
import hashlib
from pathlib import Path
import tarfile

import pytest

from tests.test_backups_restore import (
    restore_env,
    _fake_registry,
    _seed_server,
    _write_plain_tar,
)


@pytest.fixture
def backup_case(restore_env, monkeypatch):
    store, restore = restore_env["storage"], restore_env["restore"]

    def create(location="default", *, internal_index=False):
        server_id = "srv_retained"
        root = _seed_server(restore_env["tmp"], server_id)
        registry = _fake_registry(root)
        monkeypatch.setattr(restore, "get_server_process_registry", lambda: registry)
        monkeypatch.setattr(store, "get_server_process_registry", lambda: registry)
        if internal_index:
            monkeypatch.setenv("BACKUPS_DIR", str(root / "backup-index"))
        locations = {
            "default": root / "backups",
            "nested": root / "data" / "archives",
            "world": root / "world" / "archives",
            "external": restore_env["tmp"] / "external-archives",
        }
        directory = locations[location]
        directory.mkdir(parents=True)
        cfg = store.create_config(server_id, {
            "name": "Retained", "storagePath": "" if location == "default" else str(directory),
        })
        snapshots = []
        for name, world in (("prior.tar", "OLDER-WORLD"), ("chosen.tar", "RESTORED-WORLD")):
            path = directory / name
            _write_plain_tar(path, {"world/level.dat": world})
            snapshots.append(store.record_snapshot(server_id, {
                "configId": cfg["id"], "filePath": str(path), "fileName": path.name,
                "sizeBytes": path.stat().st_size,
            }))
        orphan = directory / "unindexed.tar"
        _write_plain_tar(orphan, {"evidence.txt": "UNINDEXED"})
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in directory.iterdir() if path.is_file()}
        return {
            "root": root, "directory": directory, "server_id": server_id,
            "snapshots": snapshots, "before": before, "registry": registry,
            "cfg": cfg, "store": store, "restore": restore,
        }

    return create


def assert_readable(case, *, world="LIVE-WORLD", relocated=False):
    from backend.backups import destinations
    root, store, server_id = case["root"], case["store"], case["server_id"]
    for snapshot in case["snapshots"]:
        current = store.get_snapshot(server_id, snapshot["id"])
        assert current is not None
        with destinations.materialise(current) as path:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == case["before"][Path(snapshot["filePath"])]
            with tarfile.open(path) as archive:
                assert archive.extractfile("world/level.dat").read() in (b"OLDER-WORLD", b"RESTORED-WORLD")
        if not relocated:
            assert current["filePath"] == snapshot["filePath"]
    safety = [s for s in store.list_snapshots(server_id) if s["type"] == "safety"]
    assert len(safety) == 1
    with destinations.materialise(safety[0]) as path, tarfile.open(path) as archive:
        assert archive.extractfile("world/level.dat").read().decode() == world
    return safety[0]


@pytest.mark.parametrize("location", ["default", "nested", "world", "external"])
def test_full_reset_retains_prior_unindexed_and_new_safety_archives(backup_case, location):
    case = backup_case(location)
    record = case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert (case["root"] / "world/level.dat").read_text() == "RESTORED-WORLD"
    safety = assert_readable(case)
    assert record["safetySnapshotId"] == safety["id"]
    orphan = case["directory"] / "unindexed.tar"
    assert hashlib.sha256(orphan.read_bytes()).hexdigest() == case["before"][orphan]
    assert len(case["store"].list_snapshots(case["server_id"])) == 4


def test_swap_itself_cannot_delete_internal_backup_directory(backup_case):
    case = backup_case()
    stage = case["root"].parent / "staged"
    (stage / "world").mkdir(parents=True)
    (stage / "world/level.dat").write_text("REPLACED")
    case["restore"]._atomic_swap(case["root"], stage, case["server_id"])
    for path, digest in case["before"].items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert (case["root"] / "world/level.dat").read_text() == "REPLACED"


@pytest.mark.parametrize("failure", ["no_space", "permission", "copy"])
def test_preservation_failure_keeps_live_world_and_all_indexed_archives(backup_case, monkeypatch, failure):
    from backend.backups import preservation
    case = backup_case()
    code = errno.EXDEV if failure == "copy" else (
        errno.ENOSPC if failure == "no_space" else errno.EACCES
    )
    monkeypatch.setattr(preservation.os, "link", lambda *a, **kw: (_ for _ in ()).throw(OSError(code, failure)))
    if failure == "copy":
        monkeypatch.setattr(preservation.shutil, "copy2",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError(errno.ENOSPC, "copy full")))
    with pytest.raises(OSError):
        case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert (case["root"] / "world/level.dat").read_text() == "LIVE-WORLD"
    assert_readable(case)
    assert not list(case["root"].parent.glob(".old-*"))


def test_cross_device_copy_fallback_preserves_complete_archives(backup_case, monkeypatch):
    from backend.backups import preservation
    case = backup_case()
    monkeypatch.setattr(preservation.os, "link",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError(errno.EXDEV, "different device")))
    case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert_readable(case)


@pytest.mark.parametrize("failed_rollback", [False, True])
@pytest.mark.parametrize("internal_index", [False, True])
def test_publication_and_rollback_failures_preserve_discoverable_archives(
    backup_case, monkeypatch, failed_rollback, internal_index,
):
    from backend.backups import preservation
    case = backup_case(internal_index=internal_index)
    replace = case["restore"].os.replace

    def fail_publication(source, destination):
        source, destination = Path(source), Path(destination)
        if destination == case["root"] and (
            source.name.startswith(".restore-")
            or (failed_rollback and source.name.startswith(".old-"))
        ):
            raise OSError(errno.EIO, "injected rename failure")
        return replace(source, destination)

    monkeypatch.setattr(case["restore"].os, "replace", fail_publication)
    case["registry"].get_status.return_value = {"status": "running"}
    error = preservation.SwapRollbackError if failed_rollback else OSError
    with pytest.raises(error) as caught:
        case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert_readable(case, relocated=failed_rollback)
    if failed_rollback:
        assert caught.value.holding.is_dir()
        assert caught.value.staging.is_dir()
        assert (caught.value.holding / "world/level.dat").read_text() == "LIVE-WORLD"
        case["registry"].start_server.assert_not_called()
    else:
        assert (case["root"] / "world/level.dat").read_text() == "LIVE-WORLD"


def test_restored_archive_collision_keeps_original_evidence(backup_case):
    case = backup_case()
    chosen = Path(case["snapshots"][-1]["filePath"])
    _write_plain_tar(chosen, {
        "world/level.dat": "RESTORED-WORLD", "backups/prior.tar": "NOT THE RETAINED ARCHIVE",
    })
    case["before"][chosen] = hashlib.sha256(chosen.read_bytes()).hexdigest()
    case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert_readable(case)


@pytest.mark.parametrize("collision", ["directory", "symlink"])
def test_unsafe_staged_collision_aborts_without_overwriting_backups(backup_case, collision):
    case = backup_case()
    chosen = Path(case["snapshots"][-1]["filePath"])
    if collision == "directory":
        _write_plain_tar(chosen, {"world/level.dat": "RESTORED-WORLD", "backups/prior.tar/child": "bad"})
    else:
        _write_plain_tar(chosen, {"world/level.dat": "RESTORED-WORLD"})
        with tarfile.open(chosen, "a") as archive:
            info = tarfile.TarInfo("backups")
            info.type, info.linkname = tarfile.SYMTYPE, "world"
            archive.addfile(info)
    case["before"][chosen] = hashlib.sha256(chosen.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert (case["root"] / "world/level.dat").read_text() == "LIVE-WORLD"
    assert_readable(case)


def test_symlinked_retained_file_is_rejected_before_swap(backup_case):
    case = backup_case()
    source = case["directory"] / "link.tar"
    source.symlink_to(case["directory"] / "prior.tar")
    with pytest.raises(ValueError, match="regular files"):
        case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert (case["root"] / "world/level.dat").read_text() == "LIVE-WORLD"
    assert_readable(case)


def test_preexisting_missing_record_is_not_pruned_or_claimed_recovered(backup_case, caplog):
    case = backup_case()
    missing = case["store"].record_snapshot(case["server_id"], {
        "filePath": str(case["directory"] / "missing.tar"), "fileName": "missing.tar",
    })
    case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert case["store"].get_snapshot(case["server_id"], missing["id"]) == missing
    assert not Path(missing["filePath"]).exists()
    assert "already missing" in caplog.text
    assert_readable(case)


def test_world_directory_swap_retains_nested_archives_without_old_world_data(backup_case):
    from backend.backups import world_import
    case = backup_case("world")
    stage = case["root"].parent / "normalized"
    (stage / "world").mkdir(parents=True)
    (stage / "world/level.dat").write_text("IMPORTED")
    (case["root"] / "world/stale.dat").write_text("OLD")
    world_import._swap_world_dirs(case["root"], stage, ["world"], case["server_id"])
    assert (case["root"] / "world/level.dat").read_text() == "IMPORTED"
    assert not (case["root"] / "world/stale.dat").exists()
    for path, digest in case["before"].items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_deleted_config_does_not_make_retained_archive_siblings_disposable(backup_case):
    case = backup_case("nested")
    case["store"].delete_config(case["server_id"], case["cfg"]["id"], keep_snapshots=True)
    case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert_readable(case)
    for path, digest in case["before"].items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_safety_index_failure_leaves_actual_safety_archive_and_original_records(backup_case, monkeypatch):
    case = backup_case()
    write = case["store"]._write_file

    def fail_safety_record(server_id, document):
        if any(snapshot["type"] == "safety" for snapshot in document["snapshots"]):
            raise OSError(errno.ENOSPC, "index publication full")
        return write(server_id, document)

    monkeypatch.setattr(case["store"], "_write_file", fail_safety_record)
    with pytest.raises(OSError, match="index publication full"):
        case["restore"].run_restore(case["snapshots"][-1]["id"], mode="reset")
    assert (case["root"] / "world/level.dat").read_text() == "LIVE-WORLD"
    for snapshot in case["snapshots"]:
        assert case["store"].get_snapshot(case["server_id"], snapshot["id"]) == snapshot
        path = Path(snapshot["filePath"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == case["before"][path]
    archives = list(case["directory"].glob("safety-*.tar"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        assert archive.extractfile("world/level.dat").read() == b"LIVE-WORLD"
