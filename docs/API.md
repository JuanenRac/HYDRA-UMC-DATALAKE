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

`--addr`/`--port` default to `127.0.0.1:8095` - loopback-only, matching every other internal API in this ecosystem (Anomaly-Detector, Job-Dispatcher, Telemetry-Collector) and the real CM5's own systemd unit, which already passed this same override explicitly. There is no authentication of any kind on any endpoint, so pass `--addr 0.0.0.0` only with a real understanding of what that exposes on your own network - `POST /ingest` would accept and persist telemetry from anyone who can reach it, with nothing to stop them.

All responses are `application/json`. A request body is bounded to **1,048,576 bytes** by default and a connected client has **15 seconds** to send it; deployments may set stricter values when constructing `DatalakeServer`. Oversized bodies receive `413`, and an incomplete body that reaches the socket timeout receives `408`.

---

## `POST /ingest`

Writes one sample into the store. Ingestion is idempotent per stored point:
the identity is `(sourceId, kind, field, timestamp)`. Re-delivering that exact
point replaces its value (last write wins) instead of adding a second row, so
a client retry after a lost response cannot inflate queries or aggregates.
The current telemetry contract does not include a per-point sequence/event ID;
an exact identity collision therefore follows that explicit last-write-wins
rule.

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
- `timestamp` (integer) - Unix epoch milliseconds.
- `fields` (object, optional) - field name -> numeric value. Each key becomes its own queryable `field` (see `/query` below).

**Responses**

| Status | Body | Meaning |
|---|---|---|
| 202 | `{"written": <int>}` | Sample accepted; `written` is how many individual field values were accepted (one per key in `fields`), whether newly inserted or idempotently replaced. |
| 400 | `{"error": "invalid sample: <detail>"}` | Missing/malformed `sourceId`, `kind`, `timestamp`, or a non-numeric field value. |
| 408 | `{"error": "request body timed out"}` | Client did not finish sending its declared request body within the configured timeout. |
| 413 | `{"error": "request body exceeds <limit> bytes"}` | Declared request body exceeds the configured maximum. |

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
| `end` | integer | Inclusive end timestamp (epoch ms). |
| `limit` | positive integer | Max points returned (default `1000`; `0` and negative values are rejected). |

**Response** - `200`, a JSON array:

```json
[
  {"sourceId": "arm-3", "kind": "joint_temp", "field": "j1", "timestamp": 1735689600000, "value": 42.1}
]
```

The response order is deterministic: `timestamp` ascending, then `sourceId`,
`kind`, `field`, and the internal row identity. This makes equal-timestamp
results reproducible across retries and runs.

`400 {"error": "<message>"}` on an invalid filter value (e.g. a non-integer
`start`/`end`/`limit`, or a non-positive `limit`).

---

## `GET /aggregate`

Buckets stored points into fixed-width time windows and reduces each bucket to one value.

**Query parameters**

| Param | Required | Type | Meaning |
|---|---|---|---|
| `kind` | yes | string | Sample kind to aggregate. |
| `field` | yes | string | Field name to aggregate. |
| `bucketMs` | yes | integer | Bucket width in milliseconds (must be positive). |
| `start` | yes | integer | Inclusive start timestamp (epoch ms). |
| `end` | yes | integer | Inclusive end timestamp (epoch ms). |
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

## `GET /stats`

**Response** - `200 {"sampleCount": <int>}` - current number of individual
`(field, value)` rows in the store, not the number of `/ingest` calls. An
idempotent retry that replaces an existing point does not increase this count.

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

Sets (or replaces) the retention window for one `(kind, field)` series.

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

Deletes every real stored sample older than its `(kind, field)`'s configured retention window, evaluated against the real current time. A series with no configured policy is never touched - retention is opt-in per series, not a global default.

**Response** - `200 {"deleted": <int>}` - the real number of rows removed.

---

## Errors

Any other path/method returns `404 {"error": "not found"}`.
