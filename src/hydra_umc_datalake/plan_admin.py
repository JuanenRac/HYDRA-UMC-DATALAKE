# =============================================================================
# HYDRA-UMC-DATALAKE - src/hydra_umc_datalake/plan_admin.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""Management of the tier plan over HTTP (``GET`` and ``PUT /downsample/plan``), off unless ``--downsample-admin`` is given.

The plan file of ``--downsample-plan`` stays the single source. A change is validated strictly (the shape, the order of the
widths, the retentions, the kinds that exist), written atomically (a temporary file in the same folder, then a rename; the
previous file is kept as ``<plan>.bak``), audited (``<plan>.audit.jsonl``: when, from where, what changed, never a token) and
answered with a plan of effects: what the new plan would roll up and expire, counted without touching anything.

Deleting is a separate decision. The plan can carry ``"delete": true`` but it only takes effect when the operator started the
service with ``--downsample-allow-delete``; a request cannot switch that on, and a refused attempt is audited. Everything the
service does with a plan is a dry run until both are in place (or ``--downsample-delete`` is given, which is the older switch).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

from .plan import MAX_PLAN_BYTES, PlanError, Tier, apply_plan, expand, parse_plan
from .store import DEFAULT_MAX_FINE_PER_COARSE, TimeSeriesStore, now_ms

logger = logging.getLogger("hydra_umc_datalake.downsample")

AUDIT_MAX_BYTES = 1024 * 1024
AUDIT_TAIL = 10
_TIER_KEYS = ("kind", "field", "raw", "bucket", "fineBucket", "rollupRetention", "fineRetention")


class PlanAdminError(Exception):
    """A refusal with an HTTP status, a stable ``code`` for the screen and the problems found."""

    def __init__(self, status: int, code: str, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []

    def payload(self) -> dict[str, object]:
        body: dict[str, object] = {"error": self.message, "code": self.code}
        if self.details:
            body["details"] = self.details
        return body


def _normalise(document: dict) -> dict:
    """The plan as it is stored: known keys only, the delete flag always present."""
    tiers = [{key: item[key] for key in _TIER_KEYS if key in item} for item in document["tiers"]]
    return {"tiers": tiers, "delete": document.get("delete") is True}


def plan_hash(document: dict) -> str:
    return hashlib.sha256(json.dumps(_normalise(document), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def tier_json(tier: Tier) -> dict[str, object]:
    return {
        "kind": tier.kind,
        "field": tier.field,
        "rawMs": tier.raw_ms,
        "bucketMs": tier.bucket_ms,
        "fineBucketMs": tier.fine_bucket_ms,
        "rollupRetentionMs": tier.rollup_retention_ms,
        "fineRetentionMs": tier.fine_retention_ms,
    }


def coherence_problems(tiers: list[Tier], known_kinds: set[str]) -> tuple[list[str], list[str]]:
    """What is wrong with a plan that is well formed: (incoherent widths or retentions, unknown kinds)."""
    incoherent: list[str] = []
    unknown: list[str] = []
    for index, tier in enumerate(tiers):
        where = f"tiers[{index}] ({tier.kind}/{tier.field})"
        if tier.kind != "*" and tier.kind not in known_kinds:
            unknown.append(f"{where}: the kind {tier.kind!r} has no data and no policy in this datalake")
        if tier.bucket_ms > tier.raw_ms:
            incoherent.append(f"{where}: bucket must not be longer than raw")
        if tier.rollup_retention_ms is not None and tier.rollup_retention_ms < tier.raw_ms:
            incoherent.append(f"{where}: rollupRetention must not be shorter than raw")
        if tier.fine_retention_ms is not None and tier.fine_retention_ms < tier.raw_ms:
            incoherent.append(f"{where}: fineRetention must not be shorter than raw")
    return incoherent, unknown


class PlanManager:
    """The live plan: what the timer applies, what ``GET`` shows and what ``PUT`` replaces."""

    def __init__(
        self,
        store: TimeSeriesStore,
        path: str | Path,
        *,
        allow_delete: bool = False,
        cli_delete: bool = False,
        fine_rollups: bool = False,
        max_buckets: int,
        max_fine_per_coarse: int = DEFAULT_MAX_FINE_PER_COARSE,
        auto_interval_seconds: float = 0,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.store = store
        self.path = Path(path)
        self.allow_delete = allow_delete
        self.cli_delete = cli_delete
        self.fine_rollups = fine_rollups
        self.max_buckets = max_buckets
        self.max_fine_per_coarse = max_fine_per_coarse
        self.auto_interval_seconds = auto_interval_seconds
        self.clock = clock
        self._lock = threading.Lock()
        document = self._read_file()
        self._tiers = parse_plan(document, max_fine_per_coarse=max_fine_per_coarse)
        self._document = _normalise(document)

    # -- state -----------------------------------------------------------------
    @property
    def audit_path(self) -> Path:
        return self.path.with_name(self.path.name + ".audit.jsonl")

    def snapshot(self) -> list[Tier]:
        with self._lock:
            return list(self._tiers)

    @property
    def delete_requested(self) -> bool:
        with self._lock:
            return self._document["delete"] is True

    def deleting(self) -> bool:
        """True when the timer may really delete: the older operator switch, or the plan asks and the operator allows it."""
        with self._lock:
            return self.cli_delete or (self.allow_delete and self._document["delete"] is True)

    def _read_file(self) -> dict:
        try:
            if self.path.stat().st_size > MAX_PLAN_BYTES:
                raise PlanError(f"the plan file is larger than {MAX_PLAN_BYTES} bytes")
            return json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as e:
            raise PlanError(f"cannot read the plan file: {e.strerror or e}") from None
        except json.JSONDecodeError as e:
            raise PlanError(f"the plan file is not valid JSON: {e}") from None

    # -- reading -----------------------------------------------------------------
    def get(self) -> dict[str, object]:
        with self._lock:
            document, tiers = self._document, list(self._tiers)
        return {
            "plan": document,
            "hash": plan_hash(document),
            "tiers": [tier_json(t) for t in tiers],
            "knownKinds": self.store.known_kinds(),
            "switches": {
                "allowDelete": self.allow_delete,
                "deleteRequestedByPlan": document["delete"],
                "deleteOldSwitch": self.cli_delete,
                "effectiveMode": "delete" if self.deleting() else "dryRun",
                "fineRollups": self.fine_rollups,
                "autoIntervalSeconds": self.auto_interval_seconds,
            },
            "audit": self.audit_tail(),
        }

    def audit_tail(self) -> list[object]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-AUDIT_TAIL:]
        except OSError:
            return []
        out: list[object] = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    # -- effects -----------------------------------------------------------------
    def effects(self, tiers: list[Tier], at_ms: int) -> dict[str, object]:
        """What a run of ``tiers`` at ``at_ms`` would touch. Only counts; nothing is written."""
        expanded = expand(self.store, tiers)
        named = {(t.kind, t.field) for t in expanded if not (t.kind == "*" and t.field == "*")}
        per_tier: list[dict[str, object]] = []
        total_raw = total_rollups = total_fine = 0
        for tier in expanded:
            default = tier.kind == "*" and tier.field == "*"
            cutoff = at_ms - tier.raw_ms
            cutoff -= cutoff % tier.bucket_ms
            if default:
                raw = sum(n for pair, n in self.store.count_raw_older(cutoff).items() if pair not in named)
            else:
                raw = self.store.count_raw_older(cutoff, kind=tier.kind, field=tier.field).get((tier.kind, tier.field), 0)
            rollups = 0
            if tier.rollup_retention_ms is not None:
                expiry = at_ms - tier.rollup_retention_ms
                if default:
                    rollups = sum(n for pair, n in self.store.count_rollups_ending_before(expiry).items() if pair not in named)
                else:
                    rollups = self.store.count_rollups_ending_before(expiry, kind=tier.kind, field=tier.field).get((tier.kind, tier.field), 0)
            fine = 0
            if self.fine_rollups and tier.fine_retention_ms is not None and tier.fine_bucket_ms is not None and not default:
                fine = self.store.count_rollups_ending_before(
                    at_ms - tier.fine_retention_ms, kind=tier.kind, field=tier.field, bucket_ms=tier.fine_bucket_ms
                ).get((tier.kind, tier.field), 0)
            total_raw += raw
            total_rollups += rollups
            total_fine += fine
            per_tier.append(
                {"kind": tier.kind, "field": tier.field, "rawPointsToRollUp": raw, "rollupsToExpire": rollups, "fineRollupsToExpire": fine}
            )
        return {
            "atMs": at_ms,
            "series": per_tier,
            "totals": {"rawPointsToRollUp": total_raw, "rollupsToExpire": total_rollups, "fineRollupsToExpire": total_fine},
        }

    @staticmethod
    def _changes(old: list[Tier], new: list[Tier]) -> list[dict[str, object]]:
        before = {(t.kind, t.field): t for t in old}
        after = {(t.kind, t.field): t for t in new}
        out: list[dict[str, object]] = []
        for key in sorted(set(before) | set(after)):
            if key not in before:
                out.append({"kind": key[0], "field": key[1], "change": "added"})
            elif key not in after:
                out.append({"kind": key[0], "field": key[1], "change": "removed"})
            elif before[key] != after[key]:
                a, b = tier_json(before[key]), tier_json(after[key])
                out.append({"kind": key[0], "field": key[1], "change": "changed", "fields": sorted(k for k in a if a[k] != b[k])})
        return out

    def _warnings(self, old: list[Tier], new: list[Tier]) -> list[str]:
        warnings: list[str] = []
        before = {(t.kind, t.field): t for t in old}
        for tier in new:
            name = f"{tier.kind}/{tier.field}"
            if tier.fine_bucket_ms is not None and not self.fine_rollups:
                warnings.append(f"{name}: fineBucket is stored but ignored while the fine rollups are off")
            previous = before.get((tier.kind, tier.field))
            if previous is not None and tier.raw_ms < previous.raw_ms:
                warnings.append(f"{name}: raw points will be rolled up sooner than before")
            if previous is not None and previous.rollup_retention_ms is not None and (
                tier.rollup_retention_ms is not None and tier.rollup_retention_ms < previous.rollup_retention_ms
            ):
                warnings.append(f"{name}: rollups will be kept for a shorter time than before")
        return warnings

    # -- writing -----------------------------------------------------------------
    def put(
        self,
        body: dict,
        *,
        client: str = "",
        preview_query: bool = False,
    ) -> dict[str, object]:
        """Validates ``body`` (``{"plan": {...}, "confirmDelete": bool, "expectedHash": str, "preview": bool}``), and unless it
        is a preview saves it. Raises PlanAdminError. The plan is never executed here."""
        extra = set(body) - {"plan", "confirmDelete", "expectedHash", "preview"}
        if extra or not isinstance(body.get("plan"), dict):
            raise PlanAdminError(400, "invalid_request", 'send {"plan": {...}} with, optionally, "confirmDelete", "expectedHash" and "preview"')
        for key in ("confirmDelete", "preview"):
            if key in body and not isinstance(body[key], bool):
                raise PlanAdminError(400, "invalid_request", f"{key} must be true or false")
        if "expectedHash" in body and not isinstance(body["expectedHash"], str):
            raise PlanAdminError(400, "invalid_request", "expectedHash must be text")
        preview = preview_query or body.get("preview") is True
        document = body["plan"]
        try:
            new_tiers = parse_plan(document, max_fine_per_coarse=self.max_fine_per_coarse)
        except PlanError as e:
            raise PlanAdminError(400, "invalid_plan", str(e)) from None
        incoherent, unknown = coherence_problems(new_tiers, set(self.store.known_kinds()))
        if incoherent:
            raise PlanAdminError(422, "incoherent_plan", "the widths or retentions of the plan do not fit together", incoherent)
        if unknown:
            raise PlanAdminError(422, "unknown_kind", "the plan names a kind this datalake does not know", unknown)
        stored = _normalise(document)
        new_hash = plan_hash(stored)
        with self._lock:
            old_document, old_tiers = self._document, list(self._tiers)
            old_hash = plan_hash(old_document)
            if body.get("expectedHash") not in (None, old_hash):
                raise PlanAdminError(409, "plan_changed", "the plan changed since it was read; read it again", [f"current hash {old_hash}"])
            turns_delete_on = stored["delete"] is True and old_document["delete"] is not True
            if stored["delete"] is True and not self.allow_delete:
                self._audit("delete_refused", client, old_hash, new_hash, len(new_tiers), True, [])
                raise PlanAdminError(
                    403,
                    "delete_not_allowed",
                    'the plan asks to delete but the service was not started with --downsample-allow-delete (a request cannot switch that on)',
                )
            if turns_delete_on and not preview and body.get("confirmDelete") is not True:
                raise PlanAdminError(409, "confirm_delete_required", 'send "confirmDelete": true after reading the effects to switch real deletion on')
            now = self.clock()
            changes = self._changes(old_tiers, new_tiers)
            result: dict[str, object] = {
                "saved": False,
                "preview": preview,
                "hash": new_hash,
                "previousHash": old_hash,
                "tiers": [tier_json(t) for t in new_tiers],
                "mode": "delete" if (stored["delete"] and self.allow_delete) or self.cli_delete else "dryRun",
                "changes": changes,
                "warnings": self._warnings(old_tiers, new_tiers),
                "effects": self.effects(new_tiers, now),
                "currentEffects": self.effects(old_tiers, now),
            }
            if preview:
                self._audit("plan_preview", client, old_hash, new_hash, len(new_tiers), stored["delete"], changes)
                return result
            if new_hash == old_hash:
                result["saved"] = False
                result["unchanged"] = True
                return result
            self._write_atomic(stored)
            self._tiers, self._document = new_tiers, stored
            self._audit("plan_saved", client, old_hash, new_hash, len(new_tiers), stored["delete"], changes)
        self._write_policies(old_tiers, new_tiers)
        result["saved"] = True
        return result

    def _write_atomic(self, stored: dict) -> None:
        data = (json.dumps(stored, indent=2) + "\n").encode("utf-8")
        if len(data) > MAX_PLAN_BYTES:
            raise PlanAdminError(413, "plan_too_large", f"the plan is larger than {MAX_PLAN_BYTES} bytes")
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.path.with_name(self.path.name + ".bak"))
                except OSError:
                    pass  # no backup is better than no change; the audit entry has both hashes
            os.replace(temporary, self.path)
        except OSError as e:
            raise PlanAdminError(500, "write_failed", f"cannot write the plan file: {e.strerror or e}") from None
        finally:
            try:
                temporary.unlink()
            except OSError:
                pass

    def _write_policies(self, old: list[Tier], new: list[Tier]) -> None:
        """The policies of the new plan are written (as at start); the ones of a tier that is gone are removed. Nothing is deleted."""
        try:
            keep = {(t.kind, t.field) for t in expand(self.store, new)}
            for tier in expand(self.store, old):
                if (tier.kind, tier.field) not in keep:
                    self.store.remove_downsample_policy(kind=tier.kind, field=tier.field)
            apply_plan(
                self.store, new, dry_run=True, fine_rollups=self.fine_rollups, max_buckets=self.max_buckets, max_fine_per_coarse=self.max_fine_per_coarse, clock=self.clock
            )
        except Exception:  # noqa: BLE001 - the file is saved; the next round writes the policies again
            logger.exception("downsample: the policies of the new plan could not be written")

    def _audit(self, event: str, client: str, old_hash: str, new_hash: str, tiers: int, delete: object, changes: list[dict[str, object]]) -> None:
        entry = {
            "atMs": self.clock(),
            "event": event,
            "client": client,
            "previousHash": old_hash,
            "hash": new_hash,
            "tiers": tiers,
            "deleteRequested": delete is True,
            "allowDelete": self.allow_delete,
            "changes": changes,
        }
        logger.info("downsample plan: %s from %s (%d tier(s), delete requested: %s)", event, client or "unknown", tiers, delete is True)
        try:
            if self.audit_path.exists() and self.audit_path.stat().st_size > AUDIT_MAX_BYTES:
                self.audit_path.replace(self.audit_path.with_name(self.audit_path.name + ".1"))
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError:
            logger.exception("downsample plan: the audit entry could not be written")
