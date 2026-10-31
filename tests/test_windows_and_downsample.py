# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_windows_and_downsample.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Window statistics (percentiles included) and downsampling: properties over random data, a simulated clock
for the retention of raw points, and the HTTP switches (off by default, dry run by default)."""
from __future__ import annotations

import json
import logging
import math
import random
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore
from hydra_umc_datalake.windows import parse_stats, percentile

DAY = 86_400_000
HOUR = 3_600_000
T0 = 1_800_000_000_000 - (1_800_000_000_000 % DAY)  # midnight, so day buckets line up


def _store_with(points: list[tuple[str, int, float]], kind: str = "temp", field: str = "j1") -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id=s, kind=kind, timestamp=t, fields={field: v}) for s, t, v in points])
    return store


# ---------------------------------------------------------------- percentile properties
def test_percentile_known_values() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == 2.5  # the mean of the two middle values
    assert percentile([7.0], 0.9) == 7.0
    assert percentile([0.0, 10.0], 0.25) == 2.5


def test_percentile_properties_over_random_lists() -> None:
    rng = random.Random(20260101)
    for _ in range(300):
        values = sorted(rng.uniform(-1e6, 1e6) for _ in range(rng.randint(1, 60)))
        fractions = sorted(rng.random() for _ in range(6))
        results = [percentile(values, f) for f in fractions]
        assert all(values[0] <= r <= values[-1] for r in results)
        assert results == sorted(results)  # never decreases as the fraction grows
        assert percentile(values, 0.0) == values[0] and percentile(values, 1.0) == values[-1]


def test_percentile_of_equal_values_is_that_value() -> None:
    assert percentile([3.3] * 17, 0.37) == 3.3


@pytest.mark.parametrize("bad", ["p101", "p-1", "median", "p", "count,,", "avg,p1000", "P50"])
def test_parse_stats_refuses_unknown_names(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_stats(bad)


def test_parse_stats_defaults_dedupes_and_caps() -> None:
    assert parse_stats(None) == ("count", "min", "max", "avg")
    assert parse_stats("p95, p95 ,sum") == ("p95", "sum")
    with pytest.raises(ValueError):
        parse_stats(",".join(f"p{i}" for i in range(17)))


# ---------------------------------------------------------------- window statistics
def test_window_stats_match_a_plain_python_computation() -> None:
    rng = random.Random(7)
    points = [("a", T0 + rng.randint(0, 5 * HOUR - 1) * 1, rng.uniform(-50, 50)) for _ in range(400)]
    # distinct timestamps so nothing is replaced as a retry
    seen: set[int] = set()
    points = [p for p in points if not (p[1] in seen or seen.add(p[1]))]
    store = _store_with(points)
    simple = store.window_stats(kind="temp", field="j1", bucket_ms=HOUR, start=T0, end=T0 + 5 * HOUR - 1,
                                stats=("count", "min", "max", "avg", "sum"))
    full = store.window_stats(kind="temp", field="j1", bucket_ms=HOUR, start=T0, end=T0 + 5 * HOUR - 1,
                              stats=("count", "min", "max", "avg", "sum", "p50", "p95"))
    assert len(simple) == len(full)
    for s_row, f_row in zip(simple, full, strict=True):
        values = sorted(v for _, t, v in points if t // HOUR * HOUR == s_row["bucketStart"])
        assert s_row["count"] == f_row["count"] == len(values)
        assert s_row["min"] == f_row["min"] == values[0]
        assert s_row["max"] == f_row["max"] == values[-1]
        assert math.isclose(s_row["sum"], math.fsum(values), rel_tol=1e-9, abs_tol=1e-9)  # type: ignore[arg-type]
        assert math.isclose(f_row["avg"], math.fsum(values) / len(values), rel_tol=1e-9, abs_tol=1e-9)  # type: ignore[arg-type]
        assert f_row["p50"] == percentile(values, 0.5) and f_row["p95"] == percentile(values, 0.95)
        assert f_row["min"] <= f_row["p50"] <= f_row["p95"] <= f_row["max"]  # type: ignore[operator]


def test_window_counts_add_up_to_the_points_of_the_range_and_empty_windows_are_absent() -> None:
    store = _store_with([("a", T0, 1.0), ("a", T0 + 10 * HOUR, 2.0), ("a", T0 + 10 * HOUR + 5, 3.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=HOUR, start=T0, end=T0 + 12 * HOUR)
    assert [r["bucketStart"] for r in rows] == [T0, T0 + 10 * HOUR]
    assert sum(r["count"] for r in rows) == 3  # type: ignore[misc]


def test_window_stats_filters_by_source_and_agrees_with_the_existing_aggregate() -> None:
    store = _store_with([("a", T0 + i * 1000, float(i)) for i in range(10)] + [("b", T0 + 500, 100.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=5000, start=T0, end=T0 + 9999, source_id="a", stats=("avg", "count"))
    old = store.aggregate(kind="temp", field="j1", bucket_ms=5000, start=T0, end=T0 + 9999, agg="avg", source_id="a")
    assert [(r["bucketStart"], r["avg"], r["count"]) for r in rows] == [(b.bucket_start, b.value, b.count) for b in old]


def test_percentiles_refuse_a_range_with_more_points_than_the_limit() -> None:
    store = _store_with([("a", T0 + i, float(i)) for i in range(50)])
    with pytest.raises(ValueError, match="more than 10 points"):
        store.window_stats(kind="temp", field="j1", bucket_ms=100, start=T0, end=T0 + 100, stats=("p50",), max_points=10)
    # the simple statistics do not load points, so the limit does not apply to them
    assert store.window_stats(kind="temp", field="j1", bucket_ms=100, start=T0, end=T0 + 100, stats=("count",), max_points=10)


def test_window_stats_limits_the_number_of_windows() -> None:
    store = _store_with([("a", T0, 1.0)])
    with pytest.raises(ValueError, match="buckets"):
        store.window_stats(kind="temp", field="j1", bucket_ms=1, start=T0, end=T0 + 10_000_000)
    with pytest.raises(ValueError):
        store.window_stats(kind="temp", field="j1", bucket_ms=0, start=T0, end=T0 + 5)


# ---------------------------------------------------------------- downsampling with a simulated clock
def _filled(days: int = 10, per_day: int = 24) -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    rng = random.Random(3)
    store.insert_many(
        [
            Sample(source_id=src, kind="temp", timestamp=T0 + d * DAY + h * HOUR, fields={"j1": rng.uniform(0, 100)})
            for d in range(days)
            for h in range(per_day)
            for src in ("a", "b")
        ]
    )
    return store


def test_dry_run_changes_nothing_and_reports_what_would_go(caplog: pytest.LogCaptureFixture) -> None:
    store = _filled()
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=3 * DAY, bucket_ms=DAY)
    before = store.sample_count()
    with caplog.at_level(logging.INFO, logger="hydra_umc_datalake.downsample"):
        report = store.apply_downsample(at_ms=T0 + 10 * DAY)  # dry run is the default
    assert report["dryRun"] is True
    assert store.sample_count() == before
    assert store.read_rollups(kind="temp", field="j1") == []
    # cutoff = day 7: days 0..6 are whole buckets, for two sources
    assert report["buckets"] == 14 and report["rawPoints"] == 14 * 24
    assert any("would remove" in r.message for r in caplog.records)


def test_a_real_run_rolls_up_and_removes_only_whole_old_buckets() -> None:
    store = _filled()
    raw = store.query(kind="temp", field="j1", source_id="a", limit=1000)
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=3 * DAY + HOUR, bucket_ms=DAY)
    report = store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False)
    # cutoff = day 6 + 23h: day 6 is not over yet at that cutoff, so days 0..5 go
    assert report["buckets"] == 12
    left = store.query(kind="temp", field="j1", source_id="a", limit=1000)
    assert min(p.timestamp for p in left) == T0 + 6 * DAY
    rollups = store.read_rollups(kind="temp", field="j1", source_id="a")
    assert [r["bucketStart"] for r in rollups] == [T0 + d * DAY for d in range(6)]
    for r in rollups:
        day = [p.value for p in raw if r["bucketStart"] <= p.timestamp < r["bucketStart"] + DAY]  # type: ignore[operator]
        assert r["count"] == len(day) == 24
        assert r["min"] == min(day) and r["max"] == max(day)
        assert math.isclose(r["sum"], math.fsum(day), rel_tol=1e-9)  # type: ignore[arg-type]


def test_the_total_is_conserved_and_a_second_run_changes_nothing() -> None:
    store = _filled()
    values = [p.value for p in store.query(kind="temp", field="j1", limit=1000)]
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=2 * DAY, bucket_ms=6 * HOUR)
    store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False)
    again = store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False)
    assert again["buckets"] == 0 and again["rawPoints"] == 0
    rolled = store.read_rollups(kind="temp", field="j1", limit=1000)
    kept = store.query(kind="temp", field="j1", limit=1000)
    assert sum(int(r["count"]) for r in rolled) + len(kept) == len(values)  # type: ignore[call-overload]
    assert math.isclose(sum(float(r["sum"]) for r in rolled) + sum(p.value for p in kept), math.fsum(values), rel_tol=1e-9)  # type: ignore[arg-type]


def test_a_late_point_for_a_rolled_up_bucket_is_merged_not_lost() -> None:
    store = _store_with([("a", T0 + HOUR, 10.0), ("a", T0 + 2 * HOUR, 20.0)])
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=DAY, bucket_ms=DAY)
    store.apply_downsample(at_ms=T0 + 5 * DAY, dry_run=False)
    store.insert(Sample(source_id="a", kind="temp", timestamp=T0 + 3 * HOUR, fields={"j1": 60.0}))
    store.apply_downsample(at_ms=T0 + 5 * DAY, dry_run=False)
    (row,) = store.read_rollups(kind="temp", field="j1")
    assert (row["count"], row["min"], row["max"], row["sum"], row["avg"]) == (3, 10.0, 60.0, 90.0, 30.0)
    assert store.sample_count() == 0


def test_the_clock_decides_what_is_old_enough() -> None:
    store = _store_with([("a", T0 + HOUR, 1.0)])
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=2 * DAY, bucket_ms=DAY)
    assert store.apply_downsample(at_ms=T0 + 2 * DAY, dry_run=False)["buckets"] == 0  # day 0 ends at the cutoff + 1 day
    assert store.apply_downsample(at_ms=T0 + 3 * DAY - 1, dry_run=False)["buckets"] == 0
    assert store.apply_downsample(at_ms=T0 + 3 * DAY, dry_run=False)["buckets"] == 1
    assert store.sample_count() == 0


def test_rollups_expire_after_their_own_retention() -> None:
    store = _store_with([("a", T0 + HOUR, 1.0), ("a", T0 + 2 * DAY + HOUR, 2.0)])
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, rollup_retention_ms=5 * DAY)
    store.apply_downsample(at_ms=T0 + 4 * DAY, dry_run=False)
    assert len(store.read_rollups(kind="temp", field="j1")) == 2
    dry = store.apply_downsample(at_ms=T0 + 7 * DAY + 1, dry_run=True)
    assert dry["expiredRollups"] == 1 and len(store.read_rollups(kind="temp", field="j1")) == 2
    store.apply_downsample(at_ms=T0 + 7 * DAY + 1, dry_run=False)
    assert [r["bucketStart"] for r in store.read_rollups(kind="temp", field="j1")] == [T0 + 2 * DAY]


def test_a_run_is_bounded_and_the_next_one_continues() -> None:
    store = _filled(days=6, per_day=2)
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY)
    first = store.apply_downsample(at_ms=T0 + 20 * DAY, dry_run=False, max_buckets=5)
    assert first["buckets"] == 5 and first["truncated"] is True
    second = store.apply_downsample(at_ms=T0 + 20 * DAY, dry_run=False, max_buckets=5)
    assert second["buckets"] == 5 and second["truncated"] is True
    third = store.apply_downsample(at_ms=T0 + 20 * DAY, dry_run=False, max_buckets=5)
    assert third["buckets"] == 2 and third["truncated"] is False and store.sample_count() == 0


def test_default_policy_covers_series_without_their_own() -> None:
    store = TimeSeriesStore(":memory:")
    store.insert(Sample(source_id="a", kind="temp", timestamp=T0 + HOUR, fields={"j1": 1.0, "j2": 2.0}))
    store.set_downsample_policy(kind="*", field="*", raw_ms=HOUR, bucket_ms=DAY)
    store.set_downsample_policy(kind="temp", field="j2", raw_ms=100 * DAY, bucket_ms=DAY)
    store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False)
    assert len(store.read_rollups(kind="temp", field="j1")) == 1
    assert store.read_rollups(kind="temp", field="j2") == []
    assert [p.field for p in store.query()] == ["j2"]


def test_a_retention_shorter_than_the_raw_window_is_warned() -> None:
    store = _store_with([("a", T0, 1.0)])
    store.set_retention_policy(kind="temp", field="j1", retention_ms=HOUR)
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=DAY, bucket_ms=HOUR)
    report = store.apply_downsample(at_ms=T0 + 10 * DAY)
    assert report["warnings"] and "retention" in report["warnings"][0]  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"raw_ms": 0, "bucket_ms": 1}, {"raw_ms": 1, "bucket_ms": 0}, {"raw_ms": -5, "bucket_ms": 1},
        {"raw_ms": 1, "bucket_ms": 1, "rollup_retention_ms": 0}, {"kind": "*", "raw_ms": 1, "bucket_ms": 1},
    ],
)
def test_bad_downsample_policies_are_refused(kwargs: dict[str, Any]) -> None:
    store = TimeSeriesStore(":memory:")
    args: dict[str, Any] = {"kind": "temp", "field": "j1"} | kwargs
    with pytest.raises(ValueError):
        store.set_downsample_policy(**args)


def test_a_store_that_never_enables_downsampling_keeps_its_schema() -> None:
    store = TimeSeriesStore(":memory:")
    names = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "rollups" not in names and "downsample_policies" not in names
    assert store.schema_version == 4


# ---------------------------------------------------------------- HTTP
class Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _serve(**options: Any) -> tuple[DatalakeServer, TimeSeriesStore, str, threading.Thread]:
    store = _filled(days=4, per_day=2)
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store, f"http://127.0.0.1:{server.server_address[1]}", thread


def _stop(server: DatalakeServer, store: TimeSeriesStore, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    store.close()
    thread.join(timeout=2)


def _call(url: str, method: str = "GET", payload: dict | None = None, token: str | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@pytest.fixture()
def windows_url() -> Iterator[str]:
    server, store, url, thread = _serve(enable_window_stats=True, max_window_points=1000)
    try:
        yield url
    finally:
        _stop(server, store, thread)


def test_windows_route_returns_the_requested_statistics(windows_url: str) -> None:
    query = f"kind=temp&field=j1&bucketMs={DAY}&start={T0}&before={T0 + 2 * DAY}&sourceId=a&stats=count,min,p50,p99.9"
    status, body = _call(f"{windows_url}/windows?{query}")
    assert status == 200 and body["stats"] == ["count", "min", "p50", "p99.9"]
    assert [b["bucketStart"] for b in body["buckets"]] == [T0, T0 + DAY]
    assert all(set(b) == {"bucketStart", "count", "min", "p50", "p99.9"} for b in body["buckets"])


@pytest.mark.parametrize(
    "query",
    ["kind=temp", f"kind=temp&field=j1&bucketMs=0&start={T0}&end={T0 + 1}", f"kind=temp&field=j1&bucketMs=5&start={T0}&end={T0 + 1}&stats=p200",
     f"kind=temp&field=j1&bucketMs=5&start={T0}&end={T0 + 1}&before={T0 + 2}", f"kind=temp&field=j1&bucketMs=1&start={T0}&end={T0 + 10**9}"],
)
def test_windows_route_refuses_bad_requests(windows_url: str, query: str) -> None:
    status, body = _call(f"{windows_url}/windows?{query}")
    assert status == 400 and "error" in body


def test_windows_route_is_off_unless_asked_and_does_not_open_the_downsample_routes(windows_url: str) -> None:
    assert _call(f"{windows_url}/rollups?kind=temp&field=j1")[0] == 404
    assert _call(f"{windows_url}/downsample/apply", "POST")[0] == 404


def test_windows_needs_the_read_token_when_one_is_set() -> None:
    server, store, url, thread = _serve(enable_window_stats=True, tokens={"read": "r" * 16})
    try:
        query = f"kind=temp&field=j1&bucketMs={DAY}&start={T0}&end={T0 + DAY}"
        assert _call(f"{url}/windows?{query}")[0] == 401
        assert _call(f"{url}/windows?{query}", token="r" * 16)[0] == 200
    finally:
        _stop(server, store, thread)


def test_downsample_over_http_is_a_dry_run_unless_the_request_says_otherwise() -> None:
    clock = Clock(T0 + 30 * DAY)
    server, store, url, thread = _serve(enable_downsampling=True, clock=clock, tokens={"admin": "a" * 16})
    admin = "a" * 16
    try:
        assert _call(f"{url}/downsample", "POST", {"kind": "temp", "field": "j1", "rawMs": DAY, "bucketMs": DAY})[0] == 401
        status, _ = _call(f"{url}/downsample", "POST", {"kind": "temp", "field": "j1", "rawMs": DAY, "bucketMs": DAY}, admin)
        assert status == 200
        assert _call(f"{url}/downsample", token=admin)[1] == [
            {"kind": "temp", "field": "j1", "rawMs": DAY, "bucketMs": DAY, "rollupRetentionMs": None}
        ]
        before = store.sample_count()
        status, report = _call(f"{url}/downsample/apply", "POST", token=admin)
        assert status == 200 and report["dryRun"] is True and report["buckets"] == 8 and store.sample_count() == before
        status, report = _call(f"{url}/downsample/apply", "POST", {"dryRun": False}, admin)
        assert report["dryRun"] is False and store.sample_count() == 0
        status, rolled = _call(f"{url}/rollups?kind=temp&field=j1&sourceId=a", token=admin)
        assert status == 200 and [r["count"] for r in rolled] == [2, 2, 2, 2]
        assert _call(f"{url}/downsample/apply", "POST", {"dryRun": "no"}, admin)[0] == 400
        assert _call(f"{url}/downsample", "POST", {"kind": "temp", "field": "j1", "rawMs": 0, "bucketMs": DAY}, admin)[0] == 400
        assert _call(f"{url}/downsample", "POST", {"kind": "temp", "field": "j1", "remove": True}, admin)[1]["removed"] is True
    finally:
        _stop(server, store, thread)


def test_the_delete_switch_changes_the_default_of_the_request() -> None:
    server, store, url, thread = _serve(enable_downsampling=True, downsample_dry_run_default=False, clock=Clock(T0 + 30 * DAY))
    try:
        _call(f"{url}/downsample", "POST", {"kind": "temp", "field": "j1", "rawMs": DAY, "bucketMs": DAY})
        assert _call(f"{url}/downsample/apply", "POST")[1]["dryRun"] is False
        assert store.sample_count() == 0
    finally:
        _stop(server, store, thread)


def test_existing_retention_routes_still_work_with_the_features_on() -> None:
    server, store, url, thread = _serve(enable_downsampling=True, enable_window_stats=True)
    try:
        assert _call(f"{url}/retention", "POST", {"kind": "temp", "field": "j1", "retentionMs": DAY})[0] == 200
        assert _call(f"{url}/retention")[1] == [{"kind": "temp", "field": "j1", "retentionMs": DAY}]
    finally:
        _stop(server, store, thread)
