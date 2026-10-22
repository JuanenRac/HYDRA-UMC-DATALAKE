# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_pages_batch_backup.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Cursor pages, the exclusive ``before`` bound, the series catalogue, the batch endpoint and the backup command."""
from __future__ import annotations

import json
import sqlite3
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.main import main as cli_main
from hydra_umc_datalake.store import Sample, TimeSeriesStore


@pytest.fixture()
def served() -> Iterator[tuple[str, TimeSeriesStore]]:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store, min_timestamp_ms=0, max_future_skew_ms=0, max_batch_samples=3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", store
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def _call(url: str, payload: dict | None = None) -> tuple[int, object, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if payload is not None else "GET")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read()), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def _fill(store: TimeSeriesStore, n: int) -> None:
    for i in range(n):
        store.insert(Sample(source_id="a", kind="t", timestamp=1000 + i, fields={"v": float(i)}))


def test_cursor_pages_walk_the_whole_history_without_gaps_or_repeats(served) -> None:
    url, store = served
    _fill(store, 7)
    seen, cursor, pages = [], None, 0
    while True:
        suffix = f"&cursor={cursor}" if cursor else ""
        status, body, headers = _call(f"{url}/query?limit=3{suffix}")
        assert status == 200
        seen += [p["timestamp"] for p in body]
        pages += 1
        cursor = headers.get("X-Next-Cursor")
        if not cursor:
            break
    assert seen == [1000 + i for i in range(7)]
    assert pages == 3


def test_last_full_page_has_no_cursor(served) -> None:
    url, store = served
    _fill(store, 3)
    status, body, headers = _call(f"{url}/query?limit=3")
    assert status == 200 and len(body) == 3
    assert "X-Next-Cursor" not in headers


def test_a_forged_cursor_is_a_400_not_a_crash(served) -> None:
    url, _ = served
    for bad in ("zzz", "e30", "WzEsMiwzXQ"):
        status, body, _ = _call(f"{url}/query?cursor={bad}")
        assert status == 400, bad
        assert "cursor" in body["error"]


def test_before_excludes_the_boundary_point_and_end_includes_it(served) -> None:
    url, store = served
    _fill(store, 5)  # 1000..1004
    assert [p["timestamp"] for p in _call(f"{url}/query?start=1000&end=1002")[1]] == [1000, 1001, 1002]
    assert [p["timestamp"] for p in _call(f"{url}/query?start=1000&before=1002")[1]] == [1000, 1001]
    status, body, _ = _call(f"{url}/query?end=1002&before=1002")
    assert status == 400 and "not both" in body["error"]


def test_consecutive_windows_with_before_never_count_a_point_twice(served) -> None:
    url, store = served
    _fill(store, 6)  # 1000..1005
    first = _call(f"{url}/aggregate?kind=t&field=v&bucketMs=10&start=1000&before=1003&agg=sum")[1]
    second = _call(f"{url}/aggregate?kind=t&field=v&bucketMs=10&start=1003&before=1006&agg=sum")[1]
    assert sum(b["count"] for b in first) + sum(b["count"] for b in second) == 6


def test_aggregate_still_asks_for_an_upper_bound(served) -> None:
    url, _ = served
    status, body, _ = _call(f"{url}/aggregate?kind=t&field=v&bucketMs=10&start=1000")
    assert status == 400 and "end" in body["error"]


def test_series_catalogue_lists_names_counts_and_range(served) -> None:
    url, store = served
    _fill(store, 4)
    store.insert(Sample(source_id="b", kind="x", timestamp=5000, fields={"w": 1.0}))
    status, body, _ = _call(f"{url}/series")
    assert status == 200
    assert body == [
        {"sourceId": "a", "kind": "t", "field": "v", "count": 4, "firstTimestamp": 1000, "lastTimestamp": 1003},
        {"sourceId": "b", "kind": "x", "field": "w", "count": 1, "firstTimestamp": 5000, "lastTimestamp": 5000},
    ]
    only = _call(f"{url}/series?kind=x")[1]
    assert [s["sourceId"] for s in only] == ["b"]
    assert _call(f"{url}/series?limit=0")[0] == 400


def _sample(ts: int, **fields: float) -> dict:
    return {"sourceId": "a", "kind": "t", "timestamp": ts, "fields": fields or {"v": 1.0}}


def test_batch_stores_every_sample_in_one_request(served) -> None:
    url, store = served
    status, body, _ = _call(f"{url}/ingest/batch", {"samples": [_sample(1), _sample(2, v=2.0, w=3.0)]})
    assert status == 202
    assert body == {"accepted": 2, "written": 3}
    assert store.sample_count() == 3


def test_batch_is_all_or_nothing(served) -> None:
    url, store = served
    bad = {"sourceId": "a", "kind": "t", "timestamp": 3, "fields": {"v": "high"}}
    status, body, _ = _call(f"{url}/ingest/batch", {"samples": [_sample(1), bad]})
    assert status == 400
    assert "samples[1]" in body["error"]
    assert store.sample_count() == 0


def test_batch_limits_and_shape(served) -> None:
    url, store = served
    assert _call(f"{url}/ingest/batch", {"samples": []})[0] == 400
    assert _call(f"{url}/ingest/batch", {"samples": "x"})[0] == 400
    assert _call(f"{url}/ingest/batch", {"samples": [1]})[0] == 400
    status, body, _ = _call(f"{url}/ingest/batch", {"samples": [_sample(i) for i in range(1, 5)]})
    assert status == 400 and "at most 3" in body["error"]
    assert store.sample_count() == 0


def test_batch_retry_replaces_instead_of_duplicating(served) -> None:
    url, store = served
    for _ in range(2):
        assert _call(f"{url}/ingest/batch", {"samples": [_sample(1), _sample(2)]})[0] == 202
    assert store.sample_count() == 2


def test_batch_needs_the_ingest_token_when_one_is_set() -> None:
    store = TimeSeriesStore(":memory:")
    server = DatalakeServer(("127.0.0.1", 0), store, tokens={"ingest": "i" * 16}, min_timestamp_ms=0, max_future_skew_ms=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert _call(f"{url}/ingest/batch", {"samples": [_sample(1)]})[0] == 401
        assert store.sample_count() == 0
    finally:
        server.shutdown()
        server.server_close()
        store.close()


def test_backup_is_a_consistent_copy_and_rotates(tmp_path: Path) -> None:
    db = tmp_path / "live.sqlite3"
    store = TimeSeriesStore(db)
    _fill(store, 5)
    dest = tmp_path / "copies"
    first = store.backup(dest, keep=2, now=1_700_000_000)
    store.backup(dest, keep=2, now=1_700_000_100)
    store.backup(dest, keep=2, now=1_700_000_200)
    names = sorted(p.name for p in dest.glob("datalake-*.sqlite3"))
    assert len(names) == 2 and first.name not in names
    with sqlite3.connect(str(dest / names[-1])) as copy:
        assert copy.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 5
    assert not list(dest.glob("*.partial"))
    store.close()


def test_backup_refuses_memory_and_negative_keep(tmp_path: Path) -> None:
    with TimeSeriesStore(":memory:") as memory:
        with pytest.raises(ValueError):
            memory.backup(tmp_path)
    with TimeSeriesStore(tmp_path / "x.sqlite3") as store:
        with pytest.raises(ValueError):
            store.backup(tmp_path / "b", keep=-1)


def test_backup_command_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "live.sqlite3"
    with TimeSeriesStore(db) as store:
        _fill(store, 2)
    assert cli_main(["backup", "--db", str(db), "--dest", str(tmp_path / "out")]) == 0
    assert "backup written to" in capsys.readouterr().out
    assert len(list((tmp_path / "out").glob("datalake-*.sqlite3"))) == 1
    assert cli_main(["backup", "--db", str(tmp_path / "missing.sqlite3"), "--dest", str(tmp_path / "out")]) == 2
