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
/ narrow time-series table, not one column per field. This is what lets
this store accept ANY normalized Sample from HYDRA-UMC-TELEMETRY-COLLECTOR
(whose Fields map is open-ended) without a schema migration every time a
new telemetry field shows up.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
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


_SCHEMA = """
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
"""

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
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

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
        ``fields`` map, which is a real (if unusual) case, not an error."""
        if not sample.fields:
            return 0
        rows = [
            (sample.source_id, sample.kind, field, sample.timestamp, value)
            for field, value in sample.fields.items()
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (source_id, kind, field, timestamp, value) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
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
        forcing a filter that might not fit the caller's real question."""
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
            f"{where} ORDER BY timestamp ASC LIMIT ?"
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


def now_ms() -> int:
    """Shared clock helper - unix milliseconds, matching Sample.timestamp
    and TELEMETRY-COLLECTOR's own convention."""
    return int(time.time() * 1000)
