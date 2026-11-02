# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/export.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Daily export of the raw points to Parquet files, for offline analysis (dashboards, synthetic-data tools, notebooks).

One file per UTC day, ``samples-YYYY-MM-DD.parquet``, with the columns ``source_id``, ``kind``, ``field``, ``timestamp_ms``,
``value`` and ``sequence``, sorted by time. Only what the store holds as data goes out: no token, no setting, no path. The
file is written beside its final name and renamed when complete, an existing file is never overwritten (unless asked on the
command line), and nothing is ever deleted. Only closed days (before today, UTC) are exported by the timer.

``pyarrow`` is an OPTIONAL extra (``pip install "hydra-umc-datalake[parquet]"``): without it the rest of the service works
exactly the same and the export says what is missing.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .store import TimeSeriesStore, now_ms

logger = logging.getLogger("hydra_umc_datalake.export")

PAGE_ROWS = 50_000
DEFAULT_LOOKBACK_DAYS = 7
MAX_LOOKBACK_DAYS = 3650
_NAME = re.compile(r"^samples-(\d{4}-\d{2}-\d{2})\.parquet$")
COLUMNS = ("source_id", "kind", "field", "timestamp_ms", "value", "sequence")


class ParquetUnavailable(RuntimeError):
    """pyarrow is not installed."""


def _pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise ParquetUnavailable('the Parquet export needs pyarrow: pip install "hydra-umc-datalake[parquet]"') from None
    return pa, pq


def parquet_available() -> bool:
    try:
        _pyarrow()
    except ParquetUnavailable:
        return False
    return True


def file_name(day: date) -> str:
    return f"samples-{day.isoformat()}.parquet"


def day_bounds_ms(day: date) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def export_day(store: TimeSeriesStore, day: date, dest: str | Path, *, overwrite: bool = False, page_rows: int = PAGE_ROWS) -> Path | None:
    """Writes the Parquet file of one UTC day into ``dest``. Returns its path, or None when the day has no points (no empty
    file is written) or the file already exists and ``overwrite`` is False. Raises ParquetUnavailable without pyarrow."""
    pa, pq = _pyarrow()
    target = Path(dest) / file_name(day)
    if target.exists() and not overwrite:
        return None
    start_ms, end_ms = day_bounds_ms(day)
    first = store.export_page(start_ms=start_ms, end_ms=end_ms, limit=page_rows)
    if not first:
        return None
    Path(dest).mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".{os.getpid()}.part")
    schema = pa.schema(
        [("source_id", pa.string()), ("kind", pa.string()), ("field", pa.string()),
         ("timestamp_ms", pa.int64()), ("value", pa.float64()), ("sequence", pa.int64())]
    )
    writer = pq.ParquetWriter(str(temporary), schema, compression="zstd")
    try:
        page = first
        while page:
            columns = list(zip(*[(r[2], r[3], r[4], r[0], r[5], r[6]) for r in page]))
            writer.write_table(pa.Table.from_arrays([pa.array(list(c), type=schema.field(i).type) for i, c in enumerate(columns)], schema=schema))
            last = page[-1]
            page = store.export_page(start_ms=start_ms, end_ms=end_ms, after=(last[0], last[1]), limit=page_rows) if len(page) == page_rows else []
    except BaseException:
        writer.close()
        temporary.unlink(missing_ok=True)
        raise
    writer.close()
    os.replace(temporary, target)
    return target


def export_closed_days(
    store: TimeSeriesStore, dest: str | Path, *, lookback_days: int = DEFAULT_LOOKBACK_DAYS, clock: Callable[[], int] = now_ms
) -> list[Path]:
    """Exports every closed UTC day of the last ``lookback_days`` that has points and no file yet, oldest first."""
    if not 1 <= lookback_days <= MAX_LOOKBACK_DAYS:
        raise ValueError(f"lookback_days must be between 1 and {MAX_LOOKBACK_DAYS}")
    today = datetime.fromtimestamp(clock() / 1000, tz=timezone.utc).date()
    written: list[Path] = []
    for back in range(lookback_days, 0, -1):
        path = export_day(store, today - timedelta(days=back), dest)
        if path is not None:
            logger.info("parquet export: wrote %s", path.name)
            written.append(path)
    return written


def exported_days(dest: str | Path) -> list[date]:
    """The days that already have a file in ``dest``."""
    folder = Path(dest)
    if not folder.is_dir():
        return []
    return sorted(date.fromisoformat(m.group(1)) for m in (_NAME.match(p.name) for p in folder.iterdir()) if m)
