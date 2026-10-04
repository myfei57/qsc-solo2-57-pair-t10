"""分区拆分迁移：计划、双读、核对与回滚。

管网改造把一个供热分区拆成两块时，归属变更一律按迁移走完整生命周期：
计划（一次算清继承与站归属）→ 应用（进入新旧并存期）→ 核对 → 收口或回滚。
并存期内两套归属同时可读，按哪一套算由迁移记录里每个分区的口径决定；
历史记录不可变，不跟着新分区改；回退就是回滚，不会重复计数。
"""

from .plan import CONTROL, INHERIT_ZONE, REPORT, SplitPlan, plan_split
from .record import MigrationRecord
from .reconcile import run_reconcile, zone_totals
from .registry import MigrationRegistry

__all__ = [
    "CONTROL",
    "INHERIT_ZONE",
    "REPORT",
    "MigrationRecord",
    "MigrationRegistry",
    "SplitPlan",
    "plan_split",
    "run_reconcile",
    "zone_totals",
]
