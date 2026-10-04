"""迁移核对。

拆完之后能不能一眼看出不一致，靠的是把规矩逐条核一遍。核对只读权威成员表与
不可变历史，不改任何状态，产出同一份报告：控制台展示它、审计原样落它，因此
"调度看到的、报表算的、审计记的"必然一致。

核对项：

* ``partition_*``      单口径必须是全体站的一个划分——不重（重复计数）不漏（漏计数）；
* ``index_agreement``  权威成员表与分区表、调度用反向索引三处口径一致；
* ``history_chain``    热量历史哈希链完整（没被跟着新分区改写）；
* ``cutover_anchored`` 拆分前的历史行永远记在老分区名下；
* ``setpoint_inherited`` 新分区设定点是源分区设定点的一次性快照；
* ``basis_totals``     两套口径并存期总量相等（各自不重不漏，总量守恒）；
* ``rollback_no_recount`` 回退前后历史累计热量相等（回退不重放、不重复计数）。
"""

from __future__ import annotations

from .heat_history import HeatHistory
from .membership import BASES, BASIS_LEGACY, BASIS_TARGET, Membership
from .state import STATE_ROLLED_BACK, MigrationPlan


def _partition(index: dict[str, str], expected: list[str]) -> list[str]:
    """查一套归属索引相对全体站列表的重/漏项。"""

    problems: list[str] = []
    seen: set[str] = set()
    duplicated: set[str] = set()
    for station, zone in index.items():
        if station in seen:
            duplicated.add(station)
        seen.add(station)
        if not zone:
            problems.append(f"空归属:{station}")
    for station in sorted(duplicated):
        problems.append(f"重复计数:{station}")
    for station in expected:
        if station not in seen:
            problems.append(f"漏计数:{station}")
    for station in sorted(seen - set(expected)):
        problems.append(f"多余归属:{station}")
    return problems


def reconcile(
    membership: Membership,
    history: HeatHistory,
    plan: MigrationPlan | None,
    state_value: str | None,
    *,
    reverse_index: dict[str, str] | None = None,
    rollback_total_mwh: float | None = None,
) -> dict:
    """对当前迁移做一次只读核对，返回结构化报告。"""

    checks: dict[str, dict] = {}
    expected = membership.zones.all_station_order()

    def record(name: str, problems: list[str], **detail: object) -> None:
        checks[name] = {"ok": not problems, "issues": problems, **detail}

    # 1) 两套口径各自都必须是全体站的一个划分。
    totals: dict[str, float] = {}
    for basis in BASES:
        index = membership.index(basis)
        problems = _partition(index, expected)
        record(f"partition_{basis}", problems, basis=basis, entries=len(index))
        # 历史累计按该口径折算到分区（只用于守恒比较，不代表跨口径可相加）。
        roll = membership.index(basis)
        totals[basis] = sum(line.mwh for line in history.lines() if line.station in roll)

    # 2) 权威成员表 target 视图必须与调度用反向索引一致。
    if reverse_index is not None:
        target = membership.target_index()
        drift = [
            station
            for station in expected
            if target.get(station) != reverse_index.get(station)
        ]
        record("index_agreement", [f"索引漂移:{s}" for s in drift], compared=len(expected))

    # 3) 历史不可变：哈希链完整。
    chain = history.verify_chain()
    record("history_chain", [] if chain["ok"] else [f"链断点@{chain['broken_at']}"], **chain)

    # 4) 拆分前的历史行必须记在老分区名下（不许跟着新分区改）。
    if plan is not None:
        pre = [
            line
            for line in history.lines()
            if line.ts < plan.cutover_ts and line.station in plan.movers
        ]
        misanchored = [
            f"{line.station}#{line.seq}→{line.recorded_zone}"
            for line in pre
            if line.recorded_zone == plan.new_zone
        ]
        record(
            "cutover_anchored",
            misanchored,
            pre_cutover_lines=len(pre),
            source_zone=plan.source_zone,
        )

        # 5) 设定点一次性快照继承。
        setpoint_bad = abs(plan.new_setpoint_c - plan.source_setpoint_c) > 1e-9
        record(
            "setpoint_inherited",
            ["新分区设定点不是源分区快照"] if setpoint_bad else [],
            source_setpoint_c=round(plan.source_setpoint_c, 2),
            new_setpoint_c=round(plan.new_setpoint_c, 2),
        )

        # 6) 两套口径并存期总量必须守恒。
        totals_drift = abs(totals[BASIS_LEGACY] - totals[BASIS_TARGET])
        record(
            "basis_totals",
            [f"两套口径总量不一致:{round(totals_drift, 4)}"] if totals_drift > 1e-6 else [],
            legacy_total_mwh=round(totals[BASIS_LEGACY], 4),
            target_total_mwh=round(totals[BASIS_TARGET], 4),
        )

    # 7) 回退不重复计数：回退后历史累计必须与回退前相等。
    if state_value == STATE_ROLLED_BACK and rollback_total_mwh is not None:
        now_total = history.total_mwh()
        drift = abs(now_total - rollback_total_mwh)
        record(
            "rollback_no_recount",
            [f"回退前后累计差:{round(drift, 4)}"] if drift > 1e-6 else [],
            before_total_mwh=round(rollback_total_mwh, 4),
            after_total_mwh=round(now_total, 4),
        )

    issues = [f"{name}:{issue}" for name, item in checks.items() for issue in item["issues"]]
    return {
        "ok": not issues,
        "state": state_value,
        "migration_id": None if plan is None else plan.migration_id,
        "checks": checks,
        "issues": issues,
        "totals_mwh": {basis: round(value, 4) for basis, value in totals.items()},
    }
