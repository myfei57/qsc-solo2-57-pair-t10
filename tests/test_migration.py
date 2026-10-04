"""分区拆分迁移：计划、双读、核对、回滚与历史不可变。"""

from __future__ import annotations

import pytest

from heatnet.app import build_platform
from heatnet.config import default_config
from heatnet.console.api import Api
from heatnet.errors import InterlockError, NamespaceError


def _split(platform, stations=("st02", "st03")):
    return platform.split_zone("north", list(stations), "north-west")


def test_plan_computes_inheritance_without_touching_registry(platform):
    plan = platform.plan_zone_split("north", ["st02", "st03"], "north-west")
    # 继承方式定死为整区继承：新分区整体继承源分区类型与设定点。
    assert plan["inheritance"] == "zone"
    assert plan["inherited"]["setpoint_c"] == platform.zones.get("north").setpoint_c
    assert plan["inherited"]["kind"] == platform.zones.get("north").kind
    assert plan["moving"] == ["st02", "st03"]
    assert plan["staying"] == ["st01"]
    assert plan["pre_index"]["st02"] == "north"
    assert plan["post_index"]["st02"] == "north-west"
    # 并存期口径按分区定死：调质按新分区，报表本期按老分区。
    assert plan["read_policy"]["north-west"] == {"control": "post", "report": "pre"}
    # 干跑不改任何归属。
    assert platform.mapping.zone_of("st02") == "north"
    assert platform.zones.names() == ["east", "north", "south"]
    assert platform.migrations.describe()["count"] == 0


def test_plan_rejects_stations_outside_source(platform):
    # st04 是 south 的站：名单没算清就当场拒绝，而不是静默丢掉。
    with pytest.raises(NamespaceError):
        platform.plan_zone_split("north", ["st02", "st04"], "north-west")
    with pytest.raises(NamespaceError):
        platform.plan_zone_split("north", [], "north-west")
    with pytest.raises(NamespaceError):
        platform.plan_zone_split("north", ["st02"], "north")
    with pytest.raises(NamespaceError):
        platform.plan_zone_split("north", ["st02"], "south")


def test_split_applies_plan_and_dual_read(platform):
    report = _split(platform)
    assert report["migration"]["state"] == "applied"
    assert report["reconcile"]["ok"] is True
    # 迁移期间两套归属同时可读。
    assert platform.migrations.zone_of("st02", view="pre") == "north"
    assert platform.migrations.zone_of("st02", view="post") == "north-west"
    # 按分区定口径：调质按新分区，报表按老分区。
    assert platform.migrations.zone_of("st02", purpose="control") == "north-west"
    assert platform.migrations.zone_of("st02", purpose="report") == "north"
    # 调度侧与站属性都已是新归属。
    assert platform.mapping.zone_of("st02") == "north-west"
    assert platform.service.get("st02").zone() == "north-west"


def test_reconcile_detects_tampered_attribution(platform):
    _split(platform)
    platform.stations["st02"].rezone("north")  # 人为制造归属不一致
    report = platform.reconcile_migration()
    assert report["ok"] is False
    assert "station_attributes" in report["violations"]
    # 核对结果落审计，控制台与审计看到的是同一份。
    entries = platform.audit.entries(limit=50)
    reconciles = [entry for entry in entries if entry["action"] == "migration_reconcile"]
    assert reconciles
    assert reconciles[0]["severity"] == "warning"
    assert "station_attributes" in reconciles[0]["payload"]["violations"]


def test_rollback_restores_pre_split_attribution(platform):
    split = _split(platform)
    migration_id = split["migration"]["migration_id"]
    back = platform.rollback_zone_split(migration_id)
    assert back["migration"]["state"] == "rolled_back"
    assert back["reconcile"]["ok"] is True
    checks = {item["name"]: item for item in back["reconcile"]["checks"]}
    assert checks["rollback_restored"]["ok"] is True
    assert platform.mapping.zone_of("st02") == "north"
    assert platform.service.get("st02").zone() == "north"
    assert platform.zones.names() == ["east", "north", "south"]
    # 回退是回滚：不能再来一次，否则归属变更就被执行了两遍。
    with pytest.raises(InterlockError):
        platform.rollback_zone_split(migration_id)


def test_rollback_does_not_double_count_heat(platform):
    for name in platform.meter_names():
        platform.collect_heat(name, 5.0)
    split = _split(platform)
    migration_id = split["migration"]["migration_id"]
    # 并存期两种口径汇总都等于站级总和。
    totals = platform.migration_report()["zone_totals"]
    assert sum(totals["post"].values()) == pytest.approx(30.0)
    assert sum(totals["pre"].values()) == pytest.approx(30.0)
    assert totals["post"]["north-west"] == pytest.approx(10.0)
    assert totals["pre"]["north"] == pytest.approx(15.0)
    platform.rollback_zone_split(migration_id)
    # 回滚后汇总仍然不重复计数。
    totals = platform.migration_report()["zone_totals"]
    assert sum(totals["post"].values()) == pytest.approx(30.0)
    assert sum(totals["pre"].values()) == pytest.approx(30.0)
    assert platform.ledger.total_mwh() == pytest.approx(30.0)


def test_history_is_immutable_across_split_and_rollback(platform):
    platform.collect_heat(platform.meter_names()[0], 5.0)
    settled = platform.settle_quota()
    journal_before = platform.audit.journal.count()
    split = _split(platform)
    migration_id = split["migration"]["migration_id"]
    # 应用时打下历史指纹。
    assert split["migration"]["fingerprint"]["quota_periods"] == settled["period"]
    platform.rollback_zone_split(migration_id)
    report = platform.reconcile_migration(migration_id)
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["history_immutable"]["ok"] is True
    # 已结算周期与审计流水不跟着新分区改。
    assert platform.ledger.periods() == settled["period"]
    assert platform.audit.journal.count() >= journal_before


def test_commit_ends_coexistence(platform):
    split = _split(platform)
    migration_id = split["migration"]["migration_id"]
    committed = platform.commit_zone_split(migration_id)
    assert committed["migration"]["state"] == "committed"
    # 并存期结束：报表口径切到新分区。
    assert platform.migrations.zone_of("st02", purpose="report") == "north-west"
    assert platform.migrations.active() is None
    # 收口之后不能再回滚。
    with pytest.raises(InterlockError):
        platform.rollback_zone_split(migration_id)


def test_second_split_blocked_while_migration_active(platform):
    platform.split_zone("north", ["st02"], "north-west")
    with pytest.raises(InterlockError):
        platform.split_zone("south", ["st04"], "south-west")
    with pytest.raises(InterlockError):
        platform.plan_zone_split("south", ["st04"], "south-west")


def test_migration_records_survive_restart(platform, clock):
    split = _split(platform)
    migration_id = split["migration"]["migration_id"]
    restarted = build_platform(default_config(), clock=clock, storage_root=platform.store.root)
    # 分区表重启后回到初始三分区，活跃迁移与现实对不上：留档，不再参与解析。
    record = restarted.migrations.get(migration_id)
    assert record.state == "stale"
    assert restarted.migrations.active() is None
    assert restarted.migrations.zone_of("st02") == "north"
    # 台账留档可审计，且编号不会重用。
    assert restarted.migrations.describe()["count"] == 1
    plan = restarted.plan_zone_split("north", ["st02"], "north-west")
    assert plan["migration_id"] != migration_id


def test_console_exposes_migration(platform):
    api = Api(platform)
    status, plan = api.handle(
        "POST",
        "/api/zone/split/plan",
        {},
        {"source_zone": "north", "stations": ["st02", "st03"], "new_zone": "north-west"},
    )
    assert status == 200
    assert plan["inheritance"] == "zone"

    status, split = api.handle(
        "POST",
        "/api/zone/split",
        {},
        {"source_zone": "north", "stations": ["st02", "st03"], "new_zone": "north-west"},
    )
    assert status == 200
    migration_id = split["migration"]["migration_id"]

    status, payload = api.handle("GET", "/api/migrations", {}, {})
    assert status == 200
    assert payload["active"] == migration_id
    assert payload["views"]["pre"]["st02"] == "north"
    assert payload["views"]["post"]["st02"] == "north-west"

    status, report = api.handle("POST", "/api/migration/reconcile", {}, {})
    assert status == 200
    assert report["ok"] is True
    assert report["migration_id"] == migration_id

    status, back = api.handle(
        "POST", "/api/zone/split/rollback", {}, {"migration_id": migration_id}
    )
    assert status == 200
    assert back["migration"]["state"] == "rolled_back"

    status, payload = api.handle("GET", "/api/migrations", {}, {})
    assert status == 200
    assert payload["active"] is None
    assert payload["last_reconcile"]["ok"] is True
