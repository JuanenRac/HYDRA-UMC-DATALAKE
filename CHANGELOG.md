# Changelog

All notable public work on **HYDRA-UMC-DATALAKE** is summarized here, newest
first. This changelog intentionally omits calendar dates and internal
work-session detail.

## Versioning scheme

`pyproject.toml`'s `version` field bumps automatically on every real
build (`build.sh`/`.bat` - see `bump_version.py`, run as the first real
step of both scripts).

It follows the ecosystem-wide base-10 "odometer" rule rather than
semantic-versioning judgment calls:

- `PATCH` +1 on every build
- when `PATCH` would exceed 9, it resets to 0 and `MINOR` +1 instead (e.g. `0.0.9` -> `0.1.0`, never `0.0.10`)
- the same carry cascades into `MAJOR` if `MINOR` would exceed 9

---

## [Unreleased]

- **Bounded HTTP ingestion:** JSON request bodies are now limited to 1,048,576
  bytes by default and each connected client has a 15-second request-body
  timeout. Oversized bodies receive HTTP 413; timed-out bodies receive HTTP
  408. Limits are validated at `DatalakeServer` construction and covered by
  real loopback HTTP tests.
- **Idempotent ingest retries:** exact normalized point identities
  (`sourceId`, `kind`, `field`, `timestamp`) now coalesce with explicit
  last-write-wins semantics, so a retry after a lost response cannot inflate
  stored rows, queries, or aggregates.
- **Deterministic and bounded raw queries:** `GET /query` now uses stable
  tie-breakers after the timestamp and rejects zero or negative `limit`
  values instead of delegating SQLite's special negative-limit behaviour.
- **HTTP contract and metadata correction:** `docs/API.md`, the public
  project metadata, and all README languages now describe the real
  SQLite-backed implementation instead of requiring an unimplemented external
  database deployment.

---

## Documentation - Real HTTP API reference

- **`docs/API.md`** (new) - every real endpoint (`POST /ingest`,
  `GET /query`, `GET /aggregate`, `GET /stats`) documented from the actual
  handler code in `api.py`: request/response bodies, query parameters,
  status codes, and precisely what `sampleCount` counts (per `store.py`'s
  `sample_count()` - total rows, not request count). Verified live against
  a real running server. Documentation-only - no code changed, no version
  bump.

---

## [0.0.8]

- **Fixed a real, intermittent connection reset on oversized requests** -
  found by an ecosystem-wide bug audit. Rejecting a request over the
  configured body-size limit closed the connection without ever reading any
  of the declared body; once that body was larger than the OS socket
  buffer, the client's own in-flight write got cut off and it saw a raw
  connection-reset error instead of the intended clean `413`. Flaky by
  nature - it depended on how much the kernel had already buffered before
  the handler responded. Fixed by draining a bounded amount of the
  oversized body first, so the client always finishes sending before the
  response goes out. Same pattern and same fix as this family's
  ANOMALY-DETECTOR `api.py`.

---

## [0.0.7]

- **`--addr` now defaults to `127.0.0.1`, not `0.0.0.0`** - found by an
  ecosystem-wide bug audit: this server has no authentication on any
  endpoint (`POST /ingest` accepts and persists any telemetry reading
  from anyone who can reach it), and the old default bound to every
  interface. The real CM5's own systemd unit already passed
  `--addr 127.0.0.1` explicitly, matching every other internal-only API
  here (Anomaly-Detector, Job-Dispatcher, Telemetry-Collector) - no real
  deployment's behavior changes, but running this tool bare (no systemd
  unit, a developer testing it locally) is now safe by default instead
  of silently wide open. `docs/API.md` updated to match, with an
  explicit warning on `--addr 0.0.0.0`.

## [0.0.6] - Reject empty identifiers and non-finite field values before they reach disk

- **`Sample.__post_init__`** now rejects an empty `sourceId`/`kind`, an
  empty field name, and any field value that is not finite (`NaN`,
  `Infinity`, `-Infinity`). Python's `json.loads` accepts the
  non-standard `NaN`/`Infinity`/`-Infinity` tokens by default, so a
  client could put one straight into `POST /ingest`'s `"fields"` without
  ever hitting a JSON parse error. SQLite quietly stores `NaN` as `NULL`
  (silently dropped, no error - already a real if quiet degradation) but
  stores `Infinity`/`-Infinity` as a real value, which then poisons
  `AVG`/`SUM`/`MAX` on every `aggregate()` bucket touching that row -
  permanently, since this is a persisted store, not a transient request
  a retry can correct.
- Verified with 5 new tests across `test_store.py` (including a direct
  `aggregate()` proof that a rejected `Infinity` reading never reaches
  the bucket it would have poisoned) and a real end-to-end
  `test_api.py` HTTP round-trip - `pytest` (45 passed) and
  `tools/ci_validate.py`.

## [0.0.5] - Fixed a real version-mirror drift

- **`src/hydra_umc_datalake/__init__.py`**'s `__version__` had fallen one
  real build behind `pyproject.toml`/the manifest - running only
  `bump_manifest_version.py` (which only touches its declared
  `native_version.file`, pyproject.toml) without this repo's separate
  `bump_version.py` (the one that keeps `__init__.py` mirrored) leaves
  the two drifting apart. Fixed via the real, intended sequence
  (`bump_version.py` then `bump_manifest_version.py --sync`).

## [0.0.4] - Real ecosystem live-status opt-in

- **`hydra-umc.project.json`** declares its real `service.port` (8095)
  and `health_path` (`/stats`) - HYDRA-UMC-SERVER's ecosystem status
  endpoint now does a real HTTP GET against it (expecting 2xx) instead
  of only reporting static manifest metadata.

## [0.0.3] - Real schema versioning, reversible migrations, UTC timestamps, retention

- **Real, reversible schema migrations** (`store.py`'s `Migration`/`migrate_up()`/`migrate_down()`, new) - tracked via SQLite's own built-in `PRAGMA user_version` rather than a hand-rolled bookkeeping table. The existing schema became migration 1; a new `retention_policies` table is migration 2. `TimeSeriesStore.schema_version` exposes the real, currently-applied version. Proven reversible against a real temporary database (both `:memory:` and an on-disk tempfile) in `tests/test_migrations.py` - migrating down removes exactly what its migration added and never touches unrelated real data, and re-migrating up restores the schema exactly.
- **Real, explicit UTC timestamp formatting** (`to_utc_iso8601()`, new) - `samples.timestamp` was always unix-epoch milliseconds (inherently UTC), but nothing surfaced that explicitly; a new, additive `GET /stats/range` endpoint reports the real oldest/newest stored timestamps as both raw ms and explicit UTC ISO 8601 strings (`null`, never `0`, for an empty store). A real test also proves `now_ms()` genuinely reflects UTC wall-clock time, not local time.
- **Real, validated retention** (`set_retention_policy()`/`get_retention_policy()`/`list_retention_policies()`/`apply_retention()`, new) - a per-`(kind, field)` retention window (rejects a non-positive window outright), opt-in only (a series with no configured policy is never touched), and real deletion of samples older than their configured cutoff. Exposed via new, additive `GET /retention`, `POST /retention`, and `POST /retention/apply` endpoints - the existing `/stats`/`/query`/`/aggregate`/`/ingest` routes are unchanged.
- 20 new tests (`test_migrations.py` new, plus additions to `test_store.py`/`test_api.py`) = 36 total.
- Real verification beyond the test suite: ran a real `DatalakeServer` end-to-end - ingested an old and a recent real sample, configured a 5-second retention window, applied it, and confirmed exactly the old sample was deleted while the recent one survived.

## [0.0.2] - Real time-series store: sqlite3-backed ingest/query/aggregate + HTTP API

- **`src/hydra_umc_datalake/store.py`** - `TimeSeriesStore`, a real
  sqlite3-backed time-series database: `insert()` writes one row per
  telemetry field (a narrow "long" schema so any new field
  HYDRA-UMC-TELEMETRY-COLLECTOR's `Sample.Fields` reports needs no schema
  migration), `query()` is a real filtered range read, `aggregate()` does
  genuine SQL time-bucketed downsampling (avg/min/max/sum) with
  deterministic, `start`-aligned bucket boundaries. Thread-safe: a real
  bug was found and fixed via this project's own tests - `ThreadingHTTPServer`
  calling into one shared sqlite3 connection from multiple threads
  raised `sqlite3.ProgrammingError` until `check_same_thread=False` plus
  an explicit `threading.Lock` around every access were added.
- **`src/hydra_umc_datalake/api.py`** - plain JSON/HTTP surface (stdlib
  `http.server`, no framework): `POST /ingest`, `GET /query`,
  `GET /aggregate`, `GET /stats`.
- **`src/hydra_umc_datalake/main.py`** - now wires the store to the API
  and starts a real `ThreadingHTTPServer`, instead of only printing
  identity and exiting.
- Added `pytest` as a real dev dependency (`pyproject.toml`'s
  `[project.optional-dependencies].dev`) - `build.sh`/`build.bat` now
  install it and run the real test suite as part of a normal build.
- Verified for real: 14 `pytest` cases, including a hand-checkable
  aggregation-bucketing test (values chosen so the correct averages are
  computable by hand, not just asserted against the code's own output)
  and real HTTP round-trips against a genuine `DatalakeServer` bound to
  an ephemeral loopback port in a background thread (the same standard
  as this session's Go projects' `httptest` and Rust ones' compiled
  binaries). Additionally smoke-tested the installed CLI entry point
  end-to-end with real `curl` requests.
- A future external InfluxDB/TimescaleDB backend and a production
  `docker-compose.yml` deployment still require real integration design; they
  are not represented as current functionality.

## [0.0.1] - Initial scaffolding

- **`src/hydra_umc_datalake/main.py`** - initial service entry point, since
  extended into the current SQLite-backed time-series HTTP API.
- **`pyproject.toml`** - packaging metadata, no runtime dependencies yet.
- **`bump_version.py`** - ecosystem-standard odometer bump script.
- **`build.sh` / `build.bat`**, **`run.sh` / `run.bat`** - venv creation, editable install, compile-check, and entry-point execution.
