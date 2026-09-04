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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import Sample, TimeSeriesStore, to_utc_iso8601

DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0


class RequestBodyTooLarge(ValueError):
    """The client declared a JSON body larger than this service accepts."""


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    raw_length = handler.headers.get("Content-Length", "0")
    length = int(raw_length)
    if length < 0:
        raise ValueError("Content-Length must not be negative")
    if length > handler.server.max_request_body_bytes:  # type: ignore[attr-defined]
        # Real, reproducible race found by an ecosystem-wide audit: closing
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
            sample = Sample(
                source_id=body["sourceId"],
                kind=body["kind"],
                timestamp=int(body["timestamp"]),
                fields={k: float(v) for k, v in body.get("fields", {}).items()},
            )
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid sample: {e}"})
            return
        written = self.server.store.insert(sample)
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
    ) -> None:
        if max_request_body_bytes <= 0:
            raise ValueError("max_request_body_bytes must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        super().__init__(address, Handler)
        self.store = store
        self.max_request_body_bytes = max_request_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
