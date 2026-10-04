"""不可变热量历史。

热量行一旦写下就是历史：只追加，不改写、不删除。每行记录*记录当时*的归属
``recorded_zone``（该按哪套口径算就在写的时候定死），迁移期间再带上迁移 id 与
所用口径，所以"调质按新分区、报表按老分区"时两边读的是同一批行、只是过滤口径
不同，不需要回头去改老数据。

为了能核出"历史被跟着新分区改写"，整段历史用哈希链封存：每行的链摘要由上一行
摘要与本行正文算出，任何插入、删除、篡改都会让链校验失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

GENESIS = "0"

# 参与哈希的正文键（顺序固定，保证跨进程复算一致）。
_DIGEST_KEYS = (
    "seq",
    "station",
    "mwh",
    "recorded_zone",
    "ts",
    "migration_id",
    "basis",
    "period",
    "prev_digest",
)


def _digest_of(payload: dict) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HeatLine:
    """一条不可变热量历史行。"""

    seq: int
    station: str
    mwh: float
    recorded_zone: str
    ts: float
    migration_id: str = ""
    basis: str = ""
    period: int = 0
    prev_digest: str = GENESIS
    digest: str = GENESIS

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "station": self.station,
            "mwh": round(self.mwh, 4),
            "recorded_zone": self.recorded_zone,
            "ts": round(self.ts, 3),
            "migration_id": self.migration_id,
            "basis": self.basis,
            "period": self.period,
            "prev_digest": self.prev_digest,
            "digest": self.digest,
        }


class HeatHistory:
    """追加写、哈希链封存的热量历史。"""

    def __init__(self) -> None:
        self._lines: list[HeatLine] = []

    def append(
        self,
        station: str,
        mwh: float,
        recorded_zone: str,
        ts: float,
        *,
        migration_id: str = "",
        basis: str = "",
        period: int = 0,
    ) -> HeatLine:
        """追加一行历史；迁移期间由调用方带上迁移 id 与口径。"""

        value = max(0.0, float(mwh))
        seq = len(self._lines) + 1
        prev = self._lines[-1].digest if self._lines else GENESIS
        body = {
            "seq": seq,
            "station": str(station),
            "mwh": round(value, 4),
            "recorded_zone": str(recorded_zone),
            "ts": round(float(ts), 3),
            "migration_id": str(migration_id),
            "basis": str(basis),
            "period": int(period),
            "prev_digest": prev,
        }
        line = HeatLine(**body, digest=_digest_of({key: body[key] for key in _DIGEST_KEYS}))
        self._lines.append(line)
        return line

    def lines(self) -> list[HeatLine]:
        """返回全部历史行（按写入顺序）。"""

        return list(self._lines)

    def __len__(self) -> int:
        return len(self._lines)

    def total_mwh(self) -> float:
        """历史累计热量。"""

        return sum(line.mwh for line in self._lines)

    def zones_seen(self) -> list[str]:
        """历史里出现过的全部记录时归属（排序后）。"""

        return sorted({line.recorded_zone for line in self._lines})

    def verify_chain(self) -> dict:
        """重算整条哈希链，返回是否完整以及首个断点位置。"""

        prev = GENESIS
        for index, line in enumerate(self._lines):
            body = {
                "seq": line.seq,
                "station": line.station,
                "mwh": round(line.mwh, 4),
                "recorded_zone": line.recorded_zone,
                "ts": round(line.ts, 3),
                "migration_id": line.migration_id,
                "basis": line.basis,
                "period": line.period,
                "prev_digest": prev,
            }
            expected = _digest_of({key: body[key] for key in _DIGEST_KEYS})
            if line.prev_digest != prev or line.digest != expected:
                return {"ok": False, "broken_at": index + 1, "count": len(self._lines)}
            prev = line.digest
        return {"ok": True, "broken_at": None, "count": len(self._lines)}

    def restore(self, raw_lines: list[dict]) -> dict:
        """从落盘字段重建历史并立即验链（跨重启恢复用）。"""

        self._lines = []
        for raw in raw_lines:
            self._lines.append(
                HeatLine(
                    seq=int(raw["seq"]),
                    station=str(raw["station"]),
                    mwh=float(raw["mwh"]),
                    recorded_zone=str(raw["recorded_zone"]),
                    ts=float(raw["ts"]),
                    migration_id=str(raw.get("migration_id", "")),
                    basis=str(raw.get("basis", "")),
                    period=int(raw.get("period", 0)),
                    prev_digest=str(raw.get("prev_digest", GENESIS)),
                    digest=str(raw.get("digest", GENESIS)),
                )
            )
        return self.verify_chain()
