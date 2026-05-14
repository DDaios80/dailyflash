"""Shared pytest fixtures for the smoke test suite.

Sets up sys.path so tests can import from src/, and provides a session-scoped
Supabase client fixture for tests that query the live database.

Tests are READ-ONLY by design. Never mutate production data from tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make src/ importable from tests
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(scope="session")
def supabase_client():
    """Session-scoped Supabase client. Reads creds from .env at repo root."""
    from supa import client
    return client()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT
