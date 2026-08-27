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


def test_ingest_rejects_malformed_sample(server_url: str) -> None:
    status, body = _post(f"{server_url}/ingest", {"kind": "motor_temp"})
    assert status == 400
    assert "error" in body


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
