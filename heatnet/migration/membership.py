"""两套归属与并存期口径。

迁移期间同一批站有两套归属，必须同时可读：

``legacy``  按拆分前的老分区读——迁移的站仍记在源分区名下（报表老口径）；
``target``  按拆分后的新分区读——迁移的站记在新分区名下（调质/调度新口径）。

物理拆分一提交，站就已经在新分区里了，所以 *target* 视图就是分区表本身；*legacy*
视图由"迁移中的站划回源分区"还原。迁移完成后老口径归档，只留 target；回退后
迁移作废，两套视图都回到拆分前的同一份归属。

并存期"按哪一套算"是*按分区*定的：每个分区各自选 ``legacy`` 或 ``target``。
任何一个单口径视图都必须是全体站的一个划分（不重不漏），核对时据此查重复计数
与漏计数；跨口径的总量相加没有意义，:class:`Membership` 不提供这种合计。
"""

from __future__ import annotations

from ..ns.zone import ZoneRegistry
from .state import STATE_ROLLED_BACK, MigrationPlan

BASIS_LEGACY = "legacy"
BASIS_TARGET = "target"
BASES = (BASIS_LEGACY, BASIS_TARGET)


class MigrationBasis:
    """并存期各分区的口径选择（按分区定，缺省回落）。"""

    def __init__(self, overrides: dict[str, str] | None = None, *, default: str = BASIS_TARGET) -> None:
        if default not in BASES:
            raise ValueError(f"未知口径: {default}")
        self._default = default
        self._overrides: dict[str, str] = {}
        for zone, basis in (overrides or {}).items():
            self.set(zone, basis)

    def set(self, zone: str, basis: str) -> None:
        """把某分区的口径钉死为 ``legacy`` 或 ``target``。"""

        if basis not in BASES:
            raise ValueError(f"未知口径: {basis}")
        self._overrides[str(zone)] = basis

    def of(self, zone: str) -> str:
        """返回某分区当前采用的口径。"""

        return self._overrides.get(zone, self._default)

    def describe(self) -> dict:
        return {"default": self._default, "overrides": dict(sorted(self._overrides.items()))}


class Membership:
    """权威成员表：从分区表 + 当前迁移派生两套归属。

    调度、核对、审计共用这一个对象，保证看到的是同一份事实——不允许各读各的
    反向索引。
    """

    def __init__(self, zones: ZoneRegistry) -> None:
        self._zones = zones
        self._plan: MigrationPlan | None = None
        self._state_value: str | None = None

    @property
    def zones(self) -> ZoneRegistry:
        return self._zones

    def bind(self, plan: MigrationPlan | None, state_value: str | None) -> None:
        """绑定当前活动迁移（无活动迁移时两个参数都传 ``None``）。"""

        self._plan = plan
        self._state_value = state_value

    def active(self) -> bool:
        """是否存在生效中的并存迁移（回退后的迁移不再改视图）。"""

        return self._plan is not None and self._state_value != STATE_ROLLED_BACK

    # ---- 两套视图 -----------------------------------------------------

    def target_index(self) -> dict[str, str]:
        """新口径：站 → 拆分后所在分区（即分区表事实）。"""

        return self._zones.index()

    def legacy_index(self) -> dict[str, str]:
        """老口径：站 → 拆分前归属；并存期把迁移站划回源分区。"""

        index = self.target_index()
        if self.active():
            plan = self._plan
            assert plan is not None
            for station in plan.movers:
                if station in index:
                    index[station] = plan.source_zone
        return index

    def index(self, basis: str = BASIS_TARGET) -> dict[str, str]:
        """按口径取一套"站 → 分区"归属。"""

        if basis == BASIS_TARGET:
            return self.target_index()
        if basis == BASIS_LEGACY:
            return self.legacy_index()
        raise ValueError(f"未知口径: {basis}")

    def zone_of(self, station: str, basis: str = BASIS_TARGET) -> str:
        """按口径反查一个站的归属。"""

        return self.index(basis)[station]

    def stations_in(self, zone: str, basis: str = BASIS_TARGET) -> list[str]:
        """按口径列出某分区下的站（保持分区表顺序）。"""

        index = self.index(basis)
        return [station for station in self._zones.all_station_order() if index.get(station) == zone]

    def coexistence(self) -> dict:
        """汇总并存期两套归属，供控制台与状态快照读取。"""

        if not self.active():
            return {"active": False}
        plan = self._plan
        assert plan is not None
        legacy = self.legacy_index()
        target = self.target_index()
        dual = {
            station: {"legacy": legacy[station], "target": target[station]}
            for station in plan.assigned()
            if legacy.get(station) != target.get(station)
        }
        return {
            "active": True,
            "migration_id": plan.migration_id,
            "source_zone": plan.source_zone,
            "new_zone": plan.new_zone,
            "movers": list(plan.movers),
            "stayers": list(plan.stayers),
            "dual_read": dual,
        }
