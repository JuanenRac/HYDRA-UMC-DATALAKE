# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/main.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
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
from collections.abc import Mapping
import ipaddress
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

from . import __version__
from .api import DEFAULT_MAX_BATCH_SAMPLES, DEFAULT_MAX_REQUEST_BODY_BYTES, DEFAULT_REQUEST_TIMEOUT_SECONDS, DatalakeServer
from .export import DEFAULT_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS, ParquetUnavailable, export_closed_days, export_day, parquet_available
from .health_route import health_open_requested
from .plan import MIN_INTERVAL_SECONDS, PeriodicRunner, PlanError, apply_plan, load_plan
from .plan_admin import PlanManager
from .store import DEFAULT_MAX_DOWNSAMPLE_BUCKETS, DEFAULT_MAX_FINE_PER_COARSE, DEFAULT_MAX_WINDOW_POINTS, TimeSeriesStore

PROJECT_NAME = "HYDRA-UMC-DATALAKE"
MIN_TOKEN_LENGTH = 16
TOKEN_ENV = "HYDRA_UMC_DATALAKE_TOKEN"
# The second rollup level can also be set in the environment file of the service (it is read like the tokens);
# a command-line option wins over the file. A wrong value stops the start instead of being ignored.
FINE_ROLLUPS_ENV = "HYDRA_UMC_DATALAKE_FINE_ROLLUPS"
MAX_FINE_PER_COARSE_ENV = "HYDRA_UMC_DATALAKE_MAX_FINE_PER_COARSE"
ADMIN_ENV = "HYDRA_UMC_DATALAKE_DOWNSAMPLE_ADMIN"
ALLOW_DELETE_ENV = "HYDRA_UMC_DATALAKE_DOWNSAMPLE_ALLOW_DELETE"


def fine_rollup_settings(
    flag: bool, max_flag: int | None, env: Mapping[str, str] | None = None
) -> tuple[bool, int]:
    """(enabled, most fine buckets per coarse bucket) from the command line and the environment file; ValueError on a bad value."""
    source: Mapping[str, str] = os.environ if env is None else env
    enabled = flag
    raw = source.get(FINE_ROLLUPS_ENV)
    if raw is not None and not flag:
        value = raw.strip().lower()
        if value not in ("true", "false"):
            raise ValueError(f"${FINE_ROLLUPS_ENV} must be true or false")
        enabled = value == "true"
    limit = max_flag
    if limit is None:
        raw_limit = source.get(MAX_FINE_PER_COARSE_ENV)
        if raw_limit is None:
            limit = DEFAULT_MAX_FINE_PER_COARSE
        else:
            try:
                limit = int(raw_limit.strip())
            except ValueError:
                raise ValueError(f"${MAX_FINE_PER_COARSE_ENV} must be a whole number") from None
    if limit <= 0:
        raise ValueError("the most fine buckets per coarse bucket must be positive")
    return enabled, limit


def switch_setting(flag: bool, name: str, env: Mapping[str, str] | None = None) -> bool:
    """A switch from the command line or the environment file (``true`` or ``false``); a wrong value is an error, never ignored."""
    source: Mapping[str, str] = os.environ if env is None else env
    raw = source.get(name)
    if flag or raw is None:
        return flag
    value = raw.strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"${name} must be true or false")
    return value == "true"


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
    source: Mapping[str, str] = os.environ if env is None else env
    base = shared if shared else source.get(TOKEN_ENV, "")
    tokens = {role: source.get(f"HYDRA_UMC_DATALAKE_{role.upper()}_TOKEN", "") or base for role in ("ingest", "admin", "read")}
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


def backup_main(argv: list[str]) -> int:
    """``hydra-umc-datalake backup``: a consistent copy of the database, safe while the service runs."""
    parser = argparse.ArgumentParser(prog="hydra-umc-datalake backup")
    parser.add_argument("--db", default="datalake.sqlite3", help="path to the sqlite3 database file")
    parser.add_argument("--dest", required=True, help="directory the copy is written to")
    parser.add_argument("--keep", type=int, default=7, help="how many copies to keep in --dest (0 keeps all)")
    args = parser.parse_args(argv)
    if args.keep < 0:
        print("[datalake] --keep must not be negative", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"[datalake] the database {args.db} does not exist", file=sys.stderr)
        return 2
    store = TimeSeriesStore(args.db)
    try:
        target = store.backup(args.dest, keep=args.keep)
    finally:
        store.close()
    print(f"[datalake] backup written to {target}")
    return 0


def export_main(argv: list[str]) -> int:
    """``hydra-umc-datalake export-parquet``: writes the Parquet file of one UTC day (default: yesterday). Needs pyarrow."""
    parser = argparse.ArgumentParser(prog="hydra-umc-datalake export-parquet")
    parser.add_argument("--db", default="datalake.sqlite3", help="path to the sqlite3 database file")
    parser.add_argument("--dest", required=True, help="directory the file is written to")
    parser.add_argument("--day", default=None, help="the UTC day to export, YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--overwrite", action="store_true", help="replace the file of that day if it exists (default: leave it)")
    args = parser.parse_args(argv)
    try:
        day = date.fromisoformat(args.day) if args.day else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    except ValueError:
        print("[datalake] --day must look like 2025-03-31", file=sys.stderr)
        return 2
    if not os.path.exists(args.db):
        print(f"[datalake] the database {args.db} does not exist", file=sys.stderr)
        return 2
    store = TimeSeriesStore(args.db)
    try:
        target = export_day(store, day, args.dest, overwrite=args.overwrite)
    except ParquetUnavailable as e:
        print(f"[datalake] {e}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(f"[datalake] parquet file written to {target}" if target else f"[datalake] nothing written for {day.isoformat()} (no points, or the file exists)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args_in = sys.argv[1:] if argv is None else argv
    if args_in and args_in[0] == "backup":
        return backup_main(args_in[1:])
    if args_in and args_in[0] == "export-parquet":
        return export_main(args_in[1:])
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
        "--health-open",
        action="store_true",
        help="answer GET /health (open to loopback, to other callers only with the read or administrator token); closed by default, or set HYDRA_UMC_DATALAKE_HEALTH_OPEN=1",
    )
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
    parser.add_argument(
        "--max-batch-samples",
        type=int,
        default=DEFAULT_MAX_BATCH_SAMPLES,
        help="most samples one POST /ingest/batch may carry",
    )
    parser.add_argument(
        "--max-request-body-bytes",
        type=int,
        default=DEFAULT_MAX_REQUEST_BODY_BYTES,
        help="largest request body, in bytes, that any route reads (a larger one is refused with 413)",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="how long one connection may stay silent while a request is read before it is dropped",
    )
    parser.add_argument(
        "--enable-window-stats",
        action="store_true",
        help="turn on GET /windows: count, min, max, avg, sum and percentiles per time window (off: the route does not exist)",
    )
    parser.add_argument(
        "--max-window-points",
        type=int,
        default=DEFAULT_MAX_WINDOW_POINTS,
        help="most points GET /windows may load to compute percentiles (a larger range is refused with 400)",
    )
    parser.add_argument(
        "--enable-downsampling",
        action="store_true",
        help="turn on the downsampling routes (GET /rollups, GET/POST /downsample, POST /downsample/apply); off: they do not exist",
    )
    parser.add_argument(
        "--downsample-delete",
        action="store_true",
        help="make POST /downsample/apply delete the rolled-up raw points when the request does not say dryRun (default: dry run)",
    )
    parser.add_argument(
        "--max-downsample-buckets",
        type=int,
        default=DEFAULT_MAX_DOWNSAMPLE_BUCKETS,
        help="most buckets one downsampling run rolls up (the next run continues)",
    )
    parser.add_argument(
        "--enable-fine-rollups",
        action="store_true",
        help=(
            "let a downsampling policy carry fineBucketMs: a second, narrower rollup level written beside the coarse one "
            f"(off by default; needs --enable-downsampling; also ${FINE_ROLLUPS_ENV}=true)"
        ),
    )
    parser.add_argument(
        "--max-fine-per-coarse",
        type=int,
        default=None,
        help=f"most fine buckets one coarse bucket may hold (default {DEFAULT_MAX_FINE_PER_COARSE}; also ${MAX_FINE_PER_COARSE_ENV})",
    )
    parser.add_argument(
        "--downsample-plan",
        default="",
        help="JSON file with the tiers (raw time, wide and narrow rollup widths, how long each lives) per kind; applied at start and by the timer. Needs --enable-downsampling",
    )
    parser.add_argument(
        "--downsample-admin",
        action="store_true",
        help=(
            "turn on GET and PUT /downsample/plan (admin role): read and replace the tier plan file, with the effects of a change; "
            f"off: the routes do not exist. Needs --downsample-plan (also ${ADMIN_ENV}=true)"
        ),
    )
    parser.add_argument(
        "--downsample-allow-delete",
        action="store_true",
        help=(
            'let a plan with "delete": true really delete the rolled-up raw points on the timer. No request can switch this on; '
            f"off by default, so a plan only ever runs as a dry run. Needs --downsample-plan (also ${ALLOW_DELETE_ENV}=true)"
        ),
    )
    parser.add_argument(
        "--downsample-auto-interval-seconds",
        type=float,
        default=0,
        help=f"run the downsampling by itself every this many seconds (0: never; at least {int(MIN_INTERVAL_SECONDS)}). A dry run unless --downsample-delete. Needs --enable-downsampling",
    )
    parser.add_argument(
        "--enable-production-events",
        action="store_true",
        help="turn on POST /production-events: finished cycles stored as production_event samples (off: the route does not exist)",
    )
    parser.add_argument(
        "--enable-parquet-export",
        action="store_true",
        help="write one Parquet file per closed UTC day into --parquet-export-dir (off by default; needs the optional pyarrow)",
    )
    parser.add_argument("--parquet-export-dir", default="", help="directory of the daily Parquet files (needed by --enable-parquet-export)")
    parser.add_argument(
        "--parquet-export-interval-seconds",
        type=float,
        default=3600,
        help=f"how often the export looks for closed days without a file (at least {int(MIN_INTERVAL_SECONDS)}; default 3600)",
    )
    parser.add_argument(
        "--parquet-export-lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"how many closed days back the export looks (1 to {MAX_LOOKBACK_DAYS}; default {DEFAULT_LOOKBACK_DAYS})",
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
    if args.max_batch_samples <= 0:
        print("[datalake] --max-batch-samples must be positive", file=sys.stderr)
        return 2
    if args.max_request_body_bytes <= 0 or args.request_timeout_seconds <= 0:
        print("[datalake] --max-request-body-bytes and --request-timeout-seconds must be positive", file=sys.stderr)
        return 2

    if args.max_window_points <= 0 or args.max_downsample_buckets <= 0:
        print("[datalake] --max-window-points and --max-downsample-buckets must be positive", file=sys.stderr)
        return 2
    if args.downsample_delete and not args.enable_downsampling:
        print("[datalake] --downsample-delete needs --enable-downsampling", file=sys.stderr)
        return 2
    try:
        fine_rollups, max_fine_per_coarse = fine_rollup_settings(args.enable_fine_rollups, args.max_fine_per_coarse)
    except ValueError as e:
        print(f"[datalake] {e}", file=sys.stderr)
        return 2
    if fine_rollups and not args.enable_downsampling:
        print("[datalake] the fine rollups need --enable-downsampling", file=sys.stderr)
        return 2
    if (args.downsample_plan or args.downsample_auto_interval_seconds) and not args.enable_downsampling:
        print("[datalake] --downsample-plan and --downsample-auto-interval-seconds need --enable-downsampling", file=sys.stderr)
        return 2
    if args.downsample_auto_interval_seconds and args.downsample_auto_interval_seconds < MIN_INTERVAL_SECONDS:
        print(f"[datalake] --downsample-auto-interval-seconds must be 0 or at least {int(MIN_INTERVAL_SECONDS)}", file=sys.stderr)
        return 2
    try:
        plan_admin_on = switch_setting(args.downsample_admin, ADMIN_ENV)
        allow_delete = switch_setting(args.downsample_allow_delete, ALLOW_DELETE_ENV)
    except ValueError as e:
        print(f"[datalake] {e}", file=sys.stderr)
        return 2
    if (plan_admin_on or allow_delete) and not args.downsample_plan:
        print("[datalake] --downsample-admin and --downsample-allow-delete need --downsample-plan", file=sys.stderr)
        return 2
    plan = None
    if args.downsample_plan:
        try:
            plan = load_plan(args.downsample_plan, max_fine_per_coarse=max_fine_per_coarse)
        except PlanError as e:
            print(f"[datalake] --downsample-plan: {e}", file=sys.stderr)
            return 2
    if args.enable_parquet_export:
        if not args.parquet_export_dir:
            print("[datalake] --enable-parquet-export needs --parquet-export-dir", file=sys.stderr)
            return 2
        if args.parquet_export_interval_seconds < MIN_INTERVAL_SECONDS or not 1 <= args.parquet_export_lookback_days <= MAX_LOOKBACK_DAYS:
            print("[datalake] --parquet-export-interval-seconds or --parquet-export-lookback-days is out of range", file=sys.stderr)
            return 2
        if not parquet_available():
            print('[datalake] --enable-parquet-export needs pyarrow: pip install "hydra-umc-datalake[parquet]"', file=sys.stderr)
            return 2
    if args.enable_downsampling or args.enable_parquet_export:
        logging.basicConfig(level=logging.INFO, format="[datalake] %(message)s")

    print(f"{PROJECT_NAME} v{__version__}")
    print(ROLE)

    store = TimeSeriesStore(args.db)
    manager = None
    if args.downsample_plan:
        manager = PlanManager(
            store,
            args.downsample_plan,
            allow_delete=allow_delete,
            cli_delete=args.downsample_delete,
            fine_rollups=fine_rollups,
            max_buckets=args.max_downsample_buckets,
            max_fine_per_coarse=max_fine_per_coarse,
            auto_interval_seconds=args.downsample_auto_interval_seconds,
        )
    if args.default_retention_days > 0:
        store.set_retention_policy(kind="*", field="*", retention_ms=int(args.default_retention_days * 86_400_000))
    server = DatalakeServer(
        (args.addr, args.port),
        store,
        tokens=tokens,
        max_future_skew_ms=int(args.max_future_skew_seconds * 1000),
        max_batch_samples=args.max_batch_samples,
        max_request_body_bytes=args.max_request_body_bytes,
        request_timeout_seconds=args.request_timeout_seconds,
        enable_window_stats=args.enable_window_stats,
        enable_downsampling=args.enable_downsampling,
        max_window_points=args.max_window_points,
        max_downsample_buckets=args.max_downsample_buckets,
        downsample_dry_run_default=not args.downsample_delete,
        enable_fine_rollups=fine_rollups,
        max_fine_per_coarse=max_fine_per_coarse,
        enable_production_events=args.enable_production_events,
        plan_admin=manager if plan_admin_on else None,
    )
    server.health_open = health_open_requested(args.health_open)
    runners: list[PeriodicRunner] = []
    if plan is not None:
        # the policies are written at start; deleting still needs --downsample-delete
        apply_plan(store, plan, dry_run=True, fine_rollups=fine_rollups, max_buckets=args.max_downsample_buckets, max_fine_per_coarse=max_fine_per_coarse)
        if args.parquet_export_dir and any(t.raw_ms < 2 * 86_400_000 for t in plan):
            print("[datalake] warning: a tier keeps raw points for less than two days; the daily Parquet export may miss them")
    if args.downsample_auto_interval_seconds:

        def downsample_round() -> None:
            if manager is not None:
                # the live plan: a change saved over HTTP is used from the next round; deleting needs the operator's switches
                apply_plan(store, manager.snapshot(), dry_run=not manager.deleting(), fine_rollups=fine_rollups, max_buckets=args.max_downsample_buckets, max_fine_per_coarse=max_fine_per_coarse)
            else:
                store.apply_downsample(dry_run=not args.downsample_delete, max_buckets=args.max_downsample_buckets, fine_rollups=fine_rollups)

        runners.append(PeriodicRunner("downsample", args.downsample_auto_interval_seconds, downsample_round))
    if args.enable_parquet_export:
        runners.append(
            PeriodicRunner(
                "parquet-export",
                args.parquet_export_interval_seconds,
                lambda: export_closed_days(store, args.parquet_export_dir, lookback_days=args.parquet_export_lookback_days),
                immediate=True,
            )
        )
    protected = sorted(tokens) or ["nothing"]
    print(f"[datalake] access control: {', '.join(protected)}")
    print(f"[datalake] HTTP API listening on {args.addr}:{args.port} (db={args.db})")
    print("[datalake] POST /ingest, POST /ingest/batch, GET /query, GET /aggregate, GET /series, GET /stats")
    if args.enable_window_stats:
        print("[datalake] GET /windows is on")
    if args.enable_downsampling:
        mode = "deletes" if args.downsample_delete else "dry run unless the request says dryRun=false"
        print(f"[datalake] downsampling is on: GET /rollups, GET/POST /downsample, POST /downsample/apply ({mode})")
        if fine_rollups:
            print(f"[datalake] fine rollups are on (at most {max_fine_per_coarse} per coarse bucket)")
    if args.enable_production_events:
        print("[datalake] POST /production-events is on")
    if plan is not None:
        print(f"[datalake] tier plan loaded: {len(plan)} tier(s) from {args.downsample_plan}")
        if plan_admin_on:
            print("[datalake] GET and PUT /downsample/plan are on (admin role); a change is saved and never executed by the request")
        if manager is not None and manager.delete_requested and not allow_delete:
            print('[datalake] the plan asks to delete but --downsample-allow-delete is not given: it runs as a dry run')
    for runner in runners:
        runner.start()
        deleting = manager.deleting() if manager is not None else args.downsample_delete
        note = " (dry run)" if runner.name == "downsample" and not deleting else ""
        print(f"[datalake] {runner.name} runs every {int(runner.interval)} s{note}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for runner in runners:
            runner.stop()
        server.server_close()
        store.close()
        print("[datalake] shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
