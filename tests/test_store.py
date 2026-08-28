# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_store.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hydra_umc_datalake.store import Sample, TimeSeriesStore, now_ms, to_utc_iso8601


@pytest.fixture()
def store():
    with TimeSeriesStore(":memory:") as s:
        yield s


def test_insert_writes_one_row_per_field(store: TimeSeriesStore) -> None:
    written = store.insert(
        Sample(source_id="robot-1", kind="motor_temp", timestamp=1000, fields={"value": 42.5, "ambient": 20.0})
    )
    assert written == 2
    assert store.sample_count() == 2


def test_insert_empty_fields_writes_nothing(store: TimeSeriesStore) -> None:
    written = store.insert(Sample(source_id="robot-1", kind="motor_temp", timestamp=1000, fields={}))
    assert written == 0
    assert store.sample_count() == 0


def test_query_filters_and_orders_by_timestamp(store: TimeSeriesStore) -> None:
    store.insert(Sample(source_id="robot-1", kind="motor_temp", timestamp=3000, fields={"value": 3.0}))
    store.insert(Sample(source_id="robot-1", kind="motor_temp", timestamp=1000, fields={"value": 1.0}))
    store.insert(Sample(source_id="robot-2", kind="motor_temp", timestamp=2000, fields={"value": 2.0}))

    points = store.query(source_id="robot-1")
    assert [p.timestamp for p in points] == [1000, 3000]
    assert all(p.source_id == "robot-1" for p in points)


def test_query_time_range_is_inclusive(store: TimeSeriesStore) -> None:
    for ts in (1000, 2000, 3000):
        store.insert(Sample(source_id="r1", kind="k", timestamp=ts, fields={"v": float(ts)}))

    points = store.query(start=1000, end=2000)
    assert [p.timestamp for p in points] == [1000, 2000]


def test_aggregate_avg_buckets_correctly() -> None:
    # A scenario chosen so the correct answer is computable by hand:
    # bucket 0 = [0, 1000): values 10, 20 -> avg 15
    # bucket 1 = [1000, 2000): value 100 -> avg 100
    store = TimeSeriesStore(":memory:")
    store.insert(Sample(source_id="r1", kind="temp", timestamp=100, fields={"v": 10.0}))
    store.insert(Sample(source_id="r1", kind="temp", timestamp=500, fields={"v": 20.0}))
    store.insert(Sample(source_id="r1", kind="temp", timestamp=1200, fields={"v": 100.0}))

    buckets = store.aggregate(kind="temp", field="v", bucket_ms=1000, start=0, end=1999)

    assert len(buckets) == 2
    assert buckets[0].bucket_start == 0
    assert buckets[0].value == pytest.approx(15.0)
    assert buckets[0].count == 2
    assert buckets[1].bucket_start == 1000
    assert buckets[1].value == pytest.approx(100.0)
    assert buckets[1].count == 1


def test_aggregate_supports_min_max_sum() -> None:
    store = TimeSeriesStore(":memory:")
    for v in (5.0, 15.0, 25.0):
        store.insert(Sample(source_id="r1", kind="k", timestamp=100, fields={"v": v}))

    assert store.aggregate(kind="k", field="v", bucket_ms=1000, start=0, end=999, agg="min")[0].value == 5.0
    assert store.aggregate(kind="k", field="v", bucket_ms=1000, start=0, end=999, agg="max")[0].value == 25.0
    assert store.aggregate(kind="k", field="v", bucket_ms=1000, start=0, end=999, agg="sum")[0].value == 45.0


def test_aggregate_rejects_unknown_function() -> None:
    store = TimeSeriesStore(":memory:")
    with pytest.raises(ValueError):
        store.aggregate(kind="k", field="v", bucket_ms=1000, start=0, end=999, agg="median")


def test_aggregate_rejects_non_positive_bucket() -> None:
    store = TimeSeriesStore(":memory:")
    with pytest.raises(ValueError):
        store.aggregate(kind="k", field="v", bucket_ms=0, start=0, end=999)


def test_timestamp_range_on_empty_store_is_none_not_zero(store: TimeSeriesStore) -> None:
    # (0, 0) would itself be a real, valid timestamp
    # (1970-01-01T00:00:00Z) - an empty store must report None, never a
    # value indistinguishable from real epoch-zero data.
    assert store.timestamp_range() == (None, None)


def test_timestamp_range_reports_real_oldest_and_newest(store: TimeSeriesStore) -> None:
    store.insert(Sample(source_id="r1", kind="k", timestamp=3000, fields={"v": 1.0}))
    store.insert(Sample(source_id="r1", kind="k", timestamp=1000, fields={"v": 2.0}))
    store.insert(Sample(source_id="r1", kind="k", timestamp=2000, fields={"v": 3.0}))

    assert store.timestamp_range() == (1000, 3000)


def test_to_utc_iso8601_is_real_and_explicit() -> None:
    # 2026-01-01T00:00:00Z in unix ms, a real, hand-verifiable value.
    assert to_utc_iso8601(1767225600000) == "2026-01-01T00:00:00+00:00"


def test_now_ms_reflects_real_utc_wall_clock_time() -> None:
    # A real guarantee test: this store's own clock must actually be UTC,
    # not local time - if now_ms() ever regressed to time.localtime()-based
    # math, this is what would catch it on a non-UTC dev machine.
    before = int(datetime.now(timezone.utc).timestamp() * 1000)
    measured = now_ms()
    after = int(datetime.now(timezone.utc).timestamp() * 1000)

    assert before - 1000 <= measured <= after + 1000


def test_retention_policy_rejects_non_positive_window(store: TimeSeriesStore) -> None:
    with pytest.raises(ValueError):
        store.set_retention_policy(kind="k", field="v", retention_ms=0)
    with pytest.raises(ValueError):
        store.set_retention_policy(kind="k", field="v", retention_ms=-1)


def test_retention_policy_round_trip(store: TimeSeriesStore) -> None:
    assert store.get_retention_policy(kind="k", field="v") is None

    store.set_retention_policy(kind="k", field="v", retention_ms=60_000)

    assert store.get_retention_policy(kind="k", field="v") == 60_000
    assert store.list_retention_policies() == [("k", "v", 60_000)]


def test_retention_policy_set_twice_updates_not_duplicates(store: TimeSeriesStore) -> None:
    store.set_retention_policy(kind="k", field="v", retention_ms=1000)
    store.set_retention_policy(kind="k", field="v", retention_ms=2000)

    assert store.list_retention_policies() == [("k", "v", 2000)]


def test_apply_retention_deletes_only_expired_samples_of_a_configured_series(
    store: TimeSeriesStore,
) -> None:
    store.insert(Sample(source_id="r1", kind="temp", timestamp=1000, fields={"v": 1.0}))
    store.insert(Sample(source_id="r1", kind="temp", timestamp=9000, fields={"v": 2.0}))
    store.set_retention_policy(kind="temp", field="v", retention_ms=5000)

    deleted = store.apply_retention(at_ms=10_000)

    assert deleted == 1
    remaining = store.query(kind="temp", field="v")
    assert [p.timestamp for p in remaining] == [9000]


def test_apply_retention_never_touches_a_series_with_no_policy(store: TimeSeriesStore) -> None:
    store.insert(Sample(source_id="r1", kind="untracked", timestamp=1, fields={"v": 1.0}))

    deleted = store.apply_retention(at_ms=10_000_000)

    assert deleted == 0
    assert store.sample_count() == 1
