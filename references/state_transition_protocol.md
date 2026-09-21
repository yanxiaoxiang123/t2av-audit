# 通用状态转换审核协议

本协议适用于人物、动物、车辆、容器、工具、流体、食物、文字载体及其他动态对象，不包含任何特定物品的专用判定。

## 1. 冻结原子要求

`prompt_checks` 中每条记录只能表达一个可验证命题。动作、属性、数量、空间关系、文字、对白、声音和时序分别建项；多个动作即使出现在同一句 Prompt 中也必须拆开。

每项包含：`requirement_id`、`claim_type`、稳定的 `subject_id`、单一 snake_case `predicate`、可空 `object_id`、`expected_before`、`expected_after`、`sequence_index`、精确的 `prompt_span` 和 `prompt_quote`、`metric_ids`。物理动作使用 `physical_action`，手物动作使用 `human_object_action`，并设置 `strict_transition_required=true`。

计划冻结后不得新增、删除或改写命题。发现拆分错误时应新建审核 run，不能在看过结果后修改计划。

## 2. 局部逐源帧状态转换

每个严格动作至少建立一个 `state_transition_check`。单窗口不超过 2 秒；较长动作拆成首尾相接的多个窗口。窗口必须同时包含稳定前态、变化过程和稳定后态，并提取窗口内每个源帧。

从最后一个稳定前态到第一个稳定后态之间的每张原图都必须单独打开。`viewed_frame_indices` 表示目视过的保存帧；`original_frames_opened` 与 `initial_evidence_frame_indices` 必须真实记录单独打开的原图，不能把“已提取”或“看过板”写成“已打开原图”。

连续性板只用于并排定位，最多 2×2。manifest 必须保存时间区间、ROI、网格、原始尺寸、源帧号、板路径和 SHA-256。全片概览板不能证明局部一致性。对象离开 ROI 时扩大 ROI 或使用全帧；遮挡不能自动解释去向。

## 3. 实体账本与九维检查

先固定实体和部件 ID，再逐帧记录 `before_state`、`during_state`、`after_state`、`allowed_changes`、`actual_changes` 和追踪状态。九个维度必须各出现一次：

1. `identity_count`
2. `part_structure`
3. `geometry_topology`
4. `contact_attachment`
5. `containment_occlusion`
6. `pose_anatomy`
7. `motion_force_causality`
8. `material_state`
9. `spatial_binding`

每维状态只能是 `not_applicable`、`applicable_consistent`、`applicable_uncertain` 或 `applicable_defect`。适用维度必须引用原始帧、写观察事实，并提出最强反例。反例包括但不限于身份替换、数量改变、部件消失或增生、几何突变、连接错误、穿插、遮挡后错位、因果倒置、物态异常和人体结构异常。

实体被遮挡或无法对应时标为 `occluded` 或 `unexplained`，相关维度为 `applicable_uncertain`；不能写 `confirmed_consistent`。

## 4. 冻结初审与自盲反证

初审文件只含观察、实体账本、维度结果和 `initial_verdict`，不含分数。冻结后才开始反证。

同一审核者的自盲反证必须满足至少一项：

- 单独打开初审未使用的新源帧；
- 对同一边界生成面积严格更小、位置包含于原 ROI 内的新板。

反证文件只含 `transition_id`、`method=self_blind`、`verdict`、观察、帧号及可选的新板 manifest。复核不得读取分数和最终理由。初审与复核冲突时，从原图建立 `conflict_resolution`；无法解决则最终 verdict 必须为 `uncertain`。

## 5. 严格评分传播

最终 verdict 为 `uncertain` 或 `confirmed_defect` 时，必须填写 `issue_categories` 和 `affected_metric_ids`。校验器会从异常维度、动作类型和多步骤上下文派生不可省略的类别，再传播到指标：

| 缺陷类别 | 必须影响 |
|---|---|
| `visible_structure` | VQ |
| `geometry_topology` | VQ、PH |
| `anatomy` | VQ、HM |
| `prompt_identity`、`prompt_attribute`、`prompt_action` | VF |
| `sequence`、`duration`、`completion_state` | TS |
| `contact_attachment`、`force_causality`、`material_state` | PH |
| `human_object_relation` | HM |
| `multi_step_process` | PS |

任何不确定的严格动作会派生 `prompt_action` 与 `sequence`；手物动作还派生 `human_object_relation`；包含多个严格动作时派生 `multi_step_process`。对应维度还会派生结构、几何、接触、人体、因果或物态类别。

`uncertain` 的所有受影响指标最高 3 分。确认缺陷必须填写严重度：`minor` 最高 4、`major` 最高 3、`critical` 最高 2。最终结果成功不能覆盖中间异常；同一发现影响多个指标时应分别说明，但不得机械重复缺陷。
