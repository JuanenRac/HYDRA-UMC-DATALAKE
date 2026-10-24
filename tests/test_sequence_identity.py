# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_sequence_identity.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Two real readings in the same millisecond are two points when the producer says they are different (sequence); a retry of the same
one still replaces itself, and a producer that sends no sequence behaves exactly as before."""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import MAX_SEQUENCE, Sample, TimeSeriesStore, migrate_up


def _values(store: TimeSeriesStore) -> list[float]:
    return sorted(point.value for point in store.query(source_id="r1"))


def test_two_readings_of_the_same_millisecond_with_different_sequences_are_both_kept() -> None:
    with TimeSeriesStore(":memory:") as store:
        store.insert(Sample("r1", "k", 1000, {"v": 1.0}, sequence=1))
        store.insert(Sample("r1", "k", 1000, {"v": 2.0}, sequence=2))
        assert _values(store) == [1.0, 2.0]
        assert store.sample_count() == 2


def test_a_retry_of_the_same_sequence_replaces_itself() -> None:
    with TimeSeriesStore(":memory:") as store:
        store.insert(Sample("r1", "k", 1000, {"v": 1.0}, sequence=7))
        store.insert(Sample("r1", "k", 1000, {"v": 1.5}, sequence=7))
        assert _values(store) == [1.5]


def test_without_a_sequence_the_identity_is_the_old_one() -> None:
    with TimeSeriesStore(":memory:") as store:
        store.insert(Sample("r1", "k", 1000, {"v": 1.0}))
        store.insert(Sample("r1", "k", 1000, {"v": 2.0}))
        assert _values(store) == [2.0]
        # a reading with a sequence next to one without is a different point
        store.insert(Sample("r1", "k", 1000, {"v": 3.0}, sequence=1))
        assert _values(store) == [2.0, 3.0]


def test_insert_many_follows_the_same_identity() -> None:
    with TimeSeriesStore(":memory:") as store:
        store.insert_many([
            Sample("r1", "k", 1000, {"v": 1.0}, sequence=1),
            Sample("r1", "k", 1000, {"v": 2.0}, sequence=2),
            Sample("r1", "k", 1000, {"v": 2.5}, sequence=2),
        ])
        assert _values(store) == [1.0, 2.5]


def test_a_sequence_that_is_not_a_whole_number_in_range_is_refused() -> None:
    for bad in (-1, MAX_SEQUENCE + 1, 1.5, "7", True, None):
        with pytest.raises(ValueError):
            Sample("r1", "k", 1000, {"v": 1.0}, sequence=bad)  # type: ignore[arg-type]
    Sample("r1", "k", 1000, {"v": 1.0}, sequence=MAX_SEQUENCE)


def test_a_database_made_before_the_sequence_keeps_its_data_when_it_migrates() -> None:
    connection = sqlite3.connect(":memory:")
    migrate_up(connection, target_version=3)
    connection.execute("INSERT INTO samples (source_id, kind, field, timestamp, value) VALUES ('r1', 'k', 'v', 1000, 4.0)")
    connection.commit()
    assert migrate_up(connection) == [4]
    ((value, seq),) = connection.execute("SELECT value, seq FROM samples").fetchall()
    assert (value, seq) == (4.0, 0)
    connection.close()


@pytest.fixture()
def server_url() -> Iterator[str]:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def _post(url: str, payload: object) -> tuple[int, object]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _query(url: str) -> list[dict]:
    with urllib.request.urlopen(f"{url}/query?sourceId=r1") as response:
        return json.loads(response.read())


def test_the_api_takes_the_sequence_of_the_collector(server_url: str) -> None:
    base = {"sourceId": "r1", "kind": "k", "timestamp": 1000}
    assert _post(f"{server_url}/ingest", {**base, "fields": {"v": 1.0}, "sequence": 1})[0] == 202
    assert _post(f"{server_url}/ingest", {**base, "fields": {"v": 2.0}, "sequence": 2})[0] == 202
    assert _post(f"{server_url}/ingest", {**base, "fields": {"v": 2.5}, "sequence": 2})[0] == 202
    assert sorted(point["value"] for point in _query(server_url)) == [1.0, 2.5]


def test_the_batch_route_takes_it_too(server_url: str) -> None:
    base = {"sourceId": "r1", "kind": "k", "timestamp": 1000}
    status, _ = _post(f"{server_url}/ingest/batch", {"samples": [{**base, "fields": {"v": 1.0}, "sequence": 1}, {**base, "fields": {"v": 2.0}, "sequence": 2}]})
    assert status == 202
    assert len(_query(server_url)) == 2


def test_a_bad_sequence_is_a_400(server_url: str) -> None:
    base = {"sourceId": "r1", "kind": "k", "timestamp": 1000, "fields": {"v": 1.0}}
    for bad in (-1, 1.5, "7", True, 2**60):
        status, body = _post(f"{server_url}/ingest", {**base, "sequence": bad})
        assert status == 400, bad
        assert "error" in body
    assert _post(f"{server_url}/ingest", {**base, "sequence": None})[0] == 202
    assert _post(f"{server_url}/ingest", base)[0] == 202
