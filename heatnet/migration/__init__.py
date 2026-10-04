"""分区拆分迁移：归属、口径、核对、回退与不可变历史。

拆分不是一次性改表，而是一次*迁移*：

``prepared``（拆分提交，进入并存）→ ``migrating``（新旧并存、双归属可读）→
``completed``（切换完成、老口径归档）；并存期可 ``rolled_back``（整体回滚）。

本包把规则定死在一处，避免"调质按新分区、报表按老分区"各说各话：

* 继承一律**逐站指定**：movers/stayers 必须构成源分区站的一个划分，没有整区通配；
  新分区设定点是源分区设定点的一次性快照，拆后两边独立。
* 迁移期间两套归属（``legacy`` 老分区 / ``target`` 新分区）同时可读，并存期
  按分区各自选定口径；单口径必须是全体站的一个划分（不重不漏），两套口径总量
  必须相等，**禁止跨口径合计**。
* 历史记录（热量行）只追加、带记录时归属并用哈希链封存，拆分/回退/核对都不改写。
* 回退只回归属、不重放热量，因此不重复计数。
"""

from __future__ import annotations

from .heat_history import HeatHistory, HeatLine
from .manager import MigrationManager
from .membership import BASIS_LEGACY, BASIS_TARGET, MigrationBasis, Membership
from .reconcile import reconcile
from .state import MigrationPlan, MigrationState

__all__ = [
    "BASIS_LEGACY",
    "BASIS_TARGET",
    "HeatHistory",
    "HeatLine",
    "Membership",
    "MigrationBasis",
    "MigrationManager",
    "MigrationPlan",
    "MigrationState",
    "reconcile",
]
