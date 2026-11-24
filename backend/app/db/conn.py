from pathlib import Path

import psycopg

from app.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(**kwargs) -> psycopg.Connection:
    """Fails after DB_CONNECT_TIMEOUT_S instead of hanging (e.g. Neon waking from idle)."""
    s = get_settings()
    kwargs.setdefault("connect_timeout", s.db_connect_timeout_s)
    return psycopg.connect(s.database_url, **kwargs)


def apply_schema(conn: psycopg.Connection) -> None:
    """Create the extension, tables and indexes. Idempotent."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
