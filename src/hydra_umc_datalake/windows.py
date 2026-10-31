# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/windows.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""The arithmetic of the window statistics, kept apart from SQL and HTTP so it can be tested on its own:
which statistics a request may ask for, and the percentile of a list of values."""
from __future__ import annotations

import math
import re

# Statistics a SQL GROUP BY gives without loading the points of a window.
SIMPLE_STATS = ("count", "min", "max", "avg", "sum")
DEFAULT_STATS = ("count", "min", "max", "avg")
# One request asks for at most this many statistics (every percentile costs a sort of each bucket).
MAX_STATS = 16
_PERCENTILE = re.compile(r"^p(\d{1,3}(?:\.\d{1,3})?)$")


def parse_stats(text: str | None) -> tuple[str, ...]:
    """``count,min,p95`` -> the validated, de-duplicated names in the order they were asked for.
    A percentile is written ``p<number>`` with the number from 0 to 100 (``p50``, ``p99.9``)."""
    if text is None or text == "":
        return DEFAULT_STATS
    names: list[str] = []
    for raw in text.split(","):
        name = raw.strip()
        if name in SIMPLE_STATS:
            pass
        else:
            match = _PERCENTILE.match(name)
            if match is None or not 0.0 <= float(match.group(1)) <= 100.0:
                raise ValueError(
                    f"unknown statistic {name!r}: want {list(SIMPLE_STATS)} or p<0-100> such as p95"
                )
        if name not in names:
            names.append(name)
    if len(names) > MAX_STATS:
        raise ValueError(f"at most {MAX_STATS} statistics per request")
    return tuple(names)


def percentile_fraction(name: str) -> float:
    """``p95`` -> 0.95."""
    match = _PERCENTILE.match(name)
    if match is None:
        raise ValueError(f"{name!r} is not a percentile")
    return float(match.group(1)) / 100.0


def percentile(sorted_values: list[float], fraction: float) -> float:
    """The percentile of an ascending list by linear interpolation between the two closest ranks
    (the usual "type 7" definition). It is always between the smallest and the largest value, the
    median of an even count is the mean of the two middle values, and it never decreases as the
    fraction grows."""
    if not sorted_values:
        raise ValueError("a percentile needs at least one value")
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    position = fraction * (len(sorted_values) - 1)
    low = math.floor(position)
    high = min(low + 1, len(sorted_values) - 1)
    weight = position - low
    a, b = sorted_values[low], sorted_values[high]
    value = a + (b - a) * weight
    # float rounding must never leave the interval of the two neighbours
    return min(max(value, min(a, b)), max(a, b))
