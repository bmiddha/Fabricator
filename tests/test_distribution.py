"""Fork release-source selection and native updater wiring."""
from pathlib import Path
import os
import subprocess
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import backend.system.update_service as updates
import tools.cli as cli
from tools.distribution import installer_url, release_source


@pytest.fixture(autouse=True)
def clear_source(monkeypatch, tmp_path):
    monkeypatch.delenv("FABRICATOR_REPO", raising=False)
    monkeypatch.delenv("FABRICATOR_BRANCH", raising=False)
    monkeypatch.setattr(cli, "SOURCE_FILE", tmp_path / "source.env")
    monkeypatch.setenv("FABRICATOR_UPDATE_DIR", str(tmp_path / "update"))


def test_default_source_is_fork_main():
    repo, branch = release_source()
    assert (repo, branch) == ("bmiddha/Fabricator", "main")
    assert installer_url(repo, branch) == (
        "https://raw.githubusercontent.com/bmiddha/Fabricator/main/tools/install.sh"
    )


def test_saved_source_is_data_not_shell(tmp_path, monkeypatch):
    source = tmp_path / "source.env"
    source.write_text("FABRICATOR_REPO=example/Panel\nFABRICATOR_BRANCH=feature/source\n")
    assert release_source(source_file=source) == ("example/Panel", "feature/source")
    monkeypatch.setenv("FABRICATOR_REPO", "bmiddha/Fabricator")
    assert release_source(source_file=source) == ("bmiddha/Fabricator", "feature/source")
    monkeypatch.delenv("FABRICATOR_REPO")
    source.write_text("FABRICATOR_REPO=$(touch marker)/Panel\n")
    with pytest.raises(ValueError, match="owner/repository"):
        release_source(source_file=source)


@pytest.mark.parametrize("key,value", [
    ("FABRICATOR_REPO", "https://github.com/example/Panel"),
    ("FABRICATOR_REPO", "example/Panel/extra"),
    ("FABRICATOR_REPO", "example/Panel\nINJECT=1"),
    ("FABRICATOR_BRANCH", "../main"),
    ("FABRICATOR_BRANCH", "main;echo unsafe"),
    ("FABRICATOR_BRANCH", "main\nINJECT=1"),
])
def test_unsafe_source_is_rejected(monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        release_source()


def test_cli_fetches_releases_from_saved_source(monkeypatch):
    cli.SOURCE_FILE.write_text("FABRICATOR_REPO=example/Panel\n")
    response = Mock()
    response.json.return_value = [{"tag_name": "v1.2.3"}]
    get = Mock(return_value=response)
    monkeypatch.setattr(cli.requests, "get", get)
    assert cli._get_latest_release_tag() == "v1.2.3"
    get.assert_called_once_with(
        "https://api.github.com/repos/example/Panel/releases?per_page=100", timeout=15
    )


def test_cli_passes_saved_source_to_installer(monkeypatch):
    cli.SOURCE_FILE.write_text("FABRICATOR_REPO=example/Panel\nFABRICATOR_BRANCH=release/install\n")
    monkeypatch.setattr(cli, "_get_local_version", lambda: "v1.0.0")
    monkeypatch.setattr(cli, "_get_latest_release_tag", lambda: "v1.2.3")
    run = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(cli.subprocess, "run", run)
    result = CliRunner().invoke(cli.cli, ["update"])
    assert result.exit_code == 0, result.output
    env = run.call_args.kwargs["env"]
    assert env["FABRICATOR_REPO"] == "example/Panel"
    assert env["FABRICATOR_BRANCH"] == "release/install"
    assert env["FABRICATOR_VERSION"] == "v1.2.3"


def test_panel_status_and_download_use_same_source(monkeypatch):
    monkeypatch.setenv("FABRICATOR_REPO", "example/Panel")
    monkeypatch.setenv("FABRICATOR_BRANCH", "release/install")
    monkeypatch.setattr(updates, "_fetch_latest_tag", lambda repo: "v1.2.3")
    run = Mock(return_value=Mock(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(updates.subprocess, "run", run)
    service = updates.UpdateService()
    url = "https://raw.githubusercontent.com/example/Panel/release/install/tools/install.sh"
    assert service.get_status()["updaterScript"] == url
    service._run_update_dev("v1.2.3")
    command = run.call_args.args[0][-1]
    assert command.startswith("set -o pipefail;")
    assert url in command
    assert run.call_args.kwargs["env"]["FABRICATOR_REPO"] == "example/Panel"
    assert run.call_args.kwargs["env"]["FABRICATOR_BRANCH"] == "release/install"


def test_latest_release_cache_is_scoped_to_repository(monkeypatch):
    import io
    import json

    calls = []

    def fetch(request, timeout):
        calls.append(request.full_url)
        tag = "v1.0.0" if "example/One/" in request.full_url else "v2.0.0"
        return io.BytesIO(json.dumps([{"tag_name": tag}]).encode())

    monkeypatch.setattr(updates, "_tag_cache", {"repo": None, "tag": None, "fetched_at": 0.0})
    monkeypatch.setattr(updates.request, "urlopen", fetch)
    assert updates._fetch_latest_tag("example/One") == "v1.0.0"
    assert updates._fetch_latest_tag("example/One") == "v1.0.0"
    assert updates._fetch_latest_tag("example/Two") == "v2.0.0"
    assert len(calls) == 2


def test_native_units_share_root_owned_source_file():
    script = (Path(__file__).parents[1] / "tools/install.sh").read_text()
    assert script.count("EnvironmentFile=${SOURCE_ENV_FILE}") == 2
    assert 'chown root:root "$SOURCE_ENV_FILE"' in script
    assert 'chmod 0644 "$SOURCE_ENV_FILE"' in script
    assert "chown root:root /etc/fabricator" in script
    assert 'source "$SOURCE_ENV_FILE"' not in script


@pytest.mark.parametrize("saved,override,expected", [
    ("", {}, ("bmiddha/Fabricator", "main")),
    ("FABRICATOR_REPO=example/Panel\nFABRICATOR_BRANCH=feature/install\n",
     {}, ("example/Panel", "feature/install")),
    ("FABRICATOR_REPO=example/Panel\nFABRICATOR_BRANCH=feature/install\n",
     {"FABRICATOR_REPO": "bmiddha/Fabricator"}, ("bmiddha/Fabricator", "feature/install")),
])
def test_installer_source_selection_matches_python(tmp_path, saved, override, expected):
    source = tmp_path / "source.env"
    source.write_text(saved)
    script = (Path(__file__).parents[1] / "tools/install.sh").read_text()
    # Execute only source selection, before any installer side effects.
    prefix = script.split("    # playit.gg tunnel agent", 1)[0]
    prefix = prefix.replace('SOURCE_ENV_FILE="/etc/fabricator/source.env"',
                            f'SOURCE_ENV_FILE="{source}"')
    prefix += '\n printf "%s\\n%s\\n" "$FABRICATOR_REPO" "$FABRICATOR_BRANCH"\n}\nmain\n'
    env = {key: value for key, value in os.environ.items()
           if key not in {"FABRICATOR_REPO", "FABRICATOR_BRANCH"}}
    result = subprocess.run(["bash"], input=prefix, env={**env, **override},
                            text=True, capture_output=True, check=True)
    assert tuple(result.stdout.splitlines()) == expected


def test_installer_rejects_source_before_host_changes(monkeypatch):
    monkeypatch.setenv("FABRICATOR_REPO", "example/Panel\nINJECT=1")
    result = subprocess.run(
        ["bash", str(Path(__file__).parents[1] / "tools/install.sh")],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "FABRICATOR_REPO must be a GitHub owner/repository" in result.stderr
