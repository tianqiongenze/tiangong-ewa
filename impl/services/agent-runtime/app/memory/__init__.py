"""Hermes 式三层记忆（由 Diting-PdM 同构迁移）。

| 层 | 内容 | 生命周期 | 本项目用途 |
|---|---|---|---|
| 工作记忆 WorkingMemory | 当前作业上下文（作业单/任务/机群占用） | 会话级 | 编排器热路径 |
| 情景记忆 EpisodicMemory | 历史 episode 索引（成功/失败/降级） | 持久 | 技能迭代依据 |
| 技能记忆 SkillMemory | 技能四态 + 生效版本 + 灰度 | 受控 | 执行引用（入证据链） |

纪律：三层都只追加/受控变更；情景记忆按 skill×outcome 分片；
工作记忆溢出时下沉为情景记忆（freeze）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from tiangong_core import SkillRegistry


@dataclass
class WorkingMemory:
    """工作记忆：当前进行中的作业。"""

    max_items: int = 200
    _orders: dict[str, dict[str, Any]] = field(default_factory=dict)

    def put(self, order_id: str, snapshot: dict[str, Any]) -> None:
        if len(self._orders) >= self.max_items and order_id not in self._orders:
            oldest = next(iter(self._orders))
            self.freeze(oldest)
        self._orders[order_id] = {"snapshot": snapshot, "at": time.time()}

    def get(self, order_id: str) -> dict[str, Any] | None:
        return self._orders.get(order_id)

    def active(self) -> list[str]:
        return list(self._orders)

    def freeze(self, order_id: str) -> dict[str, Any] | None:
        """下沉为情景记忆的原料。"""
        return self._orders.pop(order_id, None)

    def __len__(self) -> int:
        return len(self._orders)


@dataclass
class EpisodicMemory:
    """情景记忆：episode 索引（skill × outcome 分片 + 相似检索）。"""

    _index: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def remember(self, episode: Any) -> None:
        key = episode.shard_key()
        self._index.setdefault(key, []).append({
            "episode_id": episode.episode_id,
            "task_id": episode.task_id,
            "robot_id": episode.robot_id,
            "duration_s": episode.duration_s,
            "consent_id": episode.consent_id,
        })

    def recall(self, skill_id: str, success: bool = True,
               limit: int = 10) -> list[dict[str, Any]]:
        key = f"{skill_id}/{'ok' if success else 'fail'}"
        return self._index.get(key, [])[-limit:]

    def success_rate(self, skill_id: str) -> float | None:
        ok = len(self.recall(skill_id, True, limit=10**9))
        bad = len(self.recall(skill_id, False, limit=10**9))
        if ok + bad == 0:
            return None
        return round(ok / (ok + bad), 4)

    def shards(self) -> dict[str, int]:
        return {k: len(v) for k, v in self._index.items()}


@dataclass
class SkillMemory:
    """技能记忆：四态治理 + 生效版本。"""

    registry: SkillRegistry = field(default_factory=SkillRegistry)

    def active(self, skill_id: str) -> str | None:
        v = self.registry.active(skill_id)
        return f"{skill_id}@{v.version}" if v else None

    def states(self) -> dict[str, dict[str, str]]:
        return {s.skill_id: {v.version: v.state for v in s.versions}
                for s in self.registry.all()}


class HermesMemory:
    """三层记忆门面。"""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.working = WorkingMemory()
        self.episodic = EpisodicMemory()
        self.skill = SkillMemory(registry or SkillRegistry())

    def consolidate(self, order_id: str) -> dict[str, Any] | None:
        """工作记忆 → 情景记忆（结算时调用）。"""
        snap = self.working.freeze(order_id)
        return snap

    def stats(self) -> dict[str, Any]:
        return {"working": len(self.working), "active_orders": self.working.active(),
                "episodic_shards": self.episodic.shards(),
                "skill_states": self.skill.states()}
