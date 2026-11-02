# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/production.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Turns a finished-cycle event (the ``ProductionEvent`` that HYDRA-UMC-JOB-DISPATCHER publishes) into the sample that
HYDRA-UMC-PRODUCTION-REPORTS reads: kind ``production_event``, fields ``good`` (1 or 0) and ``cycleTimeS``, at the time the
cycle ended.

The event is checked against the same closed schema as the shared contract: exactly ``source_id``, ``at_utc``, ``good``,
``cycle_time_s`` and (optionally) ``job_id``; nothing else is accepted, so a message cannot smuggle extra data into the store.
The job id is not stored as text (the store holds numbers); it is folded into the sample's ``sequence``, so the same event sent
again replaces itself instead of counting twice, while two cycles that end in the same millisecond stay two points.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .store import MAX_SEQUENCE, Sample

KIND = "production_event"
MAX_CYCLE_TIME_S = 7 * 24 * 3600
_KEYS = {"source_id", "at_utc", "good", "cycle_time_s", "job_id"}
_MAX_TEXT = 128


def event_to_sample(event: object) -> Sample:
    """The sample of one event; ValueError or TypeError (a 400 for the caller) when the event is not valid."""
    if not isinstance(event, dict):
        raise TypeError("a production event must be an object")
    extra = set(event) - _KEYS
    if extra:
        raise ValueError(f"unknown key(s) {sorted(extra)} (the event has source_id, at_utc, good, cycle_time_s and job_id)")
    missing = {"source_id", "at_utc", "good", "cycle_time_s"} - set(event)
    if missing:
        raise ValueError(f"missing key(s) {sorted(missing)}")
    source_id = event["source_id"]
    if not isinstance(source_id, str) or not 1 <= len(source_id) <= _MAX_TEXT:
        raise TypeError(f"source_id must be a text of 1 to {_MAX_TEXT} characters")
    good = event["good"]
    if not isinstance(good, bool):
        raise TypeError("good must be true or false")
    cycle = event["cycle_time_s"]
    if isinstance(cycle, bool) or not isinstance(cycle, (int, float)):
        raise TypeError("cycle_time_s must be a number")
    cycle = float(cycle)
    if not 0 < cycle <= MAX_CYCLE_TIME_S:
        raise ValueError(f"cycle_time_s must be above 0 and at most {MAX_CYCLE_TIME_S} (one week)")
    moment = event["at_utc"]
    if not isinstance(moment, str):
        raise TypeError("at_utc must be a date-time text")
    try:
        when = datetime.fromisoformat(moment.upper().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("at_utc must be an ISO 8601 date-time") from None
    if when.tzinfo is None:
        raise ValueError("at_utc must say its time zone (for example Z)")
    timestamp = round(when.astimezone(timezone.utc).timestamp() * 1000)
    sequence = 0
    job_id = event.get("job_id")
    if job_id is not None:
        if not isinstance(job_id, str) or not 1 <= len(job_id) <= _MAX_TEXT:
            raise TypeError(f"job_id must be a text of 1 to {_MAX_TEXT} characters")
        digest = hashlib.sha256(job_id.encode("utf-8")).digest()
        sequence = 1 + int.from_bytes(digest[:7], "big") % (MAX_SEQUENCE - 1)
    return Sample(source_id=source_id, kind=KIND, timestamp=timestamp, fields={"good": 1.0 if good else 0.0, "cycleTimeS": cycle}, sequence=sequence)
