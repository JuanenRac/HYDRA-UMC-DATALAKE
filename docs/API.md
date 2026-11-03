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
| `admin` | `POST /retention`, `POST /retention/apply` (the second one **deletes history**) and, when switched on, `POST /downsample`, `POST /downsample/apply` (a real run **deletes raw points**) and, when switched on, `GET`/`PUT /downsample/plan` (a saved plan never runs by itself) | `HYDRA_UMC_DATALAKE_ADMIN_TOKEN` |
| `read` | `GET /query`, `/history`, `/aggregate`, `/series`, `/stats`, `/stats/range`, `/retention` (and, when switched on, `/windows`, `/rollups`, `GET /downsample`) | `HYDRA_UMC_DATALAKE_READ_TOKEN` |

`HYDRA_UMC_DATALAKE_TOKEN` (or `--token`) is one token for all three roles. A client sends `Authorization: Bearer <token>`; a wrong or missing one is a `401` with `WWW-Authenticate: Bearer`. The administration token is also accepted wherever another role is required. `GET /health` is closed by default (`404`, like a route that does not exist): `--health-open` (or `HYDRA_UMC_DATALAKE_HEALTH_OPEN=1`) opens it, and then it answers `{"status": "ok", "service": "hydra-umc-datalake", "version": ...}` to a caller on loopback and, to any other caller, only with the read or administrator token (`401` otherwise; with no such token configured, never). Use it for a container healthcheck or a monitor that runs on the same host. Tokens have at least 16 characters.

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

## Optional: window statistics and downsampling

Two features are **off by default**: with the switch off the routes below do not exist (`404`, exactly as before) and the database keeps the schema it has always had. They are turned on with start-up options (see [`OPERATIONS.md`](OPERATIONS.md)):

| Switch | Turns on | Role |
|---|---|---|
| `--enable-window-stats` | `GET /windows` | `read` |
| `--enable-downsampling` | `GET /rollups`, `GET /downsample`, `POST /downsample`, `POST /downsample/apply` | `read` for the two `GET`, `admin` for the two `POST` |
| `--enable-fine-rollups` (needs downsampling) | `fineBucketMs` in a policy: a second, narrower rollup level | `admin` |
| `--downsample-admin` (needs `--downsample-plan`) | `GET` and `PUT /downsample/plan`: read and replace the tier plan (see below) | `admin` |

Nothing here changes `/query`, `/history`, `/aggregate`, `/retention` or `/retention/apply`.

### `GET /windows`

Several statistics per fixed time window in one read. Windows are aligned to `start` like in `/aggregate`; a window with no points is not returned.

| Param | Required | Meaning |
|---|---|---|
| `kind`, `field` | yes | The series to reduce. |
| `bucketMs` | yes | Window width in milliseconds (at most `100000` windows per request). |
| `start` | yes | Inclusive start (epoch ms). |
| `end` / `before` | yes (one) | Inclusive / exclusive end, like `/query`. |
| `sourceId` | no | Restrict to one source. |
| `order` | no | `asc` (default) or `desc`; anything else is a `400`. With `desc` the windows are aligned to the **late end** and are open at the early edge and closed at the late one, `(low, high]`, which is the shape of the intervals of a read that runs backwards in time: the range is `start < timestamp <= end`, the window `i` counted from `end` is `(end - (i + 1) * bucketMs, end - i * bucketMs]`, each row carries `bucketStart` (the open early edge) and `bucketEnd` (the closed late edge), the answer says `"order": "desc"` and rows come oldest first. The earliest window is cut by `start` but keeps its full edges. |
| `stats` | no | Comma list, default `count,min,max,avg`. Names: `count`, `min`, `max`, `avg`, `sum`, and percentiles `p<0-100>` such as `p50`, `p95`, `p99.9` (at most 16 per request). |

```json
{
  "stats": ["count", "min", "p95"],
  "buckets": [
    {"bucketStart": 1735689600000, "count": 12, "min": 38.2, "p95": 44.9}
  ]
}
```

A percentile is the linear interpolation between the two closest ranks of the sorted values of the window; it is always between the window's minimum and maximum, and `p50` of an even count is the mean of the two middle values. The simple statistics are reduced by SQL; asking for **any** percentile loads the points of the range to sort them, so that case is bounded by `--max-window-points` (default `1000000`) and a larger range is a `400` (narrow it or filter by `sourceId`).

**Errors** - `400` for missing parameters, an unknown statistic, a non-positive `bucketMs`, too many windows or too many points.

### Downsampling: raw points for a while, rollups after

A downsampling policy says: keep the raw points of `(kind, field)` for `rawMs`; older points are summarised into rollups of `bucketMs` (per source: `count`, `min`, `max`, `sum`) and the raw points are removed. Only buckets that lie **entirely** before `now - rawMs` are touched. The roll-up and the removal of a bucket happen in one transaction, a point that arrives late for a bucket that is already rolled up is merged into it, and a second run changes nothing. Percentiles cannot be rebuilt from a rollup: they exist only for the raw part.

#### `GET /downsample`

Lists the policies: `[{"kind", "field", "rawMs", "bucketMs", "rollupRetentionMs"}]` (`rollupRetentionMs` is `null` when the rollups are kept for ever). Once any policy has asked for a second level every item also carries `fineBucketMs` (`null` for the ones without it).

#### `POST /downsample` (admin)

```json
{"kind": "joint_temp", "field": "j1", "rawMs": 604800000, "bucketMs": 3600000, "rollupRetentionMs": 31536000000}
```

Sets or replaces the policy; `rollupRetentionMs` is optional and bounds how long the rollups live. `"kind": "*", "field": "*"` is the default policy for series without one of their own. `{"kind": ..., "field": ..., "remove": true}` removes a policy (its rollups stay). A non-positive value or a `*` in only one of the two names is a `400`.

**Second, narrower level (optional, off by default).** `"fineBucketMs"` is a narrower width: it must be positive, smaller than `bucketMs`, divide it exactly and make at most `--max-fine-per-coarse` (default `1440`) fine buckets per coarse bucket, otherwise the policy is a `400`. It is accepted only when the service starts with `--enable-fine-rollups` (or `HYDRA_UMC_DATALAKE_FINE_ROLLUPS=true` in its environment file; it needs `--enable-downsampling`); with the switch off a request that sends it is a `400` and a value stored earlier stays unused. When a run rolls a coarse bucket up it also sums the same raw points into fine buckets of that width, in the same transaction and the same `rollups` table (their `bucketMs` is the fine width), so a reader whose interval edge falls inside the coarse bucket can add up the fine ones instead of marking the answer uncertain. Replacing the policy without `fineBucketMs` clears it. Coarse rollups written before the level was turned on have no fine rows, and a coarse row and its fine rows count the same points: read one width at a time (`bucketMs`) or compare the counts.

#### `POST /downsample/apply` (admin)

One run at the server's clock. **It is a dry run unless told otherwise**: nothing is written and the response says what would be removed, which is also written to the log. Send `{"dryRun": false}` to do it for real; if the service was started with `--downsample-delete`, a request that omits `dryRun` deletes. At most `--max-downsample-buckets` (default `10000`) buckets are handled per run; `truncated` tells whether more remain, and the next run continues.

```json
{
  "dryRun": true, "atMs": 1735689600000,
  "buckets": 14, "rawPoints": 336, "expiredRollups": 0, "truncated": false,
  "series": [{"sourceId": "arm-1", "kind": "joint_temp", "field": "j1", "buckets": 7, "rawPoints": 168}],
  "warnings": []
}
```

With the fine level on, the report also has `"fineBuckets"`: how many fine buckets the run wrote (or would write). `series` lists at most 50 entries. `warnings` says, for example, when a retention policy of the same series is shorter than `rawMs`: `POST /retention/apply` would then delete raw points before they are rolled up, so keep the retention at least as long as `rawMs` (or do not set one).

#### `GET /rollups`

| Param | Required | Meaning |
|---|---|---|
| `kind`, `field` | yes | The series. |
| `sourceId` | no | One source. |
| `start`, `end` / `before` | no | Window on `bucketStart`. |
| `bucketMs` | no | Only rollups of this bucket width (a policy changed later leaves older widths in place). |
| `limit` | no | Rows per call (default `1000`, at most `200000`). |
| `order` | no | `asc` (default) or `desc` (newest first, so a `limit` keeps the most recent buckets); anything else is a `400`. |

Response: `[{"sourceId", "bucketMs", "bucketStart", "count", "min", "max", "sum", "avg"}]`, oldest first (newest first with `order=desc`).

---

## Optional: finished cycles (`--enable-production-events`)

### `POST /production-events`

Off by default: without the option the route does not exist (`404`). It stores the finished cycles that HYDRA-UMC-JOB-DISPATCHER publishes (`-production-events`) as the samples that HYDRA-UMC-PRODUCTION-REPORTS reads: kind `production_event`, fields `good` (`1` or `0`) and `cycleTimeS`, at the time the cycle ended. Role `ingest`.

**Request body:** one event, or `{"events": [ ... ]}` for several (at most `--max-batch-samples`) stored in ONE transaction.

```json
{"source_id": "robot-1", "at_utc": "2027-01-15T08:00:00Z", "good": true, "cycle_time_s": 12.5, "job_id": "weld-1"}
```

The schema is closed: exactly `source_id` (1 to 128 characters), `at_utc` (an ISO 8601 date-time with its time zone), `good` (a boolean), `cycle_time_s` (above 0 and at most one week) and, optionally, `job_id` (1 to 128 characters). Anything else is a `400`. The job id is not stored as text; it is folded into the sample's `sequence`, so the same event sent again (a retry) replaces itself while two cycles that end in the same millisecond stay two points. The time must pass the same checks as any sample (not in seconds, not far ahead of this machine's clock).

| Status | Body | Meaning |
|---|---|---|
| 202 | `{"accepted": <events>, "written": <field values>}` | All stored. |
| 400 | `{"error": "invalid production event: events[1]: <detail>"}` | An invalid event (named by its index when several were sent); nothing was stored. |
| 401, 408, 411, 413, 507 | as `POST /ingest` | |

## Optional: the tier plan over HTTP (`--downsample-admin`)

Off by default: without the option the two routes below do not exist (`404`). They need `--enable-downsampling` and `--downsample-plan FILE` (the plan file stays the single source: see [`OPERATIONS.md`](OPERATIONS.md)), and both use the **`admin`** role (an administrator token, or no token on loopback like every other route). The switch can also be set as `HYDRA_UMC_DATALAKE_DOWNSAMPLE_ADMIN=true` in the environment file of the service.

Changing the plan is always safe by itself: **a request only saves a plan and reports what it would do; it never runs the downsampling and never deletes**. Deleting is a separate decision of the operator:

| Switch | Who can change it | Effect |
|---|---|---|
| `--downsample-admin` (`HYDRA_UMC_DATALAKE_DOWNSAMPLE_ADMIN`) | The operator, at start | The two routes exist. |
| `--downsample-allow-delete` (`HYDRA_UMC_DATALAKE_DOWNSAMPLE_ALLOW_DELETE`) | The operator, at start. **No request can switch it on.** | A plan with `"delete": true` makes the timer (`--downsample-auto-interval-seconds`) really delete the rolled-up raw points. Without it such a plan is refused over HTTP (`403`) and a plan file edited by hand with `"delete": true` still runs as a dry run. |
| `--downsample-delete` | The operator, at start (the older switch) | Makes the timer delete whatever the plan says. |

Every change, every preview and every refused attempt to ask for deletion is appended to `<plan file>.audit.jsonl` (when, from which address, the hashes before and after, the tiers that changed, whether deletion was requested and allowed; never a token) and to the log. The file is rotated at 1 MiB. The previous plan is kept as `<plan file>.bak` and the new one is written to a temporary file and renamed over the old one, so a crash leaves either plan complete.

### The plan document

```json
{
  "tiers": [
    {"kind": "motor_temp", "field": "*", "raw": "2d", "bucket": "1h", "fineBucket": "1m", "rollupRetention": "5y", "fineRetention": "30d"},
    {"kind": "*", "field": "*", "raw": "30d", "bucket": "1d"}
  ],
  "delete": false
}
```

Durations are a whole number of milliseconds or a number with `s`, `m`, `h`, `d`, `w` or `y`. `delete` is optional (default `false`). Checks, in this order:

1. **Shape (`400 invalid_plan`)**: only the keys above, at least one and at most 1000 tiers, `kind` and `field` non-empty (`"*"` for every field of a kind, `*`/`*` for the default tier), no tier twice, `raw` and `bucket` present, positive durations, `fineBucket` narrower than `bucket` and dividing it exactly, `fineRetention` only with `fineBucket` and not on the default tier.
2. **Coherence (`422 incoherent_plan`)**: `bucket` not longer than `raw`; `rollupRetention` and `fineRetention` not shorter than `raw`; `fineRetention` not longer than `rollupRetention`. `details` lists every problem.
3. **Known kinds (`422 unknown_kind`)**: every named kind has data, rollups or a policy in this datalake (`knownKinds` of `GET` lists them).

### `GET /downsample/plan` (admin)

```json
{
  "plan": {"tiers": [ ... ], "delete": false},
  "hash": "<sha-256 of the plan>",
  "tiers": [{"kind": "motor_temp", "field": "*", "rawMs": 172800000, "bucketMs": 3600000, "fineBucketMs": 60000, "rollupRetentionMs": 157680000000, "fineRetentionMs": 2592000000}],
  "knownKinds": ["motor_temp", "vibration"],
  "switches": {"allowDelete": false, "deleteRequestedByPlan": false, "deleteOldSwitch": false, "effectiveMode": "dryRun", "fineRollups": true, "autoIntervalSeconds": 3600},
  "audit": [{"atMs": 1735689600000, "event": "plan_saved", "client": "127.0.0.1", "previousHash": "...", "hash": "...", "tiers": 2, "deleteRequested": false, "allowDelete": false, "changes": []}]
}
```

`effectiveMode` is `dryRun` or `delete` and is what the timer does now. `audit` has the last 10 entries (`plan_saved`, `plan_preview`, `delete_refused`).

### `PUT /downsample/plan` (admin)

Request body (the schema is closed):

```json
{"plan": {"tiers": [ ... ], "delete": false}, "expectedHash": "<hash from GET>", "confirmDelete": false, "preview": false}
```

`expectedHash` is optional and makes the change fail with `409 plan_changed` if somebody else saved in between. `preview: true` (or `?preview=1`) validates and answers exactly as a save would, but writes nothing. `confirmDelete: true` is required to save a plan that turns `delete` from `false` to `true`.

Answer `200`:

```json
{
  "saved": true, "preview": false, "hash": "...", "previousHash": "...", "mode": "dryRun",
  "tiers": [ ... ],
  "changes": [{"kind": "motor_temp", "field": "*", "change": "changed", "fields": ["rawMs"]}],
  "warnings": ["motor_temp/*: raw points will be rolled up sooner than before"],
  "effects": {"atMs": 1735689600000, "series": [{"kind": "motor_temp", "field": "j1", "rawPointsToRollUp": 100, "rollupsToExpire": 0, "fineRollupsToExpire": 0}], "totals": {"rawPointsToRollUp": 100, "rollupsToExpire": 0, "fineRollupsToExpire": 0}},
  "currentEffects": { ... the same for the plan that was saved before ... }
}
```

`effects` is counted without touching anything: the raw points older than `raw` (aligned to the bucket) that the next round would roll up, the rollups past `rollupRetention` and the narrow rollups past `fineRetention` that it would expire. `mode` says whether that round would delete (`delete`) or only report (`dryRun`). Saving the same plan again answers `"saved": false, "unchanged": true`. A saved plan is used by the timer from its next round; the policies of the new tiers are written at once and the policies of the tiers that are gone are removed.

| Status | `code` | Meaning |
|---|---|---|
| 200 | | Saved, previewed or unchanged. |
| 400 | `invalid_request` | The body is not a JSON object, has another key, or a value of the wrong type. |
| 400 | `invalid_plan` | Shape of the plan (point 1 above); `error` names the tier. |
| 401 | | A valid admin bearer token is required. |
| 403 | `delete_not_allowed` | The plan has `"delete": true` and the service was not started with `--downsample-allow-delete`. Audited. |
| 408, 411, 413 | | As `POST /ingest`. |
| 409 | `confirm_delete_required` | Turning `delete` on needs `confirmDelete: true` (read the effects first). |
| 409 | `plan_changed` | `expectedHash` is not the current hash (`details` has the current one). |
| 422 | `incoherent_plan`, `unknown_kind` | Points 2 and 3 above; `details` lists them. |
| 500 | `write_failed` | The file could not be written; the old plan is untouched. |

### What a management screen should show

The contract above is what an editor in the administration of a Studio needs; it adds no other route.

- **A table of levels**, one row per tier: kind, field, raw time, wide width and its retention, narrow width and its retention (empty when the fine rollups are off, with the `fineRollups` switch said), and a row for the default tier. Durations are edited as text such as `2d` or `1h`; the `rawMs` values of `GET` are for display. The kinds offered come from `knownKinds`.
- **Live validation**: send the draft with `preview` on every pause in typing and show `details` next to the row that `tiers[n]` names; save stays disabled while there is a `4xx`.
- **A preview of the effects**: before saving, the `changes` list, the `warnings`, and the `effects` per series and in total next to `currentEffects`, so the operator sees how many raw points and rollups the next round would touch.
- **Confirmation in two steps**: first the preview, then an explicit "save" that sends `expectedHash` from the read the operator looked at. Turning deletion on adds a second, separate confirmation that sends `confirmDelete: true`.
- **A deletion warning**: the "delete" control is shown only when `switches.allowDelete` is true (otherwise it is disabled with the reason: the operator must start the service with `--downsample-allow-delete`). When it is on, a red notice says that the rolled-up raw points are removed for good and that the rollups past their retention are removed.
- **The mode and the history**: `switches.effectiveMode` as a badge (dry run or delete) and the `audit` entries as a list of who changed what.
- **Errors**: `403 delete_not_allowed`, `409 plan_changed` (offer to reload) and `409 confirm_delete_required` each have their own message, chosen by `code`, never by the English text of `error`. All the user-facing strings of the screen exist in the eight languages of the app (en, es, de, fr, it, ja, zh, pt).

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

## Daily Parquet export (command line and timer)

```bash
pip install "hydra-umc-datalake[parquet]"   # the optional extra; the service works the same without it
hydra-umc-datalake export-parquet --db datalake.sqlite3 --dest /var/lib/hydra-umc/parquet --day 2027-01-14
```

Writes `samples-<YYYY-MM-DD>.parquet` for one UTC day (default: yesterday) with the columns `source_id`, `kind`, `field`, `timestamp_ms`, `value` and `sequence`, sorted by time. Only data goes into the file: no token, no setting, no path. It is written beside its name and renamed when complete, a day without points writes no file, and an existing file is left alone unless `--overwrite` is given. Nothing is ever deleted. With `--enable-parquet-export --parquet-export-dir DIR` the service does the same by itself for every closed day of the last `--parquet-export-lookback-days` (default 7) that has no file yet, once at start and then every `--parquet-export-interval-seconds` (default 3600). The service refuses to start with the option when `pyarrow` is missing.
