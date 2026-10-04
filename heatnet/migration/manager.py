"""迁移编排：拆分提交、并存、核对、切换与回滚。

:class:`MigrationManager` 是唯一动归属的地方。它把分区表、权威成员表、不可变
热量历史、审计事件与落盘串成一次原子编排，调度/报表/审计都通过它暴露的成员表
读归属，因此不会出现各读各的反向索引。

关键边界：

* **逐站指定**：``prepare`` 必须给出 movers，stayers 由源分区全集推出；两者必须
  构成源分区站的一个划分，重复或越界一律拒绝，没有整区通配。
* **历史不重放**：回滚只做 :meth:`ZoneRegistry.merge_zone` 与归属恢复，绝不调用
  任何热量记账，所以回退不重复计数。
* **核对落审计**：每次 prepare/complete/rollback 都跑同一份 :func:`reconcile`，
  结果既返回给控制台也通过事件总线落审计。
"""

from __future__ import annotations

from typing import Callable

from ..errors import InterlockError
from ..eventbus import Event
from ..ns.zone import ZoneRegistry
from ..store.state import DurableState
from .heat_history import HeatHistory
from .membership import BASIS_TARGET, Membership, MigrationBasis
from .reconcile import reconcile
from .state import (
    STATE_COMPLETED,
    STATE_MIGRATING,
    STATE_NONE,
    STATE_PREPARED,
    STATE_ROLLED_BACK,
    MigrationPlan,
    MigrationState,
)

_ACTIVE_DOC = "migration/active"
_HISTORY_DOC = "migration/heat_history"


class MigrationManager:
    """分区拆分迁移的状态机与唯一归属写入点。"""

    def __init__(
        self,
        zones: ZoneRegistry,
        membership: Membership,
        history: HeatHistory,
        durable: DurableState,
        clock,
        bus,
        make_migration_id: Callable[[], str],
        *,
        rezone_station: Callable[[str, str, float], None] | None = None,
        rebuild_mapping: Callable[[], int] | None = None,
    ) -> None:
        self._zones = zones
        self._membership = membership
        self._history = history
        self._durable = durable
        self._clock = clock
        self._bus = bus
        self._make_id = make_migration_id
        self._rezone_station = rezone_station
        self._rebuild_mapping = rebuild_mapping
        self.basis = MigrationBasis(default=BASIS_TARGET)
        self._plan: MigrationPlan | None = None
        self._state = MigrationState(STATE_NONE)
        self._rollback_total_mwh: float | None = None
        self._restore()

    # ---- 读取 ---------------------------------------------------------

    @property
    def history(self) -> HeatHistory:
        return self._history

    @property
    def membership(self) -> Membership:
        return self._membership

    def plan(self) -> MigrationPlan | None:
        return self._plan

    def state(self) -> MigrationState:
        return self._state

    def active(self) -> MigrationPlan | None:
        """返回当前并存中的迁移计划，无并存时为 ``None``。"""

        return self._plan if self._state.active else None

    # ---- 拆分提交 -----------------------------------------------------

    def prepare(
        self,
        source_zone: str,
        movers: list[str],
        new_zone: str,
        *,
        station_setpoints_c: dict[str, float] | None = None,
    ) -> dict:
        """逐站指定地拆分并进入并存；返回核对报告。"""

        if self._state.active:
            raise InterlockError(
                f"已有迁移 {self._plan.migration_id} 处于并存期，不能再拆", component="migration"
            )
        source = self._zones.get(source_zone)
        moving = list(movers)
        if len(set(moving)) != len(moving):
            raise InterlockError("迁移站重复指定", component="migration")
        not_in_source = [s for s in moving if s not in source.stations]
        if not_in_source:
            raise InterlockError(f"站不属于源分区 {source_zone}: {not_in_source}", component="migration")
        if not moving:
            raise InterlockError("拆分必须逐站指定至少一个迁移站", component="migration")
        stayers = [s for s in source.stations if s not in moving]
        overrides = {s: float(v) for s, v in (station_setpoints_c or {}).items()}
        bad_override = [s for s in overrides if s not in moving]
        if bad_override:
            raise InterlockError(f"只能为迁移站指定新设定点: {bad_override}", component="migration")

        cutover = self._clock.now()
        plan = MigrationPlan(
            migration_id=self._make_id(),
            source_zone=source_zone,
            new_zone=new_zone,
            movers=tuple(moving),
            stayers=tuple(stayers),
            source_setpoint_c=float(source.setpoint_c),
            new_setpoint_c=float(source.setpoint_c),  # 一次性快照继承
            station_setpoints_c=overrides,
            cutover_ts=cutover,
        )
        # 物理拆分：新分区设定点取源分区快照。
        child = self._zones.split(source_zone, moving, new_zone)
        if child.setpoint_c != plan.new_setpoint_c:
            child.setpoint_c = plan.new_setpoint_c
        self._apply_station_targets(plan)
        if self._rebuild_mapping is not None:
            self._rebuild_mapping()
        self._plan = plan
        self._state = MigrationState(STATE_MIGRATING)
        self.basis = MigrationBasis(default=BASIS_TARGET)
        self._membership.bind(plan, self._state.value)
        self._persist_active()
        report = self.reconcile()
        self._publish("migration_prepared", plan, report, message=f"分区 {source_zone} 拆出 {new_zone}")
        return {
            "ok": report["ok"],
            "zone": child.as_dict(),
            "plan": plan.as_dict(),
            "state": self._state.as_dict(),
            "reconcile": report,
        }

    def set_basis(self, zone: str, basis: str) -> dict:
        """并存期按分区选定口径（legacy/target）。"""

        if not self._state.active:
            raise InterlockError("只在并存期可以改口径", component="migration")
        self.basis.set(zone, basis)
        self._persist_active()
        return {"zone": zone, "basis": basis, "selection": self.basis.describe()}

    def complete(self) -> dict:
        """切换完成：老口径归档，回退窗口关闭，只留 target。"""

        if not self._state.can_transition_to(STATE_COMPLETED):
            raise InterlockError(f"状态 {self._state.value} 不允许切换完成", component="migration")
        plan = self._plan
        assert plan is not None
        self._state = MigrationState(STATE_COMPLETED)
        self.basis = MigrationBasis(default=BASIS_TARGET)
        self._membership.bind(plan, self._state.value)
        self._persist_active()
        report = self.reconcile()
        self._publish(
            "migration_completed",
            plan,
            report,
            message=f"迁移 {plan.migration_id} 切换完成",
        )
        return {"ok": report["ok"], "state": self._state.as_dict(), "reconcile": report}

    def rollback(self) -> dict:
        """整体回滚：回归属、不重放热量；返回回退后核对报告。"""

        if not self._state.can_transition_to(STATE_ROLLED_BACK):
            raise InterlockError(f"状态 {self._state.value} 不允许回退", component="migration")
        plan = self._plan
        assert plan is not None
        rollback_total = self._history.total_mwh()
        # 只动归属：新分区并回源分区，绝不重放任何热量。
        self._zones.merge_zone(plan.new_zone, plan.source_zone)
        for station in plan.assigned():
            self._rezone(station, plan.source_zone, plan.source_setpoint_c)
        if self._rebuild_mapping is not None:
            self._rebuild_mapping()
        self._state = MigrationState(STATE_ROLLED_BACK)
        self.basis = MigrationBasis(default=BASIS_TARGET)
        # 绑定已回滚计划：membership 据 ROLLED_BACK 把两套视图都还原为拆分前归属。
        self._membership.bind(plan, self._state.value)
        self._rollback_total_mwh = rollback_total
        self._persist_active()
        report = self.reconcile()
        self._publish(
            "migration_rolled_back",
            plan,
            report,
            message=f"迁移 {plan.migration_id} 已回滚到拆分前归属",
        )
        return {"ok": report["ok"], "state": self._state.as_dict(), "reconcile": report}

    # ---- 核对 ---------------------------------------------------------

    def reconcile(self) -> dict:
        """跑一次只读核对（控制台展示 + 审计落盘共用这一份）。"""

        reverse_index = None
        if self._rebuild_mapping is not None:
            reverse_index = self._membership.zones.index()
        return reconcile(
            self._membership,
            self._history,
            self._plan,
            self._state.value,
            reverse_index=reverse_index,
            rollback_total_mwh=self._rollback_total_mwh,
        )

    def record_reconciliation(self) -> dict:
        """显式核对一次并把结果落审计（供控制台"迁移核对"按钮）。"""

        report = self.reconcile()
        self._publish(
            "migration_reconciled",
            self._plan,
            report,
            message="迁移核对通过" if report["ok"] else "迁移核对发现不一致",
        )
        return report

    # ---- 热量记账（带记录时归属，不可变） -----------------------------

    def record_heat(self, station: str, mwh: float, *, period: int = 0) -> dict:
        """把一次热量增量按当前口径记入不可变历史，返回该行。"""

        basis = self._recording_basis(station)
        zone = self._membership.zone_of(station, basis)
        line = self._history.append(
            station,
            mwh,
            zone,
            self._clock.now(),
            migration_id="" if self._plan is None else self._plan.migration_id,
            basis=basis if self._state.active else "",
            period=period,
        )
        self._persist_history()
        return line.as_dict()

    def _recording_basis(self, station: str) -> str:
        """记账口径：该站记录时归属所在分区选定的口径（按分区定）。"""

        # 先用 target 找到物理归属分区，再查该分区配置的口径。
        physical = self._membership.zone_of(station, BASIS_TARGET)
        return self.basis.of(physical)

    # ---- 内部 ---------------------------------------------------------

    def _apply_station_targets(self, plan: MigrationPlan) -> None:
        for station in plan.movers:
            target = plan.station_setpoints_c.get(station, plan.new_setpoint_c)
            self._rezone(station, plan.new_zone, target)
        for station in plan.stayers:
            self._rezone(station, plan.source_zone, plan.source_setpoint_c)

    def _rezone(self, station: str, zone: str, setpoint_c: float) -> None:
        if self._rezone_station is not None:
            self._rezone_station(station, zone, setpoint_c)

    def _publish(self, action: str, plan: MigrationPlan | None, report: dict, **extra: object) -> None:
        payload: dict[str, object] = {
            "severity": "notice" if report.get("ok") else "alarm",
            "reconcile_ok": bool(report.get("ok")),
            "issues": list(report.get("issues", [])),
        }
        if plan is not None:
            payload["migration_id"] = plan.migration_id
            payload["source_zone"] = plan.source_zone
            payload["new_zone"] = plan.new_zone
        payload.update(extra)
        self._bus.publish(Event.of("migration", "migration", action, **payload))

    # ---- 落盘与恢复 ---------------------------------------------------

    def _persist_active(self) -> None:
        if self._plan is None:
            return
        self._durable.save(
            _ACTIVE_DOC,
            {
                "plan": self._plan.as_dict(),
                "state": self._state.value,
                "basis": self.basis.describe(),
                "rollback_total_mwh": self._rollback_total_mwh,
            },
        )

    def _persist_history(self) -> None:
        self._durable.save(_HISTORY_DOC, {"lines": [line.as_dict() for line in self._history.lines()]})

    def _restore(self) -> None:
        raw = self._durable.load(_HISTORY_DOC)
        if isinstance(raw, dict) and isinstance(raw.get("lines"), list):
            result = self._history.restore(raw["lines"])
            if not result["ok"]:  # 历史被篡改必须当场暴露，不能静默恢复。
                raise InterlockError(
                    f"热量历史哈希链在第 {result['broken_at']} 行断裂", component="migration"
                )
        active = self._durable.load(_ACTIVE_DOC)
        if not isinstance(active, dict) or not isinstance(active.get("plan"), dict):
            self._membership.bind(None, None)
            return
        saved = active["plan"]
        plan = MigrationPlan(
            migration_id=str(saved["migration_id"]),
            source_zone=str(saved["source_zone"]),
            new_zone=str(saved["new_zone"]),
            movers=tuple(saved.get("movers", ())),
            stayers=tuple(saved.get("stayers", ())),
            source_setpoint_c=float(saved.get("source_setpoint_c", 0.0)),
            new_setpoint_c=float(saved.get("new_setpoint_c", 0.0)),
            station_setpoints_c={k: float(v) for k, v in (saved.get("station_setpoints_c") or {}).items()},
            cutover_ts=float(saved.get("cutover_ts", 0.0)),
        )
        state_value = str(active.get("state", STATE_MIGRATING))
        self._plan = plan
        self._state = MigrationState(state_value)
        self._rollback_total_mwh = active.get("rollback_total_mwh")
        basis = active.get("basis")
        if isinstance(basis, dict):
            self.basis = MigrationBasis(
                basis.get("overrides") or {}, default=str(basis.get("default", BASIS_TARGET))
            )
        # 分区表每次从固定配置装配：并存/完成态重启时按落盘计划重放物理拆分，
        # 回滚态不重放（本就该回到拆分前）。重放与首次拆分走同一个底层操作。
        if state_value in (STATE_PREPARED, STATE_MIGRATING, STATE_COMPLETED):
            self._replay_physical_split(plan)
        # 重启后若仍在并存，物理拆分已在装配期由分区表重建；这里只需让成员表
        # 重新绑定双视图。回滚态同样绑定，membership 会把两套视图还原为拆分前。
        self._membership.bind(plan, state_value)

    def _replay_physical_split(self, plan: MigrationPlan) -> None:
        """重启后按计划重建物理拆分（分区表、站目标、反向索引）。"""

        if plan.new_zone in self._zones.names():
            # 物理拆分已在（测试或上层已建）：只确保设定点与目标一致。
            self._zones.get(plan.new_zone).setpoint_c = plan.new_setpoint_c
        else:
            self._zones.split(plan.source_zone, list(plan.movers), plan.new_zone)
            self._zones.get(plan.new_zone).setpoint_c = plan.new_setpoint_c
        self._apply_station_targets(plan)
        if self._rebuild_mapping is not None:
            self._rebuild_mapping()

    def reset_active(self) -> bool:
        """丢弃并存中的活动迁移记录（仅交付演示/重置基线使用）。

        这是*清账*不是*回滚*：它假定分区表已经是想要的基线，因此只删落盘的活动
        迁移文档并解绑双视图，不会去并分区。生产流程的退回应走 :meth:`rollback`。
        """

        raw = self._durable.load(_ACTIVE_DOC)
        existed = isinstance(raw, dict) and bool(raw)
        self._plan = None
        self._state = MigrationState(STATE_NONE)
        self.basis = MigrationBasis(default=BASIS_TARGET)
        self._rollback_total_mwh = None
        self._membership.bind(None, None)
        self._durable.store.remove(self._durable.doc_id(_ACTIVE_DOC))
        return existed

    def describe(self) -> dict:
        """汇总迁移子系统，供状态快照与控制台读取。"""

        return {
            "state": self._state.as_dict(),
            "plan": None if self._plan is None else self._plan.as_dict(),
            "basis": self.basis.describe(),
            "coexistence": self._membership.coexistence(),
            "history": {
                "lines": len(self._history),
                "total_mwh": round(self._history.total_mwh(), 4),
                "zones_seen": self._history.zones_seen(),
                "chain": self._history.verify_chain(),
            },
        }
