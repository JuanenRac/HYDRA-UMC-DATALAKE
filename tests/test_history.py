# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_history.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET /history: one series, either order, a cursor and the bounding values of the window."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

BASE_TS = 1_800_000_000_000
SERIES = "sourceId=arm-1&kind=temp&field=j1"


@pytest.fixture()
def served() -> Iterator[str]:
    store = TimeSeriesStore(":memory:")
    for i in range(10):
        store.insert(Sample(source_id="arm-1", kind="temp", timestamp=BASE_TS + i * 1000, fields={"j1": float(i)}))
    store.insert(Sample(source_id="arm-2", kind="temp", timestamp=BASE_TS + 500, fields={"j1": 99.0}))
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


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_history_returns_only_the_requested_series_oldest_first(served: str) -> None:
    status, body = _get(f"{served}/history?{SERIES}")
    assert status == 200
    assert [p["value"] for p in body["points"]] == [float(i) for i in range(10)]
    assert body["order"] == "asc" and body["nextCursor"] is None and "bounds" not in body


def test_history_descending_pages_cover_every_point_once(served: str) -> None:
    seen: list[float] = []
    url = f"{served}/history?{SERIES}&order=desc&limit=4"
    while True:
        status, body = _get(url)
        assert status == 200
        seen += [p["value"] for p in body["points"]]
        if body["nextCursor"] is None:
            break
        url = f"{served}/history?{SERIES}&order=desc&limit=4&cursor={body['nextCursor']}"
    assert seen == [float(i) for i in range(9, -1, -1)]


def test_history_bounds_are_the_points_just_outside_the_window(served: str) -> None:
    start, before = BASE_TS + 3000, BASE_TS + 6000
    status, body = _get(f"{served}/history?{SERIES}&start={start}&before={before}&bounds=1")
    assert status == 200
    assert [p["value"] for p in body["points"]] == [3.0, 4.0, 5.0]
    assert body["bounds"]["before"] == {"timestamp": BASE_TS + 2000, "value": 2.0}
    assert body["bounds"]["after"] == {"timestamp": BASE_TS + 6000, "value": 6.0}


def test_history_bounds_are_null_at_the_edges_of_the_data(served: str) -> None:
    _, body = _get(f"{served}/history?{SERIES}&start={BASE_TS}&end={BASE_TS + 9000}&bounds=1")
    assert body["bounds"] == {"before": None, "after": None}


@pytest.mark.parametrize(
    "query",
    ["sourceId=arm-1&kind=temp", f"{SERIES}&order=sideways", f"{SERIES}&bounds=2", f"{SERIES}&limit=0", f"{SERIES}&cursor=nope", f"{SERIES}&end=1&before=2"],
)
def test_history_refuses_bad_requests(served: str, query: str) -> None:
    status, body = _get(f"{served}/history?{query}")
    assert status == 400 and "error" in body
