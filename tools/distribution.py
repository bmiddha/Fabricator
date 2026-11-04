"""Release source shared by the panel and installed CLI."""
from __future__ import annotations

import os
from pathlib import Path
import re

DEFAULT_REPO = "bmiddha/Fabricator"
DEFAULT_BRANCH = "main"
SOURCE_FILE = Path("/etc/fabricator/source.env")


def release_source(*, source_file: Path | None = None) -> tuple[str, str]:
    saved = {}
    if source_file is not None and source_file.exists():
        for line in source_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"FABRICATOR_REPO", "FABRICATOR_BRANCH"}:
                saved[key] = value
    repo = os.environ.get("FABRICATOR_REPO", saved.get("FABRICATOR_REPO", DEFAULT_REPO))
    branch = os.environ.get("FABRICATOR_BRANCH", saved.get("FABRICATOR_BRANCH", DEFAULT_BRANCH))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", repo):
        raise ValueError("FABRICATOR_REPO must be a GitHub owner/repository")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch:
        raise ValueError("FABRICATOR_BRANCH must be a Git branch name without '..'")
    return repo, branch


def installer_url(repo: str, branch: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{branch}/tools/install.sh"
