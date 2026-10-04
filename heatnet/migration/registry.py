"""迁移登记处：执行拆分计划、维护双读视图、回滚与跨重启恢复。

归属变更一律走这里：计划（plan）→ 应用（apply）→ 核对 → 收口（commit）或
回滚（rollback）。并存期内新老两套归属同时可读；按哪一套算由记录里每个分区
的口径决定，读者不许自行发挥。同一时刻只允许一个迁移处于并存期，避免多套
新旧口径交织在一起核对不清。记录落盘到 ``migration/<id>``，跨重启恢复时与
当前分区表对不上的活跃迁移会被标记 ``stale``：留档供审计，不再参与解析。
"""

from __future__ import annotations

from ..errors import InterlockError, NamespaceError
from ..ns.zone import Zone, ZoneRegistry
from ..station.mapping import StationMapping
from ..store.jsonfile import JsonStore
from .plan import CONTROL, SplitPlan, plan_split
from .record import APPLIED, MigrationRecord


class MigrationRegistry:
    """分区拆分迁移登记处。"""

    def __init__(self, zones: ZoneRegistry, mapping: StationMapping, store: JsonStore) -> None:
        self._zones = zones
        self._mapping = mapping
        self._store = store
        self._records: dict[str, MigrationRecord] = {}
        self._issued = 0

    # ---- 生命周期 -----------------------------------------------------

    def plan(
        self,
        source_name: str,
        station_names: list[str],
        new_name: str,
        now: float,
        read_policy: dict | None = None,
    ) -> SplitPlan:
        """干跑一次拆分计划：一次算清，不改任何归属。"""

        active = self.active()
        if active is not None:
            raise InterlockError(
                f"迁移 {active.migration_id} 仍在并存期，先收口或回滚再计划下一次拆分",
                component="migration",
            )
        return plan_split(
            self._zones,
            f"mig-{self._issued + 1:04d}",
            source_name,
            station_names,
            new_name,
            now,
            read_policy,
        )

    def apply(self, plan: SplitPlan, now: float, fingerprint: dict) -> tuple[MigrationRecord, Zone]:
        """按计划执行拆分：改分区表、重建调度索引、台账进入并存期。"""

        if plan.migration_id in self._records:
            raise InterlockError(
                f"迁移计划已应用过: {plan.migration_id}", component="migration"
            )
        record = MigrationRecord.from_plan(plan)
        child = self._zones.split(plan.source_zone, list(plan.moving), plan.new_zone)
        self._mapping.rebuild()
        record.mark_applied(now, self._mapping.generation(), fingerprint)
        self._records[record.migration_id] = record
        self._issued = max(self._issued, _seq_of(record.migration_id))
        self.persist(record)
        return record, child

    def rollback(self, migration_id: str, now: float) -> tuple[MigrationRecord, Zone]:
        """回滚到拆分前归属：站并回源分区、重建索引，回滚只有一次。"""

        record = self.get(migration_id)
        if record.state != APPLIED:
            raise InterlockError(
                f"迁移 {migration_id} 当前状态 {record.state}，不能回滚", component="migration"
            )
        source = self._zones.merge(record.source_zone, record.new_zone)
        self._mapping.rebuild()
        record.mark_rolled_back(now)
        self.persist(record)
        return record, source

    def commit(self, migration_id: str, now: float) -> MigrationRecord:
        """结束并存期：报表口径切到新分区，之后不能再回滚。"""

        record = self.get(migration_id)
        record.mark_committed(now)
        self.persist(record)
        return record

    # ---- 归属解析 -----------------------------------------------------

    def active(self) -> MigrationRecord | None:
        """返回当前处于并存期的迁移（没有则返回 ``None``）。"""

        for record in self._records.values():
            if record.state == APPLIED:
                return record
        return None

    def get(self, migration_id: str) -> MigrationRecord:
        """按编号取迁移记录。"""

        try:
            return self._records[migration_id]
        except KeyError as error:
            raise NamespaceError(f"未登记迁移: {migration_id}", component="migration") from error

    def index_for(self, view: str) -> dict[str, str]:
        """显式取某一套归属索引（``pre``／``post``），供报表按口径汇总。"""

        if view == "pre":
            record = self.active()
            return dict(record.pre_index) if record is not None else self._mapping.index()
        if view == "post":
            return self._mapping.index()
        raise ValueError("view 只能是 pre 或 post")

    def zone_of(self, station: str, view: str | None = None, purpose: str = CONTROL) -> str:
        """按站解析归属。

        ``view`` 显式指定读哪一套（并存期两套同时可读）；不指定时按迁移记录里
        该站所属分区为 ``purpose``（调质／报表）定死的口径解析。没有活跃迁移时
        直接读调度索引。
        """

        if view is not None:
            index = self.index_for(view)
            try:
                return index[station]
            except KeyError as error:
                raise NamespaceError(
                    f"站未进入归属索引: {station}", component="migration"
                ) from error
        record = self.active()
        if record is None:
            return self._mapping.zone_of(station)
        post_zone = record.post_index.get(station)
        if post_zone is None:
            return self._mapping.zone_of(station)
        chosen = record.read_policy.get(post_zone, {}).get(purpose, "post")
        index = record.pre_index if chosen == "pre" else record.post_index
        return index.get(station, post_zone)

    # ---- 持久化 -------------------------------------------------------

    def persist(self, record: MigrationRecord) -> None:
        """把一条记录落盘。"""

        self._store.write(f"migration/{record.migration_id}", record.as_dict())

    def restore(self) -> list[str]:
        """从落盘恢复台账；活跃迁移与现实对不上的标记 ``stale``，返回其编号。"""

        stale: list[str] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith("migration/"):
                continue
            raw = self._store.read(doc_id)
            if not isinstance(raw, dict):
                continue
            record = MigrationRecord.from_dict(raw)
            self._records[record.migration_id] = record
            self._issued = max(self._issued, _seq_of(record.migration_id))
            if record.state == APPLIED and not self._matches_world(record):
                record.mark_stale()
                self.persist(record)
                stale.append(record.migration_id)
        return stale

    def _matches_world(self, record: MigrationRecord) -> bool:
        """活跃迁移描述的世界与当前分区表是否吻合。"""

        try:
            zone = self._zones.get(record.new_zone)
        except NamespaceError:
            return False
        return all(name in zone.stations for name in record.moving)

    def describe(self) -> dict:
        """汇总台账，供状态快照与控制台读取。"""

        active = self.active()
        return {
            "count": len(self._records),
            "active": active.migration_id if active else None,
            "records": [self._records[key].as_dict() for key in sorted(self._records)],
            "views": (
                {
                    "pre": dict(sorted(active.pre_index.items())),
                    "post": dict(sorted(active.post_index.items())),
                }
                if active
                else None
            ),
        }


def _seq_of(migration_id: str) -> int:
    """从 ``mig-0007`` 形式的编号里取出序号。"""

    try:
        return int(str(migration_id).rsplit("-", 1)[-1])
    except ValueError:
        return 0
