# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_gzip_body.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A request body sent with Content-Encoding: gzip is read only when the operator allows it."""
from __future__ import annotations

import gzip
import json
import threading
import urllib.error
import urllib.request
from typing import Any

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import TimeSeriesStore


def _serve(**options: Any) -> tuple[DatalakeServer, str, threading.Thread, TimeSeriesStore]:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread, store


def _post(url: str, data: bytes, encoding: str | None = "gzip") -> int:
    headers = {"Content-Type": "application/json"}
    if encoding:
        headers["Content-Encoding"] = encoding
    request = urllib.request.Request(url + "/ingest/batch", method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status
    except urllib.error.HTTPError as e:
        e.read()
        return e.code


def _batch(count: int) -> bytes:
    samples = [{"sourceId": "a", "kind": "temp", "timestamp": 1_700_000_000_000 + i, "fields": {"c": 1.5}} for i in range(count)]
    return json.dumps({"samples": samples}).encode()


def _stop(server: DatalakeServer, thread: threading.Thread, store: TimeSeriesStore) -> None:
    server.shutdown()
    server.server_close()
    store.close()
    thread.join(timeout=2)


def test_gzip_is_refused_by_default_and_read_when_allowed() -> None:
    server, url, thread, store = _serve()
    try:
        assert _post(url, gzip.compress(_batch(2))) == 400
        assert store.query(kind="temp", field="c", limit=10) == []
    finally:
        _stop(server, thread, store)
    server, url, thread, store = _serve(accept_gzip=True)
    try:
        assert _post(url, gzip.compress(_batch(2))) in (200, 202)
        assert len(store.query(kind="temp", field="c", limit=10)) == 2
        assert _post(url, _batch(1), encoding=None) in (200, 202)  # a plain body still works
        assert _post(url, b"not gzip at all") == 400
        assert _post(url, gzip.compress(_batch(2))[:-8]) == 400  # truncated
        assert _post(url, gzip.compress(_batch(2)), encoding="br") == 400
    finally:
        _stop(server, thread, store)


def test_a_gzip_bomb_is_refused_by_its_inflated_size() -> None:
    server, url, thread, store = _serve(accept_gzip=True, max_request_body_bytes=8192)
    try:
        bomb = gzip.compress(b" " * 5_000_000 + b"{}")
        assert len(bomb) < 8192
        assert _post(url, bomb) == 413
    finally:
        _stop(server, thread, store)
