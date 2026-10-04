"""拆分计划：拆之前把新老分区该继承什么、哪些站归谁一次算清。

计划是纯计算：不改分区表、不碰索引，只产出一份可以核对、可以落审计的
:class:`SplitPlan`。继承方式定死为整区继承（``INHERIT_ZONE``）：新分区整体继承
源分区的类型与设定点，拆分时不接受逐站指定设定点——分区表是设定点的唯一事实
源，逐站私定会在拆分瞬间制造出两套口径，正是要避免的"各说各话"。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import NamespaceError
from ..ns.zone import ZoneRegistry

INHERIT_ZONE = "zone"

CONTROL = "control"
REPORT = "report"

VIEWS = ("pre", "post")

# 并存期默认口径：调质／调度按新分区，报表／结算本期仍按老分区（期末收口后切到新分区）。
DEFAULT_READ_POLICY = {CONTROL: "post", REPORT: "pre"}


@dataclass(frozen=True)
class SplitPlan:
    """一次拆分在动手前算清的全部事实。"""

    migration_id: str
    source_zone: str
    new_zone: str
    moving: tuple[str, ...]
    staying: tuple[str, ...]
    inheritance: str
    inherited: dict
    read_policy: dict
    pre_index: dict
    post_index: dict
    created_ts: float

    def as_dict(self) -> dict:
        return {
            "migration_id": self.migration_id,
            "source_zone": self.source_zone,
            "new_zone": self.new_zone,
            "moving": list(self.moving),
            "staying": list(self.staying),
            "inheritance": self.inheritance,
            "inherited": dict(self.inherited),
            "read_policy": {zone: dict(purposes) for zone, purposes in self.read_policy.items()},
            "pre_index": dict(sorted(self.pre_index.items())),
            "post_index": dict(sorted(self.post_index.items())),
            "created_ts": round(self.created_ts, 3),
        }


def plan_split(
    zones: ZoneRegistry,
    migration_id: str,
    source_name: str,
    station_names: list[str],
    new_name: str,
    now: float,
    read_policy: dict | None = None,
) -> SplitPlan:
    """一次算清：继承内容、站归属、新老两套索引与并存期口径。

    站名单必须全部属于源分区——拆分不是过滤，名单里混进别区的站说明计划本身
    没算清，当场拒绝，而不是静默丢掉。
    """

    source = zones.get(source_name)
    if not new_name:
        raise NamespaceError("新分区必须有名字", component="migration")
    if new_name == source_name:
        raise NamespaceError("拆分后的分区名不能与原分区相同", component="migration")
    if new_name in zones.names():
        raise NamespaceError(f"分区已存在: {new_name}", component="migration")

    moving = list(dict.fromkeys(str(name) for name in station_names))
    if not moving:
        raise NamespaceError("拆分必须至少指定一个站", component="migration")
    outsiders = [name for name in moving if name not in source.stations]
    if outsiders:
        raise NamespaceError(
            f"站不属于源分区 {source_name}: {', '.join(outsiders)}", component="migration"
        )

    staying = [name for name in source.stations if name not in moving]
    pre_index = zones.index()
    post_index = dict(pre_index)
    for name in moving:
        post_index[name] = new_name

    policy = {zone: dict(DEFAULT_READ_POLICY) for zone in (source_name, new_name)}
    for zone, purposes in (read_policy or {}).items():
        if zone not in policy:
            raise NamespaceError(f"口径只能针对受影响分区: {zone}", component="migration")
        for purpose, view in purposes.items():
            if purpose not in DEFAULT_READ_POLICY or view not in VIEWS:
                raise NamespaceError(
                    f"非法口径: {zone}.{purpose}={view}", component="migration"
                )
            policy[zone][purpose] = view

    return SplitPlan(
        migration_id=migration_id,
        source_zone=source_name,
        new_zone=new_name,
        moving=tuple(moving),
        staying=tuple(staying),
        inheritance=INHERIT_ZONE,
        inherited={"kind": source.kind, "setpoint_c": source.setpoint_c},
        read_policy=policy,
        pre_index=pre_index,
        post_index=post_index,
        created_ts=float(now),
    )
