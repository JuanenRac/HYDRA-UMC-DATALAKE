# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_characterization_query.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Pins the behaviour of GET /query, GET /history and the routes that exist today, so the
optional window statistics and the downsampling can never change them."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

T0 = 1_800_000_000_000


@pytest.fixture()
def served() -> Iterator[str]:
    store = TimeSeriesStore(":memory:")
    for i in range(6):
        store.insert(Sample(source_id="b", kind="temp", timestamp=T0 + i * 1000, fields={"j1": float(i), "j2": float(10 * i)}))
    store.insert(Sample(source_id="a", kind="temp", timestamp=T0 + 2000, fields={"j1": 7.0}))
    store.insert(Sample(source_id="a", kind="load", timestamp=T0, fields={"j1": 1.5}))
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


def _get(url: str) -> tuple[int, Any, Any]:
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, json.loads(resp.read()), resp.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def test_query_is_a_plain_array_ordered_by_time_then_source_kind_field(served: str) -> None:
    status, body, _ = _get(f"{served}/query")
    assert status == 200 and isinstance(body, list)
    keys = [(p["timestamp"], p["sourceId"], p["kind"], p["field"]) for p in body]
    assert keys == sorted(keys)
    assert set(body[0]) == {"sourceId", "kind", "field", "timestamp", "value"}
    assert len(body) == 6 * 2 + 2


def test_query_filters_are_additive(served: str) -> None:
    _, body, _ = _get(f"{served}/query?sourceId=b&kind=temp&field=j1")
    assert [p["value"] for p in body] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    _, body, _ = _get(f"{served}/query?kind=temp&field=j1&start={T0 + 2000}&end={T0 + 2000}")
    assert sorted((p["sourceId"], p["value"]) for p in body) == [("a", 7.0), ("b", 2.0)]


def test_query_end_is_inclusive_and_before_is_exclusive(served: str) -> None:
    _, inclusive, _ = _get(f"{served}/query?sourceId=b&kind=temp&field=j1&start={T0}&end={T0 + 2000}")
    _, exclusive, _ = _get(f"{served}/query?sourceId=b&kind=temp&field=j1&start={T0}&before={T0 + 2000}")
    assert [p["value"] for p in inclusive] == [0.0, 1.0, 2.0]
    assert [p["value"] for p in exclusive] == [0.0, 1.0]


def test_query_announces_the_next_page_in_a_header_only_when_there_is_one(served: str) -> None:
    status, first, headers = _get(f"{served}/query?sourceId=b&kind=temp&field=j1&limit=4")
    assert status == 200 and len(first) == 4 and headers.get("X-Next-Cursor")
    _, second, headers2 = _get(f"{served}/query?sourceId=b&kind=temp&field=j1&limit=4&cursor={headers['X-Next-Cursor']}")
    assert [p["value"] for p in first + second] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    assert headers2.get("X-Next-Cursor") is None


@pytest.mark.parametrize(
    "query", ["limit=0", "limit=-1", "limit=200001", "limit=x", "start=1.5", "cursor=nope", "end=1&before=2", f"start={2**70}"]
)
def test_query_refuses_bad_parameters_with_400(served: str, query: str) -> None:
    status, body, _ = _get(f"{served}/query?{query}")
    assert status == 400 and "error" in body


def test_history_shape_is_fixed(served: str) -> None:
    _, body, _ = _get(f"{served}/history?sourceId=b&kind=temp&field=j2&limit=2")
    assert set(body) == {"order", "points", "nextCursor"}
    assert body["points"] == [{"timestamp": T0, "value": 0.0}, {"timestamp": T0 + 1000, "value": 10.0}]
    assert isinstance(body["nextCursor"], str)


def test_history_and_query_agree_on_one_series(served: str) -> None:
    _, history, _ = _get(f"{served}/history?sourceId=b&kind=temp&field=j2&order=desc")
    _, query, _ = _get(f"{served}/query?sourceId=b&kind=temp&field=j2")
    assert [(p["timestamp"], p["value"]) for p in reversed(history["points"])] == [(p["timestamp"], p["value"]) for p in query]


def test_existing_aggregate_shape_and_reducers(served: str) -> None:
    base = f"{served}/aggregate?kind=temp&field=j1&sourceId=b&bucketMs=2000&start={T0}&end={T0 + 5000}"
    _, avg, _ = _get(base)
    assert avg == [
        {"bucketStart": T0, "value": 0.5, "count": 2},
        {"bucketStart": T0 + 2000, "value": 2.5, "count": 2},
        {"bucketStart": T0 + 4000, "value": 4.5, "count": 2},
    ]
    assert [b["value"] for b in _get(base + "&agg=max")[1]] == [1.0, 3.0, 5.0]
    assert [b["value"] for b in _get(base + "&agg=sum")[1]] == [1.0, 5.0, 9.0]
    assert _get(base + "&agg=median")[0] == 400


def test_routes_of_the_optional_features_do_not_exist_by_default(served: str) -> None:
    for path in ("/windows", "/rollups", "/downsample"):
        assert _get(f"{served}{path}")[0] == 404
    req = urllib.request.Request(f"{served}/downsample/apply", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req)
    assert err.value.code == 404
