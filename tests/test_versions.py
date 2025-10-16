"""Tests for version consistency across project metadata."""

from __future__ import annotations

import json
from pathlib import Path

import tomllib


def test_manifest_version_matches_pyproject() -> None:
    """Ensure the manifest version stays in sync with pyproject metadata."""
    project_root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads(
        (project_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (project_root / "custom_components" / "securemtr" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert manifest["version"] == pyproject["project"]["version"], (
        "Manifest version must match pyproject version."
    )
