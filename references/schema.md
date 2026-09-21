# `t2av_score_v1` 分阶段证据格式

新版继续使用 `t2av_score_v1`，但要求三个不可变检查点；旧结果可以不兼容。

## 1. 冻结计划 `PLAN.json`

```json
{
  "prompt_sha256": "sha256 of exact prepared prompt",
  "prompt_checks": [
    {
      "requirement_id": "R1",
      "claim_type": "human_object_action",
      "subject_id": "person_1",
      "predicate": "lift",
      "object_id": "cup_1",
      "expected_before": "cup rests on table",
      "expected_after": "same cup is supported by hand",
      "sequence_index": 0,
      "prompt_span": [0, 16],
      "prompt_quote": "exact Prompt slice",
      "metric_ids": ["VF", "TS", "PH", "HM", "PS"],
      "strict_transition_required": true
    }
  ]
}
```

`claim_type` 为 `identity / count / attribute / spatial_relation / physical_action / human_object_action / text / dialogue / audio / temporal`。`predicate` 是单一 snake_case 命题，不得包含 and/then/plus/or 连接。`prompt_quote` 必须与 `prompt_span` 精确一致。非物理命题的 `strict_transition_required` 为 false。

## 2. 冻结初审 `INITIAL.json`

顶层为 `{"state_transition_checks": [...]}`。每项字段：

- `transition_id`、`requirement_id`
- `source_interval_sec`：不超过 2 秒
- `source_frame_indices`：窗口内全部源帧，PTS 顺序
- `board_manifest_path`：最多 2×2 的局部无结论板
- `before_frame_index`、非空 `during_frame_indices`、`after_frame_index`
- `before_observation`、`transition_observation`、`after_observation`
- `entity_ledger[]`：每项含 `entity_id / before_state / during_state / after_state / allowed_changes[] / actual_changes[] / status`
- `dimension_checks[]`：九个维度各一项，含 `dimension / status / observation / frame_indices[] / strongest_counterexample`
- `strongest_counterexamples[]`
- `initial_verdict`：`confirmed_consistent / confirmed_defect / uncertain`
- `initial_evidence_frame_indices`：必须包含稳定前后态之间每个源帧
- `evidence_ids[]`

实体 `status` 为 `tracked / occluded / unexplained`。维度及状态见 [state_transition_protocol.md](state_transition_protocol.md)。

## 3. 冻结反证 `CHALLENGE.json`

```json
{
  "challenge_reviews": [
    {
      "transition_id": "T1",
      "method": "self_blind",
      "verdict": "uncertain",
      "observation": "fact from independent evidence",
      "frame_indices": [42, 43],
      "board_manifest_path": "/absolute/optional/tighter/board_manifest.json"
    }
  ]
}
```

每个初审 transition 恰好一项。`frame_indices` 至少含初审未使用的新帧，或 `board_manifest_path` 指向严格更紧的新 ROI 板。

反证发现缺陷或不确定性时，还必须填写非空 `dimension_findings[]`，每项含 `dimension / status / observation / frame_indices`，状态为 `applicable_defect` 或 `applicable_uncertain`。这些冻结发现参与最终评分传播，不改写初审记录。

## 4. 最终结果

顶层字段：

- `schema_version="t2av_score_v1"`
- `sample_id / video_path / prompt / prompt_provenance / media_metadata`：与 `input.json` 完全一致
- `summary`
- `overall_score=null`
- `metric_scores[]`：17 项
- `prompt_checks[]`：冻结计划字段原样保留，并增加 `status / evidence_ids / reason`
- `evidence[]`
- `inspection`
- `uncertainties[] / inspection_limits[]`
- `validation`：由 finalizer 生成

### `metric_scores[]`

字段为 `metric_id / metric_name / metric_type / status / score / applicability_reason / requirement_ids / rubric_anchor / rationale / failure_reason / error_types / evidence_ids / confidence / uncertainty`。

17 指标为 VQ、AE、VF、AQ、AF、AV、TS、DC、LS、TX、PH、MU、PR、SB、HM、MT、PS。通用指标 `metric_type=通用` 且必须 `已评分`；条件指标为 `条件`，未由 Prompt 启用时 `不适用`、`score=null`、`rubric_anchor=null`。已评分项使用 0–5 整数且 `rubric_anchor` 必须逐字匹配 [rubric.md](rubric.md)。0–2 分填写 `failure_reason`。

### `inspection.state_transition_checks[]`

完整复制对应冻结初审项，再增加：

- `challenge_review`：完整复制冻结反证项
- `conflict_resolution`：冲突解决对象或 null；对象含 `verdict / observation / frame_indices`
- `verdict`
- `issue_categories[]`
- `severity`：确认缺陷为 `minor / major / critical`，其他为 null
- `affected_metric_ids[]`

还须记录 `viewed_frame_indices`、`original_frames_opened`、`visual_inspection_intervals_sec`、实际听取/分析范围、`tools` 和相关日志。最终文件中的冻结字段和文件哈希必须与 checkpoint 完全相同。

### `evidence[]`

主字段：`evidence_id / metric_ids / requirement_ids / kind / role / start_time_sec / end_time_sec / observation / expected / judgment / sequence_description / frames[] / audio_segments[] / synchronization[] / clip_paths[] / log_paths[] / coverage_note / limitations[]`。

`kind` 为 `visual / audio / audiovisual / verified_absence`。

`frames[]` 包含：`frame_index / time_sec / width / height / original_image_path / annotated_image_path / frame_role / boxes[] / bbox_unavailable_reason`。位置框包含 `object_label / region_type / bbox_xyxy / observation / color`；`region_type` 为 `object / contact / text / mouth / global / search_area`。

`audio_segments[]` 包含：`audio_path / source_start_time_sec / source_end_time_sec / sample_rate_hz / channels / heard_content / verified_by_listening`。未亲听的 Gemini 证据在 evidence 层填写 `analysis_method="gemini_audio_understanding" / analysis_result_path / analysis_model`。

`synchronization[]` 包含：`event_label / visual_onset_sec / audio_onset_sec / visual_peak_sec / audio_peak_sec / visual_end_sec / audio_end_sec / offset_sec / measurement_method / time_uncertainty_sec`。

所有路径均为可重新打开的绝对路径；帧 PTS、路径和尺寸必须与提取 manifest 相同。不存在对象时使用 `search_area`，不能虚构对象框。
