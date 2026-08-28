# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_migrations.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real, reversible schema-migration tests against a real temporary
database (in-memory and on-disk tempfile) - no mocked connection, the
exact same sqlite3.Connection code path TimeSeriesStore itself uses."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from hydra_umc_datalake.store import (
    SCHEMA_VERSION,
    TimeSeriesStore,
    _current_schema_version,
    migrate_down,
    migrate_up,
)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    # Excludes sqlite_* tables: SQLite creates `sqlite_sequence` itself
    # to back AUTOINCREMENT once a table using it has ever existed, and
    # doesn't remove it when that table is dropped - a real SQLite
    # implementation detail, not part of this schema's own migrations.
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


def test_fresh_database_migrates_to_the_latest_real_version() -> None:
    conn = sqlite3.connect(":memory:")
    applied = migrate_up(conn)

    assert applied == [1, 2]
    assert _current_schema_version(conn) == SCHEMA_VERSION
    assert {"samples", "retention_policies"} <= _table_names(conn)


def test_migrate_up_is_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    migrate_up(conn)

    second_run = migrate_up(conn)

    assert second_run == []
    assert _current_schema_version(conn) == SCHEMA_VERSION


def test_migrate_up_can_stop_at_an_explicit_target() -> None:
    conn = sqlite3.connect(":memory:")
    applied = migrate_up(conn, target_version=1)

    assert applied == [1]
    assert _current_schema_version(conn) == 1
    assert "retention_policies" not in _table_names(conn)


def test_migrate_down_reverses_the_real_schema_change() -> None:
    conn = sqlite3.connect(":memory:")
    migrate_up(conn)

    reverted = migrate_down(conn, target_version=1)

    assert reverted == [2]
    assert _current_schema_version(conn) == 1
    assert "retention_policies" not in _table_names(conn)
    assert "samples" in _table_names(conn)  # migration 1's own table survives


def test_migrate_down_to_zero_removes_everything() -> None:
    conn = sqlite3.connect(":memory:")
    migrate_up(conn)

    reverted = migrate_down(conn, target_version=0)

    assert reverted == [2, 1]
    assert _current_schema_version(conn) == 0
    assert _table_names(conn) == set()


def test_migrate_down_then_up_again_restores_the_real_schema() -> None:
    conn = sqlite3.connect(":memory:")
    migrate_up(conn)
    migrate_down(conn, target_version=0)

    reapplied = migrate_up(conn)

    assert reapplied == [1, 2]
    assert {"samples", "retention_policies"} <= _table_names(conn)


def test_reversible_migration_on_a_real_temporary_disk_database(tmp_path: Path) -> None:
    # Not just ":memory:" - a real on-disk tempfile database, proving the
    # same reversible migration path works against durable storage too.
    db_path = tmp_path / "datalake.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_up(conn)
        assert _current_schema_version(conn) == SCHEMA_VERSION

        migrate_down(conn, target_version=1)
        assert _current_schema_version(conn) == 1
        assert "retention_policies" not in _table_names(conn)
    finally:
        conn.close()
    assert db_path.is_file()


def test_downgrading_preserves_unrelated_real_data() -> None:
    # A real, concrete proof that migrate_down(target=1) - which only
    # reverses migration 2 (retention_policies) - never touches real
    # sample data migration 1 owns.
    with TimeSeriesStore(":memory:") as store:
        from hydra_umc_datalake.store import Sample

        store.insert(Sample(source_id="r1", kind="k", timestamp=1000, fields={"v": 1.0}))
        assert store.sample_count() == 1

        migrate_down(store._conn, target_version=1)  # noqa: SLF001 - real internal connection, this is the migration test
        (count,) = store._conn.execute("SELECT COUNT(*) FROM samples").fetchone()  # noqa: SLF001

        assert count == 1


def test_store_exposes_its_own_real_schema_version() -> None:
    with TimeSeriesStore(":memory:") as store:
        assert store.schema_version == SCHEMA_VERSION
