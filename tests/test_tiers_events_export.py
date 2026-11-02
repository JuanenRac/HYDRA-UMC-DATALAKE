# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_tiers_events_export.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The tier plan and its timer, the production events and the daily Parquet export: all off by default and safe when on."""
from __future__ import annotations

import json
import sys
import threading
import types
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from hydra_umc_datalake import export as export_module
from hydra_umc_datalake import main as main_module
from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.plan import PeriodicRunner, PlanError, apply_plan, load_plan, parse_duration, parse_plan
from hydra_umc_datalake.production import event_to_sample
from hydra_umc_datalake.store import Sample, TimeSeriesStore

MIN = 60_000
HOUR = 3_600_000
DAY = 86_400_000
T0 = (1_800_000_000_000 // DAY) * DAY


# ------------------------------------------------------------------ the plan
def test_durations() -> None:
    assert parse_duration("90s", "x") == 90_000
    assert parse_duration("1m", "x") == MIN and parse_duration("1h", "x") == HOUR
    assert parse_duration("2d", "x") == 2 * DAY and parse_duration("1y", "x") == 365 * DAY
    assert parse_duration(5000, "x") == 5000
    for bad in ("", "5", "5x", "-1h", "0d", True, 1.5, None, "1h30m", 10**20):
        with pytest.raises(PlanError):
            parse_duration(bad, "x")


def _tier(**over: Any) -> dict[str, Any]:
    return {"kind": "temp", "field": "*", "raw": "2d", "bucket": "1h", "fineBucket": "1m", "rollupRetention": "5y", "fineRetention": "30d", **over}


def test_the_plan_is_validated() -> None:
    assert len(parse_plan({"tiers": [_tier(), {"kind": "*", "field": "*", "raw": "30d", "bucket": "1d"}]})) == 2
    bad_plans: list[Any] = [
        [], {}, {"tiers": []}, {"tiers": [1]}, {"tiers": [_tier()], "extra": 1},
        {"tiers": [_tier(surprise=1)]},
        {"tiers": [_tier(), _tier()]},  # twice
        {"tiers": [{"kind": "temp"}]},  # no raw / bucket
        {"tiers": [{"kind": "*", "field": "j1", "raw": "1d", "bucket": "1h"}]},  # a field needs a kind
        {"tiers": [_tier(fineBucket="7m")]},  # does not divide
        {"tiers": [_tier(fineBucket="2h")]},  # not narrower
        {"tiers": [_tier(fineBucket="1s")]},  # too many fine buckets per coarse one
        {"tiers": [{"kind": "temp", "field": "*", "raw": "1d", "bucket": "1h", "fineRetention": "1d"}]},  # no fineBucket
        {"tiers": [_tier(fineRetention="10y")]},  # narrow outlives the wide
        {"tiers": [{"kind": "*", "field": "*", "raw": "1d", "bucket": "1h", "fineBucket": "1m", "fineRetention": "1d"}]},
    ]
    for bad in bad_plans:
        with pytest.raises(PlanError):
            parse_plan(bad)


def test_the_plan_file_errors_are_plain(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="cannot read"):
        load_plan(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(PlanError, match="not valid JSON"):
        load_plan(broken)
    good = tmp_path / "plan.json"
    good.write_text(json.dumps({"tiers": [_tier()]}), encoding="utf-8")
    assert load_plan(good)[0].fine_retention_ms == 30 * DAY


def _lake() -> TimeSeriesStore:
    store = TimeSeriesStore(":memory:")
    store.insert_many(
        [Sample(source_id="a", kind=kind, timestamp=T0 + h * HOUR + m * MIN, fields={"j1": float(h), "j2": 1.0})
         for kind in ("temp", "other") for h in range(72) for m in (0, 30)]
    )
    return store


def test_a_plan_is_a_dry_run_until_deletion_is_asked() -> None:
    store = _lake()
    before = store.sample_count()
    tiers = parse_plan({"tiers": [_tier(raw="1d", fineRetention="1d", rollupRetention="400d")]})
    report = apply_plan(store, tiers, dry_run=True, fine_rollups=True, max_buckets=10_000, clock=lambda: T0 + 10 * DAY)
    assert report["dryRun"] is True and report["buckets"] > 0
    assert store.sample_count() == before  # nothing deleted
    # "*" expanded to the fields of the kind, and only that kind
    assert {(p["kind"], p["field"]) for p in store.list_downsample_policies()} == {("temp", "j1"), ("temp", "j2")}


def test_a_real_run_writes_both_widths_and_the_narrow_one_expires_first() -> None:
    store = _lake()
    tiers = parse_plan({"tiers": [_tier(raw="1d", fineRetention="2d", rollupRetention="400d")]})
    now = T0 + 20 * DAY
    report = apply_plan(store, tiers, dry_run=False, fine_rollups=True, max_buckets=100_000, clock=lambda: now)
    assert report["fineBuckets"] > 0
    widths = {r["bucketMs"] for r in store.read_rollups(kind="temp", field="j1", limit=100_000)}
    # the data is 17 days old: the narrow level is past its 2 days and gone in the same round, the wide one stays
    assert report["expiredFineRollups"] > 0
    assert widths == {HOUR} or widths == {HOUR, MIN}
    assert MIN not in widths
    # the other kind has no tier: untouched
    assert store.query(kind="other", field="j1", limit=1000)


def test_without_the_fine_switch_the_narrow_level_neither_exists_nor_expires() -> None:
    store = _lake()
    tiers = parse_plan({"tiers": [_tier(raw="1d")]})
    report = apply_plan(store, tiers, dry_run=False, fine_rollups=False, max_buckets=100_000, clock=lambda: T0 + 20 * DAY)
    assert "fineBuckets" not in report and report["expiredFineRollups"] == 0
    assert {r["bucketMs"] for r in store.read_rollups(kind="temp", field="j1", limit=100_000)} == {HOUR}


def test_a_field_with_its_own_tier_keeps_it() -> None:
    store = _lake()
    tiers = parse_plan({"tiers": [_tier(raw="1d"), {"kind": "temp", "field": "j2", "raw": "3d", "bucket": "1d"}]})
    apply_plan(store, tiers, dry_run=True, fine_rollups=False, max_buckets=10, clock=lambda: T0 + 20 * DAY)
    policies = {(p["kind"], p["field"]): p for p in store.list_downsample_policies()}
    assert policies[("temp", "j2")]["bucketMs"] == DAY and policies[("temp", "j1")]["bucketMs"] == HOUR


def test_the_periodic_runner_survives_a_failing_round() -> None:
    calls: list[int] = []

    def action() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    runner = PeriodicRunner("t", 60, action)
    runner.run_once()
    runner.run_once()
    assert (runner.runs, runner.failures) == (1, 1)
    with pytest.raises(ValueError):
        PeriodicRunner("t", 5, action)
    runner.start()
    runner.stop()  # joins promptly


def test_the_new_options_are_refused_when_they_do_not_fit(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    for args in (
        ["--downsample-plan", str(tmp_path / "p.json")],
        ["--downsample-auto-interval-seconds", "600"],
        ["--enable-downsampling", "--downsample-auto-interval-seconds", "5"],
        ["--enable-downsampling", "--downsample-plan", str(tmp_path / "missing.json")],
        ["--enable-parquet-export"],
        ["--enable-parquet-export", "--parquet-export-dir", str(tmp_path), "--parquet-export-interval-seconds", "5"],
    ):
        assert main_module.main(["--db", ":memory:", *args]) == 2, args
    capsys.readouterr()


# ------------------------------------------------------- production events
def _event(**over: Any) -> dict[str, Any]:
    return {"source_id": "robot-1", "at_utc": "2027-01-15T08:00:00Z", "good": True, "cycle_time_s": 12.5, "job_id": "weld-1", **over}


def test_an_event_becomes_the_sample_that_the_reports_read() -> None:
    sample = event_to_sample(_event())
    assert (sample.source_id, sample.kind, sample.fields) == ("robot-1", "production_event", {"good": 1.0, "cycleTimeS": 12.5})
    assert sample.timestamp == 1_800_000_000_000
    assert event_to_sample(_event(good=False)).fields["good"] == 0.0
    assert event_to_sample(_event(job_id=None)).sequence == 0
    assert event_to_sample(_event()).sequence == event_to_sample(_event()).sequence != event_to_sample(_event(job_id="weld-2")).sequence
    offset = event_to_sample(_event(at_utc="2027-01-15T10:00:00+02:00"))
    assert offset.timestamp == sample.timestamp


def test_a_bad_event_is_refused() -> None:
    event = _event()
    bad: list[Any] = [
        [], "x", {**event, "extra": 1}, {k: v for k, v in event.items() if k != "good"},
        _event(good=1), _event(good="yes"), _event(cycle_time_s=0), _event(cycle_time_s=-1), _event(cycle_time_s=700_000),
        _event(cycle_time_s=True), _event(cycle_time_s="1"), _event(at_utc="2027-01-15T08:00:00"), _event(at_utc="soon"),
        _event(at_utc=5), _event(source_id=""), _event(source_id=3), _event(job_id=""), _event(job_id=3),
    ]
    for item in bad:
        with pytest.raises((ValueError, TypeError)):
            event_to_sample(item)


def _post(url: str, body: Any) -> tuple[int, Any]:
    request = urllib.request.Request(url, method="POST", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _serve(store: TimeSeriesStore, **options: Any) -> tuple[DatalakeServer, str, threading.Thread]:
    server = DatalakeServer(("127.0.0.1", 0), store, max_future_skew_ms=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", thread


def test_the_production_route_does_not_exist_by_default_and_works_when_on() -> None:
    store = TimeSeriesStore(":memory:")
    server, url, thread = _serve(store)
    try:
        assert _post(url + "/production-events", _event())[0] == 404
    finally:
        server.shutdown(), server.server_close(), thread.join(timeout=2)
    server, url, thread = _serve(store, enable_production_events=True)
    try:
        assert _post(url + "/production-events", _event()) == (202, {"accepted": 1, "written": 2})
        assert _post(url + "/production-events", _event()) == (202, {"accepted": 1, "written": 2})  # a retry replaces itself
        assert _post(url + "/production-events", {"events": [_event(job_id="a"), _event(job_id="b", good=False)]})[1]["accepted"] == 2
        assert len(store.query(kind="production_event", field="good", limit=100)) == 3
        status, answer = _post(url + "/production-events", {"events": [_event(), _event(cycle_time_s=0)]})
        assert status == 400 and "events[1]" in answer["error"]
        assert len(store.query(kind="production_event", field="good", limit=100)) == 3  # all or nothing
        assert _post(url + "/production-events", {"events": []})[0] == 400
        assert _post(url + "/production-events", _event(at_utc="1970-01-02T00:00:00Z"))[0] == 400  # a time in the past by years is not a unix-ms mistake
    finally:
        server.shutdown(), server.server_close(), thread.join(timeout=2)


def test_the_production_route_asks_for_the_ingest_token() -> None:
    store = TimeSeriesStore(":memory:")
    server, url, thread = _serve(store, enable_production_events=True, tokens={"ingest": "t" * 16})
    try:
        assert _post(url + "/production-events", _event())[0] == 401
    finally:
        server.shutdown(), server.server_close(), thread.join(timeout=2)


# -------------------------------------------------------------- the export
def test_export_page_walks_the_day_in_order_without_gaps() -> None:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id=s, kind="k", timestamp=T0 + 5, fields={"a": 1.0, "b": 2.0}) for s in ("x", "y", "z")])
    store.insert(Sample(source_id="x", kind="k", timestamp=T0 - 1, fields={"a": 9.0}))  # the day before
    store.insert(Sample(source_id="x", kind="k", timestamp=T0 + DAY, fields={"a": 9.0}))  # the day after
    rows: list[Any] = []
    after = None
    while True:
        page = store.export_page(start_ms=T0, end_ms=T0 + DAY, after=after, limit=4)
        if not page:
            break
        rows += page
        after = (page[-1][0], page[-1][1])
    assert len(rows) == 6 and [r[0] for r in rows] == [T0 + 5] * 6
    assert len({r[1] for r in rows}) == 6


class _FakeParquet:
    """Stands in for pyarrow so the export is tested without it; a real-pyarrow test follows when it is installed."""

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, list[Any]]]] = {}
        self.schemas: dict[str, Any] = {}

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self
        pa = types.ModuleType("pyarrow")
        pa.string = lambda: "string"  # type: ignore[attr-defined]
        pa.int64 = lambda: "int64"  # type: ignore[attr-defined]
        pa.float64 = lambda: "float64"  # type: ignore[attr-defined]

        class Schema:
            def __init__(self, fields: list[tuple[str, str]]) -> None:
                self.fields = fields

            def field(self, i: int) -> Any:
                return types.SimpleNamespace(type=self.fields[i][1])

        class Table:
            def __init__(self, columns: dict[str, list[Any]]) -> None:
                self.columns = columns

            @staticmethod
            def from_arrays(arrays: list[Any], schema: Schema) -> "Table":
                return Table({name: list(arrays[i]) for i, (name, _) in enumerate(schema.fields)})

        pa.schema = lambda fields: Schema(fields)  # type: ignore[attr-defined]
        pa.array = lambda values, type=None: values  # type: ignore[attr-defined]
        pa.Table = Table  # type: ignore[attr-defined]
        pq = types.ModuleType("pyarrow.parquet")

        class Writer:
            def __init__(self, path: str, schema: Schema, compression: str = "") -> None:
                self.path = path
                fake.tables[path] = []
                fake.schemas[path] = schema

            def write_table(self, table: Table) -> None:
                fake.tables[self.path].append(table.columns)

            def close(self) -> None:
                Path(self.path).write_text(json.dumps(fake.tables[self.path]), encoding="utf-8")

        pq.ParquetWriter = Writer  # type: ignore[attr-defined]
        pa.parquet = pq  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "pyarrow", pa)
        monkeypatch.setitem(sys.modules, "pyarrow.parquet", pq)


def test_without_pyarrow_the_export_says_what_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    assert export_module.parquet_available() is False
    with pytest.raises(export_module.ParquetUnavailable, match="pyarrow"):
        export_module.export_day(TimeSeriesStore(":memory:"), date(2027, 1, 15), tmp_path)


def test_the_daily_export_writes_closed_days_once_and_never_deletes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeParquet()
    fake.install(monkeypatch)
    store = TimeSeriesStore(":memory:")
    day1, day2 = T0 - 2 * DAY, T0 - DAY  # two closed days; "today" is T0
    store.insert_many([Sample(source_id="r1", kind="k", timestamp=day1 + i, fields={"v": float(i)}) for i in range(5)])
    store.insert_many([Sample(source_id="r2", kind="k", timestamp=day2 + 7, fields={"v": 1.0, "w": 2.0})])
    store.insert(Sample(source_id="r1", kind="k", timestamp=T0 + 5, fields={"v": 1.0}))  # today: not closed
    written = export_module.export_closed_days(store, tmp_path, lookback_days=5, clock=lambda: T0 + 1000)
    assert len(written) == 2
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2 and all(n.startswith("samples-") and n.endswith(".parquet") for n in names)
    assert not list(tmp_path.glob("*.part"))
    first = json.loads((tmp_path / names[0]).read_text(encoding="utf-8"))
    assert first[0]["value"] == [0.0, 1.0, 2.0, 3.0, 4.0] and set(first[0]) == set(export_module.COLUMNS)
    # a second round writes nothing new and the store is untouched
    assert export_module.export_closed_days(store, tmp_path, lookback_days=5, clock=lambda: T0 + 1000) == []
    assert store.sample_count() == 5 + 2 + 1
    assert len(export_module.exported_days(tmp_path)) == 2


def test_overwrite_replaces_and_an_empty_day_writes_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakeParquet().install(monkeypatch)
    store = TimeSeriesStore(":memory:")
    store.insert(Sample(source_id="r", kind="k", timestamp=T0 - DAY, fields={"v": 1.0}))
    day = export_module.datetime.fromtimestamp((T0 - DAY) / 1000, tz=export_module.timezone.utc).date()
    assert export_module.export_day(store, day, tmp_path) is not None
    assert export_module.export_day(store, day, tmp_path) is None
    store.insert(Sample(source_id="r", kind="k", timestamp=T0 - DAY + 1, fields={"v": 2.0}))
    again = export_module.export_day(store, day, tmp_path, overwrite=True)
    assert again is not None and len(json.loads(again.read_text(encoding="utf-8"))[0]["value"]) == 2
    assert export_module.export_day(store, date(2020, 1, 1), tmp_path) is None
    with pytest.raises(ValueError):
        export_module.export_closed_days(store, tmp_path, lookback_days=0)


def test_the_export_carries_no_setting_or_secret(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeParquet()
    fake.install(monkeypatch)
    store = TimeSeriesStore(":memory:")
    store.insert(Sample(source_id="r", kind="k", timestamp=T0 - DAY, fields={"v": 1.0}))
    path = export_module.export_day(store, export_module.datetime.fromtimestamp((T0 - DAY) / 1000, tz=export_module.timezone.utc).date(), tmp_path)
    assert path is not None
    schema = next(iter(fake.schemas.values()))
    assert [name for name, _ in schema.fields] == list(export_module.COLUMNS)


def test_the_export_command_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "lake.sqlite3"
    store = TimeSeriesStore(db)
    store.insert(Sample(source_id="r", kind="k", timestamp=1_700_000_000_000, fields={"v": 1.0}))
    store.close()
    assert main_module.main(["export-parquet", "--db", str(tmp_path / "none.sqlite3"), "--dest", str(tmp_path)]) == 2
    assert main_module.main(["export-parquet", "--db", str(db), "--dest", str(tmp_path), "--day", "nope"]) == 2
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    assert main_module.main(["export-parquet", "--db", str(db), "--dest", str(tmp_path / "out"), "--day", "2023-11-14"]) == 2
    _FakeParquet().install(monkeypatch)
    assert main_module.main(["export-parquet", "--db", str(db), "--dest", str(tmp_path / "out"), "--day", "2023-11-14"]) == 0
    assert (tmp_path / "out" / "samples-2023-11-14.parquet").exists()
    assert "written to" in capsys.readouterr().out


def test_a_real_parquet_file_when_pyarrow_is_installed(tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id="r", kind="k", timestamp=T0 - DAY + i, fields={"v": float(i)}) for i in range(10)])
    day = export_module.datetime.fromtimestamp((T0 - DAY) / 1000, tz=export_module.timezone.utc).date()
    path = export_module.export_day(store, day, tmp_path, page_rows=3)
    assert path is not None
    table = pq.read_table(path)
    assert table.column_names == list(export_module.COLUMNS) and table.num_rows == 10
    assert table.column("value").to_pylist() == [float(i) for i in range(10)]
