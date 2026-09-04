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
production, not something to bolt on unasked - see mejoras_futuras.txt.
sqlite3 is a genuinely real, ACID, queryable time-series store today,
not a placeholder pretending to be one.
"""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .api import DatalakeServer
from .store import TimeSeriesStore

PROJECT_NAME = "HYDRA-UMC-DATALAKE"
ROLE = (
    "Datalake - scalable time-series storage and analysis, long-term "
    "memory of the factory, parent integrator of TELEMETRY-COLLECTOR, "
    "ANOMALY-DETECTOR and PRODUCTION-REPORTS."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hydra-umc-datalake")
    # Real gap found by an ecosystem-wide audit: this used to default to
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
    args = parser.parse_args(argv)

    print(f"{PROJECT_NAME} v{__version__}")
    print(ROLE)

    store = TimeSeriesStore(args.db)
    server = DatalakeServer((args.addr, args.port), store)
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
