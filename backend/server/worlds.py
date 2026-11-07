"""Strict, read-only discovery of supported Java Edition world groups."""
from __future__ import annotations

import gzip
import io
from pathlib import Path
import re
import struct

from backend.server.importer import read_properties

PAPER = frozenset({"paper", "purpur", "pufferfish", "folia"})
LOADERS = PAPER | {"vanilla", "fabric", "quilt", "forge", "neoforge"}


def safe_name(value: object) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", value):
        raise ValueError("Use 1-80 letters, numbers, dots, underscores or hyphens, starting with a letter or number")
    if value.rstrip(". ") != value or value.split(".")[0].lower() in {
        "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
    }:
        raise ValueError("That name is reserved by the operating system")
    return value


def level_metadata(path: Path) -> dict:
    """Read bounded NBT, including Version and DataVersion, without trusting gzip sizes."""
    limit = 16 * 1024 * 1024
    with gzip.open(path, "rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("level.dat exceeds the 16 MiB metadata limit")
    stream = io.BytesIO(raw)
    budget = 200_000

    def read(size):
        value = stream.read(size)
        if len(value) != size:
            raise ValueError("Truncated world metadata")
        return value

    def number(fmt):
        return struct.unpack(">" + fmt, read(struct.calcsize(">" + fmt)))[0]

    def text():
        return read(number("H")).decode("utf-8")

    def payload(tag, depth=0):
        nonlocal budget
        budget -= 1
        if budget < 0 or depth > 64:
            raise ValueError("World metadata exceeds structural limits")
        if tag in {1, 2, 3, 4, 5, 6}:
            return number({1: "b", 2: "h", 3: "i", 4: "q", 5: "f", 6: "d"}[tag])
        if tag == 8:
            return text()
        if tag in {7, 11, 12}:
            length = number("i")
            if length < 0 or length > limit:
                raise ValueError("Invalid NBT array length")
            read(length * {7: 1, 11: 4, 12: 8}[tag])
            return None
        if tag == 9:
            item, length = number("B"), number("i")
            if length < 0 or length > 200_000:
                raise ValueError("Invalid NBT list length")
            return [payload(item, depth + 1) for _ in range(length)]
        if tag == 10:
            result = {}
            while True:
                item = number("B")
                if item == 0:
                    return result
                name = text()
                if name in result:
                    raise ValueError("Duplicate NBT key")
                result[name] = payload(item, depth + 1)
        raise ValueError("Unsupported NBT tag")

    try:
        if number("B") != 10:
            raise ValueError("World metadata must contain a root compound")
        text()
        root = payload(10)
        data = root.get("Data", root)
        version = data.get("Version") or {}
        result = {"dataVersion": data.get("DataVersion"), "version": version.get("Name")}
        if type(result["dataVersion"]) is not int or not isinstance(result["version"], str):
            raise ValueError("World has no verifiable DataVersion / Version.Name")
        if version.get("Snapshot"):
            raise ValueError("Snapshot worlds are not supported")
        return result
    except (UnicodeError, struct.error, AttributeError) as exc:
        raise ValueError("Invalid world metadata") from exc


def discover(server: dict, root: Path) -> list[dict]:
    loader = server.get("loader")
    if loader not in LOADERS:
        raise ValueError("Only Java game servers can be copied; proxies are not supported")
    for filename in ("server.properties", "bukkit.yml"):
        if (root / filename).is_symlink():
            raise ValueError("World configuration files must not be symlinks")
    props = read_properties(root)
    name = safe_name(props.get("level-name", server.get("levelName") or "world"))
    if (root / "bukkit.yml").exists():
        # A non-default world-container breaks root-relative publication.
        from ruamel.yaml import YAML
        doc = YAML(typ="safe").load((root / "bukkit.yml").read_text()) or {}
        container = (doc.get("settings") or {}).get("world-container", ".")
        if container not in {".", ""}:
            raise ValueError("Custom Bukkit world-container layouts require manual migration")
    main = root / name
    if not main.exists():
        return []
    if main.is_symlink() or not (main / "level.dat").is_file() or (main / "level.dat").is_symlink():
        raise ValueError("Active world must be a real directory containing level.dat")
    names = [name]
    nested = sorted(entry.name for entry in main.iterdir()
                    if re.fullmatch(r"DIM-?\d+", entry.name) or entry.name == "dimensions")
    siblings = [name + suffix for suffix in ("_nether", "_the_end") if (root / (name + suffix)).exists()]
    if loader in PAPER:
        if nested:
            raise ValueError("Mixed or nested Bukkit dimensions require manual migration")
        names += siblings
        layout = "bukkit-split"
    else:
        if siblings:
            raise ValueError("Split dimensions on a non-Bukkit loader require manual migration")
        layout = "java-nested"
    metadata = level_metadata(main / "level.dat")
    if metadata["version"] != server.get("version"):
        raise ValueError("World version differs from the instance version; initialize or migrate it manually")
    dimensions = list(nested)
    for sibling in siblings:
        dimension = root / sibling
        if dimension.is_symlink() or not dimension.is_dir():
            raise ValueError("Dimension folders must not be links")
        level = dimension / "level.dat"
        if level.exists():
            if level.is_symlink() or level_metadata(level) != metadata:
                raise ValueError("Dimension metadata differs from the active world; migrate it manually")
        dimensions.append(sibling)
    custom = main / "dimensions"
    if custom.is_symlink():
        raise ValueError("Custom dimensions must not be symlinks")
    if custom.is_dir():
        for namespace in custom.iterdir():
            if namespace.is_symlink():
                raise ValueError("Dimension namespaces must not be symlinks")
        dimensions.extend(str(p.relative_to(main)) for p in sorted(custom.glob("*/*")) if p.is_dir())
    return [{"id": name, "name": name, "directories": names, "layout": layout,
             "dimensions": dimensions, **metadata}]


def require_supported_worlds(root: Path, groups: list[dict]) -> None:
    allowed = {name for group in groups for name in group["directories"]}
    additional = [p.name for p in root.iterdir()
                  if p.is_dir() and (p / "level.dat").exists() and p.name not in allowed]
    if additional:
        raise ValueError("Additional plugin-managed worlds require manual migration: " + ", ".join(sorted(additional)))
