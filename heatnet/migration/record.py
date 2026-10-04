"""迁移记录：一次拆分归属变更的生命周期与历史指纹。

状态机 ``planned → applied → committed | rolled_back``。回退就是回滚：只有
``applied``（并存期）状态能回滚，回滚后记录定格为 ``rolled_back``，再次回滚
直接拒绝——归属变更不会被执行两次，热量自然不会被重复计数。``stale`` 只出现在
跨重启恢复时：分区表已经回不到记录所描述的世界，记录留档供审计，但不再参与
归属解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import InterlockError
from .plan import REPORT, SplitPlan

PLANNED = "planned"
APPLIED = "applied"
COMMITTED = "committed"
ROLLED_BACK = "rolled_back"
STALE = "stale"
STATES = (PLANNED, APPLIED, COMMITTED, ROLLED_BACK, STALE)


@dataclass
class MigrationRecord:
    """一条迁移台账。"""

    migration_id: str
    source_zone: str
    new_zone: str
    moving: list
    staying: list
    inheritance: str
    inherited: dict
    read_policy: dict
    pre_index: dict
    post_index: dict
    created_ts: float
    state: str = PLANNED
    applied_ts: float | None = None
    closed_ts: float | None = None
    post_generation: int = 0
    fingerprint: dict = field(default_factory=dict)
    reconciles: int = 0

    @classmethod
    def from_plan(cls, plan: SplitPlan) -> "MigrationRecord":
        """把拆分计划落成一条待应用的记录。"""

        return cls(
            migration_id=plan.migration_id,
            source_zone=plan.source_zone,
            new_zone=plan.new_zone,
            moving=list(plan.moving),
            staying=list(plan.staying),
            inheritance=plan.inheritance,
            inherited=dict(plan.inherited),
            read_policy={zone: dict(purposes) for zone, purposes in plan.read_policy.items()},
            pre_index=dict(plan.pre_index),
            post_index=dict(plan.post_index),
            created_ts=plan.created_ts,
        )

    @classmethod
    def from_dict(cls, raw: dict) -> "MigrationRecord":
        """从落盘字段还原一条记录。"""

        return cls(
            migration_id=str(raw.get("migration_id", "")),
            source_zone=str(raw.get("source_zone", "")),
            new_zone=str(raw.get("new_zone", "")),
            moving=list(raw.get("moving", [])),
            staying=list(raw.get("staying", [])),
            inheritance=str(raw.get("inheritance", "")),
            inherited=dict(raw.get("inherited", {})),
            read_policy={zone: dict(p) for zone, p in dict(raw.get("read_policy", {})).items()},
            pre_index=dict(raw.get("pre_index", {})),
            post_index=dict(raw.get("post_index", {})),
            created_ts=float(raw.get("created_ts", 0.0)),
            state=str(raw.get("state", PLANNED)),
            applied_ts=raw.get("applied_ts"),
            closed_ts=raw.get("closed_ts"),
            post_generation=int(raw.get("post_generation", 0)),
            fingerprint=dict(raw.get("fingerprint", {})),
            reconciles=int(raw.get("reconciles", 0)),
        )

    def mark_applied(self, now: float, generation: int, fingerprint: dict) -> None:
        """进入并存期：记下应用时刻、调度索引代际与历史指纹。"""

        if self.state != PLANNED:
            raise InterlockError(
                f"迁移 {self.migration_id} 当前状态 {self.state}，不能重复应用",
                component="migration",
            )
        self.state = APPLIED
        self.applied_ts = float(now)
        self.post_generation = int(generation)
        self.fingerprint = dict(fingerprint)

    def mark_committed(self, now: float) -> None:
        """结束并存期：报表口径切到新分区，之后不能再回滚。"""

        if self.state != APPLIED:
            raise InterlockError(
                f"迁移 {self.migration_id} 当前状态 {self.state}，不能收口",
                component="migration",
            )
        self.state = COMMITTED
        self.closed_ts = float(now)
        for purposes in self.read_policy.values():
            purposes[REPORT] = "post"

    def mark_rolled_back(self, now: float) -> None:
        """回滚到拆分前归属；只有并存期能回滚，回滚只有一次。"""

        if self.state != APPLIED:
            raise InterlockError(
                f"迁移 {self.migration_id} 当前状态 {self.state}，不能回滚",
                component="migration",
            )
        self.state = ROLLED_BACK
        self.closed_ts = float(now)

    def mark_stale(self) -> None:
        """恢复时发现分区表已不在记录描述的世界，留档不再参与解析。"""

        if self.state == APPLIED:
            self.state = STALE

    def note_reconcile(self) -> int:
        """累计一次核对（核对次数进台账，供审计对照）。"""

        self.reconciles += 1
        return self.reconciles

    def as_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "source_zone": self.source_zone,
            "new_zone": self.new_zone,
            "moving": list(self.moving),
            "staying": list(self.staying),
            "inheritance": self.inheritance,
            "inherited": dict(self.inherited),
            "read_policy": {zone: dict(p) for zone, p in self.read_policy.items()},
            "pre_index": dict(sorted(self.pre_index.items())),
            "post_index": dict(sorted(self.post_index.items())),
            "created_ts": round(self.created_ts, 3),
            "state": self.state,
            "applied_ts": self.applied_ts,
            "closed_ts": self.closed_ts,
            "post_generation": self.post_generation,
            "fingerprint": dict(self.fingerprint),
            "reconciles": self.reconciles,
        }
