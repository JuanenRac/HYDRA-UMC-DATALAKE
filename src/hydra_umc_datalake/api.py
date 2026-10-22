# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/api.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Plain JSON/HTTP surface (stdlib ``http.server``, no framework) over
TimeSeriesStore - same "no framework for a handful of routes" convention
already established by HYDRA-UMC-JOB-DISPATCHER (Go/net-http) and
HYDRA-UMC-TELEMETRY-COLLECTOR (Go/net-http), just in Python this time.
``ThreadingHTTPServer`` (stdlib) is enough to serve real concurrent
requests without pulling in an ASGI/WSGI framework for 4 routes.
"""
from __future__ import annotations

import base64
import hmac
import json
import socket
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import MAX_AGGREGATE_BUCKETS, MAX_QUERY_LIMIT, MAX_SERIES, Sample, TimeSeriesStore, to_utc_iso8601

DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
# Samples one POST /ingest/batch may carry (the body is also bounded in bytes).
DEFAULT_MAX_BATCH_SAMPLES = 1000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
# a real, conservative floor - a CM5's own real root filesystem is
# small enough (a handful of real GB, per this ecosystem's own hardware
# notes) that "wait until it's literally 0 bytes free" is not a safe
# real threshold; 64MB gives real headroom for sqlite's own journal/WAL
# file growth during a single real write before it too runs out of room.
DEFAULT_MIN_FREE_DISK_BYTES = 64 * 1024 * 1024


# A timestamp below this is a producer that sent seconds instead of milliseconds (1700000000
# would be January 1970 and invisible to every dashboard); the lower bound is early 1973.
MIN_PLAUSIBLE_TIMESTAMP_MS = 100_000_000_000
# How far ahead of this machine's clock a sample may be (a little skew between machines is normal).
DEFAULT_MAX_FUTURE_SKEW_MS = 5 * 60 * 1000
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

# What a token allows: "ingest" writes samples, "admin" changes and applies the retention
# policies (it deletes history), "read" queries.
ROLES = ("ingest", "admin", "read")


class RequestBodyTooLarge(ValueError):
    """The client declared a JSON body larger than this service accepts."""


class LengthRequired(ValueError):
    """The body was sent without a Content-Length (for example chunked), which this service does not read."""


def _int64(value: int, name: str) -> int:
    """SQLite integers are 64-bit: a bigger one raises OverflowError inside the driver, far from the request."""
    if not _INT64_MIN <= value <= _INT64_MAX:
        raise ValueError(f"{name} is out of range")
    return value


def _int_param(params: dict[str, str], name: str) -> int:
    try:
        return _int64(int(params[name]), name)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer ({e})") from None


def _encode_cursor(cursor: tuple[int, str, str, str, int]) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(cursor)).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(text: str) -> tuple[int, str, str, str, int]:
    try:
        raw = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        ts, source, kind, field, row_id = raw
        if not (
            isinstance(ts, int) and isinstance(row_id, int)
            and not isinstance(ts, bool) and not isinstance(row_id, bool)
            and all(isinstance(x, str) for x in (source, kind, field))
        ):
            raise ValueError
        return _int64(ts, "cursor"), source, kind, field, _int64(row_id, "cursor")
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("cursor is not a cursor returned by this service") from None


def _exclusive_end(params: dict[str, str]) -> int | None:
    """The inclusive ``end`` a request means: ``end`` itself, or ``before - 1`` for the half-open
    window ``[start, before)`` (a shift, a day) that does not count its boundary point twice."""
    if "end" in params and "before" in params:
        raise ValueError("send end (inclusive) or before (exclusive), not both")
    if "before" in params:
        return _int_param(params, "before") - 1
    return _int_param(params, "end") if "end" in params else None


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    handler.body_consumed = True  # type: ignore[attr-defined]
    encoding = handler.headers.get("Transfer-Encoding", "").strip().lower()
    if encoding and encoding != "identity":
        raise LengthRequired("send the body with a Content-Length (chunked bodies are not read)")
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
        handler.close_connection = True
        raise RequestBodyTooLarge(
            f"request body exceeds {handler.server.max_request_body_bytes} bytes"  # type: ignore[attr-defined]
        )
    raw = handler.rfile.read(length) if length else b"{}"
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        # valid JSON that is not an object (a list, a string, null, a number) used to reach
        # `body.get("fields")` on POST /ingest and raise an AttributeError nothing caught:
        # the handler thread died with a traceback and the client got no response at all
        raise ValueError("the JSON body must be an object")
    return parsed


def _write_json(
    handler: BaseHTTPRequestHandler, status: int, payload: object, headers: dict[str, str] | None = None
) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _query_params(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = urlparse(handler.path)
    return {k: v[0] for k, v in parse_qs(parsed.query).items()}


def _validate_real_int(value: object, *, name: str) -> int:
    """a plain `int(value)` admits a bool (bool is a subclass of
    int in Python, so `int(True)` silently succeeds as 1 - the same real
    gap already closed for `fields` values below via an explicit
    `isinstance(value, bool)` check) and silently truncates a non-integer
    float (`int(1699999999999.9)` == 1699999999999, dropping the
    fractional part instead of rejecting it) rather than raising on
    either. Applied to every caller-supplied integer that becomes a real
    stored quantity - a sample timestamp, a retention window - so a
    malformed request is refused with a 400 instead of being silently
    coerced into a value nobody actually sent."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a real integer, got {type(value).__name__}")
    if isinstance(value, float) and not value.is_integer():
        raise TypeError(f"{name} must be a whole number, got {value!r}")
    return int(value)


def _sample_from_body(body: dict, server: "DatalakeServer") -> Sample:
    """Validates one sample object (also each element of a batch) and returns it; anything wrong is a
    KeyError, ValueError or TypeError that the caller turns into a 400."""
    # "fields" used to go straight into a dict
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
    timestamp = _validate_real_int(body["timestamp"], name="timestamp")
    # An out-of-range timestamp used to sail straight into
    # storage and only fail later, when to_utc_iso8601() (queried
    # at /stats/range or report time) tried to convert it - a
    # deferred crash on read, not a controlled 400 at write time.
    # Validated here against the exact same conversion
    # to_utc_iso8601() itself performs, so "accepted at ingest"
    # and "safely readable later" are the same real guarantee,
    # not two independently maintained bounds.
    to_utc_iso8601(timestamp)
    if timestamp < server.min_timestamp_ms:
        raise ValueError(
            f"timestamp {timestamp} is too small: unix MILLISECONDS are expected (a value in seconds is January 1970)"
        )
    max_future = server.max_future_skew_ms
    if max_future > 0 and timestamp > int(time.time() * 1000) + max_future:
        raise ValueError(f"timestamp {timestamp} is more than {max_future} ms ahead of this machine's clock")
    sample = Sample(source_id=source_id, kind=kind, timestamp=timestamp, fields=fields)
    return sample


class Handler(BaseHTTPRequestHandler):
    """``self.server`` is a ``DatalakeServer`` (below), which is what
    actually carries the ``TimeSeriesStore`` - that's the real seam that
    lets tests spin up a handler bound to a fresh in-memory store per
    test, instead of one shared global."""

    server: "DatalakeServer"
    # Keep-alive: a collector that sends a sample at a time reuses its connection instead of opening
    # one per sample. Every response carries a Content-Length; a request whose body was not read is
    # answered and the connection closed (see _finish_post).
    protocol_version = "HTTP/1.1"
    body_consumed = False

    def _authorize(self, role: str) -> bool:
        """True when this request may use ``role``; otherwise a 401 was already sent. A role with no token configured is open
        (the loopback default); an administrator token also allows everything else."""
        expected = self.server.tokens.get(role)
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        supplied = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        candidates = [expected]
        if role != "admin" and self.server.tokens.get("admin"):
            candidates.append(self.server.tokens["admin"])
        if supplied and any(hmac.compare_digest(supplied.encode(), c.encode()) for c in candidates):
            return True
        body = json.dumps({"error": f"a valid bearer token is required for {role} access"}).encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if not self.body_consumed:
            self.close_connection = True
        return False

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

    def _guarded(self, action) -> None:
        """Runs a route and turns what a hostile parameter can make the driver raise into an answer, not a dead thread."""
        try:
            action()
        except OverflowError as e:
            _write_json(self, 400, {"error": f"a number is out of range: {e}"})
        except (sqlite3.ProgrammingError, sqlite3.InterfaceError) as e:
            _write_json(self, 400, {"error": f"invalid parameter: {e}"})
        except sqlite3.Error as e:
            _write_json(self, 500, {"error": f"database error: {e}"})

    def do_POST(self) -> None:  # noqa: N802 (stdlib's own naming convention)
        path = urlparse(self.path).path
        self.body_consumed = False
        if path == "/ingest":
            if self._authorize("ingest"):
                self._guarded(self._handle_ingest)
        elif path == "/ingest/batch":
            if self._authorize("ingest"):
                self._guarded(self._handle_ingest_batch)
        elif path == "/retention":
            if self._authorize("admin"):
                self._guarded(self._handle_set_retention)
        elif path == "/retention/apply":
            if self._authorize("admin"):
                self._guarded(self._handle_apply_retention)
        else:
            _write_json(self, 404, {"error": "not found"})
        # a request body that was never read would be taken for the next request on a kept-alive connection
        if not self.body_consumed and int(self.headers.get("Content-Length", "0") or 0) > 0:
            self.close_connection = True

    def _handle_ingest(self) -> None:
        try:
            body = _read_json_body(self)
            sample = _sample_from_body(body, self.server)
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except LengthRequired as e:
            _write_json(self, 411, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, OverflowError, OSError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid sample: {e}"})
            return

        # real disk-pressure gate, checked BEFORE ever attempting the
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

    def _handle_ingest_batch(self) -> None:
        """Many samples in one request and ONE transaction: all stored or none."""
        try:
            body = _read_json_body(self)
            items = body.get("samples")
            if not isinstance(items, list):
                raise TypeError("samples must be an array of sample objects")
            if not items:
                raise ValueError("samples must not be empty")
            limit = self.server.max_batch_samples
            if len(items) > limit:
                raise ValueError(f"a batch holds at most {limit} samples, got {len(items)}")
            samples = []
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise TypeError(f"samples[{index}] must be an object")
                try:
                    samples.append(_sample_from_body(item, self.server))
                except (KeyError, ValueError, TypeError, OverflowError) as e:
                    raise type(e)(f"samples[{index}]: {e}") from None
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except LengthRequired as e:
            _write_json(self, 411, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, OverflowError, OSError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid batch: {e}"})
            return
        free_bytes = self.server.store.free_disk_bytes()
        if free_bytes is not None and free_bytes < self.server.min_free_disk_bytes:
            _write_json(self, 507, {
                "error": f"insufficient disk space: {free_bytes} byte(s) free, "
                         f"{self.server.min_free_disk_bytes} required - refusing to ingest",
            })
            return
        try:
            written = self.server.store.insert_many(samples)
        except sqlite3.OperationalError as e:
            _write_json(self, 507, {"error": f"disk write failed, likely out of real disk space: {e}"})
            return
        _write_json(self, 202, {"accepted": len(samples), "written": written})

    def _handle_set_retention(self) -> None:
        try:
            body = _read_json_body(self)
            kind, field = body["kind"], body["field"]
            if not isinstance(kind, str) or not isinstance(field, str):
                raise TypeError("kind and field must both be strings")
            self.server.store.set_retention_policy(
                kind=kind,
                field=field,
                retention_ms=_int64(_validate_real_int(body["retentionMs"], name="retentionMs"), "retentionMs"),
            )
        except RequestBodyTooLarge as e:
            _write_json(self, 413, {"error": str(e)})
            return
        except LengthRequired as e:
            _write_json(self, 411, {"error": str(e)})
            return
        except socket.timeout:
            _write_json(self, 408, {"error": "request body timed out"})
            return
        except (KeyError, ValueError, TypeError, OverflowError, json.JSONDecodeError) as e:
            _write_json(self, 400, {"error": f"invalid retention policy: {e}"})
            return
        _write_json(self, 200, {"ok": True})

    def _handle_apply_retention(self) -> None:
        deleted = self.server.store.apply_retention()
        _write_json(self, 200, {"deleted": deleted})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        params = _query_params(self)
        if path == "/health":
            # Open and cheap: whether the process answers, for a container healthcheck or a monitor.
            _write_json(self, 200, {"status": "ok"})
            return
        if path not in ("/query", "/aggregate", "/stats", "/stats/range", "/retention", "/series"):
            _write_json(self, 404, {"error": "not found"})
            return
        if not self._authorize("read"):
            return
        self._guarded(lambda: self._route_get(path, params))

    def _route_get(self, path: str, params: dict[str, str]) -> None:
        if path == "/query":
            self._handle_query(params)
        elif path == "/aggregate":
            self._handle_aggregate(params)
        elif path == "/stats":
            store = self.server.store
            payload: dict[str, object] = {"sampleCount": store.sample_count()}
            db_bytes = store.db_size_bytes()
            if db_bytes is not None:
                payload["dbBytes"] = db_bytes
                payload["freeDiskBytes"] = store.free_disk_bytes()
            _write_json(self, 200, payload)
        elif path == "/stats/range":
            self._handle_stats_range()
        elif path == "/series":
            self._handle_series(params)
        elif path == "/retention":
            _write_json(
                self,
                200,
                [
                    {"kind": kind, "field": field, "retentionMs": retention_ms}
                    for kind, field, retention_ms in self.server.store.list_retention_policies()
                ],
            )

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

    def _handle_series(self, params: dict[str, str]) -> None:
        try:
            catalogue = self.server.store.series(
                kind=params.get("kind"),
                limit=_int_param(params, "limit") if "limit" in params else MAX_SERIES,
            )
        except ValueError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        _write_json(self, 200, catalogue)

    def _handle_query(self, params: dict[str, str]) -> None:
        try:
            points, next_cursor = self.server.store.query_page(
                source_id=params.get("sourceId"),
                kind=params.get("kind"),
                field=params.get("field"),
                start=_int_param(params, "start") if "start" in params else None,
                end=_exclusive_end(params),
                limit=_int_param(params, "limit") if "limit" in params else 1000,
                after=_decode_cursor(params["cursor"]) if "cursor" in params else None,
            )
        except ValueError as e:
            _write_json(self, 400, {"error": str(e)})
            return
        # The body stays a plain array; a next page is announced in a header, so no client breaks.
        extra = {"X-Next-Cursor": _encode_cursor(next_cursor)} if next_cursor is not None else None
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
            extra,
        )

    def _handle_aggregate(self, params: dict[str, str]) -> None:
        required = {"kind", "field", "bucketMs", "start"}
        missing = required - params.keys()
        if "end" not in params and "before" not in params:
            missing = missing | {"end"}
        if missing:
            _write_json(self, 400, {"error": f"missing required params: {sorted(missing)}"})
            return
        try:
            buckets = self.server.store.aggregate(
                kind=params["kind"],
                field=params["field"],
                bucket_ms=_int_param(params, "bucketMs"),
                start=_int_param(params, "start"),
                end=_exclusive_end(params),
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
        tokens: dict[str, str] | None = None,
        min_timestamp_ms: int = MIN_PLAUSIBLE_TIMESTAMP_MS,
        max_future_skew_ms: int = DEFAULT_MAX_FUTURE_SKEW_MS,
        max_batch_samples: int = DEFAULT_MAX_BATCH_SAMPLES,
    ) -> None:
        if max_batch_samples <= 0:
            raise ValueError("max_batch_samples must be positive")
        if max_request_body_bytes <= 0:
            raise ValueError("max_request_body_bytes must be positive")
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if min_free_disk_bytes <= 0:
            raise ValueError("min_free_disk_bytes must be positive")
        tokens = {role: token for role, token in (tokens or {}).items() if token}
        unknown = set(tokens) - set(ROLES)
        if unknown:
            raise ValueError(f"unknown token role(s): {sorted(unknown)} (want {list(ROLES)})")
        super().__init__(address, Handler)
        self.store = store
        # role -> bearer token; a role without one is open. An "admin" token also allows the other roles.
        self.tokens = tokens
        self.min_timestamp_ms = min_timestamp_ms
        self.max_future_skew_ms = max_future_skew_ms
        self.max_batch_samples = max_batch_samples
        self.min_free_disk_bytes = min_free_disk_bytes
        self.max_request_body_bytes = max_request_body_bytes
        self.request_timeout_seconds = request_timeout_seconds
