# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_retention_dry_run.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""`POST /retention/apply?dryRun=1` counts what the retention would delete and deletes nothing."""
from __future__ import annotations

from test_access_and_limits import _fill, call, running

from hydra_umc_datalake.store import TimeSeriesStore, now_ms

DAY = 86_400_000


def _store() -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    now = now_ms()
    _fill(store, "temp", "value", [now - 40 * DAY, now - 10 * DAY])
    _fill(store, "brand_new_kind", "x", [now - 40 * DAY, now - 5 * DAY])
    _fill(store, "kept_long", "value", [now - 40 * DAY])
    store.set_retention_policy(kind="kept_long", field="value", retention_ms=365 * DAY)
    store.set_retention_policy(kind="temp", field="value", retention_ms=20 * DAY)
    store.set_retention_policy(kind="*", field="*", retention_ms=30 * DAY)
    return store


def test_the_preview_counts_each_policy_and_the_real_run_deletes_that_many() -> None:
    store = _store()
    before = store.sample_count()
    preview = store.retention_preview()
    assert preview["wouldDelete"] == 2
    assert sorted((p["kind"], p["field"], p["count"]) for p in preview["policies"]) == [("*", "*", 1), ("kept_long", "value", 0), ("temp", "value", 1)]  # type: ignore[attr-defined]
    assert store.sample_count() == before                       # nothing was deleted by looking
    assert store.apply_retention() == preview["wouldDelete"]


def test_over_http_the_dry_run_deletes_nothing_and_the_plain_call_still_deletes() -> None:
    store = _store()
    before = store.sample_count()
    with running(store) as (host, port):
        status, body = call(host, port, "POST", "/retention/apply?dryRun=1")
        assert status == 200 and body["dryRun"] is True and body["wouldDelete"] == 2           # type: ignore[index]
        assert store.sample_count() == before
        assert call(host, port, "POST", "/retention/apply?dryRun=true")[1]["wouldDelete"] == 2  # type: ignore[index]
        assert call(host, port, "POST", "/retention/apply?dryRun=0")[1] == {"deleted": 2}
        assert store.sample_count() == before - 2


def test_without_a_default_policy_the_preview_has_only_the_policies_that_exist() -> None:
    store = TimeSeriesStore(":memory:")
    _fill(store, "temp", "value", [1_000_000_000_000])
    assert store.retention_preview() == {"wouldDelete": 0, "policies": []}
