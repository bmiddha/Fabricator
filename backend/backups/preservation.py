"""Retain local backup evidence before replacing server directories."""
from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
import shutil
import stat
from typing import Iterable
import uuid

from backend.backups import destinations, storage

logger = logging.getLogger(__name__)


class SwapRollbackError(RuntimeError):
    """A failed rollback whose original and staged trees must not be deleted."""

    def __init__(self, holding: Path, staging: Path, error: BaseException):
        self.holding = holding
        self.staging = staging
        super().__init__(
            f"Directory replacement rollback failed: {error}. "
            f"Original files retained at {holding}; staged files retained at {staging}. "
            "Do not start the server until its installation has been recovered."
        )


def recover_snapshot_locations(server_id: str, install_path: Path, holding: Path) -> None:
    """Keep the index usable if original files could not be renamed back."""
    root = install_path.absolute()
    with storage._get_server_lock(server_id):
        index = storage._resolve_backups_file(server_id)
        index_missing = not index.exists()
        if index_missing:
            relative = _relative(index, root)
            saved_index = holding / relative if relative is not None else None
            if saved_index is None or not saved_index.is_file():
                raise FileNotFoundError("Backup index is unavailable after failed rollback")
            with saved_index.open(encoding="utf-8") as handle:
                document = json.load(handle)
        else:
            document = storage._read_file(server_id)
        changed = False
        for snapshot in document["snapshots"]:
            if destinations.is_remote(snapshot) or not snapshot.get("filePath"):
                continue
            relative = _relative(Path(snapshot["filePath"]), root)
            if relative is None or (root / relative).is_file():
                continue
            retained = holding / relative
            if retained.is_file():
                snapshot["filePath"] = str(retained)
                changed = True
        if changed or index_missing:
            storage._write_file(server_id, document)
            storage._invalidate_lookup_cache()
            logger.warning("Backup index retained recovery locations under %s", holding)


def _relative(path: Path, root: Path) -> Path | None:
    path = path.expanduser().absolute()
    try:
        return path.relative_to(root)
    except ValueError:
        try:
            return path.resolve().relative_to(root)
        except ValueError:
            return None


def _check_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Backup preservation refuses symlinked paths: {current}")


def _walk_backup_files(directory: Path) -> Iterable[Path]:
    for path in directory.iterdir():
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            yield from _walk_backup_files(path)
        elif stat.S_ISREG(mode):
            yield path
        else:
            raise ValueError(f"Backup preservation requires regular files: {path}")


def preserve_local_archives(
    install_path: Path,
    staging: Path,
    server_id: str,
    *,
    replaced_names: Iterable[str] | None = None,
) -> None:
    """Prepare retained copies without moving archives or rewriting their index.

    The default backup directory is backup-owned, including unindexed/partial
    evidence. Custom destinations contribute archive files, not unrelated game
    data. Recorded local files and the server's backup index are authoritative
    even after the owning configuration has been removed.
    """
    if install_path.is_symlink() or staging.is_symlink():
        raise ValueError("Backup preservation requires literal server and staging directories")
    root = install_path.resolve(strict=True)
    stage = staging.resolve(strict=True)
    scopes = set(replaced_names) if replaced_names is not None else None
    nested_replacement = root in stage.parents and (
        scopes is None or stage.relative_to(root).parts[0] in scopes
    )
    if root == stage or nested_replacement or stage in root.parents:
        raise ValueError("Backup preservation requires independent staging")
    files: dict[Path, bool] = {}
    visited_destinations: set[Path] = set()

    def add(path: Path, *, recorded: bool = False) -> None:
        relative = _relative(path, root)
        if relative is None:
            return
        if not relative.parts:
            raise ValueError("A backup file cannot be the server directory")
        if scopes is not None and relative.parts[0] not in scopes:
            return
        _check_components(root, relative)
        files[relative] = files.get(relative, False) or recorded

    def add_archive_siblings(destination: Path) -> None:
        relative = _relative(destination, root)
        if relative is None or (scopes is not None and relative.parts
                                and relative.parts[0] not in scopes):
            return
        _check_components(root, relative)
        directory = root / relative
        if directory in visited_destinations or not directory.exists():
            return
        visited_destinations.add(directory)
        for path in directory.iterdir():
            name = path.name.lower()
            if name.endswith((".tar", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip")) \
                    or ".tar." in name:
                add(path)
            elif name.startswith(".staging-"):
                if path.is_symlink():
                    raise ValueError(f"Backup preservation refuses symlinked paths: {path}")
                if path.is_dir():
                    for evidence in _walk_backup_files(path):
                        add(evidence)

    default = root / "backups"
    if scopes is None or "backups" in scopes:
        _check_components(root, Path("backups"))
        if default.exists():
            for path in _walk_backup_files(default):
                add(path)
    for cfg in storage.list_configs(server_id):
        if (cfg.get("destination") or {}).get("type") == "s3":
            continue
        add_archive_siblings(storage.resolve_config_storage_path(cfg))
    for snapshot in storage.list_snapshots(server_id):
        if not destinations.is_remote(snapshot) and snapshot.get("filePath"):
            path = Path(snapshot["filePath"])
            add(path, recorded=True)
            add_archive_siblings(path.parent)
    index = storage._resolve_backups_file(server_id)
    if index.exists():
        add(index)

    for relative, recorded in sorted(files.items()):
        source = root / relative
        try:
            before = source.stat()
        except FileNotFoundError:
            if not recorded:
                raise
            logger.warning("Recorded backup was already missing; retaining its index entry: %s", source)
            continue
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"Backup preservation requires a regular file: {source}")
        parent = stage
        for part in relative.parts[:-1]:
            parent = parent / part
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError(f"Restored content conflicts with retained backup directory: {parent}")
            parent.mkdir(exist_ok=True)
        target = stage / relative
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f"Restored content conflicts with retained backup file: {target}")
        temporary = target.with_name(f".retain-{uuid.uuid4().hex}")
        try:
            try:
                os.link(source, temporary, follow_symlinks=False)
            except OSError as exc:
                if exc.errno not in (errno.EXDEV, errno.EOPNOTSUPP, errno.ENOSYS, errno.EPERM):
                    raise
                shutil.copy2(source, temporary, follow_symlinks=False)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
            after = source.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise OSError(f"Backup changed during preservation: {source}")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
