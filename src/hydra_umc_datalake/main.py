# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/main.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0 - see LICENSE
# =============================================================================
"""Entry point for HYDRA-UMC-DATALAKE.

Real time-series store + HTTP API, no longer just an identity print:
store.py holds a genuine sqlite3-backed TimeSeriesStore (real ingest,
range query, time-bucketed aggregation), api.py exposes it over plain
JSON/HTTP (POST /ingest, GET /query, GET /aggregate, GET /stats).

Why sqlite3 and not InfluxDB/TimescaleDB (both named in this project's
own pyproject.toml keywords): those are real services to stand up and
operate, a deployment decision that belongs to whoever runs this in
production, not something to bolt on unasked. sqlite3 is a genuinely
real, ACID, queryable time-series store today, not a placeholder
pretending to be one.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys

from . import __version__
from .api import DatalakeServer
from .store import TimeSeriesStore

PROJECT_NAME = "HYDRA-UMC-DATALAKE"
MIN_TOKEN_LENGTH = 16
TOKEN_ENV = "HYDRA_UMC_DATALAKE_TOKEN"


def is_loopback(addr: str) -> bool:
    """True when ``addr`` listens on the loopback interface only (an empty host or 0.0.0.0 listens on all of them)."""
    host = addr.strip()
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tokens_from_environment(env: dict[str, str] | None = None, shared: str | None = None) -> dict[str, str]:
    """Role -> token. ``HYDRA_UMC_DATALAKE_TOKEN`` (or ``--token``) is one token for every role; the
    ``..._INGEST_TOKEN`` / ``..._ADMIN_TOKEN`` / ``..._READ_TOKEN`` variables give a role its own."""
    env = os.environ if env is None else env
    base = shared if shared else env.get(TOKEN_ENV, "")
    tokens = {role: env.get(f"HYDRA_UMC_DATALAKE_{role.upper()}_TOKEN", "") or base for role in ("ingest", "admin", "read")}
    return {role: token for role, token in tokens.items() if token}


def startup_problem(addr: str, tokens: dict[str, str], allow_anonymous: bool) -> str | None:
    """Why the service must not start with this configuration, or None. It stores the factory's history and
    deletes it on request, so a non-loopback bind needs a token for writing and one for the administration."""
    for role, token in tokens.items():
        if len(token) < MIN_TOKEN_LENGTH:
            return f"the {role} token must be at least {MIN_TOKEN_LENGTH} characters"
    if not is_loopback(addr) and not allow_anonymous:
        missing = [role for role in ("ingest", "admin") if role not in tokens]
        if missing:
            return (
                f"refusing to listen on {addr} without a token for {' and '.join(missing)}: set ${TOKEN_ENV} "
                "(or --token), or pass --allow-anonymous to leave them open to the whole network"
            )
    return None
ROLE = (
    "Datalake - scalable time-series storage and analysis, long-term "
    "memory of the factory, parent integrator of TELEMETRY-COLLECTOR, "
    "ANOMALY-DETECTOR and PRODUCTION-REPORTS."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydra-umc-datalake")
    # Real gap found while auditing the code: this used to default to
    # "0.0.0.0" (every interface) with zero authentication on any
    # endpoint (POST /ingest accepts and persists any telemetry reading
    # from anyone who can reach it) - the real CM5's own systemd unit
    # already overrides this to "127.0.0.1" explicitly, matching every
    # other internal-only API here (Anomaly-Detector, Job-Dispatcher,
    # Telemetry-Collector), so making it the real default too means
    # running this tool bare (no systemd unit, a developer testing it
    # locally) is safe by default instead of silently wide open.
    parser.add_argument("--addr", default="127.0.0.1", help="address to bind the HTTP API to")
    parser.add_argument("--port", type=int, default=8095, help="port for the HTTP API")
    parser.add_argument(
        "--db",
        default="datalake.sqlite3",
        help="path to the sqlite3 database file (':memory:' for a non-durable store)",
    )
    parser.add_argument("--token", default="", help=f"bearer token for every role (default: ${TOKEN_ENV})")
    parser.add_argument("--allow-anonymous", action="store_true", help="allow a non-loopback --addr without tokens")
    parser.add_argument(
        "--default-retention-days",
        type=float,
        default=0,
        help="keep every series that has no retention policy of its own for this many days (0: no default policy)",
    )
    parser.add_argument(
        "--max-future-skew-seconds",
        type=float,
        default=300,
        help="how far ahead of this machine's clock a sample may be (0 turns the check off)",
    )
    args = parser.parse_args(argv)

    tokens = tokens_from_environment(shared=args.token)
    problem = startup_problem(args.addr, tokens, args.allow_anonymous)
    if problem:
        print(f"[datalake] {problem}", file=sys.stderr)
        return 2
    if args.default_retention_days < 0 or args.max_future_skew_seconds < 0:
        print("[datalake] --default-retention-days and --max-future-skew-seconds must not be negative", file=sys.stderr)
        return 2

    print(f"{PROJECT_NAME} v{__version__}")
    print(ROLE)

    store = TimeSeriesStore(args.db)
    if args.default_retention_days > 0:
        store.set_retention_policy(kind="*", field="*", retention_ms=int(args.default_retention_days * 86_400_000))
    server = DatalakeServer(
        (args.addr, args.port), store, tokens=tokens, max_future_skew_ms=int(args.max_future_skew_seconds * 1000)
    )
    protected = sorted(tokens) or ["nothing"]
    print(f"[datalake] access control: {', '.join(protected)}")
    print(f"[datalake] HTTP API listening on {args.addr}:{args.port} (db={args.db})")
    print("[datalake] POST /ingest, GET /query, GET /aggregate, GET /stats")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
        print("[datalake] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
