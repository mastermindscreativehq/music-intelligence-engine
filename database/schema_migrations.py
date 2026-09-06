"""Ordered SQL migrations for PostgreSQL/Supabase (Phase 6).

Files live at ``database/migrations/postgres/NNNN_name.sql`` and are
applied in filename order inside transactions, tracked in a
``schema_migrations`` table. Forward-only, like the SQLite runner.

Timeout policy: migrations run on a connection that carries the
application's ``statement_timeout`` guard (8s in
:class:`database.pg_store.PostgresStorage`). DDL executes at a scope that
``EXPLAIN``/analytics READ queries never touch, so the tight runtime guard
should NOT constrain it: a schema migration may legitimately wait longer on
a Supabase tenant (metadata writes, lock acquisition). Accordingly, the
migration runner explicitly raises ``statement_timeout`` and sets a
generous (but finite) ``lock_timeout`` for the duration of each pending
migration, then restores the caller's previous settings — so the runtime
guard stays in force for every ordinary request while migrations are free
to wait for the lock they need.
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations" / "postgres"

_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

# How long a migration statement may run before PostgreSQL aborts it. The
# application's runtime guard is 8s; migrations (DDL) get far more headroom.
# Zero disables the statement timeout entirely for the migration scope.
_MIGRATION_STATEMENT_TIMEOUT_MS = 0
# How long a migration waits on a lock before failing with a clean
# "lock_timeout exceeded" error instead of silently queuing behind whatever
# holds the migration target.
_MIGRATION_LOCK_TIMEOUT_MS = 120_000
# Settings we must restore afterward so the runtime guard survives.
_MIGRATION_SCOPE_SETTINGS = ("statement_timeout", "lock_timeout")


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

    # Migrations run on the same connection as ordinary storage calls, which
    # carries an 8s statement_timeout runtime guard. DDL must not inherit that:
    # give the migration scope its own generous timeout and a finite lock
    # timeout, and always restore the caller's prior values afterward.
    prior = _read_settings(cur)
    try:
        _set_setting(cur, "statement_timeout", _MIGRATION_STATEMENT_TIMEOUT_MS,
                     prior.get("statement_timeout"))
        _set_setting(cur, "lock_timeout", _MIGRATION_LOCK_TIMEOUT_MS,
                     prior.get("lock_timeout"))
        for version, filename, sql in load_pg_migrations():
            print(f"CHECKING {filename} (v{version})")
            if version in applied:
                print(f"ALREADY APPLIED {filename} (v{version})")
                continue
            print(f"APPLYING {filename} (v{version})")
            try:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations(version, filename) "
                    "VALUES (%s, %s)", (version, filename))
                conn.commit()
                print(f"SUCCESS {filename} (v{version})")
            except Exception:
                conn.rollback()   # keep the pending transaction clean
                raise
    finally:
        _restore_settings(cur, prior)

    cur.execute("SELECT COALESCE(MAX(version), 0) AS v "
                "FROM schema_migrations")
    row = cur.fetchone()
    conn.rollback()
    return int(row["v"] if isinstance(row, dict) else row[0])


def _read_settings(cur) -> dict:
    """Best-effort snapshot of the settings we distinguish by scope."""
    snapshot: dict = {}
    for name in _MIGRATION_SCOPE_SETTINGS:
        try:
            cur.execute(f"SHOW {name}")
            row = cur.fetchone()
            snapshot[name] = (row[0] if isinstance(row, (list, tuple))
                              else row[name])
        except Exception:
            snapshot[name] = None
    return snapshot


def _set_setting(cur, name: str, value: object, previous) -> None:
    """Best-effort set of a session GUC for the migration scope.

    The migration may legitimately run beyond the application's 8s runtime
    guard, so it is intentionally *encouraged* to raise statement_timeout
    and apply a lock_timeout. Some hosts (e.g. Supabase pooler role) reject
    writes to certain GUCs; this is tolerated — but a failed ``SET`` must
    not leave the transaction aborted, or the very DDL we are trying to
    protect would fail with "current transaction is aborted". So a failure
    is followed by :meth:`rollback` to leave the connection usable.
    """
    if value is None:
        return
    try:
        # SET does not accept bind parameters, so the value is inlined as a
        # literal. It is always a fixed constant (migration timeouts) or a
        # server-reported previous value ("8s", "2min", ...) — never user
        # input.
        cur.execute(f"SET SESSION {name} TO {value}")
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass


def _restore_settings(cur, snapshot: dict) -> None:
    """Restore whatever the session had before we touched the settings.

    Like :func:`_set_setting`, a failed restore is tolerated and clears the
    aborted transaction so the connection is not poisoned for later use.
    """
    for name, value in snapshot.items():
        if value is None:
            continue
        try:
            cur.execute(f"SET SESSION {name} TO {value}")
        except Exception:
            try:
                cur.connection.rollback()
            except Exception:
                pass
