# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Plain JSON/HTTP surface (stdlib ``http.server``, no framework) over
TimeSeriesStore - same "no framework for a handful of routes" convention
already established by HYDRA-UMC-JOB-DISPATCHER (Go/net-http) and
HYDRA-UMC-TELEMETRY-COLLECTOR (Go/net-http), just in Python this time.
``ThreadingHTTPServer`` (stdlib) is enough to serve real concurrent
requests without pulling in an ASGI/WSGI framework for 4 routes.
"""
from __future__ import annotations

import json
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import Sample, TimeSeriesStore, to_utc_iso8601

DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
# C13: a real, conservative floor - a CM5's own real root filesystem is
# small enough (a handful of real GB, per this ecosystem's own hardware
# notes) that "wait until it's literally 0 bytes free" is not a safe
# real threshold; 64MB gives real headroom for sqlite's own journal/WAL
# file growth during a single real write before it too runs out of room.
DEFAULT_MIN_FREE_DISK_BYTES = 64 * 1024 * 1024


class RequestBodyTooLarge(ValueError):
    """The client declared a JSON body larger than this service accepts."""


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    raw_length = handler.headers.get("Content-Length", "0")
    length = int(raw_length)
    if length < 0:
        raise ValueError("Content-Length must not be negative")
    if length > handler.server.max_request_body_bytes:  # type: ignore[attr-defined]
        # Real, reproducible race found while auditing the code: closing
        # the connection here without reading any of an over-limit body
        # left the client's own send() still in flight once the body was
        # bigger than the OS socket buffer, so the client saw a raw
        # ConnectionAbortedError instead of this clean 413 (flaky - it
        # depended on how much the kernel had already buffered). Draining
        # a bounded amount first lets the client finish sending before the
        # response goes out, without ever holding more than one bounded
        # read in memory - the same fix this family's ANOMALY-DETECTOR
        # api.py needed for the identical pattern.
        drain_cap = handler.server.max_request_body_bytes * 16  # type: ignore[attr-defined]
        if length <= drain_cap:
            handler.rfile.read(length)
        raise RequestBodyTooLarge(
            f"request body exceeds {handler.server.max_request_body_bytes} bytes"  # type: ignore[attr-defined]
        )
    raw = handler.rfile.read(length) if length else b"{}"
    return json.loads(raw)


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _query_params(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = urlparse(handler.path)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


class Handler(BaseHTTPRequestHandler):
    """``self.server`` is a ``DatalakeServer`` (below), which is what
    actually carries the ``TimeSeriesStore`` - that's the real seam that
    lets tests spin up a handler bound to a fresh in-memory store per
    test, instead of one shared global."""

    server: "DatalakeServer"

    def setup(self) -> None:
        """Bound each client connection before a handler thread reads it."""
        super().setup()
        self.connection.settimeout(self.server.request_timeout_seconds)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet by default - stdlib's BaseHTTPRequestHandler otherwise
        # logs every request to stderr, which would drown out this
        # project's own real log lines in main.py. A real operational
        # choice, not a missing feature.
        pass

    def do_POST(self) -> None:  # noqa: N802 (stdlib's own naming convention)
        path = urlparse(self.path).path
        if path == "/ingest":
            self._handle_ingest()
        elif path == "/retention":
            self._handle_set_retention()
        elif path == "/retention/apply":
            self._handle_apply_retention()
        else:
            _write_json(self, 404, {"error": "not found"})

    def _handle_ingest(self) -> None:
        try:
            body = _read_json_body(self)
            # DATA-01 (P1): "fields" used to go straight into a dict
            # comprehension's own .items() call - a real client sending
            # "fields": [] (or null, or any other non-object JSON value)
            # raised an uncaught AttributeError, outside this handler's
            # normal 400-producing validation contract. Checked
            # explicitly here so every wrong shape becomes one controlled
            # TypeError, caught below like every other validation error.
            raw_fields = body.get("fields", {})
            if not isinstance(raw_fields, dict):
                raise TypeError(
                    "fields must be an object mapping field name to a "
                    f"finite number, got {type(raw_fields).__name__}"
                )
            fields: dict[str, float] = {}
            for key, value in raw_fields.items():
                # bool is a subclass of int in Python, so float(True)
                # silently succeeds as 1.0 - a real client sending a
                # boolean where a numeric reading belongs (a nested
                # config bug, not a deliberate 0/1 encoding) must be
                # rejected explicitly rather than stored as a number
                # nobody actually sent.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(
                        f"field {key!r} must be a real number, got {type(value).__name__}"
                    )
                fields[key] = float(value)
            source_id = body["sourceId"]
            kind = body["kind"]
            if not isinstance(source_id, str) or not isinstance(kind, str):
                # Sample.__post_init__ only checks truthiness, so a
                # nested object here (a real "tipos anidados incorrectos"
                # case) would otherwise reach sqlite3's own parameter
                # binding inside store.insert() and fail there instead,
                # outside of this handler's controlled 400 contract.
                raise TypeError("sourceId and kind must both be strings")
            timestamp = int(body["timestamp"])
            # An out-of-range timestamp used to sail straight into
            # storage and only fail later, when to_utc_iso8601() (queried
            # at /stats/range or report time) tried to convert it - a
            # deferred crash on read, not a controlled 400 at write time.
            # Validated here against the exact same conversion
            # to_utc_iso8601() itself performs, so "accepted at ingest"
            # and "safely readable later" are the same real guarantee,
            # not two independently maintained bounds.
            to_utc_iso8601(timestamp)
            sample = Sample(source_id=source_id, kind=kind, timestamp=timestamp, fields=fields)
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, OverflowError, OSError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid sample: {e}"})
            return

        # C13: real disk-pressure gate, checked BEFORE ever attempting the
        # write - the same "refuse before I/O" discipline this ecosystem
        # already applies elsewhere, not a reactive catch after a real
        # write has already started to fail. `free_disk_bytes()` is
        # `None` for a `:memory:` store (nothing to run out of), so this
        # only ever applies to a real on-disk deployment.
        free_bytes = self.server.store.free_disk_bytes()
        if free_bytes is not None and free_bytes < self.server.min_free_disk_bytes:
            _write_json(self, 507, {
                "error": f"insufficient disk space: {free_bytes} byte(s) free, "
                         f"{self.server.min_free_disk_bytes} required - refusing to ingest",
            })
            return

        try:
            written = self.server.store.insert(sample)
        except sqlite3.OperationalError as e:
            # Defense in depth: the proactive check above can still race a
            # real write from something else on the same filesystem
            # filling the last of the room in between - a genuine sqlite3
            # "disk full"/"database or disk is full" here must surface as
            # this same real, honest 507, never an unhandled 500.
            _write_json(self, 507, {"error": f"disk write failed, likely out of real disk space: {e}"})
            return
        _write_json(self, 202, {"written": written})

    def _handle_set_retention(self) -> None:
        try:
            body = _read_json_body(self)
            self.server.store.set_retention_policy(
                kind=body["kind"],
                field=body["field"],
                retention_ms=int(body["retentionMs"]),
            )
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid retention policy: {e}"})
            return
        _write_json(self, 200, {"ok": True})

    def _handle_apply_retention(self) -> None:
        deleted = self.server.store.apply_retention()
        _write_json(self, 200, {"deleted": deleted})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        params = _query_params(self)
        if path == "/query":
            self._handle_query(params)
        elif path == "/aggregate":
            self._handle_aggregate(params)
        elif path == "/stats":
            _write_json(self, 200, {"sampleCount": self.server.store.sample_count()})
        elif path == "/stats/range":
            self._handle_stats_range()
        elif path == "/retention":
            _write_json(
                self,
                200,
                [
                    {"kind": kind, "field": field, "retentionMs": retention_ms}
                    for kind, field, retention_ms in self.server.store.list_retention_policies()
                ],
            )
        else:
            _write_json(self, 404, {"error": "not found"})

    def _handle_stats_range(self) -> None:
        """Real oldest/newest sample timestamps, explicitly labeled UTC -
        a new, additive endpoint; the existing /stats response above is
        never touched, so nothing that already depends on its exact
        shape breaks."""
        oldest_ms, newest_ms = self.server.store.timestamp_range()
        _write_json(
            self,
            200,
            {
                "oldestMs": oldest_ms,
                "newestMs": newest_ms,
                "oldestUtc": to_utc_iso8601(oldest_ms) if oldest_ms is not None else None,
                "newestUtc": to_utc_iso8601(newest_ms) if newest_ms is not None else None,
            },
        )

    def _handle_query(self, params: dict[str, str]) -> None:
        try:
            points = self.server.store.query(
                source_id=params.get("sourceId"),
                kind=params.get("kind"),
                field=params.get("field"),
                start=int(params["start"]) if "start" in params else None,
                end=int(params["end"]) if "end" in params else None,
                limit=int(params.get("limit", 1000)),
            )
        except ValueError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(
            self,
            200,
            [
                {
                    "sourceId": p.source_id,
                    "kind": p.kind,
                    "field": p.field,
                    "timestamp": p.timestamp,
                    "value": p.value,
                }
                for p in points
            ],
        )

    def _handle_aggregate(self, params: dict[str, str]) -> None:
        required = {"kind", "field", "bucketMs", "start", "end"}
        missing = required - params.keys()
        if missing:
            _write_json(self, 400, {"error": f"missing required params: {sorted(missing)}"})
            return
        try:
            buckets = self.server.store.aggregate(
                kind=params["kind"],
                field=params["field"],
                bucket_ms=int(params["bucketMs"]),
                start=int(params["start"]),
                end=int(params["end"]),
                agg=params.get("agg", "avg"),
                source_id=params.get("sourceId"),
            )
        except ValueError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(
            self,
            200,
            [
                {"bucketStart": b.bucket_start, "value": b.value, "count": b.count}
                for b in buckets
            ],
        )


class DatalakeServer(ThreadingHTTPServer):
    """A real ``ThreadingHTTPServer`` that carries a ``TimeSeriesStore`` -
    every request handler reaches it via ``self.server.store``."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: TimeSeriesStore,
        *,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES,
    ) -> None:
        if max_request_body_bytes <= 0:
            raise ValueError("max_request_body_bytes must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if min_free_disk_bytes <= 0:
            raise ValueError("min_free_disk_bytes must be positive")
        super().__init__(address, Handler)
        self.store = store
        self.min_free_disk_bytes = min_free_disk_bytes
        self.max_request_body_bytes = max_request_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
