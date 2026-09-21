# 通用状态转换审核协议

本协议适用于人物、动物、车辆、容器、工具、流体、食物、文字载体及其他动态对象，不包含任何特定物品的专用判定。

## 1. 冻结原子要求

`prompt_checks` 中每条记录只能表达一个可验证命题。动作、属性、数量、空间关系、文字、对白、声音和时序分别建项；多个动作即使出现在同一句 Prompt 中也必须拆开。

每项包含：`requirement_id`、`claim_type`、稳定的 `subject_id`、单一 snake_case `predicate`、可空 `object_id`、`expected_before`、`expected_after`、`sequence_index`、精确的 `prompt_span` 和 `prompt_quote`、`metric_ids`。物理动作使用 `physical_action`，手物动作使用 `human_object_action`，并设置 `strict_transition_required=true`。

计划冻结后不得新增、删除或改写命题。发现拆分错误时应新建审核 run，不能在看过结果后修改计划。

## 2. 局部逐源帧状态转换

每个严格动作至少建立一个 `state_transition_check`。单窗口不超过 2 秒；较长动作拆成首尾相接的多个窗口。窗口必须同时包含稳定前态、变化过程和稳定后态，并提取窗口内每个源帧。

从最后一个稳定前态到第一个稳定后态之间必须覆盖每个源帧。默认优先 `inspection_mode=native_board`：查看所有原像素 2×2 局部板页，单页长边不超过1600像素，确认工具展示未缩小、目标细节可辨；单独打开前/中/后关键原图及每个疑点的连续原图。板太大、被显示工具缩小、对象离开 ROI 或有歧义时，改为 `originals` 并逐张打开边界内原图。不能为提速缩小目标后宣称等价检查。

native_board 记录 `board_pages_viewed`（manifest 中全部板页的顺序列表）、`board_reviewed_frame_indices`（全部板上源帧）、`native_resolution_verified=true`。`viewed_frame_indices` 可以包含实际看过的板上帧；`original_frames_opened` 与 `initial_evidence_frame_indices` 只记录单独打开的原图。反证中的“新帧”必须是初审原图和局部板都未看过的帧，不能换一种展示方式就当成新证据。

持续支撑、静止或匀速阶段通过全片概览追踪；在接触建立、调整、分离、遮挡变化或异常处建立严格窗口，不要机械按每2秒把整片重复审核。共享画面可服务多个动作，但每个动作的实体、判断与证据引用仍分别绑定。

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

### 动作、部件和可观察性合同

每个 transition 增加 `action_binding`，其 `subject_id / predicate / object_id` 必须与冻结命题完全一致。账本必须包含动作双方；检查当前动作的接触、因果、空间绑定时，`assessed_entity_ids` 必须覆盖双方。另一主体的支撑、背景稳定、没有悬浮，都不能代替当前操作的判断。

`part_inventory` 明确选择 `decomposed` 或 `whole_entity`，填写 `reason` 和 `part_ids`。涉及可分辨部件的连接、分离、数量或轮廓变化时必须拆分：每个部件单独建立实体 ID、`parent_id`，用 `visual_anchors` 记录 before/during/after 的 `frame_index / feature / phase`。固定可见端点、连接处、边缘或纹理特征，不能仅给整个对象起名后把不同轮廓统一称为“同一物体”。无可分辨部件的刚体移动或连续介质可以选 whole_entity，说明理由；不凭常识虚构不可见结构。

维度项增加 `assessed_entity_ids / observable_criterion / evidence_basis`。`observable_criterion` 必须回答当前动作究竟要验证什么，而非复述“检查是否异常”。`evidence_basis` 为：

- `direct_visible`：直接观察可见接触、形状或运动。
- `external_proxy`：外部可见证据支持某状态，补充 `proxy_link` 说明因果联系及限度。不能仅凭最终成功证明整个过程。
- `unobservable_internal`：内部机构本来不可见；该内部子问题标 not_applicable 并填写 `scope_reason`，不要因此认定缺陷或不确定。仍需另外检查可见动作，不得借此排除整项物理检查。
- `not_relevant`：维度与当前动作无关，填写具体 `scope_reason`。

任何 uncertain/defect 维度需填写 `alternative_explanations`：`normal_explanation / anomaly_explanation / discriminating_observation`。必须具体比较正常透视、遮挡或运动解释与异常解释，说明哪张原图上什么事实能区分它们。存在不能解释的可见变化时，不得只写“遮挡不清”；仅凭内部不可见也不能推断失败。校验器只验证记录约束，无法证明这些解释在视觉上正确。

账本实体标 `occluded` 时需 `occlusion_account`：`occluder_id / frame_indices / visible_before / visible_after / predicted_reappearance / outcome`。遮挡者必须在账本中，outcome 为 supported、contradicted 或 unresolved；contradicted 应改记 unexplained。即使仅能保留 uncertain，也要指明尚未解决的部件对应，不能用“环件/物件重叠”代替定位。

反证必须填 `reviewed_entity_ids`，覆盖动作双方及全部已登记部件；不确定或缺陷的 `dimension_findings` 使用同样的维度证据合同。紧裁剪之外仍需核对原全帧，避免裁掉原部件后误认消失。复核重点是比较上述两种解释，不是重复初审用词。

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
