"""Supabase helpers. Loads env from .env at repo root."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def client() -> Client:
    load_dotenv(ENV_PATH)
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError(f"Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY in {ENV_PATH}")
    return create_client(url, key)
