Feature: 搬运作业闭环（SPEC-001）
  作为设备工程师
  我希望用一句话下单搬运并看到带证据的结果
  从而不用写程序、出事故能定责

  Background:
    Given 演示厂区已装配（4 台机器人、3 个安全区、3 个已审批技能）
    And 人工令牌簿为空

  Scenario: 正常搬运 35kg 电池包
    When 提交作业单 TRANSPORT {from: 原料库, to: 装配车间, weight_kg: 35, x: 25, y: 14}
    Then 系统分解出 3 个任务：PICK → TRANSPORT → PLACE
    And 全部任务状态为 PENDING
    When 设备科-老周审批
    Then 全部任务状态为 APPROVED
    When 执行
    Then 全部任务状态为 DONE
    And 每个任务证据含 trace、telemetry、skill_version
    And 记录 3 条 episode（consent_id 为 CONSENT-WO-*）
    And 报告 degraded 为 false

  Scenario: 目标在高压配电房（禁入区）
    When 提交作业单 TRANSPORT {weight_kg: 10, x: 55, y: 55}
    And 审批并执行
    Then PLACE 任务状态为 ABORTED
    And 中止原因包含 "R1"
    And 报告 aborted 为 true

  Scenario: 80kg 超载无人工令牌
    When 提交作业单 TRANSPORT {weight_kg: 80, x: 25, y: 14}
    And 审批并执行
    Then 任务被拒绝且原因包含 "R2"

  Scenario: 80kg 超载有人工令牌
    Given 令牌簿登记 WO-H2 → HT-2026-0001
    When 提交作业单 WO-H2 TRANSPORT {weight_kg: 80, x: 25, y: 14}
    And 审批并执行
    Then 全部任务状态为 DONE

  Scenario: 机器人连续失败触发降级
    Given 全部机器人被注入失败
    When 提交并审批并执行 TRANSPORT {weight_kg: 10, x: 25, y: 14}
    Then 报告 degraded 为 true
    And 降级级数达到 L3_TELEOP 或 L4_HOLD

  Scenario: 停止区有人时停机
    Given 停止区检测到人员
    When 提交并审批并执行 TRANSPORT {weight_kg: 10, x: 25, y: 14}
    Then 报告 safety_halted 为 true
    And 任务状态为 FAILED 且原因包含 "R4"

  Scenario: 未审批不得执行
    When 提交作业单 TRANSPORT {weight_kg: 10, x: 25, y: 14}
    And 不审批直接执行
    Then 系统返回 400 且提示需先审批
