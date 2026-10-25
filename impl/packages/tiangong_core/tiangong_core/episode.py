"""数据飞轮：实景 episode 采集（2026 = 具身智能"数据元年"）。

每次真实作业都是一条训练数据。episode = (观测序列, 动作序列, 结果)。
采集纪律：
- 只采集**受控资产**范围内的数据（脱敏、无人脸、无商业秘密）
- 每条 episode 必须带 consent_id（数据授权单）与 skill/model 版本
- 打包成 shard，供训练场/数据服务消费（docs/04）
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Episode:
    """一次实景作业的记录。"""

    episode_id: str
    task_id: str
    robot_id: str
    skill_id: str
    skill_version: str
    model_version: str
    success: bool
    duration_s: float
    observations: tuple[Mapping[str, Any], ...] = ()
    actions: tuple[Mapping[str, Any], ...] = ()
    outcome: Mapping[str, Any] = field(default_factory=dict)
    consent_id: str = ""          # 数据授权单（无授权不得入库）

    def shard_key(self) -> str:
        """分片键：任务类型 + 成功标志（按场景聚合）。"""
        return f"{self.skill_id}/{'ok' if self.success else 'fail'}"

    def checksum(self) -> str:
        payload = json.dumps({
            "episode_id": self.episode_id,
            "task_id": self.task_id,
            "actions": [dict(a) for a in self.actions],
            "outcome": dict(self.outcome),
        }, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]


class EpisodeRecorder:
    """episode 记录器（内存版；生产换消息队列 + 对象存储）。"""

    def __init__(self) -> None:
        self._episodes: list[Episode] = []

    def record(self, ep: Episode) -> Episode:
        if not ep.consent_id:
            raise ValueError(f"episode {ep.episode_id} 缺 consent_id，拒绝入库")
        self._episodes.append(ep)
        return ep

    def by_skill(self, skill_id: str) -> list[Episode]:
        return [e for e in self._episodes if e.skill_id == skill_id]

    def success_rate(self, skill_id: str) -> float | None:
        eps = self.by_skill(skill_id)
        if not eps:
            return None
        return sum(1 for e in eps if e.success) / len(eps)

    def shards(self) -> dict[str, list[Episode]]:
        out: dict[str, list[Episode]] = {}
        for e in self._episodes:
            out.setdefault(e.shard_key(), []).append(e)
        return out

    def export_shard(self, key: str) -> str:
        """导出一个分片为 JSONL 文本（供数据服务消费）。"""
        lines = [json.dumps({
            "episode_id": e.episode_id,
            "task_id": e.task_id,
            "skill": f"{e.skill_id}@{e.skill_version}",
            "model": e.model_version,
            "success": e.success,
            "duration_s": e.duration_s,
            "checksum": e.checksum(),
            "consent_id": e.consent_id,
        }, ensure_ascii=False) for e in self.shards().get(key, [])]
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._episodes)
