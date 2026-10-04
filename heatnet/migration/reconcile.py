"""迁移核对：拆完（或回滚完）把归属、设定点、历史与计数对一遍。

核对读的就是调度在用的同一份分区表与索引，结果同时供控制台展示与落审计，
三方看到的是同一份事实。任何一项不过，``ok`` 为 ``False`` 且 ``violations``
列出未过项名字。核对项覆盖：调度索引与分区表一致、站属性与调度一致、站不
重复入区、设定点继承没被私改、历史记录只增不改、双读可用、调度用的是应用
时的索引、回滚真的回到拆分前、两种口径汇总都不重复计数。
"""

from __future__ import annotations

from ..ns.zone import ZoneRegistry
from ..quota.quota import QuotaLedger
from ..station.mapping import StationMapping
from ..station.station import HeatStation
from .record import APPLIED, COMMITTED, ROLLED_BACK, MigrationRecord

TOLERANCE = 1e-6


def zone_totals(ledger: QuotaLedger, index: dict[str, str]) -> dict[str, float]:
    """按给定归属索引把各站本期热量汇总到分区（每站恰好计一次）。"""

    totals: dict[str, float] = {}
    for station in ledger.stations():
        zone = index.get(station, "unassigned")
        totals[zone] = round(totals.get(zone, 0.0) + ledger.allocated_mwh(station), 4)
    return totals


def run_reconcile(
    record: MigrationRecord | None,
    *,
    zones: ZoneRegistry,
    mapping: StationMapping,
    stations: dict[str, HeatStation],
    ledger: QuotaLedger,
    audit_journal_count: int,
) -> dict:
    """对一次迁移（或当前归属）跑全项核对，返回核对报告。"""

    checks: list[dict] = []

    def check(name: str, ok: bool, detail: object) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    state = record.state if record is not None else None

    # 调度索引必须就是从当前分区表派生出来的那一份。
    table_index = zones.index()
    drift = {
        station: {"mapping": mapping.index().get(station), "table": zone}
        for station, zone in table_index.items()
        if mapping.index().get(station) != zone
    }
    check("index_matches_table", not drift, drift or "调度索引与分区表一致")

    # 站对象上的分区属性必须跟调度索引一致（调质按它、报表按它，不能各说各话）。
    mismatched = {
        name: {"station": station.zone(), "mapping": mapping.index().get(name)}
        for name, station in sorted(stations.items())
        if station.zone() != mapping.index().get(name)
    }
    check("station_attributes", not mismatched, mismatched or "站属性与调度索引一致")

    # 一个站只能出现在一个分区里，否则按区汇总就是重复计数。
    seen: dict[str, str] = {}
    duplicated: list[str] = []
    for zone_name in zones.names():
        for station in zones.stations(zone_name):
            if station in seen:
                duplicated.append(station)
            seen[station] = zone_name
    check("no_double_membership", not duplicated, duplicated or "每站恰好归属一个分区")

    # 整区继承的设定点不许在并存期被私改。
    if state in (APPLIED, COMMITTED):
        zone = zones.get(record.new_zone)
        intact = (
            zone.kind == record.inherited.get("kind")
            and abs(zone.setpoint_c - float(record.inherited.get("setpoint_c", 0.0))) < TOLERANCE
        )
        check(
            "inheritance_intact",
            intact,
            {
                "expected": dict(record.inherited),
                "actual": {"kind": zone.kind, "setpoint_c": zone.setpoint_c},
            },
        )
    else:
        check("inheritance_intact", True, "skipped: 无并存或已收口的迁移")

    # 历史不可变：审计流水与已结算周期只增不改，不许跟着新分区重写。
    if record is not None and record.fingerprint:
        fingerprint = record.fingerprint
        now = {"audit_journal": audit_journal_count, "quota_periods": ledger.periods()}
        immutable = (
            audit_journal_count >= int(fingerprint.get("audit_journal", 0))
            and ledger.periods() >= int(fingerprint.get("quota_periods", 0))
        )
        check("history_immutable", immutable, {"fingerprint": fingerprint, "now": now})
    else:
        check("history_immutable", True, "skipped: 无历史指纹")

    # 并存期内新老两套归属必须同时可读。
    if state == APPLIED:
        broken = [
            station
            for station in record.moving
            if record.pre_index.get(station) != record.source_zone
            or record.post_index.get(station) != record.new_zone
        ]
        check("dual_read_available", not broken, broken or "新老两套归属都可读")
    else:
        check("dual_read_available", True, "skipped: 非并存期")

    # 调度跑的就是应用迁移时重建出来的那一代索引。
    if state == APPLIED:
        check(
            "dispatch_view_consistent",
            mapping.generation() == record.post_generation,
            {"mapping_generation": mapping.generation(), "post_generation": record.post_generation},
        )
    else:
        check("dispatch_view_consistent", True, "skipped: 非并存期")

    # 回滚之后归属必须真的回到拆分前。
    if state == ROLLED_BACK:
        restored = mapping.index()
        drifted = {
            station: {"expected": zone, "actual": restored.get(station)}
            for station, zone in record.pre_index.items()
            if restored.get(station) != zone
        }
        extra = [station for station in restored if station not in record.pre_index]
        check(
            "rollback_restored",
            not drifted and not extra,
            {"drifted": drifted, "extra": extra} if drifted or extra else "归属已回到拆分前",
        )
    else:
        check("rollback_restored", True, "skipped: 未回滚")

    # 两种口径汇总都必须等于站级总和：哪个口径都不许重复计数。
    station_total = sum(ledger.allocated_mwh(station) for station in ledger.stations())
    post_total = sum(zone_totals(ledger, mapping.index()).values())
    balanced = abs(post_total - station_total) < TOLERANCE
    detail: dict = {
        "station_total": round(station_total, 4),
        "post_total": round(post_total, 4),
    }
    if record is not None:
        pre_total = sum(zone_totals(ledger, record.pre_index).values())
        balanced = balanced and abs(pre_total - station_total) < TOLERANCE
        detail["pre_total"] = round(pre_total, 4)
    check("zone_totals_balanced", balanced, detail)

    violations = [item["name"] for item in checks if not item["ok"]]
    return {
        "ok": not violations,
        "migration_id": record.migration_id if record is not None else None,
        "state": state,
        "checks": checks,
        "violations": violations,
    }
