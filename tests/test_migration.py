"""分区拆分迁移：逐站继承、双口径并存、核对、回退与历史不可变。"""

from __future__ import annotations

import pytest

from heatnet.app import build_platform
from heatnet.clock import ManualClock
from heatnet.config import default_config
from heatnet.errors import InterlockError
from heatnet.migration.heat_history import HeatHistory
from heatnet.migration.membership import Membership, MigrationBasis
from heatnet.migration.state import (
    STATE_COMPLETED,
    STATE_MIGRATING,
    STATE_NONE,
    STATE_ROLLED_BACK,
    MigrationState,
)
from heatnet.ns.zone import Zone, ZoneRegistry


def _split(platform, *, movers=("st02", "st03"), new_zone="north-west", **kw):
    return platform.split_zone("north", list(movers), new_zone, **kw)


# ---- 规则 1：继承一律逐站指定 -----------------------------------------


def test_split_is_per_station_assignment_with_partition(platform):
    report = _split(platform)
    plan = report["plan"]
    assert plan["movers"] == ["st02", "st03"]
    # stayers 由源分区全集推出，movers ∪ stayers 正好是源分区站（一个划分）。
    assert plan["stayers"] == ["st01"]
    assert sorted(plan["movers"] + plan["stayers"]) == ["st01", "st02", "st03"]


def test_split_rejects_non_partition_inputs(platform):
    # 空迁移（整区通配）不许
    with pytest.raises(InterlockError):
        _split(platform, movers=())
    # 站不属于源分区
    with pytest.raises(InterlockError):
        _split(platform, movers=("st04",))
    # 同一站重复指定
    with pytest.raises(InterlockError):
        _split(platform, movers=("st02", "st02"))
    # 不能在并存期再拆
    _split(platform)
    with pytest.raises(InterlockError):
        _split(platform, movers=("st01",), new_zone="north-east")
    assert platform.migrations.state().value == STATE_MIGRATING


def test_setpoint_is_one_time_snapshot_then_independent(platform):
    report = _split(platform)
    plan = report["plan"]
    # 新分区设定点 = 源分区设定点的一次性快照
    assert plan["new_setpoint_c"] == plan["source_setpoint_c"] == 62.0
    # 拆后两边独立：改源分区不影响新分区
    platform.zones.get("north").setpoint_c = 70.0
    assert platform.zones.get("north-west").setpoint_c == 62.0


def test_per_station_setpoint_override_only_for_movers(platform):
    with pytest.raises(InterlockError):
        _split(platform, station_setpoints_c={"st01": 55.0})  # st01 是 stayer
    report = _split(platform, station_setpoints_c={"st02": 57.5})
    assert report["plan"]["station_setpoints_c"] == {"st02": 57.5}
    assert platform.stations["st02"].target_c() == pytest.approx(57.5)
    # 未覆盖的迁移站回落新分区设定点
    assert platform.stations["st03"].target_c() == pytest.approx(62.0)


# ---- 规则 2：并存期两套归属同时可读、口径按分区定 -----------------------


def test_dual_membership_readable_during_coexistence(platform):
    _split(platform)
    membership = platform.membership
    # 迁移站：target 记新分区，legacy 记老分区
    assert membership.zone_of("st02", "target") == "north-west"
    assert membership.zone_of("st02", "legacy") == "north"
    # 未迁移站两套一致
    assert membership.zone_of("st01", "target") == "north"
    assert membership.zone_of("st01", "legacy") == "north"
    coexist = platform.migration_coexistence()
    assert coexist["active"] is True
    assert set(coexist["dual_read"]) == {"st02", "st03"}


def test_each_basis_is_a_partition_no_double_count(platform):
    _split(platform)
    all_stations = platform.zones.all_station_order()
    for basis in ("legacy", "target"):
        index = platform.membership.index(basis)
        # 不重不漏：每个站恰好出现一次
        assert sorted(index) == sorted(all_stations)
    # 两套口径的分组各是一个划分
    for basis in ("legacy", "target"):
        grouped_total = sum(
            len(platform.membership.stations_in(zone, basis))
            for zone in set(platform.membership.index(basis).values())
        )
        assert grouped_total == len(all_stations)


def test_recording_basis_is_chosen_per_zone(platform):
    platform.collect_heat("st02.heat-meter", 5.0)  # 拆前：记老分区
    _split(platform)
    # 默认 target：迁移站记新分区
    line_target = platform.collect_heat("st02.heat-meter", 2.0)
    assert line_target["recorded_zone"] == "north-west"
    assert line_target["basis"] == "target"
    # 把新分区的报表口径钉为 legacy：迁移站改记老分区
    platform.set_migration_basis("north-west", "legacy")
    line_legacy = platform.collect_heat("st02.heat-meter", 3.0)
    assert line_legacy["recorded_zone"] == "north"
    assert line_legacy["basis"] == "legacy"


# ---- 规则 3：历史不可变 -------------------------------------------------


def test_history_is_append_only_and_anchored_to_old_zone(platform):
    platform.collect_heat("st02.heat-meter", 5.0)
    pre_seq = platform.heat_history.lines()[-1].seq
    _split(platform)
    platform.collect_heat("st02.heat-meter", 2.0)
    # 拆前行永远记老分区，拆后行记新分区
    lines = platform.heat_history.lines()
    assert lines[pre_seq - 1].recorded_zone == "north"
    assert lines[-1].recorded_zone == "north-west"
    assert platform.heat_history.verify_chain()["ok"] is True


def test_history_chain_detects_tampering(platform):
    platform.collect_heat("st01.heat-meter", 1.0)
    platform.collect_heat("st02.heat-meter", 2.0)
    lines = platform.heat_history.lines()
    # 篡改：把拆前归属改写成新分区（模拟"跟着新分区改历史"）
    object.__setattr__(lines[1], "recorded_zone", "north-west")
    result = platform.heat_history.verify_chain()
    assert result["ok"] is False
    assert result["broken_at"] == 2


def test_tampered_history_is_rejected_on_restore(tmp_path, clock):
    root = str(tmp_path / "state")
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.collect_heat("st01.heat-meter", 1.0)
    _split(platform)
    # 直接改落盘历史
    import json
    import os

    doc = os.path.join(root, "state", "migration", "heat_history.json")
    raw = json.loads(open(doc, encoding="utf-8").read())
    raw["lines"][0]["recorded_zone"] = "tampered"
    json.dump(raw, open(doc, "w", encoding="utf-8"))
    with pytest.raises(InterlockError):
        build_platform(default_config(), clock=clock, storage_root=root)


# ---- 规则 4：核对供控制台展示并落审计 ----------------------------------


def test_reconcile_report_is_exposed_and_audited(platform):
    _split(platform)
    report = platform.reconcile_migration()
    assert report["ok"] is True
    for check in ("partition_legacy", "partition_target", "index_agreement", "history_chain",
                  "cutover_anchored", "setpoint_inherited", "basis_totals"):
        assert check in report["checks"]
        assert report["checks"][check]["ok"] is True
    # 落审计：控制台展示与审计是同一份报告
    assert platform.audit.count("migration_reconciled") == 1
    entry = platform.audit.entries(limit=50)
    audited = [item for item in entry if item["action"] == "migration_reconciled"][0]
    assert audited["payload"]["reconcile_ok"] is True


def test_prepare_complete_rollback_all_emit_audit(platform):
    _split(platform)
    platform.complete_migration()
    platform2 = build_platform(default_config(), clock=ManualClock(),
                               storage_root=platform.store.root + "_x")
    _split(platform2)
    platform2.rollback_migration()
    assert platform.audit.count("migration_prepared") == 1
    assert platform.audit.count("migration_completed") == 1
    assert platform2.audit.count("migration_rolled_back") == 1


# ---- 规则 5：回退=回滚，不重复计数 --------------------------------------


def test_rollback_restores_assignment_without_replaying_heat(platform):
    platform.collect_heat("st01.heat-meter", 4.0)
    platform.collect_heat("st02.heat-meter", 6.0)
    _split(platform)
    platform.set_migration_basis("north-west", "legacy")
    platform.collect_heat("st02.heat-meter", 3.0)
    before = platform.heat_history.total_mwh()
    meter_total_before = platform.meters.total_mwh()

    report = platform.rollback_migration()
    assert report["state"]["value"] == STATE_ROLLED_BACK
    # 归属回到拆分前：新分区消失，站回老分区
    assert platform.zones.names() == ["east", "north", "south"]
    assert platform.zones.zone_of("st02") == "north"
    assert platform.stations["st02"].zone() == "north"
    # 回退不重放热量：累计与回退前严格相等，表数不变
    assert platform.heat_history.total_mwh() == pytest.approx(before)
    assert platform.meters.total_mwh() == pytest.approx(meter_total_before)
    # 核对报告里"回退不重复计数"通过
    assert report["reconcile"]["checks"]["rollback_no_recount"]["ok"] is True


def test_rollback_only_allowed_during_coexistence(platform):
    _split(platform)
    platform.complete_migration()
    assert platform.migrations.state().value == STATE_COMPLETED
    with pytest.raises(InterlockError):
        platform.rollback_migration()


def test_rollback_survives_restart_and_still_no_recount(tmp_path, clock):
    root = str(tmp_path / "state")
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    platform.collect_heat("st02.heat-meter", 5.0)
    _split(platform)
    platform.collect_heat("st02.heat-meter", 2.0)
    total = platform.heat_history.total_mwh()
    platform.rollback_migration()

    restarted = build_platform(default_config(), clock=clock, storage_root=root)
    assert restarted.migrations.state().value == STATE_ROLLED_BACK
    assert restarted.zones.names() == ["east", "north", "south"]
    assert restarted.membership.zone_of("st02") == "north"
    assert restarted.heat_history.total_mwh() == pytest.approx(total)
    assert restarted.migrations.reconcile()["issues"] == []


def test_completed_split_survives_restart(tmp_path, clock):
    root = str(tmp_path / "state")
    platform = build_platform(default_config(), clock=clock, storage_root=root)
    _split(platform)
    platform.complete_migration()

    restarted = build_platform(default_config(), clock=clock, storage_root=root)
    assert restarted.migrations.state().value == STATE_COMPLETED
    assert restarted.zones.names() == ["east", "north", "north-west", "south"]
    assert restarted.membership.zone_of("st02", "target") == "north-west"
    assert restarted.stations["st02"].zone() == "north-west"


# ---- 两套口径总量守恒（并存期不能重也不能漏） ---------------------------


def test_dual_basis_totals_match(platform):
    for name, val in (("st01", 3.0), ("st02", 4.0), ("st03", 5.0), ("st04", 2.0)):
        platform.collect_heat(f"{name}.heat-meter", val)
    _split(platform)
    platform.collect_heat("st02.heat-meter", 1.0)
    report = platform.reconcile_migration()
    totals = report["totals_mwh"]
    # 两套口径只是归属不同，覆盖同一批站，总量必须相等
    assert totals["legacy"] == pytest.approx(totals["target"])


# ---- 单元：Membership / Basis / 状态机 / HeatHistory --------------------


def test_membership_views_without_active_migration():
    zones = ZoneRegistry()
    zones.add(Zone(name="a", setpoint_c=60.0))
    zones.assign("s1", "a")
    membership = Membership(zones)
    assert membership.active() is False
    assert membership.coexistence() == {"active": False}
    assert membership.legacy_index() == membership.target_index() == {"s1": "a"}


def test_migration_basis_rejects_unknown():
    basis = MigrationBasis()
    assert basis.of("whatever") == "target"
    basis.set("north-west", "legacy")
    assert basis.of("north-west") == "legacy"
    with pytest.raises(ValueError):
        basis.set("north-west", "sideways")


def test_state_machine_transitions():
    assert MigrationState(STATE_NONE).active is False
    assert MigrationState(STATE_MIGRATING).active is True
    assert MigrationState(STATE_MIGRATING).can_transition_to(STATE_COMPLETED) is True
    assert MigrationState(STATE_MIGRATING).can_transition_to(STATE_ROLLED_BACK) is True
    assert MigrationState(STATE_COMPLETED).can_transition_to(STATE_ROLLED_BACK) is False
    assert MigrationState(STATE_ROLLED_BACK).finished is True
    with pytest.raises(ValueError):
        MigrationState("bogus")


def test_heat_history_chain_ordering():
    history = HeatHistory()
    history.append("s1", 1.0, "north", 100.0)
    history.append("s1", 2.0, "north", 101.0)
    assert len(history) == 2
    assert history.total_mwh() == pytest.approx(3.0)
    assert history.zones_seen() == ["north"]
    assert history.verify_chain()["ok"] is True
    # 重建后链仍可校验
    restored = HeatHistory()
    assert restored.restore([line.as_dict() for line in history.lines()])["ok"] is True
    assert restored.total_mwh() == pytest.approx(3.0)


def test_zone_registry_merge_is_inverse_of_split():
    zones = ZoneRegistry()
    zones.add(Zone(name="north", setpoint_c=62.0))
    for station in ("s1", "s2", "s3"):
        zones.assign(station, "north")
    zones.split("north", ["s2", "s3"], "north-west")
    assert zones.splits() == 1
    assert zones.zone_of("s2") == "north-west"
    zones.merge_zone("north-west", "north")
    assert zones.merges() == 1
    assert zones.names() == ["north"]
    assert zones.stations("north") == ["s1", "s2", "s3"]
    assert zones.zone_of("s2") == "north"


# ---- 控制台接口 --------------------------------------------------------


def test_console_migration_endpoints(platform):
    from heatnet.console.api import Api

    api = Api(platform)
    paths = [route["path"] for route in api.routes()]
    for path in (
        "/api/migration",
        "/api/migration/coexistence",
        "/api/migration/reconcile",
        "/api/migration/basis",
        "/api/migration/complete",
        "/api/migration/rollback",
    ):
        assert path in paths

    status, split = api.handle(
        "POST", "/api/zone/split", {},
        {"source_zone": "north", "new_zone": "north-west", "stations": ["st02", "st03"]},
    )
    assert status == 200 and split["ok"] is True

    status, coexist = api.handle("GET", "/api/migration/coexistence", {}, {})
    assert status == 200 and coexist["active"] is True

    status, basis = api.handle(
        "POST", "/api/migration/basis", {}, {"zone": "north-west", "basis": "legacy"}
    )
    assert status == 200 and basis["basis"] == "legacy"

    status, report = api.handle("POST", "/api/migration/reconcile", {}, {})
    assert status == 200 and report["ok"] is True

    status, rollback = api.handle("POST", "/api/migration/rollback", {}, {})
    assert status == 200 and rollback["state"]["value"] == STATE_ROLLED_BACK


def test_console_complete_blocks_rollback(platform):
    from heatnet.console.api import Api

    api = Api(platform)
    api.handle("POST", "/api/zone/split", {},
               {"source_zone": "north", "new_zone": "north-west", "stations": ["st02"]})
    status, _ = api.handle("POST", "/api/migration/complete", {}, {})
    assert status == 200
    status, payload = api.handle("POST", "/api/migration/rollback", {}, {})
    assert status == 409
    assert payload["error"] == "InterlockError"


def test_status_exposes_migration_section(platform):
    _split(platform)
    status = platform.status()
    migration = status["migration"]
    assert migration["state"]["value"] == STATE_MIGRATING
    assert migration["coexistence"]["active"] is True
    assert migration["plan"]["new_zone"] == "north-west"
    assert migration["history"]["chain"]["ok"] is True


def test_dispatch_and_membership_share_one_fact(platform):
    """调度读的反向索引与权威成员表 target 视图必须一致（核对里的 index_agreement）。"""

    platform.start()
    platform.push_weather(-8.0)
    platform.startup(-8.0, 60.0)
    _split(platform)
    # 权威成员表 target 视图 == 调度实际使用的 mapping 反向索引
    target = platform.membership.target_index()
    assert target == platform.mapping.describe()["index"]
    report = platform.reconcile_migration()
    assert report["checks"]["index_agreement"]["ok"] is True
    # 迁移后对新分区调度仍然成立，计划里含迁移站
    dispatch = platform.dispatch(60.0, 60.0, ["north-west"])
    assert dispatch["ok"] is True
    planned = {flow["station"] for flow in dispatch["flows"]}
    assert {"st02", "st03"}.issubset(planned)
