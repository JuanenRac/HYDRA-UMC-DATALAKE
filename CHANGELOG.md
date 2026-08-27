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
