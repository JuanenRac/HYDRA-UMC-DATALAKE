# HTTP API Reference

Real, plain JSON/HTTP surface implemented in
[`src/hydra_umc_datalake/api.py`](../src/hydra_umc_datalake/api.py) with
Python's stdlib `http.server` (no framework dependency, same convention as
HYDRA-UMC-JOB-DISPATCHER and HYDRA-UMC-TELEMETRY-COLLECTOR's Go
`net/http` servers). One `TimeSeriesStore` instance lives for the
process's lifetime.

Start it with:

```bash
hydra-umc-datalake --addr 127.0.0.1 --port 8095
```

`--addr`/`--port` default to `127.0.0.1:8095` - loopback-only, matching every other internal API in this ecosystem (Anomaly-Detector, Job-Dispatcher, Telemetry-Collector) and the real CM5's own systemd unit, which already passed this same override explicitly.

## Access control

Every route is open unless a token is configured for its role. There are three roles:

| Role | Routes | Token variable |
|---|---|---|
| `ingest` | `POST /ingest`, `POST /ingest/batch` | `HYDRA_UMC_DATALAKE_INGEST_TOKEN` |
| `admin` | `POST /retention`, `POST /retention/apply` (the second one **deletes history**) | `HYDRA_UMC_DATALAKE_ADMIN_TOKEN` |
| `read` | `GET /query`, `/history`, `/aggregate`, `/series`, `/stats`, `/stats/range`, `/retention` | `HYDRA_UMC_DATALAKE_READ_TOKEN` |

`HYDRA_UMC_DATALAKE_TOKEN` (or `--token`) is one token for all three roles. A client sends `Authorization: Bearer <token>`; a wrong or missing one is a `401` with `WWW-Authenticate: Bearer`. The administration token is also accepted wherever another role is required. `GET /health` is always open (`{"status": "ok"}`), for a container healthcheck. Tokens have at least 16 characters.

Listening on anything but loopback (`--addr 0.0.0.0`) is refused unless the `ingest` and `admin` tokens are set, or `--allow-anonymous` says on purpose that the whole network may write and delete. Reading stays open unless a `read` token is set (the stack keeps it open inside its own network).

All responses are `application/json`. Connections are kept alive (HTTP/1.1), so a client that sends many samples reuses one; a request whose body was not read (a refused one) closes its connection, and a body sent with `Transfer-Encoding: chunked` is a `411`. A request body is bounded to **1,048,576 bytes** by default and a connected client has **15 seconds** to send it; deployments may set stricter values when constructing `DatalakeServer`. Oversized bodies receive `413`, and an incomplete body that reaches the socket timeout receives `408`.

---

## `POST /ingest`

Writes one sample into the store. Ingestion is idempotent per stored point:
the identity is `(sourceId, kind, field, timestamp, sequence)`, where `sequence`
is the optional whole number from 0 to 2^53 - 1 that the telemetry collector
attaches to each sample (0 when it is not sent). Re-delivering that exact point
replaces its value (last write wins) instead of adding a second row, so a
client retry after a lost response cannot inflate queries or aggregates. A
producer that sends no `sequence` has the identity `(sourceId, kind, field,
timestamp)`, as it always had, and an exact collision follows the same
last-write-wins rule; one that does send it keeps two different readings of
the same millisecond as two points.

**Request body**

```json
{
  "sourceId": "arm-3",
  "kind": "joint_temp",
  "timestamp": 1735689600000,
  "fields": {"j1": 42.1, "j2": 39.8}
}
```

- `sourceId` (string) - which HYDRA-UMC node/robot this sample came from.
- `kind` (string) - the sample category (e.g. `joint_temp`, `vibration`).
- `timestamp` (integer) - Unix epoch **milliseconds**; must fall within the range this service can actually convert to a UTC label (see `/stats/range`), must not be below 100000000000 (early 1973: a smaller value is a producer that sent seconds, which would be January 1970 and invisible to every dashboard), and must not be more than 5 minutes ahead of this machine's clock (`--max-future-skew-seconds`, `0` turns it off).
- `fields` (object, optional) - field name -> numeric value (booleans are rejected, not silently coerced to `1`/`0`). Each key becomes its own queryable `field` (see `/query` below).

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 202 | `{"written": <int>}` | Sample accepted; `written` is how many individual field values were accepted (one per key in `fields`), whether newly inserted or idempotently replaced. |
| 400 | `{"error": "invalid sample: <detail>"}` | Missing/malformed `sourceId`/`kind` (must be strings), `timestamp` (missing, non-integer, or outside the representable range), `fields` (must be an object, if present), or a field value that is not a real number. |
| 401 | `{"error": "a valid bearer token is required for ingest access"}` | A token is configured for writing and the request has no valid one. |
| 408 | `{"error": "request body timed out"}` | Client did not finish sending its declared request body within the configured timeout. |
| 411 | `{"error": "send the body with a Content-Length ..."}` | The body was chunked. |
| 413 | `{"error": "request body exceeds <limit> bytes"}` | Declared request body exceeds the configured maximum. |

Without a `sequence`, two readings of the same point in the same millisecond share one identity, so the second replaces the first: send the `sequence` of the sample (the collector does) from a source that can report faster than 1 kHz, or when frames are stamped on arrival. `sequence` must be a whole number from 0 to 2^53 - 1; anything else is a 400.

---

## `GET /query`

Returns raw stored points matching filters.

**Query parameters** (all optional, `?key=value&...`)

| Param | Type | Meaning |
|---|---|---|
| `sourceId` | string | Filter to one source. |
| `kind` | string | Filter to one sample kind. |
| `field` | string | Filter to one field name. |
| `start` | integer | Inclusive start timestamp (epoch ms). |
| `end` | integer | **Inclusive** end timestamp (epoch ms): a point exactly at `end` is returned. A caller that works with half-open windows `[start, end)` (a shift, a day) must ask for `end - 1`, or the point on the boundary is counted in both windows. |
| `before` | integer | **Exclusive** end timestamp (epoch ms): the window is `[start, before)`, so two consecutive windows (a shift, a day) never count the point on their shared boundary twice. Send `end` or `before`, not both (`400`). |
| `cursor` | string | The value of the `X-Next-Cursor` header of the previous page: the query continues right behind the last point of that page. A value this service did not return is a `400`. |
| `limit` | positive integer | Max points returned (default `1000`, at most `200000`; `0`, negative values and bigger ones are rejected). The answer is built in memory: ask for a narrower range rather than a bigger limit. |

**Response** - `200`, a JSON array:

```json
[
  {"sourceId": "arm-3", "kind": "joint_temp", "field": "j1", "timestamp": 1735689600000, "value": 42.1}
]
```

When there are more points than `limit`, the response carries an `X-Next-Cursor` header; send its value as `cursor` (with the same filters) to read the next page. The last page has no such header. The body stays a plain array.

The response order is deterministic: `timestamp` ascending, then `sourceId`,
`kind`, `field`, and the internal row identity. This makes equal-timestamp
results reproducible across retries and runs.

`400 {"error": "<message>"}` on an invalid filter value (e.g. a non-integer
`start`/`end`/`limit`, a number beyond 64 bits, a non-positive `limit` or one above the maximum).

---

## `GET /aggregate`

Buckets stored points into fixed-width time windows and reduces each bucket to one value.

**Query parameters**

| Param | Required | Type | Meaning |
|---|---|---|---|
| `kind` | yes | string | Sample kind to aggregate. |
| `field` | yes | string | Field name to aggregate. |
| `bucketMs` | yes | integer | Bucket width in milliseconds (must be positive; the range may hold at most `100000` buckets, otherwise `400`). |
| `start` | yes | integer | Inclusive start timestamp (epoch ms). |
| `end` | yes (or `before`) | integer | Inclusive end timestamp (epoch ms). |
| `before` | yes (or `end`) | integer | Exclusive end timestamp (epoch ms), the same window `[start, before)` as in `/query`. |
| `agg` | no | string | Reducer: one of `avg` (default), `min`, `max`, `sum`. |
| `sourceId` | no | string | Restrict to one source. |

**Response** - `200`, a JSON array of buckets:

```json
[
  {"bucketStart": 1735689600000, "value": 41.4, "count": 12}
]
```

`count` is how many raw points fell into that bucket.

**Errors** - `400 {"error": "missing required params: [...]"}` if any of `kind`/`field`/`bucketMs`/`start`/`end` is absent; `400 {"error": "unknown aggregate '<x>', want one of ['avg', 'max', 'min', 'sum']"}` for an invalid `agg`; `400 {"error": "bucket_ms must be positive"}` for a non-positive `bucketMs`.

---

## `GET /history`

One series read the way a historian client asks for it (the OPC-UA server uses it to answer history reads): the points of a window in either order, a page cursor and, on request, the values just outside the window.

**Query parameters**

| Param | Type | Meaning |
|---|---|---|
| `sourceId`, `kind`, `field` | string | **Required**: the one series to read. |
| `start` | integer | Inclusive start timestamp (epoch ms). |
| `end` / `before` | integer | Inclusive / exclusive end (epoch ms); send one of them, not both (`400`). |
| `order` | `asc` (default) or `desc` | Oldest first or newest first. |
| `limit` | positive integer | Points per page (default `1000`, same maximum as `/query`). |
| `cursor` | string | The `nextCursor` of the previous page, with the same filters and the same `order`. |
| `bounds` | `0` (default) or `1` | With `1`, also return the newest point before `start` and the oldest point after the end of the window. |

**Response** - `200`:

```json
{
  "order": "asc",
  "points": [{"timestamp": 1735689600000, "value": 42.1}],
  "nextCursor": null,
  "bounds": {"before": {"timestamp": 1735689590000, "value": 41.9}, "after": null}
}
```

`nextCursor` is `null` on the last page; `bounds` is present only when asked for, and each side is `null` where the series has nothing beyond that limit (or the window has no such limit). A missing required parameter, an unknown `order` or `bounds`, or a bad filter is a `400`. Requires the `read` token when one is set.

---

## `GET /series`

The catalogue of what is stored, so a dashboard does not have to guess names: one entry per `(sourceId, kind, field)`, ordered by kind, field and source.

**Query parameters** (optional): `kind` restricts the catalogue to one sample kind; `limit` (default and maximum `100000`).

**Response** - `200`:

```json
[{"sourceId": "arm-3", "kind": "joint_temp", "field": "j1", "count": 5120, "firstTimestamp": 1735689600000, "lastTimestamp": 1735776000000}]
```

---

## `POST /ingest/batch`

Writes many samples in **one request and one transaction**: either all of them are stored or none. The identity and the last-write-wins rule are those of `POST /ingest`; every element goes through the same validation.

**Request body**

```json
{"samples": [{"sourceId": "arm-3", "kind": "joint_temp", "timestamp": 1735689600000, "fields": {"j1": 42.1}}]}
```

At most `1000` samples per request (`--max-batch-samples`); the body is also bounded in bytes like every other body.

| Status | Body | Meaning |
|---|---|---|
| 202 | `{"accepted": <samples>, "written": <field values>}` | All stored. |
| 400 | `{"error": "invalid batch: samples[2]: <detail>"}` | Empty or too long list, or an invalid element (named by its index); nothing was stored. |
| 401, 408, 411, 413, 507 | as `POST /ingest` | |

---

## `GET /stats`

**Response** - `200 {"sampleCount": <int>}` - current number of individual
`(field, value)` rows in the store, not the number of `/ingest` calls. An
idempotent retry that replaces an existing point does not increase this count.
For a database on disk it also reports `dbBytes` (the file and its write-ahead
log) and `freeDiskBytes` (the free space of the disk it is on, cached for one
second), so a monitor can see a store that grows toward the disk.

---

## `GET /stats/range`

Real oldest/newest stored timestamps, labeled explicitly as UTC - `samples.timestamp` is always unix-epoch milliseconds (inherently UTC), but a human-facing consumer should never have to guess that from a bare integer.

**Response** - `200`:

```json
{"oldestMs": 1735689600000, "newestMs": 1735776000000, "oldestUtc": "2025-01-01T00:00:00+00:00", "newestUtc": "2025-01-02T00:00:00+00:00"}
```

All four fields are `null` for an empty store - never `0`, which would itself be a real, valid timestamp (`1970-01-01T00:00:00Z`).

---

## `GET /retention`

Lists every currently-configured retention policy.

**Response** - `200`, a JSON array:

```json
[{"kind": "joint_temp", "field": "j1", "retentionMs": 604800000}]
```

---

## `POST /retention`

Sets (or replaces) the retention window for one `(kind, field)` series. `kind` and `field` must be non-empty strings. `"kind": "*", "field": "*"` sets the **default policy**: it applies to every series that has no policy of its own, so a series that appears later (the schema is open) is not left to grow for ever; a `*` in only one of the two is a `400`. `--default-retention-days N` sets it at start. Requires the `admin` token when one is set.

**Request body**

```json
{"kind": "joint_temp", "field": "j1", "retentionMs": 604800000}
```

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true}` | Policy stored. |
| 400 | `{"error": "invalid retention policy: <detail>"}` | Missing/malformed `kind`/`field`/`retentionMs`, or a non-positive `retentionMs`. |
| 408 | `{"error": "request body timed out"}` | Client did not finish sending its declared request body within the configured timeout. |
| 413 | `{"error": "request body exceeds <limit> bytes"}` | Declared request body exceeds the configured maximum. |

---

## `POST /retention/apply`

Deletes every real stored sample older than its `(kind, field)`'s configured retention window, evaluated against the real current time. A series with no policy of its own is only touched if a default policy (`*`/`*`) was configured - retention is opt-in, never a global default that could surprise an operator who never asked for it. When rows were deleted the space is given back to the disk (incremental vacuum, on a database created by this version; the write-ahead log is checkpointed). Requires the `admin` token when one is set; the systemd timer sends `HYDRA_UMC_DATALAKE_ADMIN_TOKEN` from `/etc/hydra-umc/datalake.env`.

**Response** - `200 {"deleted": <int>}` - the real number of rows removed.

---

## Errors

Any other path/method returns `404 {"error": "not found"}`. A parameter the database cannot take (a number beyond 64 bits, a list where a string belongs) is a `400`, never a connection closed without an answer.

The database on disk runs in WAL mode with `synchronous=NORMAL`: a long read no longer blocks writes and a commit does not wait for a second fsync; after a power cut the last commits may be lost, the file is not corrupted.

---

## Backup (command line)

```bash
hydra-umc-datalake backup --db datalake.sqlite3 --dest /var/backups/hydra-umc-datalake --keep 7
```

Writes `datalake-<UTC time>.sqlite3` into `--dest` with SQLite's online backup, so the copy is consistent even while the service is writing, and keeps the `--keep` newest copies (`0` keeps all). It does not need the HTTP service to run and does not need a token.
