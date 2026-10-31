# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/store.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The real time-series store - a genuine, queryable, on-disk database
(sqlite3, Python stdlib), not an in-memory placeholder that forgets
everything on restart. sqlite3 is a deliberate choice for v0: it is
already real, ACID, and ships with every Python install - reaching for
InfluxDB/TimescaleDB (both named in this project's own pyproject.toml
keywords) means standing up and operating a whole extra service, which
is a real infrastructure decision that belongs to whoever deploys this,
not something to bolt on unasked - that migration stays real, deferred
future work rather than attempted here.

Schema: one row per (source, kind, field, timestamp) reading - a "long"
/ narrow time-series table, not one column per field. Re-delivery of the
same identity is idempotent and replaces its value (last write wins), so a
collector retry cannot inflate raw queries or aggregates. This is what lets
this store accept ANY normalized Sample from HYDRA-UMC-TELEMETRY-COLLECTOR
(whose Fields map is open-ended) without a schema migration every time a
new telemetry field shows up.
"""
from __future__ import annotations

import logging
import math
import shutil
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone

from .windows import DEFAULT_STATS, parse_stats, percentile, percentile_fraction

logger = logging.getLogger("hydra_umc_datalake.downsample")

MAX_SEQUENCE = 2**53 - 1


@dataclass(frozen=True)
class Sample:
    """Mirrors HYDRA-UMC-TELEMETRY-COLLECTOR's own normalized telemetry
    shape (sourceId/kind/timestamp/fields) - the same JSON a collector
    would eventually POST here, once it has a real sink to write to."""

    source_id: str
    kind: str
    timestamp: int  # unix milliseconds
    fields: dict[str, float]
    # Optional, per-producer counter the telemetry collector already attaches to each sample. It makes two different readings of the
    # same source in the same millisecond two points instead of one overwriting the other; the same (source, kind, field, timestamp,
    # sequence) sent again (a retry) still replaces itself. 0 means "not given": the identity is then (source, kind, field, timestamp)
    # as it has always been.
    sequence: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or not 0 <= self.sequence <= MAX_SEQUENCE:
            raise ValueError(f"sequence must be a whole number from 0 to {MAX_SEQUENCE}")
        # Real, explicit gate before a reading ever reaches insert() -
        # trusting every caller to enforce this individually would let one
        # missed check silently corrupt persisted data (unlike a transient
        # bad request, a bad row written to disk stays wrong until someone
        # notices). json.loads accepts the non-standard NaN/Infinity/
        # -Infinity tokens by default, so a client CAN put one straight
        # into "fields" without ever hitting a JSON parse error - and
        # SQLite happens to store NaN as NULL (silently dropped, no
        # error - already a real if quiet degradation) but stores
        # Infinity/-Infinity as a real value that then poisons every
        # AVG/SUM/MAX aggregate() bucket touching that row, permanently,
        # since this is a persisted store, not a transient request.
        if not self.source_id:
            raise ValueError("source_id must not be empty")
        if not self.kind:
            raise ValueError("kind must not be empty")
        for field, value in self.fields.items():
            if not field:
                raise ValueError("field name must not be empty")
            if not math.isfinite(value):
                raise ValueError(f"field {field!r} must be a finite number, got {value}")


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
    Migration(
        version=3,
        up_sql="""
        CREATE INDEX IF NOT EXISTS idx_samples_kind_field_source_ts
            ON samples (kind, field, source_id, timestamp);
        """,
        down_sql="""
        DROP INDEX IF EXISTS idx_samples_kind_field_source_ts;
        """,
    ),
    Migration(
        version=4,
        # The sequence of the sample a point came from (0 when the producer sent none). The index serves the scoped replacement of a
        # retried point; it is not UNIQUE on purpose, so a database that already holds equal rows from an older version still migrates.
        up_sql="""
        ALTER TABLE samples ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_samples_identity
            ON samples (source_id, kind, field, timestamp, seq);
        """,
        down_sql="""
        DROP INDEX IF EXISTS idx_samples_identity;
        ALTER TABLE samples DROP COLUMN seq;
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

# A query is materialised in memory, as a list and then as JSON. These are the ceilings
# DatalakeServer applies by default; PRODUCTION-REPORTS asks for up to 200000 points.
MAX_QUERY_LIMIT = 200_000
MAX_AGGREGATE_BUCKETS = 100_000
MAX_SERIES = 100_000
# Optional features (off by default): the points a window statistic may load to sort them for
# percentiles, the buckets one downsampling run may roll up, and the series a report lists.
DEFAULT_MAX_WINDOW_POINTS = 1_000_000
DEFAULT_MAX_DOWNSAMPLE_BUCKETS = 10_000
REPORT_SERIES_LIMIT = 50

# Created only when the downsampling switch is on, so a database that never uses it keeps exactly the
# schema it has always had (the numbered migrations above are untouched).
_ROLLUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS downsample_policies (
    kind TEXT NOT NULL,
    field TEXT NOT NULL,
    raw_ms INTEGER NOT NULL,
    bucket_ms INTEGER NOT NULL,
    rollup_retention_ms INTEGER,
    PRIMARY KEY (kind, field)
);
CREATE TABLE IF NOT EXISTS rollups (
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    field TEXT NOT NULL,
    bucket_ms INTEGER NOT NULL,
    bucket_start INTEGER NOT NULL,
    n INTEGER NOT NULL,
    vmin REAL NOT NULL,
    vmax REAL NOT NULL,
    vsum REAL NOT NULL,
    PRIMARY KEY (source_id, kind, field, bucket_ms, bucket_start)
);
CREATE INDEX IF NOT EXISTS idx_rollups_series ON rollups (kind, field, bucket_start);
"""


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
        if str(path) != ":memory:":
            # created once here, not by the "getter" that reports the free space
            parent = Path(path).parent
            (parent if parent != Path("") else Path(".")).mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        # C13 - real gap found while auditing the code: this
        # store never reacted to real disk pressure at all, and
        # insert()'s own real sqlite3 write sat outside any try/except in
        # api.py's own _handle_ingest(), so a genuinely full disk would
        # have surfaced as an unhandled 500 (or worse) instead of a real,
        # honest, distinct response. `:memory:` has no real disk backing
        # to check - `free_disk_bytes()` is `None` for it, on purpose.
        self._path = None if str(path) == ":memory:" else Path(path)
        self._free_disk_cache: tuple[float, int] | None = None
        with self._lock:
            if self._path is not None:
                # On a database that does not exist yet, deleted rows give their space back
                # (incremental_vacuum after a retention run); this has no effect on an existing file.
                if _current_schema_version(self._conn) == 0:
                    self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
                # WAL: a long read no longer blocks a write, and with synchronous=NORMAL a commit
                # does not wait for a second fsync (a power cut can lose the last commits, never
                # corrupt the file); the sample that was acknowledged moments before it is the
                # price of not paying an fsync per sample on an SD card or eMMC.
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            migrate_up(self._conn)

    def free_disk_bytes(self) -> int | None:
        """Real free space, in bytes, on the filesystem backing this
        store's own database file - `None` for a `:memory:` store, which
        has no real disk to run out of. Checked against the real
        directory the database file lives in (not the file itself, which
        may not exist yet on a brand-new store) so this works before the
        very first real write too. The answer is reused for one second: it
        is asked before every sample, and a system call per sample buys no
        accuracy a second does not."""
        if self._path is None:
            return None
        now = time.monotonic()
        cached = self._free_disk_cache
        if cached is not None and now - cached[0] < 1.0:
            return cached[1]
        directory = self._path.parent if self._path.parent != Path("") else Path(".")
        free = shutil.disk_usage(directory).free
        self._free_disk_cache = (now, free)
        return free

    def db_size_bytes(self) -> int | None:
        """Bytes the database takes on disk (the file and its write-ahead log), or None for ``:memory:``."""
        if self._path is None:
            return None
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(str(self._path) + suffix).stat().st_size
            except OSError:
                pass
        return total

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
        field, timestamp, sequence)``. Retrying that same telemetry point replaces
        its value instead of creating another row, which is essential when a
        network client retries a POST after losing its response. A producer that
        sends no sequence (0) has the identity ``(source_id, kind, field,
        timestamp)`` and an exact collision uses deterministic last-write-wins
        semantics; one that sends a sequence keeps two different readings of the
        same millisecond as two points.
        """
        if not sample.fields:
            return 0
        rows = [
            (sample.source_id, sample.kind, field, sample.timestamp, value, sample.sequence)
            for field, value in sorted(sample.fields.items())
        ]
        with self._lock:
            try:
                for source_id, kind, field, timestamp, value, seq in rows:
                    # There is intentionally no automatic database-wide
                    # cleanup: a Datalake upgrade must not silently delete
                    # historical records. This scoped replacement only
                    # coalesces the exact point currently being retried,
                    # inside the same lock and SQLite transaction as its
                    # replacement insert.
                    self._conn.execute(
                        "DELETE FROM samples WHERE source_id = ? AND kind = ? "
                        "AND field = ? AND timestamp = ? AND seq = ?",
                        (source_id, kind, field, timestamp, seq),
                    )
                    self._conn.execute(
                        "INSERT INTO samples (source_id, kind, field, timestamp, value, seq) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (source_id, kind, field, timestamp, value, seq),
                    )
            except BaseException:
                # api.py's own handler now validates every field's type
                # before ever reaching this call, but Sample's dataclass
                # typing is not enforced at runtime - a caller reaching
                # this store directly (bypassing the HTTP layer) with a
                # bad nested value could still fail mid-loop, e.g. on
                # sqlite3's own parameter binding. Without this rollback,
                # the rows already executed before the failure stayed
                # uncommitted-but-pending on this connection's implicit
                # transaction, ready to be silently committed by some
                # later, unrelated insert() call - a real partial write,
                # just deferred to whichever call happened to commit next.
                self._conn.rollback()
                raise
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
        """The points of ``query_page`` without its continuation."""
        return self.query_page(
            source_id=source_id, kind=kind, field=field, start=start, end=end, limit=limit
        )[0]

    def query_page(
        self,
        *,
        source_id: str | None = None,
        kind: str | None = None,
        field: str | None = None,
        start: int | None = None,
        end: int | None = None,
        limit: int = 1000,
        after: tuple[int, str, str, str, int] | None = None,
        descending: bool = False,
    ) -> tuple[list[Point], tuple[int, str, str, str, int] | None]:
        """Real range query, oldest first (newest first with ``descending``), plus the cursor of the next page (None when there
        is no more). ``after`` is a cursor returned by a previous call: the query then
        continues right behind that row, so a long history is read in bounded pages.

        Every filter is optional and
        additive (AND) - Every filter is optional and
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
        if limit > MAX_QUERY_LIMIT:
            raise ValueError(f"limit must be at most {MAX_QUERY_LIMIT}")
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
        if after is not None:
            comparison = "<" if descending else ">"
            clauses.append(f"(timestamp, source_id, kind, field, id) {comparison} (?, ?, ?, ?, ?)")
            params.extend(after)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        # one more than asked tells whether there is a next page without a second query
        direction = "DESC" if descending else "ASC"
        sql = (
            f"SELECT source_id, kind, field, timestamp, value, id FROM samples "
            f"{where} ORDER BY timestamp {direction}, source_id {direction}, kind {direction}, "
            f"field {direction}, id {direction} LIMIT ?"
        )
        params.append(limit + 1)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        points = [
            Point(source_id=r[0], kind=r[1], field=r[2], timestamp=r[3], value=r[4])
            for r in rows
        ]
        next_cursor = (rows[-1][3], rows[-1][0], rows[-1][1], rows[-1][2], rows[-1][5]) if more and rows else None
        return points, next_cursor

    def history_bounds(
        self, *, source_id: str, kind: str, field: str, start: int | None, end: int | None
    ) -> tuple[Point | None, Point | None]:
        """The bounding values of a history read: the newest point strictly before ``start`` and the
        oldest point strictly after ``end`` of one series (None where there is none, or no such limit).
        A historian client draws the line into the window from them."""

        def one(operator: str, bound: int | None, order: str) -> Point | None:
            if bound is None:
                return None
            with self._lock:
                row = self._conn.execute(
                    "SELECT source_id, kind, field, timestamp, value FROM samples "
                    f"WHERE source_id = ? AND kind = ? AND field = ? AND timestamp {operator} ? "
                    f"ORDER BY timestamp {order}, id {order} LIMIT 1",
                    (source_id, kind, field, bound),
                ).fetchone()
            return Point(source_id=row[0], kind=row[1], field=row[2], timestamp=row[3], value=row[4]) if row else None

        return one("<", start, "DESC"), one(">", end, "ASC")

    def series(self, *, kind: str | None = None, limit: int = 10_000) -> list[dict[str, object]]:
        """The catalogue of what is stored: one entry per (source, kind, field) with its point count
        and its first and last timestamp, so a dashboard does not have to guess the names."""
        if limit <= 0 or limit > MAX_SERIES:
            raise ValueError(f"limit must be between 1 and {MAX_SERIES}")
        sql = "SELECT source_id, kind, field, COUNT(*), MIN(timestamp), MAX(timestamp) FROM samples"
        params: list[object] = []
        if kind is not None:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " GROUP BY source_id, kind, field ORDER BY kind, field, source_id LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {"sourceId": r[0], "kind": r[1], "field": r[2], "count": int(r[3]), "firstTimestamp": int(r[4]), "lastTimestamp": int(r[5])}
            for r in rows
        ]

    def insert_many(self, samples: list[Sample]) -> int:
        """Writes several samples in ONE transaction (one commit, one fsync): either all of them are
        stored or none. Same identity and last-write-wins rule as ``insert``."""
        rows = [
            (s.source_id, s.kind, field, s.timestamp, value, s.sequence)
            for s in samples
            for field, value in sorted(s.fields.items())
        ]
        if not rows:
            return 0
        with self._lock:
            try:
                for source_id, kind, field, timestamp, value, seq in rows:
                    self._conn.execute(
                        "DELETE FROM samples WHERE source_id = ? AND kind = ? AND field = ? AND timestamp = ? AND seq = ?",
                        (source_id, kind, field, timestamp, seq),
                    )
                    self._conn.execute(
                        "INSERT INTO samples (source_id, kind, field, timestamp, value, seq) VALUES (?, ?, ?, ?, ?, ?)",
                        (source_id, kind, field, timestamp, value, seq),
                    )
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()
        return len(rows)

    def backup(self, destination_dir: str | Path, *, keep: int = 7, now: float | None = None) -> Path:
        """A consistent copy of the database (SQLite's online backup API, safe while the service writes)
        into ``destination_dir`` as ``datalake-<UTC time>.sqlite3``; only the ``keep`` newest copies
        are kept. ``keep`` of 0 keeps them all. A ``:memory:`` store has nothing to copy."""
        if self._path is None:
            raise ValueError("an in-memory store cannot be backed up")
        if keep < 0:
            raise ValueError("keep must not be negative")
        directory = Path(destination_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = directory / f"datalake-{stamp}.sqlite3"
        counter = 1
        while target.exists():
            target = directory / f"datalake-{stamp}-{counter}.sqlite3"
            counter += 1
        partial = target.with_suffix(".partial")
        copy = sqlite3.connect(str(partial))
        try:
            with self._lock:
                self._conn.backup(copy)
        finally:
            copy.close()
        partial.replace(target)
        if keep:
            copies = sorted(directory.glob("datalake-*.sqlite3"), key=lambda p: (p.stat().st_mtime, p.name))
            for old in copies[:-keep]:
                old.unlink()
        return target

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
        if end >= start and (end - start) // bucket_ms + 1 > MAX_AGGREGATE_BUCKETS:
            raise ValueError(
                f"the range holds more than {MAX_AGGREGATE_BUCKETS} buckets: use a larger bucketMs or a shorter range"
            )

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
        of time, is never a real policy, just a bug.

        ``kind="*", field="*"`` sets the DEFAULT policy: it applies to every
        series that has no policy of its own, so a series that appears later
        (the schema is open) is not left to grow for ever. A ``*`` in only one
        of the two is refused."""
        if retention_ms <= 0:
            raise ValueError(f"retention_ms must be positive, got {retention_ms}")
        if not isinstance(kind, str) or not isinstance(field, str) or not kind or not field:
            raise ValueError("kind and field must be non-empty strings")
        if (kind == "*") != (field == "*"):
            raise ValueError("'*' is only accepted for both kind and field together (the default policy)")
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
        with no policy of its own is only touched if a default policy
        (``*``/``*``) was configured: retention is opt-in, never a global
        default that could surprise an operator who never asked for it.
        When rows were deleted, the space they took is given back to the
        disk (incremental vacuum on a database created by this version,
        and a checkpoint of the write-ahead log). Returns the real number
        of rows deleted."""
        current = at_ms if at_ms is not None else now_ms()
        with self._lock:
            policies = self._conn.execute(
                "SELECT kind, field, retention_ms FROM retention_policies"
            ).fetchall()
            deleted = 0
            default_ms: int | None = None
            for kind, field, retention_ms in policies:
                if kind == "*" and field == "*":
                    default_ms = retention_ms
                    continue
                cutoff = current - retention_ms
                cursor = self._conn.execute(
                    "DELETE FROM samples WHERE kind = ? AND field = ? AND timestamp < ?",
                    (kind, field, cutoff),
                )
                deleted += cursor.rowcount
            if default_ms is not None:
                cursor = self._conn.execute(
                    "DELETE FROM samples WHERE timestamp < ? AND NOT EXISTS ("
                    "SELECT 1 FROM retention_policies p WHERE p.kind = samples.kind AND p.field = samples.field)",
                    (current - default_ms,),
                )
                deleted += cursor.rowcount
            self._conn.commit()
            if deleted and self._path is not None:
                try:
                    # the pragma only runs while its (empty) result is fetched
                    self._conn.execute("PRAGMA incremental_vacuum").fetchall()
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass  # giving space back is best effort; the rows are already gone
        return deleted

    # ------------------------------------------------------------------
    # Optional: native window statistics (percentiles included)
    # ------------------------------------------------------------------
    def window_stats(
        self,
        *,
        kind: str,
        field: str,
        bucket_ms: int,
        start: int,
        end: int,
        stats: tuple[str, ...] = DEFAULT_STATS,
        source_id: str | None = None,
        max_points: int = DEFAULT_MAX_WINDOW_POINTS,
    ) -> list[dict[str, object]]:
        """Several statistics per fixed time window in one read: ``count``, ``min``, ``max``, ``avg``, ``sum`` and
        percentiles (``p50``, ``p95``...). Windows are aligned to ``start`` like ``aggregate`` and a window with no
        points is not returned. The simple statistics are reduced by SQL; asking for a percentile loads the points of
        the range (never more than ``max_points``, otherwise the request is refused) and sorts each window."""
        names = parse_stats(",".join(stats))
        if bucket_ms <= 0:
            raise ValueError("bucket_ms must be positive")
        if max_points <= 0:
            raise ValueError("max_points must be positive")
        if end >= start and (end - start) // bucket_ms + 1 > MAX_AGGREGATE_BUCKETS:
            raise ValueError(
                f"the range holds more than {MAX_AGGREGATE_BUCKETS} buckets: use a larger bucketMs or a shorter range"
            )
        clauses = ["kind = ?", "field = ?", "timestamp >= ?", "timestamp <= ?"]
        params: list[object] = [kind, field, start, end]
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)
        where = " AND ".join(clauses)
        percentiles = [n for n in names if n.startswith("p")]
        if not percentiles:
            sql = (
                "SELECT ((timestamp - ?) / ?) AS idx, COUNT(*), MIN(value), MAX(value), SUM(value) "
                f"FROM samples WHERE {where} GROUP BY idx ORDER BY idx ASC"
            )
            with self._lock:
                rows = self._conn.execute(sql, [start, bucket_ms, *params]).fetchall()
            out: list[dict[str, object]] = []
            for idx, count, vmin, vmax, vsum in rows:
                full = {"count": int(count), "min": vmin, "max": vmax, "sum": vsum, "avg": vsum / count}
                out.append({"bucketStart": start + int(idx) * bucket_ms, **{n: full[n] for n in names}})
            return out
        sql = (
            "SELECT ((timestamp - ?) / ?) AS idx, value FROM samples "
            f"WHERE {where} ORDER BY timestamp ASC, id ASC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, [start, bucket_ms, *params, max_points + 1]).fetchall()
        if len(rows) > max_points:
            raise ValueError(
                f"the range holds more than {max_points} points: percentiles need a shorter range or a source filter"
            )
        grouped: dict[int, list[float]] = {}
        for idx, value in rows:
            grouped.setdefault(int(idx), []).append(float(value))
        result: list[dict[str, object]] = []
        for idx in sorted(grouped):
            values = sorted(grouped[idx])
            total = math.fsum(values)
            full = {"count": len(values), "min": values[0], "max": values[-1], "sum": total, "avg": total / len(values)}
            entry: dict[str, object] = {"bucketStart": start + idx * bucket_ms}
            for name in names:
                entry[name] = percentile(values, percentile_fraction(name)) if name.startswith("p") else full[name]
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # Optional: downsampling (raw points for N time, rollups after)
    # ------------------------------------------------------------------
    def ensure_rollup_tables(self) -> None:
        """Creates the policy and rollup tables (idempotent). Called only when the downsampling switch is on."""
        with self._lock:
            self._conn.executescript(_ROLLUP_SCHEMA)
            self._conn.commit()

    def set_downsample_policy(
        self, *, kind: str, field: str, raw_ms: int, bucket_ms: int, rollup_retention_ms: int | None = None
    ) -> None:
        """Keep the raw points of (kind, field) for ``raw_ms``; older ones are summarised into rollups of
        ``bucket_ms`` (count, min, max, sum per source and bucket) and removed. ``rollup_retention_ms`` bounds how
        long the rollups live (None: for ever). ``*``/``*`` is the default policy for series with none of their own."""
        if not isinstance(kind, str) or not isinstance(field, str) or not kind or not field:
            raise ValueError("kind and field must be non-empty strings")
        if (kind == "*") != (field == "*"):
            raise ValueError("'*' is only accepted for both kind and field together (the default policy)")
        if raw_ms <= 0 or bucket_ms <= 0:
            raise ValueError("raw_ms and bucket_ms must be positive")
        if rollup_retention_ms is not None and rollup_retention_ms <= 0:
            raise ValueError("rollup_retention_ms must be positive when given")
        self.ensure_rollup_tables()
        with self._lock:
            self._conn.execute(
                "INSERT INTO downsample_policies (kind, field, raw_ms, bucket_ms, rollup_retention_ms) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (kind, field) DO UPDATE SET raw_ms = excluded.raw_ms, bucket_ms = excluded.bucket_ms, "
                "rollup_retention_ms = excluded.rollup_retention_ms",
                (kind, field, raw_ms, bucket_ms, rollup_retention_ms),
            )
            self._conn.commit()

    def remove_downsample_policy(self, *, kind: str, field: str) -> bool:
        """Removes a policy (its rollups stay). True when there was one."""
        self.ensure_rollup_tables()
        with self._lock:
            cursor = self._conn.execute("DELETE FROM downsample_policies WHERE kind = ? AND field = ?", (kind, field))
            self._conn.commit()
        return cursor.rowcount > 0

    def list_downsample_policies(self) -> list[dict[str, object]]:
        self.ensure_rollup_tables()
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, field, raw_ms, bucket_ms, rollup_retention_ms FROM downsample_policies ORDER BY kind, field"
            ).fetchall()
        return [
            {"kind": k, "field": f, "rawMs": int(r), "bucketMs": int(b), "rollupRetentionMs": int(x) if x is not None else None}
            for k, f, r, b, x in rows
        ]

    def read_rollups(
        self,
        *,
        kind: str,
        field: str,
        source_id: str | None = None,
        start: int | None = None,
        end: int | None = None,
        bucket_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, object]]:
        """The rollup buckets of one (kind, field), oldest first, with ``bucketStart`` in [start, end]."""
        if limit <= 0 or limit > MAX_QUERY_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
        self.ensure_rollup_tables()
        clauses = ["kind = ?", "field = ?"]
        params: list[object] = [kind, field]
        for column, operator, value in (
            ("source_id", "=", source_id),
            ("bucket_start", ">=", start),
            ("bucket_start", "<=", end),
            ("bucket_ms", "=", bucket_ms),
        ):
            if value is not None:
                clauses.append(f"{column} {operator} ?")
                params.append(value)
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, bucket_ms, bucket_start, n, vmin, vmax, vsum FROM rollups "
                f"WHERE {' AND '.join(clauses)} ORDER BY bucket_start ASC, source_id ASC, bucket_ms ASC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return [
            {"sourceId": s, "bucketMs": int(b), "bucketStart": int(t), "count": int(n), "min": lo, "max": hi, "sum": total, "avg": total / n}
            for s, b, t, n, lo, hi, total in rows
        ]

    def apply_downsample(
        self,
        *,
        at_ms: int | None = None,
        dry_run: bool = True,
        max_buckets: int = DEFAULT_MAX_DOWNSAMPLE_BUCKETS,
    ) -> dict[str, object]:
        """One downsampling run at ``at_ms`` (the real clock by default). Only buckets that lie entirely before
        ``at_ms - raw_ms`` are touched: their raw points are summed into the rollup (merged with an existing
        one, so a late point is counted too) and deleted in the same transaction, so a crash loses nothing and
        a second run changes nothing. At most ``max_buckets`` buckets are handled per run (``truncated`` says
        more remain). With ``dry_run`` nothing is written; the report and the log say what would be removed."""
        if max_buckets <= 0:
            raise ValueError("max_buckets must be positive")
        current = at_ms if at_ms is not None else now_ms()
        self.ensure_rollup_tables()
        report: dict[str, object] = {}
        series_totals: dict[tuple[str, str, str], list[int]] = {}
        warnings: list[str] = []
        buckets_done = rows_removed = rollups_expired = 0
        truncated = False
        verb = "would remove" if dry_run else "removed"
        with self._lock:
            policies = self._conn.execute(
                "SELECT kind, field, raw_ms, bucket_ms, rollup_retention_ms FROM downsample_policies ORDER BY kind, field"
            ).fetchall()
            retention = {(k, f): int(r) for k, f, r in self._conn.execute("SELECT kind, field, retention_ms FROM retention_policies")}
            budget = max_buckets
            try:
                for kind, field, raw_ms, bucket_ms, rollup_retention_ms in policies:
                    is_default = kind == "*" and field == "*"
                    cutoff = current - raw_ms
                    limit_ts = cutoff - (cutoff % bucket_ms)  # buckets that end at or before the cutoff
                    scope = "NOT EXISTS (SELECT 1 FROM downsample_policies p WHERE p.kind = samples.kind AND p.field = samples.field)"
                    scope_params: list[object] = []
                    if not is_default:
                        scope, scope_params = "kind = ? AND field = ?", [kind, field]
                    held = retention.get((kind, field), retention.get(("*", "*")))
                    if held is not None and held < raw_ms:
                        warnings.append(
                            f"{kind}/{field}: the retention of {held} ms is shorter than rawMs ({raw_ms} ms), "
                            "so raw points are deleted before they are rolled up"
                        )
                    groups = self._conn.execute(
                        "SELECT source_id, kind, field, timestamp - ((timestamp % ?) + ?) % ? AS bs, COUNT(*), MIN(value), MAX(value), SUM(value) "
                        f"FROM samples WHERE {scope} AND timestamp < ? "
                        "GROUP BY source_id, kind, field, bs ORDER BY bs ASC, source_id ASC, kind ASC, field ASC LIMIT ?",
                        [bucket_ms, bucket_ms, bucket_ms, *scope_params, limit_ts, budget + 1],
                    ).fetchall()
                    if len(groups) > budget:
                        truncated = True
                        groups = groups[:budget]
                    budget -= len(groups)
                    for source_id, g_kind, g_field, bucket_start, count, vmin, vmax, vsum in groups:
                        if not dry_run:
                            self._conn.execute(
                                "INSERT INTO rollups (source_id, kind, field, bucket_ms, bucket_start, n, vmin, vmax, vsum) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (source_id, kind, field, bucket_ms, bucket_start) "
                                "DO UPDATE SET n = n + excluded.n, vmin = MIN(vmin, excluded.vmin), "
                                "vmax = MAX(vmax, excluded.vmax), vsum = vsum + excluded.vsum",
                                (source_id, g_kind, g_field, bucket_ms, bucket_start, count, vmin, vmax, vsum),
                            )
                            deleted = self._conn.execute(
                                "DELETE FROM samples WHERE source_id = ? AND kind = ? AND field = ? AND timestamp >= ? AND timestamp < ?",
                                (source_id, g_kind, g_field, bucket_start, bucket_start + bucket_ms),
                            ).rowcount
                            if deleted != count:  # cannot happen under the lock; never leave a half-summed bucket
                                raise sqlite3.IntegrityError("raw rows changed while they were rolled up")
                        buckets_done += 1
                        rows_removed += int(count)
                        tally = series_totals.setdefault((source_id, g_kind, g_field), [0, 0])
                        tally[0] += 1
                        tally[1] += int(count)
                    if rollup_retention_ms is not None:
                        expiry = current - rollup_retention_ms
                        if is_default:
                            expire_scope = "NOT EXISTS (SELECT 1 FROM downsample_policies p WHERE p.kind = rollups.kind AND p.field = rollups.field)"
                            expire_params: list[object] = []
                        else:
                            expire_scope, expire_params = "kind = ? AND field = ?", [kind, field]
                        condition = f"{expire_scope} AND bucket_start + bucket_ms <= ?"
                        if dry_run:
                            (expired,) = self._conn.execute(
                                f"SELECT COUNT(*) FROM rollups WHERE {condition}", [*expire_params, expiry]
                            ).fetchone()
                        else:
                            expired = self._conn.execute(f"DELETE FROM rollups WHERE {condition}", [*expire_params, expiry]).rowcount
                        rollups_expired += int(expired)
                if dry_run:
                    self._conn.rollback()
                else:
                    self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
            if not dry_run and rows_removed and self._path is not None:
                try:
                    self._conn.execute("PRAGMA incremental_vacuum").fetchall()
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
        ordered = sorted(series_totals.items())
        for (source_id, kind, field), (n_buckets, n_rows) in ordered:
            logger.info("downsample: %s %s/%s/%s: %d raw point(s) in %d bucket(s)", verb, source_id, kind, field, n_rows, n_buckets)
        for message in warnings:
            logger.warning("downsample: %s", message)
        logger.info(
            "downsample %s: %d bucket(s), %d raw point(s), %d expired rollup(s)%s",
            "dry run" if dry_run else "run", buckets_done, rows_removed, rollups_expired, ", more remain" if truncated else "",
        )
        report.update(
            {
                "dryRun": dry_run,
                "atMs": current,
                "buckets": buckets_done,
                "rawPoints": rows_removed,
                "expiredRollups": rollups_expired,
                "truncated": truncated,
                "series": [
                    {"sourceId": s, "kind": k, "field": f, "buckets": b, "rawPoints": r}
                    for (s, k, f), (b, r) in ordered[:REPORT_SERIES_LIMIT]
                ],
                "warnings": warnings,
            }
        )
        return report


def now_ms() -> int:
    """Shared clock helper - unix milliseconds, matching Sample.timestamp
    and TELEMETRY-COLLECTOR's own convention."""
    return int(time.time() * 1000)
