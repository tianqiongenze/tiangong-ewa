Feature: 安全运行时与人工确认（SPEC-002）
  作为安全专员
  我希望机器人的每个动作都经过安全审查
  从而杜绝人身与设备事故

  Background:
    Given 安全区：高压配电房为禁入区、人机交叉区为停止区、人行通道为限速区

  Scenario: 禁入区物理拒绝
    When 审查动作 MOVE 目标 (55, 55)
    Then 系统抛出 R1 红线错误
    And 对应任务状态为 ABORTED

  Scenario: 超载必须人工令牌
    When 审查动作 TRANSPORT payload 80kg 且无令牌
    Then 系统抛出 R2 红线错误
    When 同一动作携带令牌 HT-2026-0001
    Then 审查通过

  Scenario: 停止区有人立即停机
    Given 停止区遥测显示有人员
    When 审查任意 MOVE 动作
    Then 返回停机裁定 speed_limit = 0
    And 原因包含 "R4"
    And 任务状态为 FAILED 等待人工确认

  Scenario: 限速区降速
    When 审查动作 MOVE 目标 (25, 14)（人行通道限速区）
    Then 返回 speed_limit = 0.3

  Scenario: 急停抢占
    When 安全专员对 AMR-01 触发急停
    Then AMR-01 进入 stopped 集合
    And 后续对该机器人的执行全部失败并留因

  Scenario: LLM 失效时安全仍生效
    Given LLM 规划器不可用（降级为规则规划器）
    When 审查禁入区动作
    Then 仍然拒绝（安全不依赖 LLM）
