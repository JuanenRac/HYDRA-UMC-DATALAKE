# Changelog

All notable changes to this project are documented here.

## [Unreleased]

- **README in the universal layout:** the README and its seven translations follow the layout that the four families share (22 sections in a fixed order), with an architecture picture and a repository-structure picture drawn from `docs/architecture.json` and `docs/structure.json`; the CI validator checks both pictures and the file tree. Where the repository has a service or an MQTT transport, the README now says how to start it and which environment variables it reads.

- **The layout of the README is checked in the CI:** the validator runs `tools/universal_readme_check.py` and lists, as a warning, what differs from the universal layout of the README.

## [0.1.9] - Two readings of the same millisecond are two points

- The compose file gives the anomaly detector the token it needs (`ANOMALY_TOKEN`): the detector refuses to listen outside loopback without one, so the service used to exit at start inside the stack. It also keeps one baseline per series in the volume, and the READMEs list the three tokens the stack asks for.
- **Two readings of the same millisecond are two points.** `POST /ingest` and `/ingest/batch` take the optional `sequence` that the telemetry collector already sends, and it joins the identity of a point (`sourceId, kind, field, timestamp, sequence`); a retry of the same sequence still replaces itself, and a producer with no sequence behaves as before. Schema migration 4 adds the `seq` column (0 for everything already stored, with an index; reversible).
- A long history is read in pages: when a query has more points than its `limit`, the response carries an `X-Next-Cursor` header and the `cursor` parameter continues from there; the body stays a plain array.
- Queries and aggregates accept `before`, an exclusive end, so consecutive windows (shifts, days) never count their shared boundary point twice; `end` stays inclusive and sending both is refused.
- `GET /series` lists what is stored (source, kind, field, count, first and last timestamp), so dashboards do not have to guess names.
- `POST /ingest/batch` stores up to `--max-batch-samples` samples (default 1000) in one request and one transaction, all or nothing.
- `hydra-umc-datalake backup --dest <dir> --keep <n>` writes a consistent copy of the database with SQLite's online backup and keeps the newest copies.
- Building no longer changes the version; raising it is the explicit `bump` command (`python tools/hydra_project_tool.py bump`). The build scripts only report the version they built, and CI fails if a build script runs a bump.
- The build and run scripts use the Python of the `.venv` directly instead of an activate script, and create the venv with the `py` launcher when there is one; CI fails if a script activates a venv.
- The banner follows the animated style of the ecosystem banners, with the name, the kind and the technologies of the project.
- Added `tools/hydra_project_tool.py`: the same `validate`, `test`, `version`, `status` and `bump` commands in every repository. The checks run with `HYDRA_UMC_CI=1` and fail if the version changes; only `bump` changes it.

## [0.1.8]

- Maturity is functional: nothing here has been verified on real hardware yet.
- The READMEs say, in the eight languages, what is planned and what has only been tested against fakes, emulators or fixtures, and carry a short safety and security status note.

## [0.1.7]

- Build version synchronized with `hydra-umc.project.json` and the repository-native version source.
- The licence is written as GPL-3.0-or-later in every header and in the package metadata.

## [0.1.6] - Tokens, bounded reads, plausible timestamps, a retention default, WAL

- **Access control.** Nothing in the API had authentication, and `POST /retention` followed by `POST /retention/apply` deleted the whole history of anyone who could reach the port (in the compose stack that is every container). There are now three roles with optional bearer tokens: `ingest` (`POST /ingest`), `admin` (`POST /retention`, `POST /retention/apply`) and `read`, from `HYDRA_UMC_DATALAKE_TOKEN` (one token for all) or one variable per role. Listening beyond loopback is refused unless the `ingest` and `admin` tokens are set (or `--allow-anonymous`). The compose file requires `DATALAKE_TOKEN` and gives it to the collector, the retention systemd unit sends the administration token, and `GET /health` is open for healthchecks.
- **Bounded reads.** `limit` has a ceiling of 200000 and an aggregate may span at most 100000 buckets (a bigger request used to build the whole table in memory and could be killed by the memory limit of the unit); numbers beyond 64 bits and values of the wrong type in a query or a retention policy are a `400` instead of an exception that left the client without an answer.
- **Plausible timestamps.** A timestamp in seconds (January 1970 and invisible to every dashboard, deleted at once by a retention run), a negative one, and one more than 5 minutes ahead of the clock (`--max-future-skew-seconds`) are refused with `400`.
- **Retention that cannot be forgotten.** A default policy (`kind` and `field` both `*`, or `--default-retention-days`) covers every series without a policy of its own; deleting rows now gives the space back to the disk (incremental vacuum on a database created by this version, WAL checkpoint), and `GET /stats` reports `dbBytes` and `freeDiskBytes`. No default is configured unless asked for.
- **Throughput.** The database runs in WAL mode with `synchronous=NORMAL`, connections are kept alive (HTTP/1.1, a chunked body is a `411`), the free-space check is cached for a second and no longer creates a directory, and a new index serves aggregates by source.
- `docs/API.md` states that `end` is inclusive (a half-open window must ask for `end - 1`), the limits, the roles and the same-millisecond identity caveat; the README no longer says the collector feeds the datalake from the Server and counts 94 tests.

## [0.1.5] - Non-object JSON bodies are a 400 on the ingest routes

- **A non-object JSON body crashed the ingest handler:** `POST /ingest` read the body with `json.loads()` and called `.get()` on it, so valid JSON that is not an object (a list, a string, `null`, a number, a boolean) raised an `AttributeError` nothing caught - the handler thread died with a traceback and the client got no response at all, on the main write route of the whole data pipeline. Every body that is not a JSON object is now a `400` ("the JSON body must be an object") on all the POST routes, and the server keeps answering afterwards (confirmed against the real server with five body shapes on `/ingest` and `/retention`).
- **Tests:** a new test covers the five non-object bodies on both routes and that `/stats` still answers afterwards (68 tests); the README count is corrected.

## [0.1.4] - A docker-compose.yml that starts the pipeline, Portuguese README

- **A compose file that starts the pipeline:** the old `docker-compose.yml` could not start anything useful - it set a `HYDRA_DATALAKE_STORAGE=timescaledb` variable nothing reads, published a placeholder port, ran every service with no `command` (the entry points print their identity and exit when invoked bare), built the Go collector from the repository root although its module lives in `src/`, and left each API listening on its container loopback, unreachable from the others. It is rewritten as four real long-running services (`datalake`, `telemetry-collector`, `anomaly-detector`, `production-reports`), each with an explicit command, healthchecks and `depends_on: service_healthy`, a non-root user, named volumes for the database and the fitted baseline, and every published port bound to `127.0.0.1` only. The collector requires `TELEMETRY_COLLECTOR_TOKEN` (`${VAR:?message}`, at least 16 characters) for its ingest routes.
- **Checked without Docker:** there is no Docker in this environment, so the images were not built; the same four commands were run directly as one stack (collector with a token, datalake, anomaly detector with a baseline file, production reports) and a sample was followed end to end (rejected without the token, accepted with it, flushed to the datalake and read back, and every service answered its health route). `tools/verify_compose.py` now checks the file's structure in CI - an explicit command per service, no floating image tag, loopback-only ports, required tokens, a healthcheck behind every `service_healthy` wait, a non-root user - and was shown to reject five deliberately broken variants.
- **Docs:** the README describes the new compose file and says plainly what was and was not verified, and a README in Portuguese (`README_por.md`) joins the other translations.

## [0.1.3] - Real sustained-session coverage for retention

- New `test_sustained_ingest_and_retention_over_many_rounds_never_leaks_or_over_deletes`:
  100 simulated rounds of insert-then-`apply_retention()`, checked after
  every single round - every other retention test in this repo only ever
  exercised one round. Proves the per-round deletion never drifts, never
  leaves a stale row behind, never deletes the sample just written, and
  that `sample_count()` stays bounded by the retention window instead of
  growing with the full history of a long session. Confirmed to fail
  (a real stale sample surviving) against a deliberately widened cutoff,
  restored before committing.
- 67/67 tests pass (`tests/test_store.py`, `tests/test_migrations.py`,
  `tests/test_api.py`), up from 66 - synced across all 7 README languages.

## [0.1.2] - a boolean or fractional timestamp/retention window was silently coerced

`int(body["timestamp"])` and `int(body["retentionMs"])` both admitted a
bare `bool` (a subclass of `int` in Python, so `int(True)` silently
succeeds as `1`) and silently truncated a non-integer float
(`int(1000.9)` == `1000`) instead of rejecting either - the same class of
gap already closed for `fields` values in the same handler. A new shared
`_validate_real_int()` helper rejects both cases with a controlled 400
instead of storing a value nobody actually sent. Test count: 61 -> 66
(5 new: boolean/fractional timestamp, a whole-number float timestamp
still accepted, boolean/fractional retention window).

## [0.1.1] - honesty check section in every README

Added a "Honesty check" paragraph right after the badges in `README.md`
and all 6 translated READMEs, naming the real modules
(`store.py`/`api.py`/`main.py`), the real test count (61 passing:
`tests/test_store.py`, `tests/test_migrations.py`, `tests/test_api.py`),
and stating plainly what is not built yet - an external
InfluxDB/TimescaleDB backend remains a documented future option with no
code behind it, and the `docker-compose.yml` integration with the 3
sibling repos has never been exercised as one running stack from this
repo's own test suite. Documents the real, current state of what's
implemented vs. planned; no behavior changed.

## [0.1.0] - real disk-pressure handling - refused before I/O, not an unhandled crash

This store never reacted to real disk pressure at all, and `insert()`'s
own real sqlite3 write sat outside any `try`/`except` in `_handle_ingest()`
- a genuinely full disk would have surfaced as an unhandled 500 (or
worse) instead of a real, honest, distinct response.

New `TimeSeriesStore.free_disk_bytes()` (real `shutil.disk_usage()` on
the directory backing the database file - `None` for a `:memory:` store,
which has no real disk to run out of). `DatalakeServer` gains a real
`min_free_disk_bytes` floor (64MB default - real headroom for sqlite's
own journal/WAL growth during a single write before it too runs out of
room) checked BEFORE every `POST /ingest` attempts a write, refusing
with a real `507 Insufficient Storage` rather than reactively. A genuine
`sqlite3.OperationalError` during the write itself (the proactive check
racing a real disk fill from something else) is now also caught as the
same real 507, never an unhandled crash.

Verified: full pytest suite (61/61, 8 new - `:memory:` exemption, a real
on-disk free-space check against `shutil.disk_usage()` directly, the
507 gate itself both ways, and the sqlite3 disk-full defense-in-depth
path), `tools/ci_validate.py` PASS.

## [0.0.9] - reject malformed ingest bodies at the real boundary

- `POST /ingest` reached `.items` on a `"fields"` value that
  was never validated as an object - `"fields": []` (or `null`) raised
  an uncaught `AttributeError`, outside this handler's own controlled
  400 contract. Fixed: `fields` must now be a real JSON object, every
  field value must be a real number (a boolean - a subclass of `int`
  in Python - is now explicitly rejected instead of silently stored as
  `1.0`/`0.0`), `sourceId`/`kind` must both be real strings (a nested
  object used to reach `sqlite3`'s own parameter binding inside
  `store.insert()` and fail there instead), and `timestamp` must fall
  within the real range `to_utc_iso8601()` can convert - an
  out-of-range value used to be accepted and stored silently, only
  failing later at `/stats/range` (report/query time), a deferred
  crash on read rather than a controlled 400 at write time.
- **`store.py`'s `insert()`** now rolls back on a mid-batch failure
  instead of leaving the already-executed (but uncommitted) rows of
  that same call pending on the connection's implicit transaction,
  where a later, unrelated `insert()` call could have silently
  committed them - a real, if narrower, partial-write path independent
  of the HTTP-layer validation above (a caller could always reach the
  store directly).
- removed the two remaining references to
  an internal planning file with no public equivalent
  (`main.py`, `store.py`) - the reasoning they supported
  (sqlite3 over InfluxDB/TimescaleDB being a real, deferred deployment
  decision) is now stated in place instead of pointing at a document
  outside this repository.
- 6 new regression tests (54 total) - explicit end-to-end and store-level
  coverage for each malformed-body case above. `python -m pytest
  tests/ -v`: all passing.
- **New `systemd/hydra-umc-datalake-retention.service`/`.timer`** - found on code review: `apply_retention()`
  is real and tested but only ever fired via a manual
  `POST /retention/apply` - no scheduler called it, so a configured
  per-series retention policy never actually applied itself in
  production. The new timer runs the oneshot service daily (with
  `Persistent=true`, so a CM5 powered off at the scheduled time still
  catches up), which calls the real, already-running service's own
  endpoint over loopback (`curl`) rather than touching the sqlite3 file
  directly. Both units are optional, left for the operator to enable
  after review, same policy the main service already follows - wired
  into `HYDRA-UMC-OS`'s own `install_datalake.sh`.
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

## [0.0.8]

- **Fixed a real, intermittent connection reset on oversized requests** -
  found on code review. Rejecting a request over the
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

- **`--addr` now defaults to `127.0.0.1`, not `0.0.0.0`** - found on code review: this server has no authentication on any
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
- **`bump_version.py`** - ecosystem-standard bump script.
- **`build.sh` / `build.bat`**, **`run.sh` / `run.bat`** - venv creation, editable install, compile-check, and entry-point execution.

## Earlier history

### Documentation - Real HTTP API reference

- **`docs/API.md`** (new) - every real endpoint (`POST /ingest`,
  `GET /query`, `GET /aggregate`, `GET /stats`) documented from the actual
  handler code in `api.py`: request/response bodies, query parameters,
  status codes, and precisely what `sampleCount` counts (per `store.py`'s
  `sample_count()` - total rows, not request count). Verified live against
  a real running server. Documentation-only - no code changed, no version
  bump.

---
