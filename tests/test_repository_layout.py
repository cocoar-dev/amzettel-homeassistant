"""Keep local workspace data out of the publishable repository."""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    [".local/private.json", ".local/venv/bin/python", ".local/archive/internal.md", ".alta/tasks/local.md"],
)
def test_local_workspace_paths_are_ignored(path):
    result = subprocess.run(
        ["git", "-C", str(ROOT), "check-ignore", "--no-index", "--stdin"],
        input=path + "\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == path


def test_git_index_contains_no_local_workspace_data():
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True, check=True
    )
    forbidden = {".local", ".alta", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
    assert not [
        path for path in result.stdout.split("\0") if path and forbidden.intersection(Path(path).parts)
    ]


def test_development_tools_use_the_local_workspace():
    settings = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert settings["tool"]["pytest"]["ini_options"]["cache_dir"] == ".local/cache/pytest"
    assert settings["tool"]["ruff"]["cache-dir"] == ".local/cache/ruff"
    workflow = yaml.safe_load((ROOT / ".github/workflows/validate.yml").read_text())
    job = workflow["jobs"]["pytest"]
    assert job["env"]["PYTHONPYCACHEPREFIX"].endswith("/.local/cache/pycache")
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        "uv venv --python 3.14 .local/venv",
        "uv pip install --python .local/venv/bin/python -r requirements_test.txt",
        ".local/venv/bin/python -m pytest -q",
    ]


def test_license_metadata_covers_code_and_separate_brand_assets():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT AND LicenseRef-amZettel-Brand-Assets"
    assert set(project["license-files"]) == {"LICENSE", "NOTICE", "TRADEMARK.md"}
    for name in project["license-files"]:
        assert (ROOT / name).is_file()
    license_text = (ROOT / "LICENSE").read_text()
    assert license_text.startswith("MIT License\n\nCopyright (c) 2026 COCOAR e.U.\n")
    assert "Permission is hereby granted, free of charge" in license_text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in license_text
    assert "LicenseRef-amZettel-Brand-Assets" in (ROOT / "TRADEMARK.md").read_text()


def test_license_scope_is_discoverable_in_both_languages():
    for name in ("README.md", "README.de.md"):
        text = (ROOT / name).read_text()
        for target in ("LICENSE", "NOTICE", "TRADEMARK.md", "SECURITY.md"):
            assert f"]({target})" in text
    for directory, prefix in ((ROOT / "docs", "../"), (ROOT / "docs" / "de", "../../")):
        assert f"]({prefix}TRADEMARK.md)" in (directory / "branding.md").read_text()
    notice = " ".join((ROOT / "NOTICE").read_text().split())
    assert "custom_components/amzettel/brand/" in notice
    assert "not covered by the MIT License" in notice


def test_installed_integration_keeps_its_license_and_asset_notices():
    # HACS and manual installs copy the integration folder, not the repository root.
    for name in ("LICENSE", "NOTICE", "TRADEMARK.md"):
        assert (ROOT / "custom_components" / "amzettel" / name).read_bytes() == (ROOT / name).read_bytes()


def test_maintainer_metadata_is_consistent():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "custom_components" / "amzettel" / "manifest.json").read_text())
    assert project["authors"] == [{"name": "COCOAR e.U."}]
    assert project["maintainers"] == [{"name": "Bernhard Windisch", "email": "bwi@cocoar.dev"}]
    assert manifest["codeowners"] == ["@windischb"]
    entries = [
        line.split() for line in (ROOT / ".github" / "CODEOWNERS").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert entries == [["*", *manifest["codeowners"]]]


def test_security_policy_uses_the_existing_private_contact():
    policy = (ROOT / "SECURITY.md").read_text()
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["maintainers"][0]["email"] in policy
    assert "Do not open a public issue" in policy
    assert "/security/advisories/new" not in policy  # No claim that private reporting is enabled.
