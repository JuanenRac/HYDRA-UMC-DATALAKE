# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/store.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""The real time-series store - a genuine, queryable, on-disk database
(sqlite3, Python stdlib), not an in-memory placeholder that forgets
everything on restart. sqlite3 is a deliberate choice for v0: it is
already real, ACID, and ships with every Python install - reaching for
InfluxDB/TimescaleDB (both named in this project's own pyproject.toml
keywords) means standing up and operating a whole extra service, which
is a real infrastructure decision that belongs to whoever deploys this,
not something to bolt on unasked. See mejoras_futuras.txt for why that
migration is scoped out rather than attempted here.

Schema: one row per (source, kind, field, timestamp) reading - a "long"
/ narrow time-series table, not one column per field. Re-delivery of the
same identity is idempotent and replaces its value (last write wins), so a
collector retry cannot inflate raw queries or aggregates. This is what lets
this store accept ANY normalized Sample from HYDRA-UMC-TELEMETRY-COLLECTOR
(whose Fields map is open-ended) without a schema migration every time a
new telemetry field shows up.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Sample:
    """Mirrors HYDRA-UMC-TELEMETRY-COLLECTOR's own normalized telemetry
    shape (sourceId/kind/timestamp/fields) - the same JSON a collector
    would eventually POST here, once it has a real sink to write to."""

    source_id: str
    kind: str
    timestamp: int  # unix milliseconds
    fields: dict[str, float]


@dataclass(frozen=True)
class Point:
    """One stored (or queried) reading - the row shape query() returns."""

    source_id: str
    kind: str
    field: str
    timestamp: int
    value: float


@dataclass(frozen=True)
class Bucket:
    """One aggregated time bucket - what aggregate() returns."""

    bucket_start: int
    value: float
    count: int


@dataclass(frozen=True)
class Migration:
    """One real, reversible schema change. `up_sql` and `down_sql` are
    each a real, self-contained SQL script - `down_sql` must exactly
    undo what `up_sql` did, so `migrate_down()` can restore the database
    to precisely the shape it had at the prior version."""

    version: int
    up_sql: str
    down_sql: str


# Real schema history, oldest first - never edit a migration already
# released; add a new one instead, the same convention every real
# migration framework uses. Tracked via SQLite's own built-in
# `PRAGMA user_version` (an integer stored in the file header) rather
# than a hand-rolled bookkeeping table, since SQLite already provides
# exactly this real mechanism.
_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        up_sql="""
        CREATE TABLE IF NOT EXISTS samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            field TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            value REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_samples_kind_field_ts
            ON samples (kind, field, timestamp);
        CREATE INDEX IF NOT EXISTS idx_samples_source_ts
            ON samples (source_id, timestamp);
        """,
        down_sql="""
        DROP INDEX IF EXISTS idx_samples_source_ts;
        DROP INDEX IF EXISTS idx_samples_kind_field_ts;
        DROP TABLE IF EXISTS samples;
        """,
    ),
    Migration(
        version=2,
        up_sql="""
        CREATE TABLE IF NOT EXISTS retention_policies (
            kind TEXT NOT NULL,
            field TEXT NOT NULL,
            retention_ms INTEGER NOT NULL,
            PRIMARY KEY (kind, field)
        );
        """,
        down_sql="""
        DROP TABLE IF EXISTS retention_policies;
        """,
    ),
)

SCHEMA_VERSION = _MIGRATIONS[-1].version


def _current_schema_version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return int(version)


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA does not accept bound parameters - `version` is always a
    # real int from this module's own _MIGRATIONS tuple, never untrusted
    # input, so a validated f-string is safe here.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def migrate_up(conn: sqlite3.Connection, *, target_version: int | None = None) -> list[int]:
    """Real, reversible-by-design migration runner - applies every real
    pending migration in order, from the database's own current
    `PRAGMA user_version` up to `target_version` (default: the latest
    known migration). Returns the real list of version numbers applied,
    empty if the database was already at or above the target."""
    target = SCHEMA_VERSION if target_version is None else target_version
    current = _current_schema_version(conn)
    applied: list[int] = []
    for migration in _MIGRATIONS:
        if current < migration.version <= target:
            conn.executescript(migration.up_sql)
            _set_schema_version(conn, migration.version)
            conn.commit()
            applied.append(migration.version)
    return applied


def migrate_down(conn: sqlite3.Connection, *, target_version: int) -> list[int]:
    """Real rollback - applies `down_sql` for every migration strictly
    above `target_version`, in reverse order, restoring the database to
    exactly the shape it had at `target_version`. Returns the real list
    of version numbers reverted, empty if the database was already at
    or below the target. Tested against a real temporary database in
    tests/test_migrations.py - never assumed correct from the SQL text
    alone."""
    current = _current_schema_version(conn)
    reverted: list[int] = []
    for migration in reversed(_MIGRATIONS):
        if target_version < migration.version <= current:
            conn.executescript(migration.down_sql)
            _set_schema_version(conn, migration.version - 1)
            conn.commit()
            reverted.append(migration.version)
    return reverted


def to_utc_iso8601(timestamp_ms: int) -> str:
    """Real, explicit UTC formatting for a stored unix-millisecond
    timestamp - `samples.timestamp` is always unix-epoch milliseconds
    (inherently UTC, never a local-time value), but a human-facing
    output should say so explicitly rather than leaving a bare integer
    to be misread as local time."""
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


_VALID_AGGREGATES = {"avg", "min", "max", "sum"}


class TimeSeriesStore:
    """A real sqlite3-backed time-series store. Use ``":memory:"`` for a
    real (not mocked) in-memory database in tests - same engine, same
    SQL, same correctness guarantees as the on-disk case, just not
    durable across process restarts."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        # check_same_thread=False + an explicit Lock (below) is the real
        # fix for DatalakeServer's ThreadingHTTPServer: sqlite3's default
        # thread affinity would otherwise raise on the very first
        # request handled by a thread other than the one that opened the
        # connection - caught by this project's own httptest-equivalent
        # real HTTP tests (see tests/test_api.py), not assumed away.
        # sqlite3 connections still aren't safe for concurrent use from
        # multiple threads even with that flag, so every access below
        # goes through self._lock to serialize them for real.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            migrate_up(self._conn)

    @property
    def schema_version(self) -> int:
        """The database's own real, currently-applied schema version -
        read straight from `PRAGMA user_version`, never a value this
        class tracks separately (and could drift from the real file)."""
        with self._lock:
            return _current_schema_version(self._conn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "TimeSeriesStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def insert(self, sample: Sample) -> int:
        """Writes one normalized Sample as N rows (one per field). Returns
        the number of rows written - 0 for a sample with an empty
        ``fields`` map, which is a real (if unusual) case, not an error.

        The natural identity of one stored point is ``(source_id, kind,
        field, timestamp)``. Retrying that same telemetry point replaces its
        value instead of creating another row, which is essential when a
        network client retries a POST after losing its response. The current
        telemetry contract has no per-point sequence/event ID, therefore an
        exact identity collision uses deterministic last-write-wins semantics.
        """
        if not sample.fields:
            return 0
        rows = [
            (sample.source_id, sample.kind, field, sample.timestamp, value)
            for field, value in sorted(sample.fields.items())
        ]
        with self._lock:
            for source_id, kind, field, timestamp, value in rows:
                # There is intentionally no automatic database-wide cleanup:
                # a Datalake upgrade must not silently delete historical
                # records. This scoped replacement only coalesces the exact
                # point currently being retried, inside the same lock and
                # SQLite transaction as its replacement insert.
                self._conn.execute(
                    "DELETE FROM samples WHERE source_id = ? AND kind = ? "
                    "AND field = ? AND timestamp = ?",
                    (source_id, kind, field, timestamp),
                )
                self._conn.execute(
                    "INSERT INTO samples (source_id, kind, field, timestamp, value) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (source_id, kind, field, timestamp, value),
                )
            self._conn.commit()
        return len(rows)

    def query(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        field: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 1000,
    ) -> list[Point]:
        """Real range query, oldest first. Every filter is optional and
        additive (AND) - an unfiltered query() returns everything up to
        ``limit``, a real (if expensive) thing to allow rather than
        forcing a filter that might not fit the caller's real question.

        Ties at the same timestamp are ordered by source, kind, field and
        row id, so callers receive reproducible output instead of SQLite's
        unspecified insertion order. ``limit`` must stay positive: SQLite
        interprets a negative LIMIT as unlimited, which would defeat this
        API's bounded-read contract.
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        params: list[object] = []
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if field is not None:
            clauses.append("field = ?")
            params.append(field)
        if start is not None:
            clauses.append("timestamp >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp <= ?")
            params.append(end)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT source_id, kind, field, timestamp, value FROM samples "
            f"{where} ORDER BY timestamp ASC, source_id ASC, kind ASC, field ASC, id ASC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            Point(source_id=r[0], kind=r[1], field=r[2], timestamp=r[3], value=r[4])
            for r in rows
        ]

    def aggregate(
        self,
        *,
        kind: str,
        field: str,
        bucket_ms: int,
        start: int,
        end: int,
        agg: str = "avg",
        source_id: str | None = None,
    ) -> list[Bucket]:
        """Real time-bucketed downsampling - genuine SQL aggregation, not
        a Python loop pretending to be one. ``bucket_start`` values are
        aligned to ``start`` (bucket N spans
        [start + N*bucket_ms, start + (N+1)*bucket_ms)), so bucket
        boundaries are deterministic and reproducible for the same
        ``start``/``bucket_ms`` regardless of what data happens to exist.
        """
        if agg not in _VALID_AGGREGATES:
            raise ValueError(f"unknown aggregate {agg!r}, want one of {sorted(_VALID_AGGREGATES)}")
        if bucket_ms <= 0:
            raise ValueError("bucket_ms must be positive")

        clauses = ["kind = ?", "field = ?", "timestamp >= ?", "timestamp <= ?"]
        params: list[object] = [kind, field, start, end]
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)

        where = " AND ".join(clauses)
        # `(timestamp - start) / bucket_ms` with SQLite's integer division
        # is exactly the "which bucket does this row fall into" computation.
        sql = (
            f"SELECT ((timestamp - ?) / ?) AS bucket_idx, "
            f"{agg.upper()}(value), COUNT(*) "
            f"FROM samples WHERE {where} "
            f"GROUP BY bucket_idx ORDER BY bucket_idx ASC"
        )
        with self._lock:
            rows = self._conn.execute(sql, [start, bucket_ms, *params]).fetchall()
        return [
            Bucket(bucket_start=start + int(idx) * bucket_ms, value=val, count=count)
            for idx, val, count in rows
        ]

    def sample_count(self) -> int:
        """Total rows stored - real operational visibility for /stats."""
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM samples").fetchone()
        return int(count)

    def timestamp_range(self) -> tuple[int | None, int | None]:
        """Real oldest/newest stored timestamps (unix ms), or (None,
        None) for an empty store - never (0, 0), which would be a real,
        valid timestamp (1970-01-01T00:00:00Z) and therefore a lie."""
        with self._lock:
            oldest, newest = self._conn.execute(
                "SELECT MIN(timestamp), MAX(timestamp) FROM samples"
            ).fetchone()
        return (
            int(oldest) if oldest is not None else None,
            int(newest) if newest is not None else None,
        )

    def set_retention_policy(self, *, kind: str, field: str, retention_ms: int) -> None:
        """Real, validated retention policy for one (kind, field) series
        - how long a sample may live before `apply_retention()` may
        delete it. Rejects a non-positive window outright: a retention
        policy that keeps nothing, or keeps data for a negative amount
        of time, is never a real policy, just a bug."""
        if retention_ms <= 0:
            raise ValueError(f"retention_ms must be positive, got {retention_ms}")
        with self._lock:
            self._conn.execute(
                "INSERT INTO retention_policies (kind, field, retention_ms) VALUES (?, ?, ?) "
                "ON CONFLICT (kind, field) DO UPDATE SET retention_ms = excluded.retention_ms",
                (kind, field, retention_ms),
            )
            self._conn.commit()

    def get_retention_policy(self, *, kind: str, field: str) -> int | None:
        """The real, currently-configured retention window for (kind,
        field) in ms, or None if no policy was ever set - distinct from
        0, which `set_retention_policy` never allows to be stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT retention_ms FROM retention_policies WHERE kind = ? AND field = ?",
                (kind, field),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def list_retention_policies(self) -> list[tuple[str, str, int]]:
        """Every real, currently-configured (kind, field, retention_ms)
        policy - what `apply_retention()` is about to act on."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, field, retention_ms FROM retention_policies"
            ).fetchall()
        return [(kind, field, int(retention_ms)) for kind, field, retention_ms in rows]

    def apply_retention(self, *, at_ms: int | None = None) -> int:
        """Real deletion of every sample older than its (kind, field)'s
        configured retention window, evaluated against `at_ms` (real
        wall-clock UTC time by default - see `now_ms()` below). A series
        with no configured policy is never touched: retention here is
        opt-in per (kind, field), not a global default that could
        surprise an operator who never asked for it. Returns the real
        number of rows deleted."""
        current = at_ms if at_ms is not None else now_ms()
        with self._lock:
            policies = self._conn.execute(
                "SELECT kind, field, retention_ms FROM retention_policies"
            ).fetchall()
            deleted = 0
            for kind, field, retention_ms in policies:
                cutoff = current - retention_ms
                cursor = self._conn.execute(
                    "DELETE FROM samples WHERE kind = ? AND field = ? AND timestamp < ?",
                    (kind, field, cutoff),
                )
                deleted += cursor.rowcount
            self._conn.commit()
        return deleted


def now_ms() -> int:
    """Shared clock helper - unix milliseconds, matching Sample.timestamp
    and TELEMETRY-COLLECTOR's own convention."""
    return int(time.time() * 1000)
