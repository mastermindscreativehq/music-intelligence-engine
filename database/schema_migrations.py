"""Ordered SQL migrations for PostgreSQL/Supabase (Phase 6).

Files live at ``database/migrations/postgres/NNNN_name.sql`` and are
applied in filename order inside transactions, tracked in a
``schema_migrations`` table. Forward-only, like the SQLite runner.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations" / "postgres"

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def load_pg_migrations(folder: Path | None = None
                       ) -> list[tuple[int, str, str]]:
    """Return [(version, filename, sql)] sorted by version."""
    directory = Path(folder) if folder else MIGRATIONS_DIR
    found: list[tuple[int, str, str]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.sql")):
            match = _FILENAME.match(path.name)
            if not match:
                raise ValueError(
                    f"migration filename must be NNNN_name.sql: "
                    f"{path.name}")
            found.append((int(match.group(1)), path.name,
                          path.read_text(encoding="utf-8")))
    versions = [v for v, _, _ in found]
    if len(set(versions)) != len(versions):
        raise ValueError("duplicate migration versions")
    return sorted(found, key=lambda item: item[0])


def apply_pg_migrations(conn) -> int:
    """Apply pending migrations; returns the resulting schema version."""
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, filename TEXT NOT NULL, "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    conn.commit()
    cur.execute("SELECT version FROM schema_migrations")
    rows = cur.fetchall()
    applied = {int(r["version"] if isinstance(r, dict) else r[0])
               for r in rows}
    for version, filename, sql in load_pg_migrations():
        if version in applied:
            continue
        cur.execute(sql)
        cur.execute("INSERT INTO schema_migrations(version, filename) "
                    "VALUES (%s, %s)", (version, filename))
        conn.commit()
    cur.execute("SELECT COALESCE(MAX(version), 0) AS v "
                "FROM schema_migrations")
    row = cur.fetchone()
    conn.rollback()
    return int(row["v"] if isinstance(row, dict) else row[0])
