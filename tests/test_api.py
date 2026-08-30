# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Real HTTP round-trips: a genuine DatalakeServer bound to an ephemeral
loopback port in a background thread, hit with real urllib requests -
the same "real server, real socket, no mocked client" standard used for
this session's Go projects (net/http/httptest) and Rust ones (a
compiled release binary), just via Python's own stdlib HTTP client."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import TimeSeriesStore


@pytest.fixture()
def server_url() -> Iterator[str]:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def _post(url: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _get(url: str) -> tuple[int, object]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_ingest_then_query_real_round_trip(server_url: str) -> None:
    status, body = _post(
        f"{server_url}/ingest",
        {"sourceId": "robot-1", "kind": "motor_temp", "timestamp": 1000, "fields": {"value": 42.5}},
    )
    assert status == 202
    assert body == {"written": 1}

    status, points = _get(f"{server_url}/query?sourceId=robot-1")
    assert status == 200
    assert len(points) == 1
    assert points[0]["value"] == 42.5
    assert points[0]["kind"] == "motor_temp"


def test_ingest_retry_is_idempotent_over_real_http(server_url: str) -> None:
    first = {"sourceId": "robot-1", "kind": "motor_temp", "timestamp": 1000, "fields": {"value": 42.5}}
    retry = {"sourceId": "robot-1", "kind": "motor_temp", "timestamp": 1000, "fields": {"value": 43.0}}

    assert _post(f"{server_url}/ingest", first) == (202, {"written": 1})
    assert _post(f"{server_url}/ingest", retry) == (202, {"written": 1})

    status, points = _get(f"{server_url}/query?sourceId=robot-1&kind=motor_temp&field=value")
    assert status == 200
    assert points == [{"sourceId": "robot-1", "kind": "motor_temp", "field": "value", "timestamp": 1000, "value": 43.0}]

    status, stats = _get(f"{server_url}/stats")
    assert status == 200
    assert stats == {"sampleCount": 1}


def test_ingest_rejects_malformed_sample(server_url: str) -> None:
    status, body = _post(f"{server_url}/ingest", {"kind": "motor_temp"})
    assert status == 400
    assert "error" in body


def test_ingest_rejects_a_non_finite_field_value(server_url: str) -> None:
    # Real end-to-end regression: json.dumps/json.loads both pass the
    # non-standard NaN/Infinity tokens through on this stdlib round-trip,
    # so a real client CAN put one on the wire without a JSON encode/
    # decode error - only Sample.__post_init__'s own explicit finite
    # check (exercised here through the real HTTP surface, not just the
    # unit tests in test_store.py) stands between that and an Infinity
    # reading permanently poisoning every aggregate() bucket that touches
    # it once persisted.
    status, body = _post(
        f"{server_url}/ingest",
        {"sourceId": "robot-1", "kind": "motor_temp", "timestamp": 1000, "fields": {"value": float("inf")}},
    )
    assert status == 400
    assert "error" in body

    status, stats = _get(f"{server_url}/stats")
    assert status == 200
    assert stats == {"sampleCount": 0}


def test_aggregate_real_round_trip(server_url: str) -> None:
    for ts, v in [(100, 10.0), (500, 20.0), (1200, 100.0)]:
        _post(f"{server_url}/ingest", {"sourceId": "r1", "kind": "temp", "timestamp": ts, "fields": {"v": v}})

    status, buckets = _get(f"{server_url}/aggregate?kind=temp&field=v&bucketMs=1000&start=0&end=1999")
    assert status == 200
    assert len(buckets) == 2
    assert buckets[0]["value"] == pytest.approx(15.0)
    assert buckets[1]["value"] == pytest.approx(100.0)


def test_aggregate_missing_params_is_400(server_url: str) -> None:
    status, body = _get(f"{server_url}/aggregate?kind=temp")
    assert status == 400
    assert "error" in body


def test_stats_reports_real_sample_count(server_url: str) -> None:
    _post(f"{server_url}/ingest", {"sourceId": "r1", "kind": "k", "timestamp": 1, "fields": {"a": 1.0, "b": 2.0}})
    status, body = _get(f"{server_url}/stats")
    assert status == 200
    assert body == {"sampleCount": 2}


def test_unknown_route_is_404(server_url: str) -> None:
    status, body = _get(f"{server_url}/nope")
    assert status == 404


def test_query_rejects_non_positive_limit_over_real_http(server_url: str) -> None:
    for limit in ("0", "-1"):
        status, body = _get(f"{server_url}/query?limit={limit}")
        assert status == 400
        assert body == {"error": "limit must be positive"}


def test_stats_range_on_empty_store_is_null_not_zero(server_url: str) -> None:
    status, body = _get(f"{server_url}/stats/range")
    assert status == 200
    assert body == {"oldestMs": None, "newestMs": None, "oldestUtc": None, "newestUtc": None}


def test_stats_range_reports_real_utc_labeled_timestamps(server_url: str) -> None:
    _post(f"{server_url}/ingest", {"sourceId": "r1", "kind": "k", "timestamp": 1767225600000, "fields": {"v": 1.0}})

    status, body = _get(f"{server_url}/stats/range")

    assert status == 200
    assert body["oldestMs"] == 1767225600000
    assert body["oldestUtc"] == "2026-01-01T00:00:00+00:00"
    assert body["oldestUtc"] == body["newestUtc"]


def test_retention_real_end_to_end_round_trip(server_url: str) -> None:
    status, body = _get(f"{server_url}/retention")
    assert status == 200
    assert body == []

    status, body = _post(f"{server_url}/retention", {"kind": "temp", "field": "v", "retentionMs": 5000})
    assert status == 200
    assert body == {"ok": True}

    status, body = _get(f"{server_url}/retention")
    assert status == 200
    assert body == [{"kind": "temp", "field": "v", "retentionMs": 5000}]

    _post(f"{server_url}/ingest", {"sourceId": "r1", "kind": "temp", "timestamp": 1000, "fields": {"v": 1.0}})
    _post(f"{server_url}/ingest", {"sourceId": "r1", "kind": "temp", "timestamp": 9000, "fields": {"v": 2.0}})

    status, body = _post(f"{server_url}/retention/apply", {})
    assert status == 200
    # /retention/apply always evaluates against the real, current wall-clock
    # time (no override over HTTP) - both fixture timestamps are toy values
    # from 1970, so both are real millennia past any 5-second retention
    # window as of today.
    assert body["deleted"] == 2


def test_set_retention_rejects_non_positive_window(server_url: str) -> None:
    status, body = _post(f"{server_url}/retention", {"kind": "k", "field": "v", "retentionMs": 0})
    assert status == 400
    assert "error" in body
