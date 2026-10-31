# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_backward_windows.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Windows aligned to the late end (a read that runs backwards in time) and rollups newest first.

The forward behaviour is pinned first (it must not change); the backward windows are then compared with a plain
Python computation over random data, and the edges (a point exactly on a window edge, on the start, on the end) are
checked one by one."""
from __future__ import annotations

import json
import random
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

T0 = 1_800_000_000_000


def _store(points: list[tuple[str, int, float]]) -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id=s, kind="temp", timestamp=t, fields={"j1": v}) for s, t, v in points])
    return store


# ---------------------------------------------------------------- the forward windows do not change
def test_forward_windows_keep_their_shape_and_closed_start() -> None:
    store = _store([("a", T0, 1.0), ("a", T0 + 9, 2.0), ("a", T0 + 10, 3.0), ("a", T0 + 25, 4.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0, end=T0 + 25, stats=("count", "sum"))
    assert rows == [
        {"bucketStart": T0, "count": 2, "sum": 3.0},
        {"bucketStart": T0 + 10, "count": 1, "sum": 3.0},
        {"bucketStart": T0 + 20, "count": 1, "sum": 4.0},
    ]


# ---------------------------------------------------------------- backward windows
def test_backward_windows_are_open_at_the_early_edge_and_closed_at_the_late_one() -> None:
    # end = T0 + 30, bucket 10: (T0+20, T0+30], (T0+10, T0+20], (T0, T0+10]
    store = _store([("a", T0, 100.0), ("a", T0 + 1, 1.0), ("a", T0 + 10, 2.0), ("a", T0 + 11, 3.0), ("a", T0 + 20, 4.0), ("a", T0 + 30, 5.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0, end=T0 + 30, stats=("count", "min", "max"), backward=True)
    assert rows == [
        {"bucketStart": T0, "bucketEnd": T0 + 10, "count": 2, "min": 1.0, "max": 2.0},
        {"bucketStart": T0 + 10, "bucketEnd": T0 + 20, "count": 2, "min": 3.0, "max": 4.0},
        {"bucketStart": T0 + 20, "bucketEnd": T0 + 30, "count": 1, "min": 5.0, "max": 5.0},
    ]  # the point exactly on start (100.0) belongs to no window, like in a backward processed read


def test_backward_earliest_window_is_cut_by_start_but_reports_its_full_edges() -> None:
    store = _store([("a", T0 + 3, 1.0), ("a", T0 + 8, 2.0), ("a", T0 + 14, 3.0), ("a", T0 + 29, 4.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0 + 5, end=T0 + 29, stats=("count",), backward=True)
    assert [(r["bucketStart"], r["bucketEnd"], r["count"]) for r in rows] == [(T0 - 1, T0 + 9, 1), (T0 + 9, T0 + 19, 1), (T0 + 19, T0 + 29, 1)]
    assert sum(int(r["count"]) for r in rows) == 3


def test_backward_matches_a_plain_python_computation() -> None:
    rng = random.Random(77)
    points = [("a" if rng.random() < 0.6 else "b", T0 + rng.randint(0, 5000), rng.uniform(-50, 50)) for _ in range(400)]
    points = list({(s, t): (s, t, v) for s, t, v in points}.values())  # one value per source and instant
    store = _store(points)
    start, end, width = T0 + 123, T0 + 4987, 400
    expected: dict[int, list[float]] = {}
    for _, t, v in points:
        if start < t <= end:
            expected.setdefault((end - t) // width, []).append(v)
    for source in (None, "a"):
        wanted = {i: [v for (s, t, v) in points if start < t <= end and (end - t) // width == i and (source is None or s == source)] for i in expected}
        rows = store.window_stats(kind="temp", field="j1", bucket_ms=width, start=start, end=end, stats=("count", "min", "max", "sum"), source_id=source, backward=True)
        got = {(end - int(r["bucketEnd"])) // width: r for r in rows}  # type: ignore[call-overload]
        assert {i for i, v in wanted.items() if v} == set(got)
        for i, values in wanted.items():
            if not values:
                continue
            row = got[i]
            assert row["count"] == len(values) and row["min"] == min(values) and row["max"] == max(values)
            assert row["sum"] == pytest.approx(sum(values))
            assert row["bucketEnd"] == end - i * width and row["bucketStart"] == end - (i + 1) * width
        ends = [r["bucketEnd"] for r in rows]
        assert ends == sorted(ends)  # oldest first


def test_backward_percentiles_agree_with_the_simple_statistics_and_stay_oldest_first() -> None:
    store = _store([("a", T0 + i, float(i)) for i in range(1, 41)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0, end=T0 + 40, stats=("count", "min", "p50", "max"), backward=True)
    assert [r["bucketEnd"] for r in rows] == [T0 + 10, T0 + 20, T0 + 30, T0 + 40]
    assert [r["count"] for r in rows] == [10, 10, 10, 10]
    assert rows[0]["min"] == 1.0 and rows[0]["max"] == 10.0 and rows[0]["p50"] == 5.5


def test_backward_with_no_points_or_a_window_without_points_returns_nothing_for_it() -> None:
    store = _store([("a", T0 + 5, 1.0), ("a", T0 + 95, 2.0)])
    rows = store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0, end=T0 + 100, stats=("count",), backward=True)
    assert [r["bucketEnd"] for r in rows] == [T0 + 10, T0 + 100]
    assert store.window_stats(kind="temp", field="j1", bucket_ms=10, start=T0 + 200, end=T0 + 300, backward=True) == []


def test_backward_keeps_the_limits_of_the_forward_windows() -> None:
    store = _store([("a", T0, 1.0)])
    with pytest.raises(ValueError):
        store.window_stats(kind="temp", field="j1", bucket_ms=0, start=T0, end=T0 + 1, backward=True)
    with pytest.raises(ValueError):
        store.window_stats(kind="temp", field="j1", bucket_ms=1, start=T0, end=T0 + 10**9, backward=True)
    many = _store([("a", T0 + i, 1.0) for i in range(1, 30)])
    with pytest.raises(ValueError):
        many.window_stats(kind="temp", field="j1", bucket_ms=100, start=T0, end=T0 + 30, stats=("p50",), max_points=10, backward=True)


# ---------------------------------------------------------------- rollups newest first
def test_rollups_can_be_read_newest_first_and_a_limit_keeps_the_most_recent() -> None:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id="a", kind="temp", timestamp=T0 + i * 100, fields={"j1": float(i)}) for i in range(10)])
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=1, bucket_ms=100)
    store.apply_downsample(at_ms=T0 + 10_000, dry_run=False)
    up = store.read_rollups(kind="temp", field="j1")
    down = store.read_rollups(kind="temp", field="j1", descending=True)
    assert [r["bucketStart"] for r in down] == [r["bucketStart"] for r in reversed(up)] and len(up) == 10
    newest = store.read_rollups(kind="temp", field="j1", limit=3, descending=True)
    assert [r["bucketStart"] for r in newest] == [T0 + 900, T0 + 800, T0 + 700]
    oldest = store.read_rollups(kind="temp", field="j1", limit=3)
    assert [r["bucketStart"] for r in oldest] == [T0, T0 + 100, T0 + 200]


# ---------------------------------------------------------------- HTTP
@pytest.fixture()
def served() -> Iterator[str]:
    store = _store([("a", T0 + i * 5, float(i)) for i in range(1, 21)])
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, enable_window_stats=True, enable_downsampling=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def _get(url: str) -> tuple[int, Any]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_the_route_answers_backward_windows_and_says_so(served: str) -> None:
    status, body = _get(f"{served}/windows?kind=temp&field=j1&bucketMs=50&start={T0}&end={T0 + 100}&stats=count&order=desc")
    assert status == 200 and body["order"] == "desc"
    assert [(b["bucketStart"], b["bucketEnd"], b["count"]) for b in body["buckets"]] == [(T0, T0 + 50, 10), (T0 + 50, T0 + 100, 10)]


def test_the_route_without_order_is_the_old_answer(served: str) -> None:
    status, body = _get(f"{served}/windows?kind=temp&field=j1&bucketMs=50&start={T0}&before={T0 + 100}&stats=count")
    assert status == 200 and "order" not in body
    assert all(set(b) == {"bucketStart", "count"} for b in body["buckets"])
    assert _get(f"{served}/windows?kind=temp&field=j1&bucketMs=50&start={T0}&before={T0 + 100}&stats=count&order=asc")[1] == body


@pytest.mark.parametrize("order", ["up", "DESC", "1"])
def test_the_route_refuses_an_unknown_order(served: str, order: str) -> None:
    assert _get(f"{served}/windows?kind=temp&field=j1&bucketMs=50&start={T0}&end={T0 + 100}&order={order}")[0] == 400
    assert _get(f"{served}/rollups?kind=temp&field=j1&order={order}")[0] == 400


def test_the_rollups_route_takes_order(served: str) -> None:
    status, body = _get(f"{served}/rollups?kind=temp&field=j1&order=desc")
    assert status == 200 and body == []  # nothing rolled up yet; the parameter is accepted
