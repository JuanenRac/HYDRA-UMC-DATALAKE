# =============================================================================
# HYDRA-UMC-DATALAKE - tests/test_plan_admin.py
# Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
# GPL-3.0-or-later - see LICENSE
# =============================================================================
"""GET and PUT /downsample/plan: off by default, strict, atomic, audited, and never a way to switch deletion on."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from hydra_umc_datalake import main as main_module
from hydra_umc_datalake.api import DatalakeServer
from hydra_umc_datalake.plan_admin import PlanManager
from hydra_umc_datalake.store import Sample, TimeSeriesStore

HOUR = 3_600_000
DAY = 86_400_000
NOW = 1_800_000_000_000


def _tier(**over: Any) -> dict[str, Any]:
    return {"kind": "temp", "field": "*", "raw": "2d", "bucket": "1h", "rollupRetention": "30d", **over}


def _call(method: str, url: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class _Rig:
    def __init__(
        self, tmp_path: Path, *, allow_delete: bool = False, admin: bool = True, tokens: dict[str, str] | None = None, plan: dict | None = None
    ) -> None:
        self.store = TimeSeriesStore(":memory:")
        # 100 old points of temp/a (three days before NOW), a fresh one, and an old point of another kind
        self.store.insert_many([Sample(source_id="s", kind="temp", timestamp=NOW - 3 * DAY + i, fields={"a": float(i)}) for i in range(100)])
        self.store.insert(Sample(source_id="s", kind="temp", timestamp=NOW, fields={"a": 1.0}))
        self.store.insert(Sample(source_id="s", kind="other", timestamp=NOW - 40 * DAY, fields={"b": 1.0}))
        self.path = tmp_path / "plan.json"
        self.path.write_text(json.dumps(plan or {"tiers": [_tier()]}), encoding="utf-8")
        self.manager = PlanManager(self.store, self.path, allow_delete=allow_delete, max_buckets=1000, clock=lambda: NOW)
        self.server = DatalakeServer(
            ("127.0.0.1", 0),
            self.store,
            max_future_skew_ms=0,
            enable_downsampling=True,
            tokens=tokens,
            plan_admin=self.manager if admin else None,
            clock=lambda: NOW,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/downsample/plan"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Callable[..., _Rig]]:
    made: list[_Rig] = []

    def make(**kw: Any) -> _Rig:
        made.append(_Rig(tmp_path, **kw))
        return made[-1]

    yield make
    for r in made:
        r.close()


def test_the_routes_do_not_exist_by_default(rig: Callable[..., _Rig]) -> None:
    r = rig(admin=False)
    assert _call("GET", r.url)[0] == 404
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier()]}})[0] == 404


def test_get_shows_the_plan_the_switches_and_the_kinds(rig: Callable[..., _Rig]) -> None:
    r = rig()
    status, answer = _call("GET", r.url)
    assert status == 200
    assert answer["plan"] == {"tiers": [_tier()], "delete": False}
    assert answer["tiers"][0]["bucketMs"] == HOUR and answer["tiers"][0]["rawMs"] == 2 * DAY
    assert answer["knownKinds"] == ["other", "temp"]
    assert answer["switches"]["allowDelete"] is False and answer["switches"]["effectiveMode"] == "dryRun"
    assert len(answer["hash"]) == 64


def test_both_routes_ask_for_the_admin_token(rig: Callable[..., _Rig]) -> None:
    r = rig(tokens={"admin": "a" * 16, "read": "r" * 16})
    assert _call("GET", r.url)[0] == 401
    assert _call("GET", r.url, token="r" * 16)[0] == 401  # read is not enough
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier()]}}, token="r" * 16)[0] == 401
    assert _call("GET", r.url, token="a" * 16)[0] == 200
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier(raw="3d")]}}, token="a" * 16)[0] == 200


def test_put_saves_atomically_keeps_a_backup_and_returns_the_effects(rig: Callable[..., _Rig], tmp_path: Path) -> None:
    r = rig()
    before = r.path.read_text(encoding="utf-8")
    status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier(raw="1d")]}})
    assert status == 200 and answer["saved"] is True and answer["mode"] == "dryRun"
    assert answer["changes"] == [{"kind": "temp", "field": "*", "change": "changed", "fields": ["rawMs"]}]
    assert answer["effects"]["totals"]["rawPointsToRollUp"] == 100
    assert answer["currentEffects"]["totals"]["rawPointsToRollUp"] == 100  # three days old: past two days too
    assert json.loads(r.path.read_text(encoding="utf-8"))["tiers"][0]["raw"] == "1d"
    assert (tmp_path / "plan.json.bak").read_text(encoding="utf-8") == before
    assert not (tmp_path / "plan.json.tmp").exists()
    assert r.store.sample_count() == 102  # nothing was executed
    assert _call("GET", r.url)[1]["plan"]["tiers"][0]["raw"] == "1d"
    assert r.manager.snapshot()[0].raw_ms == DAY  # the timer uses the new plan


def test_preview_does_not_write(rig: Callable[..., _Rig]) -> None:
    r = rig()
    before = r.path.read_text(encoding="utf-8")
    status, answer = _call("PUT", r.url + "?preview=1", {"plan": {"tiers": [_tier(raw="1d")]}})
    assert status == 200 and answer["saved"] is False and answer["preview"] is True
    status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier(raw="1d")]}, "preview": True})
    assert status == 200 and answer["saved"] is False
    assert r.path.read_text(encoding="utf-8") == before


def test_the_same_plan_is_unchanged(rig: Callable[..., _Rig]) -> None:
    r = rig()
    status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier()]}})
    assert status == 200 and answer["saved"] is False and answer["unchanged"] is True


@pytest.mark.parametrize(
    "plan,code,status",
    [
        ({"tiers": []}, "invalid_plan", 400),
        ({"tiers": [_tier(raw="-1d")]}, "invalid_plan", 400),
        ({"tiers": [_tier(), _tier()]}, "invalid_plan", 400),
        ({"tiers": [_tier(surprise=1)]}, "invalid_plan", 400),
        ({"tiers": [_tier(fineBucket="2h")]}, "invalid_plan", 400),  # the narrow width must be narrower
        ({"tiers": [_tier(fineBucket="7m")]}, "invalid_plan", 400),  # and divide the wide one
        ({"tiers": [_tier(delete=True)]}, "invalid_plan", 400),
        ({"tiers": [_tier()], "delete": "yes"}, "invalid_plan", 400),
        ({"tiers": [_tier(bucket="3d")]}, "incoherent_plan", 422),  # wider than the raw time
        ({"tiers": [_tier(rollupRetention="1d")]}, "incoherent_plan", 422),  # rollups die before the raw points are rolled up
        ({"tiers": [_tier(fineBucket="1m", fineRetention="1d")]}, "incoherent_plan", 422),
        ({"tiers": [_tier(kind="nope")]}, "unknown_kind", 422),
    ],
)
def test_strict_validation(rig: Callable[..., _Rig], plan: dict, code: str, status: int) -> None:
    r = rig()
    before = r.path.read_text(encoding="utf-8")
    got_status, answer = _call("PUT", r.url, {"plan": plan})
    assert (got_status, answer["code"]) == (status, code)
    assert r.path.read_text(encoding="utf-8") == before


def test_bad_requests(rig: Callable[..., _Rig]) -> None:
    r = rig()
    bodies: list[Any] = [
        {},
        {"plan": []},
        {"plan": {"tiers": [_tier()]}, "extra": 1},
        {"plan": {"tiers": [_tier()]}, "confirmDelete": "x"},
        {"plan": {"tiers": [_tier()]}, "expectedHash": 5},
    ]
    for body in bodies:
        status, answer = _call("PUT", r.url, body)
        assert status == 400 and answer["code"] == "invalid_request"
    request = urllib.request.Request(r.url, method="PUT", data=b"not json", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request)
    assert caught.value.code == 400


def test_a_stale_hash_is_refused(rig: Callable[..., _Rig]) -> None:
    r = rig()
    old_hash = _call("GET", r.url)[1]["hash"]
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier(raw="3d")]}, "expectedHash": old_hash})[0] == 200
    status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier(raw="4d")]}, "expectedHash": old_hash})
    assert status == 409 and answer["code"] == "plan_changed"


def test_a_request_cannot_switch_deletion_on(rig: Callable[..., _Rig]) -> None:
    r = rig(allow_delete=False)
    before = r.path.read_text(encoding="utf-8")
    for extra in ({}, {"confirmDelete": True}):
        status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier()], "delete": True}, **extra})
        assert status == 403 and answer["code"] == "delete_not_allowed"
    assert _call("PUT", r.url + "?preview=1", {"plan": {"tiers": [_tier()], "delete": True}})[0] == 403
    assert r.path.read_text(encoding="utf-8") == before
    assert r.manager.deleting() is False
    audit = [json.loads(line) for line in r.manager.audit_path.read_text(encoding="utf-8").splitlines()]
    assert [a["event"] for a in audit] == ["delete_refused"] * 3 and audit[0]["allowDelete"] is False


def test_deletion_needs_the_operator_switch_and_a_confirmation(rig: Callable[..., _Rig]) -> None:
    r = rig(allow_delete=True)
    assert r.manager.deleting() is False  # allowed is not asked for
    document = {"plan": {"tiers": [_tier()], "delete": True}}
    status, answer = _call("PUT", r.url, document)
    assert status == 409 and answer["code"] == "confirm_delete_required"
    status, answer = _call("PUT", r.url + "?preview=1", document)  # a preview needs no confirmation and shows the mode
    assert status == 200 and answer["mode"] == "delete" and answer["saved"] is False
    status, answer = _call("PUT", r.url, {**document, "confirmDelete": True})
    assert status == 200 and answer["saved"] is True and answer["mode"] == "delete"
    assert r.manager.deleting() is True
    assert r.store.sample_count() == 102  # saving never executes the plan
    status, answer = _call("PUT", r.url, {"plan": {"tiers": [_tier()], "delete": False}})  # back to a dry run: no confirmation
    assert status == 200 and answer["mode"] == "dryRun" and r.manager.deleting() is False
    events = [a["event"] for a in r.manager.audit_tail()]  # type: ignore[attr-defined]
    assert events == ["plan_preview", "plan_saved", "plan_saved"]
    assert _call("GET", r.url)[1]["audit"][-1]["event"] == "plan_saved"


def test_a_delete_flag_in_the_file_is_ignored_without_the_switch(tmp_path: Path) -> None:
    store = TimeSeriesStore(":memory:")
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({"tiers": [_tier(kind="*", field="*")], "delete": True}), encoding="utf-8")
    assert PlanManager(store, path, allow_delete=False, max_buckets=10).deleting() is False
    assert PlanManager(store, path, allow_delete=True, max_buckets=10).deleting() is True
    assert PlanManager(store, path, allow_delete=False, cli_delete=True, max_buckets=10).deleting() is True  # the older switch


def test_the_effects_count_raw_points_and_expired_rollups(rig: Callable[..., _Rig]) -> None:
    default = {"kind": "*", "field": "*", "raw": "30d", "bucket": "1d", "rollupRetention": "60d"}
    r = rig(plan={"tiers": [_tier(), default]})
    new_default = {"kind": "*", "field": "*", "raw": "10d", "bucket": "1d"}
    status, answer = _call("PUT", r.url + "?preview=1", {"plan": {"tiers": [_tier(), new_default]}})
    assert status == 200
    by_series = {(s["kind"], s["field"]): s for s in answer["effects"]["series"]}
    assert by_series[("temp", "a")]["rawPointsToRollUp"] == 100
    assert by_series[("*", "*")]["rawPointsToRollUp"] == 1  # other/b, 40 days old; temp/a has its own tier
    assert answer["currentEffects"]["totals"]["rawPointsToRollUp"] == 101
    r.store.ensure_rollup_tables()
    r.store._conn.execute("INSERT INTO rollups VALUES ('s','temp','a',?,?,1,0,0,0)", (HOUR, NOW - 60 * DAY))
    r.store._conn.commit()
    answer = _call("PUT", r.url + "?preview=1", {"plan": {"tiers": [_tier(rollupRetention="10d")]}})[1]
    assert answer["effects"]["totals"]["rollupsToExpire"] == 1


def test_removing_a_tier_removes_its_policy_and_writing_one_adds_it(rig: Callable[..., _Rig]) -> None:
    r = rig()
    other = {"kind": "other", "field": "*", "raw": "5d", "bucket": "1d"}
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier(), other]}})[0] == 200
    policies = {(p["kind"], p["field"]) for p in r.store.list_downsample_policies()}  # type: ignore[attr-defined]
    assert ("temp", "a") in policies and ("other", "b") in policies
    assert _call("PUT", r.url, {"plan": {"tiers": [_tier()]}})[0] == 200
    policies = {(p["kind"], p["field"]) for p in r.store.list_downsample_policies()}  # type: ignore[attr-defined]
    assert ("temp", "a") in policies and ("other", "b") not in policies


def test_startup_switches(tmp_path: Path) -> None:
    assert main_module.switch_setting(False, "X_NOT_SET") is False
    assert main_module.switch_setting(True, "X_NOT_SET") is True
    assert main_module.switch_setting(False, "X", {"X": "true"}) is True
    assert main_module.switch_setting(True, "X", {"X": "false"}) is True  # the command line wins
    with pytest.raises(ValueError):
        main_module.switch_setting(False, "X", {"X": "maybe"})
    for flag in ("--downsample-admin", "--downsample-allow-delete"):  # both need a plan
        assert main_module.main(["--enable-downsampling", flag, "--db", str(tmp_path / "d.sqlite3")]) == 2
