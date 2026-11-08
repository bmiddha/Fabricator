"""Versioned, credential-free instance metadata and strict recovery archives."""
from __future__ import annotations

import os
from contextlib import contextmanager, nullcontext
import gzip
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile

from backend.server import worlds
from backend.utils.zip import safe_extract_tar


MAX_ENTRIES = 200_000
MAX_METADATA_BYTES = 256 * 1024 * 1024


class ArchiveError(ValueError):
    """Archive validation errors that contain no source values."""


class _Reader:
    def __init__(self, source, budget):
        self.source = source
        self.budget = budget
        self.headers_only = True

    def read(self, size=-1):
        if size < 0 or size > 1024 * 1024:
            raise ArchiveError("Recovery archive metadata exceeds the 1 MiB record limit")
        data = self.source.read(size)
        if self.headers_only:
            self.budget.metadata_bytes += len(data)
            if self.budget.metadata_bytes > MAX_METADATA_BYTES:
                raise ArchiveError("Recovery archive headers/metadata exceed the 256 MiB limit")
        return data

    def __getattr__(self, name):
        return getattr(self.source, name)


@contextmanager
def _tar(source, budget, *, compressed=False):
    with (gzip.GzipFile(fileobj=source) if compressed else nullcontext(source)) as decoded:
        with tarfile.open(fileobj=_Reader(decoded, budget), mode="r:") as tf:
            yield tf


def byte_limit():
    value = int(os.environ.get("FABRICATOR_MAX_S3_IMPORT_BYTES", 100 * 1024**3))
    if value <= 0:
        raise ValueError("FABRICATOR_MAX_S3_IMPORT_BYTES must be positive")
    return value


def relative(value):
    if (not isinstance(value, str) or not value or len(value) > 1024
            or "\\" in value or ":" in value or value.startswith("/")
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>"|?*' for c in value)
            or any(p in ("", ".", "..") for p in value.split("/"))):
        raise ValueError("Recovery archive/launch paths must be safe relative paths")
    parts = value.split("/")
    if (len(parts) > 32 or any(p.endswith((".", " ")) for p in parts)
            or any(re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p, re.I) for p in parts)):
        raise ValueError("Recovery paths must be bounded and portable across operating systems")
    return PurePosixPath(value)


def _flags(value):
    tokens = shlex.split(value) if isinstance(value, str) else value or []
    if not isinstance(tokens, list) or len(tokens) > 64:
        raise ValueError("Portable JVM flags must be a bounded array")
    for token in tokens:
        if not isinstance(token, str) or not re.fullmatch(
            r"(?:-XX:[+-][A-Za-z][A-Za-z0-9]*|-XX:[A-Za-z][A-Za-z0-9]*=\d+[kKmMgG]?"
            r"|-X(?:ss|mn)\d+[kKmMgG]?|-Dfile.encoding=[A-Za-z0-9_-]+)", token
        ):
            raise ValueError("Custom JVM flags require manual review on the target installation")
    return tokens


def server_metadata(value):
    if not isinstance(value, dict):
        raise ValueError("Recovery requires explicit loader, game version and launch metadata")
    loader = value.get("loader")
    version = value.get("version")
    if loader not in worlds.LOADERS:
        raise ValueError("Recovery supports Java Minecraft instances, not proxies or unknown loaders")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version):
        raise ValueError("Recovery requires a released Minecraft game version")
    build = value.get("loaderVersion") or ""
    if not isinstance(build, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]{0,100}", build):
        raise ValueError("Loader build metadata is invalid")
    if loader in {"fabric", "quilt", "forge", "neoforge"} and not build:
        raise ValueError("Recovery requires the original loader build")
    launch = value.get("launch")
    if not isinstance(launch, dict) or launch.get("type") not in {"jar", "args_file"}:
        raise ValueError("Recovery requires a jar or argument-file launch specification")
    field = "jar" if launch["type"] == "jar" else "args_file"
    target = str(relative(launch.get(field)))
    if not target.endswith(".jar" if field == "jar" else ".txt"):
        raise ValueError("Recovery launch target has the wrong file extension")
    if value.get("command"):
        raise ValueError("Custom launch commands require manual migration")
    program = launch.get("program_args")
    if program not in (None, [], ["nogui"]):
        raise ValueError("Custom program arguments require manual review on the target installation")
    memory = value.get("memory", 4)
    minimum = value.get("memoryMin", memory)
    unit = value.get("memoryUnit", "GB")
    if (type(memory) not in (int, float) or type(minimum) not in (int, float)
            or not 0 < minimum <= memory <= 1024 or unit not in {"GB", "MB"}):
        raise ValueError("Recovery memory settings are invalid")
    return {
        "loader": loader, "version": version, "loaderVersion": build,
        "launch": {"type": launch["type"], field: target,
                   "jvm_args": _flags(launch.get("jvm_args")), "program_args": program},
        "jvmArgs": shlex.join(_flags(value.get("jvmArgs"))),
        "memory": memory, "memoryMin": minimum, "memoryUnit": unit,
    }


def manifest(server):
    """Do not make an otherwise useful backup fail for a nonportable launch."""
    result = {"format": "fabricator-instance", "version": 1,
              "platform": "windows" if os.name == "nt" else "posix"}
    try:
        result["server"] = server_metadata(server)
    except ValueError as exc:
        result.update(requiresOperatorMetadata=True, reason=str(exc))
    return result


class Archive:
    """Preflight every member before extracting, including both hybrid layers."""

    def __init__(self):
        self.seen = {}
        self.parents = set()
        self.paths = {}
        self.headers = 0
        self.metadata_bytes = 0
        self.entries = 0
        self.bytes = 0

    def members(self, tf):
        for member in tf:
            self.headers += 1
            if self.headers > MAX_ENTRIES:
                raise ArchiveError("Recovery archive exceeds the configured header/entry limit")
            if member.name in ("", ".") and member.isdir():
                continue
            try:
                name = str(relative(member.name.rstrip("/") if member.isdir() else member.name))
            except ValueError as exc:
                raise ArchiveError(str(exc)) from None
            if not (member.isfile() or member.isdir()) or member.issparse():
                raise ArchiveError("Recovery rejects links, sparse files and special archive members")
            folded = name.casefold()
            if folded in self.seen:
                raise ArchiveError("Recovery archive contains duplicate or case-colliding paths")
            for parent in PurePosixPath(folded).parents:
                if self.seen.get(str(parent)) == "file":
                    raise ArchiveError("Recovery archive has conflicting file and directory paths")
                self.parents.add(str(parent))
            if member.isfile() and folded in self.parents:
                raise ArchiveError("Recovery archive has conflicting file and directory paths")
            self.seen[folded] = "dir" if member.isdir() else "file"
            for path in (PurePosixPath(name), *PurePosixPath(name).parents):
                spelling = str(path)
                if spelling == ".":
                    continue
                key = spelling.casefold()
                if key in self.paths and self.paths[key] != spelling:
                    raise ArchiveError("Recovery archive contains case-colliding directory paths")
                self.paths[key] = spelling
            self.entries = len(self.paths)
            self.bytes += member.size
            if self.entries > MAX_ENTRIES or self.bytes > byte_limit():
                raise ArchiveError("Recovery archive exceeds configured entry or expanded-byte limits")

    def read(self, path, destination=None):
        with Path(path).open("rb") as source, _tar(source, self) as outer:
            first = outer.next()
            hybrid = first is not None and first.name == "data.tar.gz"
            if hybrid:
                members = []
                for member in outer:
                    if (len(members) == 2 or not member.isfile() or member.issparse()
                            or member.size > byte_limit()):
                        raise ArchiveError("Invalid or oversized hybrid recovery archive")
                    members.append(member)
                if ([m.name for m in members] != ["data.tar.gz", "worlds.tar"]
                        or any(not m.isfile() or m.issparse() for m in members)):
                    raise ArchiveError("Invalid hybrid recovery archive")
                if sum(m.size for m in members) > byte_limit():
                    raise ArchiveError("Recovery archive exceeds configured byte limits")
                outer.fileobj.headers_only = False
                for member in members:
                    with outer.extractfile(member) as stream, _tar(stream, self, compressed=member.name == "data.tar.gz") as inner:
                        self.members(inner)
                        inner.fileobj.headers_only = False
                        if destination is not None:
                            safe_extract_tar(inner, destination, allow_internal_links=False)
            else:
                self.members(outer)
                outer.fileobj.headers_only = False
                if destination is not None:
                    safe_extract_tar(outer, destination, allow_internal_links=False)
        return {"expandedBytes": self.bytes, "entries": self.entries}


def inspect_archive(path):
    try:
        return Archive().read(Path(path))
    except ArchiveError:
        raise
    except (ValueError, tarfile.TarError, EOFError, OSError):
        raise ValueError("Recovery archive is not a readable Fabricator tar archive") from None


def extract_archive(path, destination):
    try:
        return Archive().read(Path(path), Path(destination))
    except ArchiveError:
        raise
    except (ValueError, tarfile.TarError, EOFError, OSError):
        raise ValueError("Recovery archive extraction failed; no instance was registered") from None
