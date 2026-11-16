from pathlib import Path

import psycopg

from app.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, **kwargs)


def apply_schema(conn: psycopg.Connection) -> None:
    """Create the extension, tables and indexes. Idempotent."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
