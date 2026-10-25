"""技能治理：版本化、审批、灰度、回滚（宪法第十条）。

技能是"受控资产"：变更需审批 → 灰度 → 可回滚。
四态与 Diting-PdM 保持一致：PENDING / APPROVED / REJECTED / SUPERSEDED。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from .models import Skill, SkillVersion


class SkillState:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class SkillError(ValueError):
    """技能治理违规（如未审批上线、回滚到不存在版本）。"""


class SkillRegistry:
    """内存技能注册表（生产可换持久化实现，接口不变）。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        if skill.skill_id in self._skills:
            raise SkillError(f"技能已存在: {skill.skill_id}（用 publish 新版本）")
        self._skills[skill.skill_id] = skill

    def publish(self, skill_id: str, version: str, entry: str,
                canary_pct: int = 0) -> SkillVersion:
        """发布新版本（初始 PENDING，需审批）。"""
        if skill_id not in self._skills:
            raise SkillError(f"技能未注册: {skill_id}")
        if any(v.version == version for v in self._skills[skill_id].versions):
            raise SkillError(f"版本已存在: {skill_id}@{version}")
        if not 0 <= canary_pct <= 100:
            raise SkillError(f"灰度比例非法: {canary_pct}")
        from dataclasses import replace
        v = SkillVersion(skill_id=skill_id, version=version, entry=entry,
                         canary_pct=canary_pct)
        self._skills[skill_id] = replace(
            self._skills[skill_id],
            versions=self._skills[skill_id].versions + (v,))
        return v

    def approve(self, skill_id: str, version: str, approved_by: str,
                canary_pct: Optional[int] = None) -> SkillVersion:
        """审批通过；同技能旧 APPROVED 版本自动 SUPERSEDED。"""
        skill = self._get(skill_id)
        found = None
        new_versions = []
        for v in skill.versions:
            if v.version == version:
                if v.state == SkillState.APPROVED:
                    raise SkillError(f"已审批: {version}")
                found = v
                new_versions.append(replace_v(v, state=SkillState.APPROVED,
                                              approved_by=approved_by,
                                              canary_pct=v.canary_pct if canary_pct is None else canary_pct))
            elif v.state == SkillState.APPROVED:
                new_versions.append(replace_v(v, state=SkillState.SUPERSEDED))
            else:
                new_versions.append(v)
        if found is None:
            raise SkillError(f"版本不存在: {skill_id}@{version}")
        self._skills[skill_id] = _replace_versions(skill, tuple(new_versions))
        return _find(self._skills[skill_id], version)

    def reject(self, skill_id: str, version: str) -> SkillVersion:
        skill = self._get(skill_id)
        return self._set_state(skill, version, SkillState.REJECTED)

    def rollback(self, skill_id: str, to_version: str) -> SkillVersion:
        """回滚：目标版本重新 APPROVED，当前生效版本 SUPERSEDED。"""
        skill = self._get(skill_id)
        target = _find(skill, to_version)
        if target is None:
            raise SkillError(f"回滚目标不存在: {to_version}")
        if target.state == SkillState.REJECTED:
            raise SkillError(f"不能回滚到已拒绝版本: {to_version}")
        cur = skill.active_version()
        if cur is not None and cur.version == to_version:
            raise SkillError(f"已在目标版本: {to_version}")
        if cur is not None:
            self._set_state(skill, cur.version, SkillState.SUPERSEDED)
        return self.approve(skill_id, to_version, approved_by="rollback")

    def active(self, skill_id: str) -> Optional[SkillVersion]:
        skill = self._skills.get(skill_id)
        return skill.active_version() if skill else None

    def get(self, skill_id: str) -> Skill:
        return self._get(skill_id)

    def all(self) -> Iterable[Skill]:
        return self._skills.values()

    # -- 内部 -------------------------------------------------------------
    def _get(self, skill_id: str) -> Skill:
        if skill_id not in self._skills:
            raise SkillError(f"技能未注册: {skill_id}")
        return self._skills[skill_id]

    def _set_state(self, skill: Skill, version: str, state: str) -> SkillVersion:
        found = None
        vs = []
        for v in skill.versions:
            if v.version == version:
                found = replace_v(v, state=state)
                vs.append(found)
            else:
                vs.append(v)
        if found is None:
            raise SkillError(f"版本不存在: {version}")
        self._skills[skill.skill_id] = _replace_versions(skill, tuple(vs))
        return found


def replace_v(v: SkillVersion, **kw: object) -> SkillVersion:
    from dataclasses import replace
    return replace(v, **kw)


def _replace_versions(skill: Skill, versions: tuple[SkillVersion, ...]) -> Skill:
    from dataclasses import replace
    return replace(skill, versions=versions)


def _find(skill: Skill, version: str) -> Optional[SkillVersion]:
    return next((v for v in skill.versions if v.version == version), None)
