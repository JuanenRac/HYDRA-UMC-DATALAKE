# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_access_and_limits.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Who may write, who may delete the history, what a query may cost, what a
sample's timestamp has to look like, and what a hostile parameter does."""
from __future__ import annotations

import http.client
import json
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.main import is_loopback, startup_problem, tokens_from_environment
from hydra_umc_datalake.store import MAX_QUERY_LIMIT, Sample, TimeSeriesStore, now_ms

INGEST = "ingest-token-0123456789"
ADMIN = "admin-token-0123456789"
READ = "read-token-0123456789"


@contextmanager
def running(store: TimeSeriesStore, **options) -> Iterator[tuple[str, int]]:
    server = DatalakeServer(("127.0.0.1", 0), store, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def call(host: str, port: int, method: str, path: str, payload: object | None = None, token: str | None = None) -> tuple[int, object]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def sample(ts: int | None = None, **extra) -> dict:
    return {"sourceId": "robot-1", "kind": "motor_temp", "timestamp": ts if ts is not None else now_ms(), "fields": {"value": 1.5}, **extra}


# ------------------------------------------------------------------ access control
def test_without_a_token_configured_everything_stays_open_on_loopback() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        assert call(host, port, "POST", "/ingest", sample())[0] == 202
        assert call(host, port, "POST", "/retention/apply")[0] == 200


def test_ingest_and_administration_have_separate_tokens() -> None:
    store = TimeSeriesStore(":memory:")
    with running(store, tokens={"ingest": INGEST, "admin": ADMIN}) as (host, port):
        assert call(host, port, "POST", "/ingest", sample())[0] == 401
        assert call(host, port, "POST", "/ingest", sample(), token="wrong-token-0123456789")[0] == 401
        assert call(host, port, "POST", "/ingest", sample(), token=INGEST)[0] == 202
        assert call(host, port, "POST", "/ingest", sample(), token=ADMIN)[0] == 202
        # the ingest token cannot touch the retention policies
        policy = {"kind": "motor_temp", "field": "value", "retentionMs": 1}
        assert call(host, port, "POST", "/retention", policy, token=INGEST)[0] == 401
        assert call(host, port, "POST", "/retention/apply", token=INGEST)[0] == 401
        assert call(host, port, "POST", "/retention")[0] == 401
        assert call(host, port, "POST", "/retention", policy, token=ADMIN)[0] == 200


def test_an_unauthorised_caller_cannot_delete_the_history() -> None:
    store = TimeSeriesStore(":memory:")
    with running(store, tokens={"ingest": INGEST, "admin": ADMIN}) as (host, port):
        old = sample(ts=1_700_000_000_000)
        assert call(host, port, "POST", "/ingest", old, token=INGEST)[0] == 202
        store.set_retention_policy(kind="motor_temp", field="value", retention_ms=1)
        assert call(host, port, "POST", "/retention/apply")[0] == 401
        assert call(host, port, "POST", "/retention/apply", token=INGEST)[0] == 401
        assert store.sample_count() == 1
        assert call(host, port, "POST", "/retention/apply", token=ADMIN)[1] == {"deleted": 1}


def test_reads_are_open_unless_a_read_token_is_set() -> None:
    store = TimeSeriesStore(":memory:")
    with running(store, tokens={"ingest": INGEST, "admin": ADMIN}) as (host, port):
        assert call(host, port, "GET", "/query")[0] == 200
        assert call(host, port, "GET", "/stats")[0] == 200
    with running(store, tokens={"ingest": INGEST, "admin": ADMIN, "read": READ}) as (host, port):
        assert call(host, port, "GET", "/query")[0] == 401
        assert call(host, port, "GET", "/stats")[0] == 401
        assert call(host, port, "GET", "/retention")[0] == 401
        assert call(host, port, "GET", "/query", token=READ)[0] == 200
        assert call(host, port, "GET", "/query", token=ADMIN)[0] == 200
        assert call(host, port, "GET", "/query", token=INGEST)[0] == 401
        # a liveness probe needs no secret
        assert call(host, port, "GET", "/health")[0] == 200


def test_an_unknown_token_role_is_refused() -> None:
    with pytest.raises(ValueError):
        DatalakeServer(("127.0.0.1", 0), TimeSeriesStore(":memory:"), tokens={"root": "x" * 20})


# ------------------------------------------------------------------ startup policy
def test_loopback_detection() -> None:
    for addr in ("127.0.0.1", "::1", "localhost", "127.1.2.3"):
        assert is_loopback(addr), addr
    for addr in ("0.0.0.0", "", "192.168.1.5", "::", "example.com"):
        assert not is_loopback(addr), addr


def test_a_non_loopback_bind_needs_tokens_for_writing_and_for_administration() -> None:
    assert startup_problem("127.0.0.1", {}, False) is None
    assert "ingest and admin" in startup_problem("0.0.0.0", {}, False)  # type: ignore[operator]
    assert "admin" in startup_problem("0.0.0.0", {"ingest": INGEST}, False)  # type: ignore[operator]
    assert startup_problem("0.0.0.0", {"ingest": INGEST, "admin": ADMIN}, False) is None
    assert startup_problem("0.0.0.0", {}, True) is None
    assert "at least 16" in startup_problem("127.0.0.1", {"admin": "short"}, False)  # type: ignore[operator]


def test_tokens_come_from_the_environment() -> None:
    assert tokens_from_environment({}) == {}
    assert tokens_from_environment({"HYDRA_UMC_DATALAKE_TOKEN": ADMIN}) == {"ingest": ADMIN, "admin": ADMIN, "read": ADMIN}
    mixed = tokens_from_environment({"HYDRA_UMC_DATALAKE_TOKEN": INGEST, "HYDRA_UMC_DATALAKE_ADMIN_TOKEN": ADMIN})
    assert mixed["admin"] == ADMIN and mixed["ingest"] == INGEST
    assert tokens_from_environment({}, shared=READ)["read"] == READ


# ------------------------------------------------------------------ connections
def test_one_connection_serves_many_samples() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            body = json.dumps(sample())
            for _ in range(5):
                connection.request("POST", "/ingest", body=body, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                assert response.status == 202
                response.read()
            connection.request("GET", "/stats")
            assert json.loads(connection.getresponse().read())["sampleCount"] == 1  # the same point five times: one row
        finally:
            connection.close()


def test_a_request_whose_body_is_not_read_does_not_poison_the_connection() -> None:
    with running(TimeSeriesStore(":memory:"), tokens={"ingest": INGEST, "admin": ADMIN}) as (host, port):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            # refused before its body is read: the server answers 401 and closes
            connection.request("POST", "/ingest", body=json.dumps(sample()), headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 401
            response.read()
            # the next request on a fresh connection is not confused by the leftover body
            connection.close()
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request("GET", "/stats")
            assert connection.getresponse().status == 200
        finally:
            connection.close()


def test_a_chunked_body_is_a_411_not_a_confusing_400() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.putrequest("POST", "/ingest")
            connection.putheader("Transfer-Encoding", "chunked")
            connection.putheader("Content-Type", "application/json")
            connection.endheaders()
            body = json.dumps(sample()).encode()
            connection.send(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            response = connection.getresponse()
            assert response.status == 411
        finally:
            connection.close()


# ------------------------------------------------------------------ timestamps
def test_a_timestamp_in_seconds_or_negative_or_in_the_future_is_refused() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        assert call(host, port, "POST", "/ingest", sample(ts=1_700_000_000))[0] == 400  # seconds
        assert "MILLISECONDS" in call(host, port, "POST", "/ingest", sample(ts=1_700_000_000))[1]["error"]  # type: ignore[index]
        assert call(host, port, "POST", "/ingest", sample(ts=-5))[0] == 400
        assert call(host, port, "POST", "/ingest", sample(ts=now_ms() + 10 * 60 * 1000))[0] == 400
        assert call(host, port, "POST", "/ingest", sample(ts=now_ms() + 60 * 1000))[0] == 202  # ordinary clock skew
        assert call(host, port, "POST", "/ingest", sample(ts=1_700_000_000_000))[0] == 202


def test_the_future_check_can_be_switched_off() -> None:
    with running(TimeSeriesStore(":memory:"), max_future_skew_ms=0) as (host, port):
        assert call(host, port, "POST", "/ingest", sample(ts=now_ms() + 3_600_000))[0] == 202


# ------------------------------------------------------------------ hostile parameters
def test_numbers_beyond_64_bits_are_a_400_not_a_dead_handler() -> None:
    big = 10**30
    with running(TimeSeriesStore(":memory:")) as (host, port):
        for path in (
            f"/query?start={big}",
            f"/query?end={big}",
            f"/query?limit={big}",
            f"/aggregate?kind=k&field=f&bucketMs={big}&start=0&end=10",
            f"/aggregate?kind=k&field=f&bucketMs=1&start={big}&end={big}",
        ):
            status, body = call(host, port, "GET", path)
            assert status == 400, (path, status, body)
        assert call(host, port, "POST", "/retention", {"kind": "k", "field": "f", "retentionMs": big})[0] == 400
        assert call(host, port, "POST", "/ingest", {**sample(), "timestamp": big})[0] == 400
        # the server is still there
        assert call(host, port, "GET", "/stats")[0] == 200


def test_a_retention_policy_with_the_wrong_types_is_a_400() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        for payload in (
            {"kind": ["x"], "field": "f", "retentionMs": 5},
            {"kind": "k", "field": {"a": 1}, "retentionMs": 5},
            {"kind": "", "field": "f", "retentionMs": 5},
            {"kind": "k", "field": "", "retentionMs": 5},
            {"kind": "*", "field": "f", "retentionMs": 5},
        ):
            assert call(host, port, "POST", "/retention", payload)[0] == 400, payload
        assert call(host, port, "GET", "/stats")[0] == 200


def test_a_non_numeric_parameter_names_itself() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        status, body = call(host, port, "GET", "/query?start=yesterday")
        assert status == 400 and "start" in body["error"]  # type: ignore[index]


# ------------------------------------------------------------------ bounded reads
def test_a_query_limit_above_the_maximum_is_refused_and_the_maximum_is_accepted() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        assert call(host, port, "GET", f"/query?limit={MAX_QUERY_LIMIT + 1}")[0] == 400
        assert call(host, port, "GET", f"/query?limit={MAX_QUERY_LIMIT}")[0] == 200


def test_an_aggregate_over_too_many_buckets_is_refused() -> None:
    with running(TimeSeriesStore(":memory:")) as (host, port):
        status, body = call(host, port, "GET", "/aggregate?kind=k&field=f&bucketMs=1&start=0&end=1000000000")
        assert status == 400 and "buckets" in body["error"]  # type: ignore[index]
        assert call(host, port, "GET", "/aggregate?kind=k&field=f&bucketMs=100000&start=0&end=1000000000")[0] == 200


# ------------------------------------------------------------------ retention and disk
def _fill(store: TimeSeriesStore, kind: str, field: str, timestamps: list[int]) -> None:
    for ts in timestamps:
        store.insert(Sample(source_id="s", kind=kind, timestamp=ts, fields={field: 1.0}))


def test_a_default_policy_covers_the_series_that_have_none() -> None:
    store = TimeSeriesStore(":memory:")
    day = 86_400_000
    now = now_ms()
    _fill(store, "temp", "value", [now - 40 * day, now - 10 * day])
    _fill(store, "brand_new_kind", "x", [now - 40 * day, now - 5 * day])
    _fill(store, "kept_long", "value", [now - 40 * day])
    store.set_retention_policy(kind="kept_long", field="value", retention_ms=365 * day)
    store.set_retention_policy(kind="*", field="*", retention_ms=30 * day)

    deleted = store.apply_retention(at_ms=now)

    assert deleted == 2  # the two 40-day-old points of series without a policy of their own
    remaining = {(p.kind, p.timestamp) for p in store.query()}
    assert ("kept_long", now - 40 * day) in remaining  # its own, longer, policy wins
    assert ("temp", now - 10 * day) in remaining


def test_without_a_default_policy_nothing_is_deleted_by_surprise() -> None:
    store = TimeSeriesStore(":memory:")
    _fill(store, "temp", "value", [1_000_000_000_000])
    assert store.apply_retention(at_ms=now_ms()) == 0
    assert store.sample_count() == 1


def test_the_default_policy_needs_both_wildcards() -> None:
    store = TimeSeriesStore(":memory:")
    with pytest.raises(ValueError):
        store.set_retention_policy(kind="*", field="value", retention_ms=1000)
    with pytest.raises(ValueError):
        store.set_retention_policy(kind="temp", field="*", retention_ms=1000)


def test_an_on_disk_store_uses_wal_and_gives_deleted_space_back(tmp_path) -> None:
    path = tmp_path / "lake.sqlite3"
    store = TimeSeriesStore(path)
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # incremental
        index = conn.execute("SELECT name FROM sqlite_master WHERE name = 'idx_samples_kind_field_source_ts'").fetchone()
        assert index is not None
    finally:
        conn.close()
    rows = [(f"s{i % 7}", "temp", "value", 1_600_000_000_000 + i, float(i)) for i in range(40_000)]
    with store._lock:  # noqa: SLF001 - bulk fill, the public insert() is one sample at a time
        store._conn.executemany("INSERT INTO samples (source_id, kind, field, timestamp, value) VALUES (?, ?, ?, ?, ?)", rows)
        store._conn.commit()
    store.set_retention_policy(kind="temp", field="value", retention_ms=1)
    before = store.db_size_bytes()
    assert before and before > 500_000

    assert store.apply_retention() == 40_000

    after = store.db_size_bytes()
    assert after is not None and after < before / 4, (before, after)
    store.close()


def test_stats_report_the_size_of_the_database_and_the_free_disk(tmp_path) -> None:
    store = TimeSeriesStore(tmp_path / "lake.sqlite3")
    with running(store) as (host, port):
        status, body = call(host, port, "GET", "/stats")
        assert status == 200
        assert body["dbBytes"] > 0 and body["freeDiskBytes"] > 0  # type: ignore[index]
    store.close()
    memory = TimeSeriesStore(":memory:")
    with running(memory) as (host, port):
        assert "dbBytes" not in call(host, port, "GET", "/stats")[1]  # type: ignore[operator]


def test_free_disk_space_is_not_asked_of_the_system_for_every_sample(tmp_path, monkeypatch) -> None:
    store = TimeSeriesStore(tmp_path / "lake.sqlite3")
    calls = {"n": 0}
    real = shutil.disk_usage

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(shutil, "disk_usage", counting)
    first = store.free_disk_bytes()
    for _ in range(50):
        assert store.free_disk_bytes() == first
    assert calls["n"] == 1
    store.close()


def test_the_getter_for_free_space_no_longer_creates_directories(tmp_path) -> None:
    target = tmp_path / "deep" / "er" / "lake.sqlite3"
    store = TimeSeriesStore(target)  # the directory is created here, once, by the store itself
    assert target.parent.is_dir()
    store.close()


def test_the_timestamp_check_waits_for_the_clock(monkeypatch) -> None:
    # a sample 4 minutes ahead is inside the default 5 minutes of skew
    with running(TimeSeriesStore(":memory:")) as (host, port):
        assert call(host, port, "POST", "/ingest", sample(ts=int(time.time() * 1000) + 4 * 60 * 1000))[0] == 202
