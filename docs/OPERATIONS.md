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

The options go on the command line of the service, for example in the `ExecStart` line of the systemd unit; there is no separate configuration file.

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

## What the tests cover

The existing routes are pinned by characterization tests; the percentile and rollup arithmetic by property tests over random data (a percentile stays between the minimum and the maximum and never decreases with the fraction; the counts and sums of the rollups plus the kept raw points equal the original data; a second run changes nothing); the retention of raw points by tests with a simulated clock, so no test waits for time to pass.
