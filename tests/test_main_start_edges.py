# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_main_start_edges.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The start of the service and its two small commands at their edges: every combination of options that stops the start with exit 2 and says why, the
settings read from the environment file, the choice of tokens, and the backup command on a database that is not there."""
from __future__ import annotations

from pathlib import Path

import pytest

from hydra_umc_datalake import main as m
from hydra_umc_datalake.store import Sample, TimeSeriesStore


@pytest.fixture(autouse=True)
def _no_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start that gets past the checks must not listen: the server stands in with one that stops at once."""

    class Stop:
        def __init__(self, address, store, **options) -> None:
            self.options = options

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            pass

    monkeypatch.setattr(m, "DatalakeServer", Stop)
    for name in ("HYDRA_UMC_DATALAKE_TOKEN", m.FINE_ROLLUPS_ENV, m.MAX_FINE_PER_COARSE_ENV, m.ADMIN_ENV, m.ALLOW_DELETE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(("flags", "words"), [
    (["--default-retention-days", "-1"], "must not be negative"),
    (["--max-future-skew-seconds", "-1"], "must not be negative"),
    (["--max-batch-samples", "0"], "--max-batch-samples"),
    (["--max-request-body-bytes", "0"], "must be positive"),
    (["--request-timeout-seconds", "0"], "must be positive"),
    (["--max-window-points", "0"], "--max-window-points"),
    (["--max-downsample-buckets", "0"], "--max-window-points"),
    (["--downsample-delete"], "needs --enable-downsampling"),
    (["--max-fine-per-coarse", "0", "--enable-downsampling"], "positive"),
    (["--downsample-auto-interval-seconds", "5"], "need --enable-downsampling"),
    (["--downsample-plan", "plan.json"], "need --enable-downsampling"),
    (["--enable-downsampling", "--downsample-auto-interval-seconds", "5"], "must be 0 or at least"),
    (["--enable-downsampling", "--downsample-admin"], "need --downsample-plan"),
    (["--enable-downsampling", "--downsample-allow-delete"], "need --downsample-plan"),
    (["--enable-parquet-export"], "needs --parquet-export-dir"),
])
def test_an_option_set_that_cannot_work_stops_the_start_and_says_why(flags, words, capsys):
    assert m.main(["--db", ":memory:", *flags]) == 2
    assert words in capsys.readouterr().err


def test_a_plan_file_that_is_not_there_stops_the_start(tmp_path: Path, capsys):
    assert m.main(["--db", ":memory:", "--enable-downsampling", "--downsample-plan", str(tmp_path / "missing.json")]) == 2
    assert "--downsample-plan:" in capsys.readouterr().err


def test_parquet_export_needs_sensible_numbers_and_the_library(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    base = ["--db", ":memory:", "--enable-parquet-export", "--parquet-export-dir", str(tmp_path)]
    assert m.main([*base, "--parquet-export-interval-seconds", "1"]) == 2
    assert "out of range" in capsys.readouterr().err
    assert m.main([*base, "--parquet-export-lookback-days", "0"]) == 2
    assert "out of range" in capsys.readouterr().err
    monkeypatch.setattr(m, "parquet_available", lambda: False)
    assert m.main(base) == 2
    assert "needs pyarrow" in capsys.readouterr().err


def test_a_key_file_that_is_not_there_stops_the_start(tmp_path: Path, capsys):
    assert m.main(["--db", ":memory:", "--identity-key-file", str(tmp_path / "nokey")]) == 2
    assert capsys.readouterr().err.startswith("[datalake]")


def test_a_token_that_is_too_short_stops_the_start(capsys):
    assert m.main(["--db", ":memory:", "--token", "short"]) == 2
    assert "at least 16" in capsys.readouterr().err


def test_a_start_that_passes_every_check_says_what_is_on(capsys):
    assert m.main(["--db", ":memory:", "--enable-window-stats", "--enable-downsampling", "--accept-gzip", "--enable-production-events", "--default-retention-days", "30"]) == 0
    out = capsys.readouterr().out
    for line in ("GET /windows is on", "downsampling is on", "gzip request bodies are accepted", "POST /production-events is on", "shutting down"):
        assert line in out


@pytest.mark.parametrize("value", ["yes", "1", "TRUE ?"])
def test_a_switch_in_the_environment_file_that_is_not_true_or_false_is_an_error(value):
    with pytest.raises(ValueError, match="must be true or false"):
        m.switch_setting(False, m.ADMIN_ENV, {m.ADMIN_ENV: value})


def test_a_switch_is_on_from_the_flag_or_from_the_file_and_the_flag_wins():
    assert m.switch_setting(True, m.ADMIN_ENV, {m.ADMIN_ENV: "false"}) is True
    assert m.switch_setting(False, m.ADMIN_ENV, {m.ADMIN_ENV: " True "}) is True
    assert m.switch_setting(False, m.ADMIN_ENV, {m.ADMIN_ENV: "false"}) is False
    assert m.switch_setting(False, m.ADMIN_ENV, {}) is False


def test_the_fine_rollup_settings_come_from_the_flags_then_the_file_and_a_wrong_value_is_an_error():
    assert m.fine_rollup_settings(False, None, {}) == (False, m.DEFAULT_MAX_FINE_PER_COARSE)
    assert m.fine_rollup_settings(False, None, {m.FINE_ROLLUPS_ENV: "true", m.MAX_FINE_PER_COARSE_ENV: " 12 "}) == (True, 12)
    assert m.fine_rollup_settings(True, 5, {m.FINE_ROLLUPS_ENV: "false", m.MAX_FINE_PER_COARSE_ENV: "99"}) == (True, 5)
    for env in ({m.FINE_ROLLUPS_ENV: "maybe"}, {m.MAX_FINE_PER_COARSE_ENV: "many"}, {m.MAX_FINE_PER_COARSE_ENV: "-3"}):
        with pytest.raises(ValueError):
            m.fine_rollup_settings(False, None, env)
    with pytest.raises(ValueError, match="positive"):
        m.fine_rollup_settings(False, 0, {})


def test_the_tokens_of_a_role_win_over_the_shared_one_and_an_empty_one_is_no_token():
    env = {"HYDRA_UMC_DATALAKE_TOKEN": "s" * 20, "HYDRA_UMC_DATALAKE_ADMIN_TOKEN": "a" * 20, "HYDRA_UMC_DATALAKE_READ_TOKEN": ""}
    assert m.tokens_from_environment(env) == {"ingest": "s" * 20, "admin": "a" * 20, "read": "s" * 20}
    assert m.tokens_from_environment({}) == {}
    assert m.tokens_from_environment({}, shared="x" * 20) == {role: "x" * 20 for role in ("ingest", "admin", "read")}


@pytest.mark.parametrize(("addr", "expected"), [("127.0.0.1", True), ("::1", True), (" localhost ", True), ("LOCALHOST", True), ("0.0.0.0", False), ("", False), ("lan.example", False)])
def test_loopback_is_the_loopback_interface_only(addr, expected):
    assert m.is_loopback(addr) is expected


def test_a_network_bind_with_the_identity_of_the_server_needs_no_token():
    assert m.startup_problem("0.0.0.0", {}, False, identity_on=True) is None
    assert m.startup_problem("0.0.0.0", {}, False, identity_on=False) is not None


def test_the_backup_command_refuses_a_negative_keep_and_a_database_that_is_not_there(tmp_path: Path, capsys):
    assert m.backup_main(["--db", str(tmp_path / "x.sqlite3"), "--dest", str(tmp_path), "--keep", "-1"]) == 2
    assert "--keep" in capsys.readouterr().err
    assert m.backup_main(["--db", str(tmp_path / "x.sqlite3"), "--dest", str(tmp_path / "out")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_the_backup_command_writes_a_copy_that_opens_with_the_same_data(tmp_path: Path, capsys):
    db = tmp_path / "plant.sqlite3"
    store = TimeSeriesStore(db)
    store.insert(Sample("press-1", "temperature", 1_700_000_000_000, {"value": 20.5}))
    store.close()
    assert m.backup_main(["--db", str(db), "--dest", str(tmp_path / "out"), "--keep", "1"]) == 0
    copy = next((tmp_path / "out").glob("datalake-*.sqlite3"))
    with TimeSeriesStore(copy) as restored:
        assert restored.sample_count() == 1


def test_the_export_command_refuses_a_day_that_is_not_a_date_and_a_database_that_is_not_there(tmp_path: Path, capsys):
    assert m.export_main(["--db", str(tmp_path / "x.sqlite3"), "--dest", str(tmp_path), "--day", "yesterday"]) == 2
    assert "--day must look like" in capsys.readouterr().err
    assert m.export_main(["--db", str(tmp_path / "x.sqlite3"), "--dest", str(tmp_path), "--day", "2025-03-31"]) == 2
    assert "does not exist" in capsys.readouterr().err
