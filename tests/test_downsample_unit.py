# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_downsample_unit.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The example downsampling unit: static checks of the files and the script run against a real service."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.store import Sample, TimeSeriesStore

SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
SCRIPT = SYSTEMD / "hydra-umc-datalake-downsample.sh"
DAY = 86_400_000
T0 = 1_800_000_000_000 - (1_800_000_000_000 % DAY)
TOKEN = "a" * 16
HAVE_TOOLS = shutil.which("sh") is not None and shutil.which("curl") is not None


def test_the_unit_files_are_consistent_and_safe_by_default() -> None:
    service = (SYSTEMD / "hydra-umc-datalake-downsample.service").read_text(encoding="utf-8")
    timer = (SYSTEMD / "hydra-umc-datalake-downsample.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "NoNewPrivileges=true" in service and "ProtectSystem=strict" in service
    assert "ExecStart=/opt/hydra-umc/datalake/bin/downsample.sh" in service
    assert "DOWNSAMPLE_DRY_RUN=false" in service  # documented as the opt-in, only in a comment
    assert not any(line.startswith("Environment") and "DRY_RUN=false" in line for line in service.splitlines())
    assert "OnCalendar=" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer
    assert "dry" in SCRIPT.read_text(encoding="utf-8").lower()


class Clock:
    def __call__(self) -> int:
        return T0 + 30 * DAY


@pytest.fixture()
def lake() -> Iterator[tuple[str, TimeSeriesStore]]:
    store = TimeSeriesStore(":memory:")
    store.insert_many([Sample(source_id="a", kind="temp", timestamp=T0 + i * (DAY // 2), fields={"j1": float(i)}) for i in range(8)])
    server = DatalakeServer(("127.0.0.1", 0), store, enable_downsampling=True, clock=Clock(), tokens={"admin": TOKEN})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        url + "/downsample", method="POST", headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        data=json.dumps({"kind": "temp", "field": "j1", "rawMs": DAY, "bucketMs": DAY}).encode(),
    )
    urllib.request.urlopen(request).read()
    try:
        yield url, store
    finally:
        server.shutdown()
        server.server_close()
        store.close()
        thread.join(timeout=2)


def _run(url: str, **env: str) -> subprocess.CompletedProcess[str]:
    full = {**os.environ, "HYDRA_UMC_DATALAKE_URL": url, "HYDRA_UMC_DATALAKE_ADMIN_TOKEN": TOKEN}
    for name in ("DRY_RUN", "MAX_PASSES", "TIMEOUT"):
        full.pop(f"HYDRA_UMC_DATALAKE_DOWNSAMPLE_{name}", None)
    full.update(env)
    return subprocess.run(["sh", str(SCRIPT)], env=full, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs sh and curl")
def test_the_script_is_a_dry_run_by_default(lake: tuple[str, TimeSeriesStore]) -> None:
    url, store = lake
    before = store.sample_count()
    result = _run(url)
    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout and '"dryRun": true' in result.stdout
    assert store.sample_count() == before


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs sh and curl")
def test_only_an_explicit_false_applies(lake: tuple[str, TimeSeriesStore]) -> None:
    url, store = lake
    result = _run(url, HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN="false")
    assert result.returncode == 0, result.stderr
    assert "APPLYING" in result.stdout and '"dryRun": false' in result.stdout
    assert store.sample_count() == 0


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs sh and curl")
@pytest.mark.parametrize("name,value", [
    ("DRY_RUN", "0"), ("DRY_RUN", "no"), ("DRY_RUN", "FALSE"), ("DRY_RUN", ""),
    ("MAX_PASSES", "0"), ("MAX_PASSES", "101"), ("MAX_PASSES", "x"), ("TIMEOUT", "0"), ("TIMEOUT", "-1"),
])
def test_a_bad_value_is_refused_before_any_call(lake: tuple[str, TimeSeriesStore], name: str, value: str) -> None:
    url, store = lake
    before = store.sample_count()
    result = _run(url, **{f"HYDRA_UMC_DATALAKE_DOWNSAMPLE_{name}": value})
    assert result.returncode == 2, result.stdout
    assert store.sample_count() == before


@pytest.mark.skipif(not HAVE_TOOLS, reason="needs sh and curl")
def test_a_wrong_token_or_an_unreachable_service_fails_the_unit(lake: tuple[str, TimeSeriesStore]) -> None:
    url, store = lake
    assert _run(url, HYDRA_UMC_DATALAKE_ADMIN_TOKEN="b" * 16, HYDRA_UMC_DATALAKE_DOWNSAMPLE_DRY_RUN="false").returncode != 0
    assert _run("http://127.0.0.1:9").returncode != 0
    assert store.sample_count() == 8
