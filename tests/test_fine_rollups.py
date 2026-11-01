# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_fine_rollups.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The optional second, narrower rollup level: off by default, exact when on, and never a change for the coarse level."""
from __future__ import annotations

import json
import math
import random
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from hydra_umc_datalake import main as main_module
from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

T0 = 1_800_000_000_000
MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
T0 = (T0 // DAY) * DAY  # whole days, so the coarse buckets line up with the data


def _filled() -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    rng = random.Random(11)
    store.insert_many(
        [
            Sample(source_id=src, kind="temp", timestamp=T0 + h * HOUR + m * MIN, fields={"j1": rng.uniform(0, 100)})
            for h in range(48)
            for m in (0, 7, 31, 59)
            for src in ("a", "b")
        ]
    )
    return store


# ---------------------------------------------------------------- the store
def test_without_the_switch_a_policy_with_a_fine_width_writes_only_the_coarse_level() -> None:
    store = _filled()
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR)
    report = store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False)
    assert "fineBuckets" not in report
    assert {r["bucketMs"] for r in store.read_rollups(kind="temp", field="j1", limit=1000)} == {DAY}


def test_the_fine_level_adds_up_to_the_coarse_one() -> None:
    store = _filled()
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR)
    report = store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False, fine_rollups=True)
    assert report["fineBuckets"] == 2 * 48
    coarse = store.read_rollups(kind="temp", field="j1", bucket_ms=DAY, limit=1000)
    fine = store.read_rollups(kind="temp", field="j1", bucket_ms=HOUR, limit=1000)
    assert len(coarse) == 4 and len(fine) == 96
    for c in coarse:
        inside = [f for f in fine if f["sourceId"] == c["sourceId"] and c["bucketStart"] <= f["bucketStart"] < c["bucketStart"] + DAY]  # type: ignore[operator]
        assert len(inside) == 24
        assert sum(int(f["count"]) for f in inside) == c["count"]  # type: ignore[call-overload]
        assert min(f["min"] for f in inside) == c["min"]  # type: ignore[type-var]
        assert max(f["max"] for f in inside) == c["max"]  # type: ignore[type-var]
        assert math.isclose(math.fsum(float(f["sum"]) for f in inside), float(c["sum"]), rel_tol=1e-9)  # type: ignore[arg-type]


def test_a_dry_run_counts_the_fine_buckets_and_writes_nothing() -> None:
    store = _filled()
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=6 * HOUR)
    before = store.sample_count()
    report = store.apply_downsample(at_ms=T0 + 10 * DAY, fine_rollups=True)
    assert report["dryRun"] is True and report["fineBuckets"] == 2 * 2 * 4
    assert store.sample_count() == before and store.read_rollups(kind="temp", field="j1") == []


def test_a_late_point_is_merged_into_both_levels() -> None:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id="a", kind="temp", timestamp=T0 + HOUR + 5, fields={"j1": 10.0})])
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR)
    store.apply_downsample(at_ms=T0 + 5 * DAY, dry_run=False, fine_rollups=True)
    store.insert(Sample(source_id="a", kind="temp", timestamp=T0 + HOUR + 9, fields={"j1": 30.0}))
    store.apply_downsample(at_ms=T0 + 5 * DAY, dry_run=False, fine_rollups=True)
    (coarse,) = store.read_rollups(kind="temp", field="j1", bucket_ms=DAY)
    (fine,) = store.read_rollups(kind="temp", field="j1", bucket_ms=HOUR)
    for row in (coarse, fine):
        assert (row["count"], row["min"], row["max"], row["sum"]) == (2, 10.0, 30.0, 40.0)
    assert fine["bucketStart"] == T0 + HOUR


def test_a_second_run_changes_nothing() -> None:
    store = _filled()
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR)
    store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False, fine_rollups=True)
    snapshot = store.read_rollups(kind="temp", field="j1", limit=1000)
    again = store.apply_downsample(at_ms=T0 + 10 * DAY, dry_run=False, fine_rollups=True)
    assert again["buckets"] == 0 and again["fineBuckets"] == 0
    assert store.read_rollups(kind="temp", field="j1", limit=1000) == snapshot


def test_the_policy_listing_shows_the_fine_width_only_once_one_was_asked_for() -> None:
    store = TimeSeriesStore(":memory:")
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY)
    assert "fineBucketMs" not in store.list_downsample_policies()[0]
    store.set_downsample_policy(kind="temp", field="j2", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR)
    by_field = {p["field"]: p for p in store.list_downsample_policies()}
    assert by_field["j1"]["fineBucketMs"] is None and by_field["j2"]["fineBucketMs"] == HOUR
    store.set_downsample_policy(kind="temp", field="j2", raw_ms=HOUR, bucket_ms=DAY)  # replacing without it clears it
    assert {p["field"]: p for p in store.list_downsample_policies()}["j2"]["fineBucketMs"] is None


def test_a_store_without_a_fine_policy_keeps_the_columns_it_always_had() -> None:
    store = TimeSeriesStore(":memory:")
    store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY)
    columns = [row[1] for row in store._conn.execute("PRAGMA table_info(downsample_policies)")]
    assert "fine_bucket_ms" not in columns


@pytest.mark.parametrize(
    "fine",
    [0, -1, DAY, 2 * DAY, 7 * HOUR, 5],  # 5 ms gives far more than 1440 fine buckets
)
def test_bad_fine_widths_are_refused(fine: int) -> None:
    store = TimeSeriesStore(":memory:")
    with pytest.raises(ValueError):
        store.set_downsample_policy(kind="temp", field="j1", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=fine)


def test_the_ratio_limit_is_configurable() -> None:
    store = TimeSeriesStore(":memory:")
    with pytest.raises(ValueError):
        store.set_downsample_policy(kind="t", field="f", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR, max_fine_per_coarse=23)
    store.set_downsample_policy(kind="t", field="f", raw_ms=HOUR, bucket_ms=DAY, fine_bucket_ms=HOUR, max_fine_per_coarse=24)


# ---------------------------------------------------------------- HTTP
def _call(url: str, method: str = "GET", payload: dict | None = None) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _serve(**options: Any) -> tuple[DatalakeServer, TimeSeriesStore, str, threading.Thread]:
    store = _filled()
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, clock=lambda: T0 + 10 * DAY, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store, f"http://127.0.0.1:{server.server_address[1]}", thread


def _stop(server: DatalakeServer, store: TimeSeriesStore, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    store.close()
    thread.join(timeout=2)


POLICY = {"kind": "temp", "field": "j1", "rawMs": HOUR, "bucketMs": DAY, "fineBucketMs": HOUR}


def test_over_http_the_fine_width_is_refused_while_the_switch_is_off() -> None:
    server, store, url, thread = _serve(enable_downsampling=True)
    try:
        status, body = _call(f"{url}/downsample", "POST", POLICY)
        assert status == 400 and "fineBucketMs" in body["error"]
        assert _call(f"{url}/downsample", "POST", {k: v for k, v in POLICY.items() if k != "fineBucketMs"})[0] == 200
    finally:
        _stop(server, store, thread)


def test_the_switch_needs_downsampling() -> None:
    server, store, url, thread = _serve(enable_fine_rollups=True)  # downsampling off: the switch has no effect
    try:
        assert server.enable_fine_rollups is False
        assert _call(f"{url}/downsample", "POST", POLICY)[0] == 404
    finally:
        _stop(server, store, thread)


def test_over_http_a_run_writes_both_levels_and_the_listing_shows_the_width() -> None:
    server, store, url, thread = _serve(enable_downsampling=True, enable_fine_rollups=True, max_fine_per_coarse=48)
    try:
        assert _call(f"{url}/downsample", "POST", POLICY)[0] == 200
        assert _call(f"{url}/downsample", "POST", {**POLICY, "fineBucketMs": 60_000})[0] == 400  # 1440 per day > 48
        assert _call(f"{url}/downsample")[1][0]["fineBucketMs"] == HOUR
        status, report = _call(f"{url}/downsample/apply", "POST", {"dryRun": False})
        assert status == 200 and report["fineBuckets"] == 96
        status, rows = _call(f"{url}/rollups?kind=temp&field=j1&bucketMs={HOUR}&limit=1000")
        assert status == 200 and len(rows) == 96
    finally:
        _stop(server, store, thread)


def test_a_bad_fine_value_over_http_is_a_400() -> None:
    server, store, url, thread = _serve(enable_downsampling=True, enable_fine_rollups=True)
    try:
        for bad in ("x", 1.5, True, -1):
            assert _call(f"{url}/downsample", "POST", {**POLICY, "fineBucketMs": bad})[0] == 400
    finally:
        _stop(server, store, thread)


# ---------------------------------------------------------------- the command line and the environment file
def _capture(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    class FakeServer:
        def __init__(self, address, store, **options) -> None:
            seen["options"] = options

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(main_module, "DatalakeServer", FakeServer)
    monkeypatch.delenv(main_module.FINE_ROLLUPS_ENV, raising=False)
    monkeypatch.delenv(main_module.MAX_FINE_PER_COARSE_ENV, raising=False)
    return seen


def test_the_fine_level_is_off_by_default_on_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    assert main_module.main(["--db", ":memory:", "--enable-downsampling"]) == 0
    assert seen["options"]["enable_fine_rollups"] is False and seen["options"]["max_fine_per_coarse"] == 1440


def test_the_flag_and_the_environment_file_turn_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture(monkeypatch)
    assert main_module.main(["--db", ":memory:", "--enable-downsampling", "--enable-fine-rollups", "--max-fine-per-coarse", "60"]) == 0
    assert seen["options"]["enable_fine_rollups"] is True and seen["options"]["max_fine_per_coarse"] == 60
    monkeypatch.setenv(main_module.FINE_ROLLUPS_ENV, "true")
    monkeypatch.setenv(main_module.MAX_FINE_PER_COARSE_ENV, "120")
    assert main_module.main(["--db", ":memory:", "--enable-downsampling"]) == 0
    assert seen["options"]["enable_fine_rollups"] is True and seen["options"]["max_fine_per_coarse"] == 120


@pytest.mark.parametrize(
    "flags, env",
    [
        (["--enable-fine-rollups"], {}),  # without --enable-downsampling
        (["--enable-downsampling", "--max-fine-per-coarse", "0"], {}),
        (["--enable-downsampling"], {"HYDRA_UMC_DATALAKE_FINE_ROLLUPS": "yes"}),
        (["--enable-downsampling"], {"HYDRA_UMC_DATALAKE_MAX_FINE_PER_COARSE": "many"}),
        (["--enable-downsampling"], {"HYDRA_UMC_DATALAKE_MAX_FINE_PER_COARSE": "-3"}),
    ],
)
def test_wrong_values_stop_the_start(monkeypatch: pytest.MonkeyPatch, flags: list[str], env: dict[str, str]) -> None:
    _capture(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert main_module.main(["--db", ":memory:", *flags]) == 2
