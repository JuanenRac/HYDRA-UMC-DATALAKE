# Operations

How to run the optional features of the data lake. Everything here is **off by default**: a service started with no extra option behaves exactly as it always has (no window statistics, no downsampling, the existing `/retention` rules only). The routes themselves are in [`API.md`](API.md).

## Start-up options

| Option | Default | Meaning |
|---|---|---|
| `--enable-window-stats` | off | Turns on `GET /windows` (count, min, max, avg, sum and percentiles per time window). |
| `--max-window-points` | `1000000` | Most points `GET /windows` may load to compute percentiles; a larger range is a `400`. Memory grows with this value, so lower it on a small machine. |
| `--enable-downsampling` | off | Turns on `GET /rollups`, `GET /downsample`, `POST /downsample` and `POST /downsample/apply`, and creates the two tables they use (`downsample_policies`, `rollups`). |
| `--downsample-delete` | off | A `POST /downsample/apply` that does not say `dryRun` then deletes. Needs `--enable-downsampling`. Without it every run is a dry run unless the request sends `"dryRun": false`. |
| `--max-downsample-buckets` | `10000` | Most buckets one run rolls up. A run that stops at the limit says `truncated: true`; the next run continues. |
| `--health-open` | off | Opens `GET /health` (`404` while closed): `{"status", "service", "version"}` to loopback, to other callers only with the read or administrator token. Also `HYDRA_UMC_DATALAKE_HEALTH_OPEN=1`. The installer of the OS sets it on the CM5. |
| `--enable-fine-rollups` | off | Lets a policy carry `fineBucketMs`, a second, narrower rollup level written beside the coarse one. Needs `--enable-downsampling`. Also `HYDRA_UMC_DATALAKE_FINE_ROLLUPS=true` in the environment file (the flag wins). |
| `--max-fine-per-coarse` | `1440` | Most fine buckets one coarse bucket may hold; a policy that would make more is refused. Also `HYDRA_UMC_DATALAKE_MAX_FINE_PER_COARSE` in the environment file. |
| `--downsample-plan FILE` | none | A JSON file with the tiers of every kind (see below). The policies are written at start. Needs `--enable-downsampling`. |
| `--downsample-admin` | off | Turns on `GET` and `PUT /downsample/plan` (admin role) to read and replace the plan file; a request only saves and reports the effects, it never runs or deletes. Needs `--downsample-plan`. Also `HYDRA_UMC_DATALAKE_DOWNSAMPLE_ADMIN=true` in the environment file. See `API.md`. |
| `--downsample-allow-delete` | off | Lets a plan with `"delete": true` make the timer really delete. No request can switch it on; without it a plan is always a dry run. Needs `--downsample-plan`. Also `HYDRA_UMC_DATALAKE_DOWNSAMPLE_ALLOW_DELETE=true`. |
| `--downsample-auto-interval-seconds` | `0` (never) | Runs the downsampling by itself every this many seconds (at least 60). It is a **dry run** unless `--downsample-delete` is given. Needs `--enable-downsampling`. |
| `--accept-gzip` | off | Reads request bodies sent with `Content-Encoding: gzip` (for example the batch of the telemetry collector). The inflated size is bounded by `--max-request-body-bytes`; without the switch a compressed body is refused with `400`. |
| `--enable-production-events` | off | Turns on `POST /production-events` (finished cycles stored as `production_event` samples). |
| `--enable-parquet-export` | off | Writes one Parquet file per closed UTC day into `--parquet-export-dir`. Needs the optional `pyarrow` (`pip install "hydra-umc-datalake[parquet]"`); the service refuses to start with the option without it. |
| `--parquet-export-dir` | none | Directory of the daily files. |
| `--parquet-export-interval-seconds` | `3600` | How often the export looks for closed days without a file (at least 60). |
| `--parquet-export-lookback-days` | `7` | How many closed days back it looks (1 to 3650). |

The options go on the command line of the service, for example in the `ExecStart` line of the systemd unit. The two fine-rollup settings can also live in the environment file the unit already reads (`/etc/hydra-umc/datalake.env`), so they can be changed without editing the unit; a wrong value there stops the start instead of being ignored.

## Downsampling step by step

1. **Decide the policy** per series: how long the raw points stay (`rawMs`), the width of the rollup (`bucketMs`) and, if wanted, how long the rollups stay (`rollupRetentionMs`). A default policy for every other series is `"kind": "*", "field": "*"`.
2. **Set it** with `POST /downsample` (admin token).
3. **Look before you delete.** `POST /downsample/apply` with no body is a dry run: nothing is written, the response lists the series and the number of buckets and raw points that would go, and the log has one line per series (`downsample: would remove ...`). Read the `warnings` too.
4. **Run it for real** with `{"dryRun": false}` (or start the service with `--downsample-delete` and let a timer call the route with no body). Each bucket is rolled up and its raw points removed in one transaction.
5. **Repeat** on a timer (for example daily). A run only handles buckets that are completely older than `now - rawMs`, so repeating it is safe; a second run right after the first changes nothing.

Things to know:

- Only whole buckets are touched. With `rawMs` of 7 days and `bucketMs` of one day, the raw points of the current day and of the six before it stay.
- The rollup keeps `count`, `min`, `max` and `sum` per source and bucket, so `avg` is exact, but a **percentile of old data cannot be computed**: percentiles exist only for the raw part. Read old data with `GET /rollups`, recent data with `GET /windows`.
- A point that arrives late for an already rolled-up bucket is merged into that rollup on the next run.
- Changing `bucketMs` of a policy later does not rewrite old rollups; both widths stay (filter with `bucketMs` when reading).
- Removing a policy does not delete its rollups; they stay until a policy with `rollupRetentionMs` covers them.

### The fine level (optional, off by default)

A coarse rollup (say one day) cannot be split: an OPC UA client that reads with an interval whose edge falls inside a bucket gets an answer marked uncertain. When that matters, turn the second level on and give the policy a narrower width that divides the coarse one:

1. Start the service with `--enable-fine-rollups` (or put `HYDRA_UMC_DATALAKE_FINE_ROLLUPS=true` in `/etc/hydra-umc/datalake.env`), next to `--enable-downsampling`.
2. Set the policy with `"fineBucketMs"`, for example `{"kind": "joint_temp", "field": "j1", "rawMs": 604800000, "bucketMs": 86400000, "fineBucketMs": 3600000}`.
3. Read a dry run: the report has `fineBuckets`. A real run writes both levels in one transaction.

Cost: up to `bucketMs / fineBucketMs` more rows per coarse bucket and source (24 for one day and one hour), bounded by `--max-fine-per-coarse`. Rollups written before the level was on stay coarse-only. The fine rows live in the `rollups` table beside the coarse ones, so a reader of `GET /rollups` without `bucketMs` sees both widths and must not add them together: filter by `bucketMs`, or compare the counts of a coarse row with those of the fine rows inside it.

### A tier plan: one file for the downsampling and retention of every kind

`--downsample-plan` reads a JSON file ([`tiers.example.json`](tiers.example.json)) instead of one `POST /downsample` per series:

```json
{"tiers": [
  {"kind": "motor_temp", "field": "*", "raw": "2d", "bucket": "1h", "fineBucket": "1m", "rollupRetention": "5y", "fineRetention": "30d"},
  {"kind": "*", "field": "*", "raw": "30d", "bucket": "1d"}
]}
```

- `raw`: how long the raw points stay. `bucket`: the wide rollup. `fineBucket` (optional): a narrower one that divides it, written only when `--enable-fine-rollups` is on. `rollupRetention`: how long the rollups live (both widths). `fineRetention` (optional, needs `fineBucket`, not on the default tier): how long the NARROW width lives, so one minute keeps 30 days and one hour keeps 5 years. Durations are milliseconds or a number with `s`, `m`, `h`, `d`, `w` or `y` (365 days).
- `"field": "*"` with a named kind covers every field of that kind seen so far (raw points, rollups or policies), also the ones that appear later; a field with a tier of its own keeps it. `"kind": "*", "field": "*"` is the default for series with no tier.
- A bad plan stops the start with the tier and the reason. The plan only writes policies: nothing is deleted because a plan exists. With `--downsample-auto-interval-seconds` the round is a dry run until `--downsample-delete` is given (or the plan says `"delete": true` and `--downsample-allow-delete` is given), and the narrow level only expires when it is deleting. If a tier keeps raw points for less than two days and the Parquet export is on, the start warns that the export may miss them (export first, then downsample).

### Running it on a timer (example units)

`systemd/` holds an example pair, `hydra-umc-datalake-downsample.service` and `.timer`, plus the small script they run (`hydra-umc-datalake-downsample.sh`, installed as `/opt/hydra-umc/datalake/bin/downsample.sh`). The service must run with `--enable-downsampling` (without it the route answers 404 and the unit fails). **It is a dry run by default**: the journal shows what would be rolled up and deleted, and nothing changes until you set `HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN=false` in `/etc/hydra-umc/datalake.env`. The units are not enabled by any installer; enabling them is a decision for the operator after reading a dry run.

| Variable | Default | Meaning |
|---|---|---|
| `HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN` | `true` | `true` or `false`; anything else (including an empty value) is refused with exit code 2 before any call. |
| `HYDRA_UMC_DATALAKE_ADMIN_TOKEN` | empty | Administration token; sent on curl's standard input, never in the command line. Empty: no header (loopback without tokens). |
| `HYDRA_UMC_DATALAKE_URL` | `http://127.0.0.1:8095` | Base URL of the service. |
| `HYDRA_UMC_DATALAKE_DOWNSAMPLE_MAX_PASSES` | `1` | Runs per call while the answer says `truncated: true`, 1 to 100 (a dry run is always one pass). |
| `HYDRA_UMC_DATALAKE_DOWNSAMPLE_TIMEOUT` | `300` | Seconds curl may take per pass. |

The timer fires daily at 03:30 with up to 15 minutes of random delay and `Persistent=true`, after the retention timer. The script is tested against a real service in `tests/test_downsample_unit.py`; the units themselves have not run under systemd on the CM5.

### Interaction with `/retention`

The existing retention (`POST /retention`, `POST /retention/apply`, `--default-retention-days`) deletes raw points older than its window **without** rolling them up. If a series has both, keep the retention at least as long as `rawMs` (or do not set one for that series); otherwise `POST /retention/apply` removes points before the downsampling can summarise them. The dry-run report warns about this.

### Limits and memory

| Limit | Where | Value |
|---|---|---|
| Windows per `GET /windows` request | code | `100000` |
| Statistics per request | code | `16` |
| Points loaded for percentiles | `--max-window-points` | `1000000` |
| Buckets per downsampling run | `--max-downsample-buckets` | `10000` |
| Series listed in a run report | code | `50` |
| Rows per `GET /rollups` call | code | `200000` |

## Production events and the daily Parquet export

`POST /production-events` (see [`API.md`](API.md)) and the Parquet export are separate switches and both are off by default. The export is the offline door for dashboards, synthetic-data tools and notebooks: one file per closed UTC day, data only, never overwritten and never deleting. Keep the raw points (`raw` of the tier, or the retention) for at least two days so a day is complete when the export reaches it.

## What the tests cover

The existing routes are pinned by characterization tests; the percentile and rollup arithmetic by property tests over random data (a percentile stays between the minimum and the maximum and never decreases with the fraction; the counts and sums of the rollups plus the kept raw points equal the original data; a second run changes nothing); the retention of raw points by tests with a simulated clock, so no test waits for time to pass.
