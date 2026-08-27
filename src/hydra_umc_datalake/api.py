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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import Sample, TimeSeriesStore


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0))
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

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Quiet by default - stdlib's BaseHTTPRequestHandler otherwise
        # logs every request to stderr, which would drown out this
        # project's own real log lines in main.py. A real operational
        # choice, not a missing feature.
        pass

    def do_POST(self) -> None:  # noqa: N802 (stdlib's own naming convention)
        if urlparse(self.path).path != "/ingest":
            _write_json(self, 404, {"error": "not found"})
            return
        try:
            body = _read_json_body(self)
            sample = Sample(
                source_id=body["sourceId"],
                kind=body["kind"],
                timestamp=int(body["timestamp"]),
                fields={k: float(v) for k, v in body.get("fields", {}).items()},
            )
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid sample: {e}"})
            return
        written = self.server.store.insert(sample)
        _write_json(self, 202, {"written": written})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        params = _query_params(self)
        if path == "/query":
            self._handle_query(params)
        elif path == "/aggregate":
            self._handle_aggregate(params)
        elif path == "/stats":
            _write_json(self, 200, {"sampleCount": self.server.store.sample_count()})
        else:
            _write_json(self, 404, {"error": "not found"})

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

    def __init__(self, address: tuple[str, int], store: TimeSeriesStore) -> None:
        super().__init__(address, Handler)
        self.store = store
