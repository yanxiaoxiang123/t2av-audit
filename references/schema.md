# t2av_score_v1 证据与结果格式

本 Skill 的 `prompt` 字段保存实际送入生成模型的 Prompt；若同时有 raw_prompt 与 rewriter_prompt，以 rewriter_prompt 为评分依据，原始文案写入 `prompt_provenance`。输出目录由准备脚本生成，不使用项目旧示例路径。

1、文件与通用约定

    结果文件：Output JSONL Path，UTF-8 编码。每个样本保存为一行完整 JSON 对象；当前任务只处理一个样本，因此结果文件必须恰好包含一条非空记录。
    每条记录必须压缩为单行，行内不允许换行；文件内不允许 Markdown、代码块、注释、空行或 JSON 之外的说明。
    证据目录：Evidence Directory，按需建立以下子目录：
        frames/original/     原始证据帧
        frames/annotated/    标注证据帧
        audio/              音频片段
        clips/              视频片段
        logs/               探测、检查与时间映射日志

    保留原始输入，不覆盖已有其他样本结果。
    以下字段均须保留；未知或无法核验的可空字段填写 null，不用空字符串冒充有效值。
    没有记录的数组填写 []；所有文件路径使用实际存在且已核验的绝对路径。
    时间单位为秒，使用源媒体时间轴；帧号从 0 开始。
    本节的结构示意仅用于阅读，实际结果必须填写真实检查数据。

    2、顶层结构

    result
    ├─ schema_version       字符串，固定为 t2av_score_v1
    ├─ sample_id            字符串，输入样本标识
    ├─ video_path           字符串，输入视频路径
    ├─ prompt               字符串，完整实际模型输入 Prompt
    ├─ summary              字符串，检查完成情况及主要结论
    ├─ overall_score        固定为 null，不计算未定义的总分
    ├─ media_metadata       对象，媒体元数据（见第 3 节）
    ├─ metric_scores[]      17 项指标评分（见第 4 节）
    ├─ prompt_checks[]      Prompt 要求对照（见第 5 节）
    ├─ evidence[]           所有评分共用的证据库（见第 6 节）
    ├─ inspection           对象，实际检查记录（见第 7 节）
    ├─ uncertainties[]      字符串数组，未解决的疑点
    ├─ inspection_limits[]  字符串数组，检查范围及能力限制
    └─ validation           对象，输出校验结果（见第 8 节）

    关联方式：
        metric_scores.requirement_ids → prompt_checks.requirement_id
        metric_scores.evidence_ids    → evidence.evidence_id
        prompt_checks.evidence_ids    → evidence.evidence_id
        evidence.requirement_ids      → prompt_checks.requirement_id
        evidence.metric_ids           → metric_scores.metric_id

    3、media_metadata：媒体元数据

    duration_sec         数值或 null，媒体时长
    width / height       整数或 null，原始呈现帧宽高
    fps                  数值或 null，帧率
    variable_frame_rate  布尔值或 null，是否可变帧率
    has_audio            布尔值或 null，是否存在音轨
    sample_rate_hz       整数或 null，音频采样率
    channels             整数或 null，音频声道数
    timeline_basis       字符串或 null，PTS 时间基准及映射说明
    av_start_offset_sec  数值或 null，音频起始时间减视频起始时间

    4、metric_scores[]：逐指标评分

    每项对应一个指标，17 个指标必须完整且唯一：
    VQ、AE、VF、AQ、AF、AV、TS、DC、LS、TX、PH、MU、PR、SB、HM、MT、PS。

    字段                   类型             含义
    metric_id              字符串           上述指标代码
    metric_name            字符串           评分表中的中文指标名
    metric_type            字符串           通用 / 条件
    status                 字符串           已评分 / 不适用
    score                  整数或 null      已评分为 0–5；其他状态为 null
    applicability_reason   字符串           启用、不适用或规则不明确的依据
    requirement_ids        字符串数组       关联的 Prompt 要求 ID
    rubric_anchor          字符串或 null    该指标对应分数的原文锚点
    rationale              字符串           观察事实如何支持所选分数
    failure_reason         字符串或 null    0–2 分必填的具体失败原因
    error_types            字符串数组       从该指标的常见错误中选取
    evidence_ids           字符串数组       关联的真实证据 ID
    confidence             字符串           高 / 中 / 低
    uncertainty            字符串或 null    尚不能确认的内容

    填写约束：
    - 已评分：rubric_anchor 必填，evidence_ids 非空，rationale 说明范围和严重程度。
    - 不适用：仅限 Prompt 未涉及的条件指标，score 和 rubric_anchor 均为 null，并说明原因。
    - 通用指标和已启用的条件指标必须为已评分，score 为 0–5 整数并填写 rubric_anchor。
    - 不适用依据 Prompt 判断，不强行制造视听证据。
    - 客观质量指标的 requirement_ids 可为空，但须在理由中说明评估范围。
    - error_types 无错误时为 []；选择“其他类型错误”时必须解释。
    - confidence 表示证据可信程度，不能代替缺失证据。

    5、prompt_checks[]：Prompt 要求对照

    requirement_id  字符串，样本内唯一要求 ID
    prompt_quote    字符串，实际模型输入 Prompt 原文片段
    requirement     字符串，拆解后的可验证要求
    metric_ids      字符串数组，涉及的指标代码
    status          字符串，已呈现 / 部分呈现 / 未呈现 / 与要求矛盾 / 无法判断
    evidence_ids    字符串数组，对应证据 ID
    reason          字符串，对照结论及依据

    6、evidence[]：证据库

    每条证据只保存一次，可被多个指标引用；各指标须分别解释引用依据。

    6.1 证据主字段

        evidence_id           字符串，样本内唯一证据 ID
        metric_ids            字符串数组，关联指标
        requirement_ids       字符串数组，关联要求
        kind                  visual / audio / audiovisual / verified_absence
        role                  support / defect / absence / context
        start_time_sec        数值或 null，证据区间起点
        end_time_sec          数值或 null，证据区间终点
        observation           字符串，实际看到、听到或探测到的事实
        expected              字符串，对应要求或质量基准
        judgment              确认事实 / 疑似问题 / 无法判断
        sequence_description  字符串或 null，动态事件发生前、中、后的变化
        frames                数组，证据帧（见 6.2）
        audio_segments        数组，音频片段（见 6.4）
        synchronization       数组，同步测量（见 6.5）
        clip_paths            字符串数组，已核验的视频片段路径
        log_paths             字符串数组，已核验的日志路径
        coverage_note         字符串，证据覆盖范围；缺失结论须说明搜索范围和盲区
        limitations           字符串数组，该证据的限制

        observation 不得混入 Prompt 预期；未知时间必须在 limitations 说明原因。

    6.2 frames[]：证据帧

        frame_index           整数，原始帧号
        time_sec              数值，帧的实际呈现时间
        width / height        整数，原始呈现帧尺寸
        original_image_path   字符串，原始证据帧路径
        annotated_image_path  字符串或 null，标注图路径
        frame_role            before / during / after / representative / temporal_context
        boxes                 数组，位置框（见 6.3）
        bbox_unavailable_reason  字符串或 null，无框或无标注图时说明原因

        frames 只收录实际保存并目视核验的帧，不生成空白帧记录占位。
        纯音频配套帧使用 temporal_context；无可解码画面时 frames=[] 并说明。

    6.3 frames[].boxes[]：位置框

        object_label  字符串，实际可见的对象名称
        region_type   object / contact / text / mouth / global / search_area
        bbox_xyxy     四个整数，[left, top, right, bottom]
        observation   字符串，框内具体可见事实
        color         字符串，标注框颜色；异常 red，满足要求 green

        坐标基于原始呈现帧，原点左上，右/下边界不包含在框内：
            0 <= left < right <= width
            0 <= top < bottom <= height
        局部视觉评分能定位时必须画框；无可见声源时 boxes=[]，不得虚构位置。
        全帧构图框使用 global；缺失对象的搜索范围使用 search_area，不能冒充对象框。

    6.4 audio_segments[]：音频片段

        audio_path             字符串，真实音频片段路径
        source_start_time_sec  数值，片段在源媒体中的起点
        source_end_time_sec    数值，片段在源媒体中的终点
        sample_rate_hz         整数，片段实际采样率
        channels               整数，片段实际声道数
        heard_content          字符串，实际听到的内容，听不清处明确标记
        verified_by_listening  布尔值，是否实际听取并核验

        未实际听取不得写 true。云端模型分析保持该字段为 false，另在该音频证据项记录 analysis_method="gemini_audio_understanding"、analysis_result_path 和 analysis_model；依据本次模型明确提供的声音证据评分，局部不足写入 uncertainty，不改变指标已评分状态。

    6.5 synchronization[]：同步测量

        event_label          字符串，声画对应事件名称
        visual_onset_sec     数值或 null，视觉事件起点
        audio_onset_sec      数值或 null，声音事件起点
        visual_peak_sec      数值或 null，视觉事件峰值
        audio_peak_sec       数值或 null，声音事件峰值
        visual_end_sec       数值或 null，视觉事件终点
        audio_end_sec        数值或 null，声音事件终点
        offset_sec           数值或 null，所测声音时刻减对应视觉时刻
        measurement_method   字符串，测量方法及 offset_sec 使用的起点/峰值/终点
        time_uncertainty_sec 数值或 null，测量时间不确定度

        正偏移表示声音滞后；不能可靠测量的值填 null，不虚报精度。

    7、inspection：实际检查记录

    此对象须记录以下实际信息，不得用计划检查范围代替已完成范围：
    - 视觉检查区间及抽帧频率。
    - 已目视的 frame_index 列表，或保存该列表的真实清单路径。
    - 实际听取的音频区间。
    - 关键片段复查记录。
    - 使用的工具、原始日志路径，以及旋转处理和裁剪坐标映射记录。

    8、validation：输出校验结果

    以下检查字段均为布尔值或 null：
        json_parse_ok             每条非空记录能否独立解析为完整 JSON 对象
        all_17_metrics_present    17 个指标是否完整且唯一
        references_valid          所有 ID 引用是否有效
        files_verified            所有证据文件是否已重新打开核验
        coordinates_verified      位置框是否符合坐标规范且覆盖所述区域
        score_evidence_complete   每个分数的必需证据是否齐全

    errors：字符串数组，记录具体校验错误；无错误为 []。
    检查未执行时填 null；只有实际检查通过才填 true，失败填 false。

【提交前核验】

    - 逐行解析 JSONL，确认文件恰好包含当前样本的一条记录，并检查 17 个指标完整、唯一，状态与 score 类型一致。
    - 检查条件指标启用依据；不适用仅限 Prompt 未涉及的条件指标且不参与分数计算。通用指标和已启用的条件指标不得出现无法判断或 null 分数。
    - 检查每个数字分数引用的证据真实存在、足以支持对应锚点；所有 0–2 分有失败原因。
    - 检查证据 ID、要求 ID、指标 ID 的交叉引用；所有路径可访问并已重新打开。
    - 核对帧号、PTS、音频区间、图像尺寸和位置框边界；逐张确认框覆盖所描述区域。
    - 核对所有依赖音频的评分均有真实听取记录或本次 Gemini 云端音频分析记录，所有动态判断均有时间序列证据。
    - 删除猜测性事实和伪精确数值；未证实的疑点不作为错误。检查限制写入 uncertainty 或 limitations，不妨碍依据现有证据和疑罪从无原则选择评分锚点。
    - 最终回复 JSON 路径、证据目录及简短结论，明确未完成项。写入或验证失败必须报告，不宣称任务完成。
