"""Guards against reintroducing external statistics writes."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

FORBIDDEN_ATTRIBUTES: tuple[str, ...] = ("async_add_external_statistics",)
FORBIDDEN_MODULES: tuple[str, ...] = (
    "homeassistant.components.recorder.statistics",
    "homeassistant.components.recorder.models.statistics",
)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _iter_securemtr_files() -> Iterator[Path]:
    """Yield Python source files within the securemtr integration package."""

    base_dir = PACKAGE_ROOT / "custom_components" / "securemtr"
    yield from base_dir.rglob("*.py")


def _scan_forbidden_references(path: Path) -> set[str]:
    """Return forbidden reference summaries discovered within the file."""

    violations: set[str] = set()
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_MODULES:
                    violations.add(f"import:{alias.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in FORBIDDEN_MODULES:
                for alias in node.names:
                    violations.add(f"from:{module}.{alias.name}")
            for alias in node.names:
                if alias.name in FORBIDDEN_ATTRIBUTES and module:
                    violations.add(f"from:{module}.{alias.name}")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRIBUTES:
                violations.add(f"attr:{node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_ATTRIBUTES:
                violations.add(f"name:{node.id}")

    return violations


def test_integration_avoids_external_statistics_calls() -> None:
    """Ensure the integration never references recorder external statistics helpers."""

    offenders: dict[str, set[str]] = {}
    for path in _iter_securemtr_files():
        violations = _scan_forbidden_references(path)
        if violations:
            offenders[str(path.relative_to(PACKAGE_ROOT))] = violations

    assert not offenders, f"Forbidden external statistics references found: {offenders}"
