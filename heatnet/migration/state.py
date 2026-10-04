"""迁移计划与迁移状态。

计划在拆分提交那一刻冻结：它记录源分区、新分区、逐站去向、设定点快照与回退
所需的拆分前快照。迁移状态机只有四个状态：

``prepared``    拆分已提交、并存视图已建立（``prepare`` 的终态即进入并存）；
``migrating``   新旧并存，两套归属可读，允许核对与回退；
``completed``    切换完成，老口径归档，只允许 target，回退窗口关闭；
``rolled_back``  已整体回滚到拆分前归属，终态。

状态只沿 ``prepared → migrating → completed`` 前进，或从并存态转到
``rolled_back``；任何越界跳转都按联锁失败拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field

STATE_NONE = "none"
STATE_PREPARED = "prepared"
STATE_MIGRATING = "migrating"
STATE_COMPLETED = "completed"
STATE_ROLLED_BACK = "rolled_back"

# 允许的状态前进/回退路径。
_TRANSITIONS: dict[str, frozenset[str]] = {
    STATE_NONE: frozenset({STATE_MIGRATING}),
    STATE_PREPARED: frozenset({STATE_MIGRATING, STATE_ROLLED_BACK}),
    STATE_MIGRATING: frozenset({STATE_COMPLETED, STATE_ROLLED_BACK}),
    STATE_COMPLETED: frozenset(),
    STATE_ROLLED_BACK: frozenset(),
}


@dataclass(frozen=True)
class MigrationPlan:
    """一次拆分在提交时冻结的计划（逐站指定，无整区通配）。"""

    migration_id: str
    source_zone: str
    new_zone: str
    movers: tuple[str, ...]
    stayers: tuple[str, ...]
    source_setpoint_c: float
    new_setpoint_c: float
    station_setpoints_c: dict[str, float] = field(default_factory=dict)
    cutover_ts: float = 0.0

    def assigned(self) -> tuple[str, ...]:
        """计划里显式指定了去向的全部站（movers ∪ stayers）。"""

        return tuple(self.movers) + tuple(self.stayers)

    def as_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "source_zone": self.source_zone,
            "new_zone": self.new_zone,
            "movers": list(self.movers),
            "stayers": list(self.stayers),
            "source_setpoint_c": round(self.source_setpoint_c, 2),
            "new_setpoint_c": round(self.new_setpoint_c, 2),
            "station_setpoints_c": {
                name: round(value, 2) for name, value in sorted(self.station_setpoints_c.items())
            },
            "cutover_ts": round(self.cutover_ts, 3),
        }


@dataclass(frozen=True)
class MigrationState:
    """迁移状态机当前所处状态。"""

    value: str

    def __post_init__(self) -> None:
        if self.value not in _TRANSITIONS:
            raise ValueError(f"未知迁移状态: {self.value}")

    def can_transition_to(self, target: str) -> bool:
        """是否允许转到 ``target``。"""

        return target in _TRANSITIONS.get(self.value, frozenset())

    @property
    def active(self) -> bool:
        """是否处于并存期（两套归属同时可读、可回退）。"""

        return self.value in (STATE_PREPARED, STATE_MIGRATING)

    @property
    def finished(self) -> bool:
        """是否已经终态（完成或回退）。"""

        return self.value in (STATE_COMPLETED, STATE_ROLLED_BACK)

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "active": self.active,
            "finished": self.finished,
            "allow_rollback": self.can_transition_to(STATE_ROLLED_BACK),
            "allow_complete": self.can_transition_to(STATE_COMPLETED),
        }
