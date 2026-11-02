# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/plan.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""A tier plan: the downsampling and the retention of every kind of data in ONE file, and the timer that applies it.

The plan is a JSON file (``--downsample-plan``) read at start. Each tier says, for a kind (and one field, or ``"*"`` for every
field of the kind that is stored now or later), how long the raw points stay, the width of the wide rollup (for example one
hour), optionally a narrower one (for example one minute), and how long each width lives::

    {"tiers": [
      {"kind": "motor_temp", "field": "*", "raw": "2d", "bucket": "1h", "fineBucket": "1m",
       "rollupRetention": "5y", "fineRetention": "30d"},
      {"kind": "*", "field": "*", "raw": "30d", "bucket": "1d"}
    ]}

Durations are a whole number of milliseconds or a number with a unit: ``s``, ``m``, ``h``, ``d``, ``w`` or ``y`` (365 days).
Nothing is deleted because a plan exists: the plan only writes the policies. The runner works as a dry run unless the service
was started with ``--downsample-delete``, and the narrow level only exists when the fine rollups are on.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .store import DEFAULT_MAX_FINE_PER_COARSE, TimeSeriesStore, now_ms

logger = logging.getLogger("hydra_umc_datalake.downsample")

MAX_PLAN_BYTES = 256 * 1024
MAX_TIERS = 1000
MIN_INTERVAL_SECONDS = 60.0
_UNITS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000, "y": 31_536_000_000}
_DURATION = re.compile(r"^(\d+)([smhdwy])$")
_KEYS = {"kind", "field", "raw", "bucket", "fineBucket", "rollupRetention", "fineRetention"}


class PlanError(ValueError):
    """The plan file cannot be used; the message says which tier and why."""


def parse_duration(value: object, name: str) -> int:
    """``"1h"`` or ``3600000`` -> milliseconds; anything else is a PlanError."""
    if isinstance(value, bool):
        raise PlanError(f"{name} must be a duration such as 30d or a number of milliseconds")
    if isinstance(value, int):
        ms = value
    elif isinstance(value, str):
        match = _DURATION.match(value.strip().lower())
        if match is None:
            raise PlanError(f"{name}: {value!r} is not a duration such as 90s, 1m, 1h, 30d or 5y")
        ms = int(match.group(1)) * _UNITS[match.group(2)]
    else:
        raise PlanError(f"{name} must be a duration such as 30d or a number of milliseconds")
    if ms <= 0 or ms > 100 * _UNITS["y"]:
        raise PlanError(f"{name} must be positive and at most 100 years")
    return ms


@dataclass(frozen=True)
class Tier:
    kind: str
    field: str
    raw_ms: int
    bucket_ms: int
    fine_bucket_ms: int | None = None
    rollup_retention_ms: int | None = None
    fine_retention_ms: int | None = None


def parse_plan(document: object, *, max_fine_per_coarse: int = DEFAULT_MAX_FINE_PER_COARSE) -> list[Tier]:
    """Validates a decoded plan and returns its tiers (PlanError on the first problem)."""
    if not isinstance(document, dict) or set(document) - {"tiers"} or not isinstance(document.get("tiers"), list):
        raise PlanError('the plan must be an object with one key, "tiers", holding a list')
    items = document["tiers"]
    if not items or len(items) > MAX_TIERS:
        raise PlanError(f"the plan needs between 1 and {MAX_TIERS} tiers")
    tiers: list[Tier] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(items):
        where = f"tiers[{index}]"
        if not isinstance(item, dict):
            raise PlanError(f"{where} must be an object")
        extra = set(item) - _KEYS
        if extra:
            raise PlanError(f"{where}: unknown key(s) {sorted(extra)}")
        kind, field = item.get("kind"), item.get("field", "*")
        if not isinstance(kind, str) or not kind or not isinstance(field, str) or not field:
            raise PlanError(f"{where}: kind and field must be non-empty strings")
        if kind == "*" and field != "*":
            raise PlanError(f"{where}: a field needs a kind (only the default tier is * and *)")
        if (kind, field) in seen:
            raise PlanError(f"{where}: {kind}/{field} is listed twice")
        seen.add((kind, field))
        for key in ("raw", "bucket"):
            if key not in item:
                raise PlanError(f"{where}: {key} is required")
        raw = parse_duration(item["raw"], f"{where}.raw")
        bucket = parse_duration(item["bucket"], f"{where}.bucket")
        fine = parse_duration(item["fineBucket"], f"{where}.fineBucket") if "fineBucket" in item else None
        keep = parse_duration(item["rollupRetention"], f"{where}.rollupRetention") if "rollupRetention" in item else None
        keep_fine = parse_duration(item["fineRetention"], f"{where}.fineRetention") if "fineRetention" in item else None
        if fine is not None and (fine >= bucket or bucket % fine != 0 or bucket // fine > max_fine_per_coarse):
            raise PlanError(f"{where}: fineBucket must be smaller than bucket, divide it exactly and make at most {max_fine_per_coarse} per bucket")
        if keep_fine is not None:
            if fine is None:
                raise PlanError(f"{where}: fineRetention needs fineBucket")
            if kind == "*":
                raise PlanError(f"{where}: fineRetention is not available on the default tier (name the kinds)")
            if keep is not None and keep_fine > keep:
                raise PlanError(f"{where}: fineRetention must not be longer than rollupRetention")
        tiers.append(Tier(kind, field, raw, bucket, fine, keep, keep_fine))
    return tiers


def load_plan(path: str | Path, *, max_fine_per_coarse: int = DEFAULT_MAX_FINE_PER_COARSE) -> list[Tier]:
    """Reads and validates the plan file."""
    file = Path(path)
    try:
        if file.stat().st_size > MAX_PLAN_BYTES:
            raise PlanError(f"the plan file is larger than {MAX_PLAN_BYTES} bytes")
        document = json.loads(file.read_text(encoding="utf-8"))
    except OSError as e:
        raise PlanError(f"cannot read the plan file: {e.strerror or e}") from None
    except json.JSONDecodeError as e:
        raise PlanError(f"the plan file is not valid JSON: {e}") from None
    return parse_plan(document, max_fine_per_coarse=max_fine_per_coarse)


def expand(store: TimeSeriesStore, tiers: list[Tier]) -> list[Tier]:
    """The tiers with ``field: "*"`` of a named kind replaced by one tier per field stored now; a field with a tier of its own keeps it."""
    explicit = {(t.kind, t.field) for t in tiers if t.field != "*" or t.kind == "*"}
    out: list[Tier] = [t for t in tiers if t.field != "*" or t.kind == "*"]
    for tier in tiers:
        if tier.field != "*" or tier.kind == "*":
            continue
        fields = store.known_fields(tier.kind)
        for field in fields:
            if (tier.kind, field) not in explicit:
                out.append(Tier(tier.kind, field, tier.raw_ms, tier.bucket_ms, tier.fine_bucket_ms, tier.rollup_retention_ms, tier.fine_retention_ms))
    return out


def apply_plan(
    store: TimeSeriesStore,
    tiers: list[Tier],
    *,
    dry_run: bool = True,
    fine_rollups: bool = False,
    max_buckets: int,
    max_fine_per_coarse: int = DEFAULT_MAX_FINE_PER_COARSE,
    clock: Callable[[], int] = now_ms,
) -> dict[str, object]:
    """Writes the policies of the plan, runs the downsampling (a dry run unless ``dry_run`` is False) and expires the narrow
    level where the plan asks for it. Returns the downsampling report plus ``expiredFineRollups``."""
    for tier in expand(store, tiers):
        fine = tier.fine_bucket_ms if fine_rollups else None
        store.set_downsample_policy(
            kind=tier.kind,
            field=tier.field,
            raw_ms=tier.raw_ms,
            bucket_ms=tier.bucket_ms,
            rollup_retention_ms=tier.rollup_retention_ms,
            fine_bucket_ms=fine,
            max_fine_per_coarse=max_fine_per_coarse,
        )
    at = clock()
    report = store.apply_downsample(at_ms=at, dry_run=dry_run, max_buckets=max_buckets, fine_rollups=fine_rollups)
    expired = 0
    if fine_rollups:
        for tier in expand(store, tiers):
            if tier.fine_retention_ms is not None and tier.fine_bucket_ms is not None:
                expired += store.expire_rollups(
                    kind=tier.kind, field=tier.field, bucket_ms=tier.fine_bucket_ms, older_than_ms=at - tier.fine_retention_ms, dry_run=dry_run
                )
    report["expiredFineRollups"] = expired
    if expired:
        logger.info("downsample: %s %d narrow rollup(s) past their own retention", "would remove" if dry_run else "removed", expired)
    return report


class PeriodicRunner:
    """Calls ``action`` every ``interval_seconds`` on a daemon thread until ``stop()``. An exception is logged and the next
    round still runs. The first call comes after one interval, or at once with ``immediate``."""

    def __init__(self, name: str, interval_seconds: float, action: Callable[[], object], *, immediate: bool = False) -> None:
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(f"the interval of {name} must be at least {int(MIN_INTERVAL_SECONDS)} seconds")
        self.name = name
        self.interval = interval_seconds
        self._action = action
        self._immediate = immediate
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self.runs = 0
        self.failures = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def run_once(self) -> None:
        try:
            self._action()
            self.runs += 1
        except Exception:  # noqa: BLE001 - the timer must outlive one bad round
            self.failures += 1
            logger.exception("%s: the round failed", self.name)

    def _loop(self) -> None:
        if self._immediate and not self._stop.is_set():
            self.run_once()
        while not self._stop.wait(self.interval):
            self.run_once()
