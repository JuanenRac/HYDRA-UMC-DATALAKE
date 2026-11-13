# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_simulation_bench.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Simulation bench: the real store and the real HTTP server against a simulated plant.

The plant is a fleet of sensors whose readings are drawn from a fixed seed and sent the way a flaky network delivers them: out of order, in batches, twice, from
several threads at once. The checks compare what the data lake returns with an independent model kept by the test, and the scenarios inject what goes wrong in a
plant: a restart, a copy of the database, a file that is not a database, readings that are not numbers, a body that is not JSON, a timestamp from the future,
a writer without a token. Nothing here says how the store behaves on the disk of a real controller: that needs the controller."""
from __future__ import annotations

import json
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

SEED = 20260101
SENSORS = ("press-1", "press-2", "press-3", "oven-1")
BASE_MS = 1_700_000_000_000


@contextmanager
def _running(store: TimeSeriesStore, **options) -> Iterator[str]:
    options.setdefault("min_timestamp_ms", 0)
    options.setdefault("max_future_skew_ms", 0)
    server = DatalakeServer(("127.0.0.1", 0), store, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _call(base: str, method: str, path: str, payload: object = None, *, token: str | None = None, raw: bytes | None = None) -> tuple[int, object]:
    data = raw if raw is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        body = error.read()
        return error.code, json.loads(body) if body else None


def _plant(rng: random.Random, points: int) -> list[dict]:
    """`points` readings per sensor, one per second, with a slow drift and noise."""
    samples = []
    for sensor in SENSORS:
        level = rng.uniform(20, 80)
        for index in range(points):
            samples.append({"sourceId": sensor, "kind": "temperature", "timestamp": BASE_MS + index * 1000,
                            "fields": {"value": round(level + 0.01 * index + rng.gauss(0, 0.3), 3)}})
    return samples


def _model(samples: list[dict]) -> dict[tuple[str, int], float]:
    return {(s["sourceId"], s["timestamp"]): s["fields"]["value"] for s in samples}


def _read_all(base: str) -> dict[tuple[str, int], float]:
    status, points = _call(base, "GET", "/query?kind=temperature&limit=100000")
    assert status == 200
    return {(p["sourceId"], p["timestamp"]): p["value"] for p in points}  # type: ignore[index]


def test_a_flaky_network_that_reorders_batches_and_retries_them_leaves_exactly_the_model() -> None:
    rng = random.Random(SEED)
    samples = _plant(rng, 60)
    delivery = [samples[i:i + 25] for i in range(0, len(samples), 25)]
    rng.shuffle(delivery)
    delivery += rng.sample(delivery, 4)  # the answer of four batches was lost and the sender tried again
    with TimeSeriesStore(":memory:") as store, _running(store) as base:
        for batch in delivery:
            status, body = _call(base, "POST", "/ingest/batch", {"samples": batch})
            assert status in (200, 202), body
        assert _read_all(base) == _model(samples)
        assert store.sample_count() == len(SENSORS) * 60


def test_several_writers_at_once_lose_nothing() -> None:
    rng = random.Random(SEED + 1)
    samples = _plant(rng, 40)
    with TimeSeriesStore(":memory:") as store, _running(store) as base:
        errors: list[object] = []

        def writer(chunk: list[dict]) -> None:
            for sample in chunk:
                status, body = _call(base, "POST", "/ingest", sample)
                if status != 202:
                    errors.append((status, body))

        threads = [threading.Thread(target=writer, args=(samples[i::4],)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        assert _read_all(base) == _model(samples)


def test_a_restart_of_the_service_keeps_everything_that_was_acknowledged(tmp_path: Path) -> None:
    rng = random.Random(SEED + 2)
    samples = _plant(rng, 30)
    path = tmp_path / "plant.sqlite3"
    first = TimeSeriesStore(path)
    with _running(first) as base:
        assert _call(base, "POST", "/ingest/batch", {"samples": samples[:60]})[0] in (200, 202)
    first.close()
    second = TimeSeriesStore(path)
    with _running(second) as base:
        assert _read_all(base) == _model(samples[:60])
        assert _call(base, "POST", "/ingest/batch", {"samples": samples[60:]})[0] in (200, 202)
        assert _read_all(base) == _model(samples)
    second.close()


def test_a_backup_taken_while_the_plant_writes_is_a_complete_database_of_its_own(tmp_path: Path) -> None:
    rng = random.Random(SEED + 3)
    samples = _plant(rng, 30)
    store = TimeSeriesStore(tmp_path / "plant.sqlite3")
    with _running(store) as base:
        assert _call(base, "POST", "/ingest/batch", {"samples": samples[:50]})[0] in (200, 202)
        copy = store.backup(tmp_path / "backups", keep=2, now=1_800_000_000)
        assert _call(base, "POST", "/ingest/batch", {"samples": samples[50:]})[0] in (200, 202)
    store.close()
    restored = TimeSeriesStore(copy)
    with _running(restored) as base:
        assert _read_all(base) == _model(samples[:50])  # the copy knows nothing of what came after it
    restored.close()


def test_the_backups_beyond_the_ones_to_keep_are_removed_oldest_first(tmp_path: Path) -> None:
    store = TimeSeriesStore(tmp_path / "plant.sqlite3")
    store.insert(Sample("press-1", "temperature", BASE_MS, {"value": 1.0}))
    made = [store.backup(tmp_path / "backups", keep=3, now=1_800_000_000 + 60 * n) for n in range(5)]
    left = sorted(p.name for p in (tmp_path / "backups").glob("datalake-*.sqlite3"))
    assert left == sorted(p.name for p in made[-3:])
    store.close()


def test_a_file_that_is_not_a_database_is_refused_and_left_alone(tmp_path: Path) -> None:
    path = tmp_path / "plant.sqlite3"
    path.write_bytes(b"this is not a database, a controller lost power while it was being copied" * 20)
    with pytest.raises(sqlite3.DatabaseError):
        TimeSeriesStore(path)
    assert path.read_bytes().startswith(b"this is not a database")


def test_readings_that_are_not_numbers_never_reach_the_store() -> None:
    with TimeSeriesStore(":memory:") as store, _running(store) as base:
        good = {"sourceId": "press-1", "kind": "temperature", "timestamp": BASE_MS, "fields": {"value": 21.5}}
        assert _call(base, "POST", "/ingest", good)[0] == 202
        for bad in (
            b'{"sourceId": "press-1", "kind": "temperature", "timestamp": 5, "fields": {"value": NaN}}',
            b'{"sourceId": "press-1", "kind": "temperature", "timestamp": 5, "fields": {"value": Infinity}}',
            b'{"sourceId": "press-1", "kind": "temperature", "timestamp": 5, "fields": {"value": "hot"}}',
            b'{"sourceId": "press-1", "kind": "temperature", "timestamp": 5, "fields": {"value": null}}',
            b'{"sourceId": "", "kind": "temperature", "timestamp": 5, "fields": {"value": 1}}',
            b'{"sourceId": "press-1", "kind": "temperature", "timestamp": "now", "fields": {"value": 1}}',
            b"[1, 2, 3]",
            b"{not json",
            b"",
        ):
            status, _ = _call(base, "POST", "/ingest", raw=bad)
            assert 400 <= status < 500, bad
        assert store.sample_count() == 1


def test_a_batch_with_one_bad_sample_stores_none_of_it() -> None:
    rng = random.Random(SEED + 4)
    batch = _plant(rng, 5)
    batch[7]["fields"]["value"] = "broken"
    with TimeSeriesStore(":memory:") as store, _running(store) as base:
        status, body = _call(base, "POST", "/ingest/batch", {"samples": batch})
        assert status == 400 and "samples[7]" in str(body)
        assert store.sample_count() == 0


def test_a_batch_over_the_limit_and_a_body_over_the_limit_are_refused() -> None:
    rng = random.Random(SEED + 5)
    with TimeSeriesStore(":memory:") as store, _running(store, max_batch_samples=10, max_request_body_bytes=2048) as base:
        assert _call(base, "POST", "/ingest/batch", {"samples": _plant(rng, 5)[:11]})[0] == 400
        assert _call(base, "POST", "/ingest/batch", {"samples": _plant(rng, 200)})[0] == 413
        assert _call(base, "POST", "/ingest/batch", {"samples": []})[0] == 400
        assert store.sample_count() == 0


def test_a_timestamp_from_the_future_is_refused_when_the_clock_says_so() -> None:
    now = int(time.time() * 1000)
    with TimeSeriesStore(":memory:") as store, _running(store, min_timestamp_ms=BASE_MS - 3_600_000, max_future_skew_ms=60_000) as base:
        ok = {"sourceId": "press-1", "kind": "temperature", "timestamp": now - 500, "fields": {"value": 1.0}}
        future = {**ok, "timestamp": now + 3_600_000}
        ancient = {**ok, "timestamp": 1_000}
        assert _call(base, "POST", "/ingest", ok)[0] == 202
        assert 400 <= _call(base, "POST", "/ingest", future)[0] < 500
        assert 400 <= _call(base, "POST", "/ingest", ancient)[0] < 500
        assert store.sample_count() == 1


def test_a_writer_without_the_token_cannot_write_and_a_reader_cannot_administer() -> None:
    sample = {"sourceId": "press-1", "kind": "temperature", "timestamp": BASE_MS, "fields": {"value": 3.0}}
    tokens = {"ingest": "i" * 24, "admin": "a" * 24, "read": "r" * 24}
    with TimeSeriesStore(":memory:") as store, _running(store, tokens=tokens) as base:
        assert _call(base, "POST", "/ingest", sample)[0] == 401
        assert _call(base, "POST", "/ingest", sample, token="wrong" * 6)[0] == 401
        assert _call(base, "POST", "/ingest", sample, token=tokens["read"])[0] == 401
        assert _call(base, "POST", "/ingest", sample, token=tokens["ingest"])[0] == 202
        assert _call(base, "POST", "/retention/apply", token=tokens["read"])[0] == 401
        assert _call(base, "GET", "/query", token=tokens["read"])[0] == 200
        assert store.sample_count() == 1


def test_retention_removes_only_what_is_older_than_its_window_and_the_rest_matches_the_model() -> None:
    rng = random.Random(SEED + 6)
    samples = _plant(rng, 120)
    last = BASE_MS + 119 * 1000
    with TimeSeriesStore(":memory:") as store, _running(store) as base:
        assert _call(base, "POST", "/ingest/batch", {"samples": samples})[0] in (200, 202)
        assert _call(base, "POST", "/retention", {"kind": "temperature", "field": "value", "retentionMs": 30_000})[0] == 200
        # the policy is a rule, not an action: nothing is deleted until it is applied
        assert store.sample_count() == len(samples)
        store.apply_retention(at_ms=last)
        kept = {key: value for key, value in _model(samples).items() if key[1] >= last - 30_000}
        assert _read_all(base) == kept
        assert store.apply_retention(at_ms=last) == 0  # applying it again changes nothing


def test_the_buckets_of_the_aggregate_match_an_average_computed_by_hand() -> None:
    rng = random.Random(SEED + 7)
    samples = [s for s in _plant(rng, 100) if s["sourceId"] == "press-1"]
    with TimeSeriesStore(":memory:") as store:
        for sample in samples:
            store.insert(Sample(sample["sourceId"], sample["kind"], sample["timestamp"], sample["fields"]))
        buckets = store.aggregate(kind="temperature", field="value", bucket_ms=10_000, start=BASE_MS, end=BASE_MS + 99_000, agg="avg")
        assert len(buckets) == 10
        for index, bucket in enumerate(buckets):
            window = [s["fields"]["value"] for s in samples[index * 10:(index + 1) * 10]]
            assert bucket.count == 10 and bucket.value == pytest.approx(sum(window) / 10)
            assert bucket.bucket_start == BASE_MS + index * 10_000


def test_the_same_seed_gives_the_same_database_and_another_seed_does_not() -> None:
    def run(seed: int) -> dict[tuple[str, int], float]:
        with TimeSeriesStore(":memory:") as store, _running(store) as base:
            assert _call(base, "POST", "/ingest/batch", {"samples": _plant(random.Random(seed), 20)})[0] in (200, 202)
            return _read_all(base)

    assert run(SEED) == run(SEED)
    assert run(SEED) != run(SEED + 1)
