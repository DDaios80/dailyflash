"""Smoke test: every src/*.py module imports without error.

What this catches:
- Python syntax errors that slipped through editor checks
- Missing import dependencies (e.g., new import without requirements.txt update)
- Import-time misconfiguration (e.g., env var reads at module top level)
- Circular imports
- Module-level errors that would crash the cron at startup

Runs fast (<1s) and doesn't touch the database. Should be the first test
to fail if a deploy is broken.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

SRC_DIR = Path(__file__).resolve().parent.parent / "src"

# Discover every top-level .py in src/. Skip __pycache__, dotfiles,
# and any module starting with _ (internal helpers usually don't have
# importable side effects worth smoke-checking).
PYTHON_MODULES = sorted(
    p.stem
    for p in SRC_DIR.glob("*.py")
    if not p.stem.startswith("_") and p.name != "__init__.py"
)

# Sanity: we should have found a reasonable number of modules.
# If this assertion fails, conftest.py probably didn't set up sys.path right.
assert len(PYTHON_MODULES) >= 10, (
    f"Expected at least 10 modules in {SRC_DIR}, found {len(PYTHON_MODULES)}. "
    f"sys.path setup may be broken."
)


@pytest.mark.parametrize("module_name", PYTHON_MODULES)
def test_module_imports_without_error(module_name):
    """Each src/*.py imports cleanly.

    If this fails, the named module has a syntax error, missing dependency,
    or import-time bug. Check the assertion error for the underlying cause.
    """
    try:
        importlib.import_module(module_name)
    except Exception as e:
        pytest.fail(
            f"src/{module_name}.py failed to import: "
            f"{type(e).__name__}: {e}"
        )
