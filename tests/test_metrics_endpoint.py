# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_metrics_endpoint.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET /metrics of a real server: the text format of Prometheus with the version and the health of the store, the read token where the service
has one, and nothing a request carried."""
from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

from hydra_umc_datalake import __version__
from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import TimeSeriesStore

READ_TOKEN = "read-token-0123456789"


@contextmanager
def serving(tokens: dict[str, str] | None = None) -> Iterator[tuple[str, TimeSeriesStore]]:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, tokens=tokens)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        store.close()


def _raw(url: str, token: str | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        with error:
            return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def test_metrics_are_the_prometheus_text_with_the_version_and_the_health_of_the_store() -> None:
    with serving() as (base, _):
        status, kind, text = _raw(base + "/metrics")
    assert status == 200
    assert kind == "text/plain; version=0.0.4; charset=utf-8"
    assert f'hydra_umc_service_info{{service="hydra-umc-datalake",version="{__version__}"}} 1' in text
    assert re.search(r'^hydra_umc_service_health\{service="hydra-umc-datalake"\} 1$', text, re.MULTILINE)
    assert re.search(r'^hydra_umc_service_health_check\{service="hydra-umc-datalake",check="store"\} 1$', text, re.MULTILINE)


def test_a_store_that_fails_makes_the_service_unhealthy_but_still_answers() -> None:
    with serving() as (base, store):
        store.close()
        status, _, text = _raw(base + "/metrics")
    assert status == 200
    assert re.search(r'^hydra_umc_service_health\{service="hydra-umc-datalake"\} 0$', text, re.MULTILINE)


def test_metrics_need_the_read_token_when_the_service_has_one() -> None:
    with serving({"read": READ_TOKEN}) as (base, _):
        refused = _raw(base + "/metrics")
        allowed = _raw(base + "/metrics", READ_TOKEN)
    assert refused[0] == 401
    assert "hydra_umc" not in refused[2]
    assert allowed[0] == 200 and "hydra_umc_service_info" in allowed[2]


def test_metrics_carry_nothing_a_request_carried() -> None:
    with serving() as (base, _):
        _raw(base + "/no-such-route?series=very-private-name")
        _, _, text = _raw(base + "/metrics?token=leaked-secret")
    for needle in ("very-private-name", "leaked-secret", "no-such-route", "127.0.0.1"):
        assert needle not in text
