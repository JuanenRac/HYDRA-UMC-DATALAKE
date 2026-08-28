# Changelog

All notable work on **HYDRA-UMC-DATALAKE** is summarized here, newest first. Full
session-by-session detail (including dates) lives in a private,
unpublished internal log - this file is public, so it intentionally
omits calendar dates.

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

## Documentation - Real HTTP API reference

- **`docs/API.md`** (new) - every real endpoint (`POST /ingest`,
  `GET /query`, `GET /aggregate`, `GET /stats`) documented from the actual
  handler code in `api.py`: request/response bodies, query parameters,
  status codes, and precisely what `sampleCount` counts (per `store.py`'s
  `sample_count()` - total rows, not request count). Verified live against
  a real running server. Documentation-only - no code changed, no version
  bump.

---

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
- What's still not real, on purpose - see `mejoras_futuras.txt`: a real
  InfluxDB/TimescaleDB backend (both named in this project's own
  badges/keywords) - that's a real infrastructure decision for whoever
  deploys this, not bolted on unasked; retention/downsampling policies;
  and wiring `docker-compose.yml` to a real deployment.

## [0.0.1] - Initial scaffolding

- **`src/hydra_umc_datalake/main.py`** - minimal real entry point. No ingestion logic yet - the InfluxDB/TimescaleDB-backed time-series pipeline for industrial robotic data lands in a later pass.
- **`pyproject.toml`** - packaging metadata, no runtime dependencies yet.
- **`bump_version.py`** - ecosystem-standard odometer bump script.
- **`build.sh` / `build.bat`**, **`run.sh` / `run.bat`** - venv creation, editable install, compile-check, and entry-point execution.
