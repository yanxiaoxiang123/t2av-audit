# t2av-audit

Cross-harness Codex Skill for evidence-grounded review of generated text-to-audio-video clips.

It accepts a local video and the actual generation prompt, freezes atomic requirements and two evidence passes, extracts source-PTS frames and original-track WAV evidence, and validates a complete 17-metric `t2av_score_v1` result.

## Install

```bash
python scripts/install.py
```

The installer connects Claude Code to the shared Skill directory and creates a separate Pillow runtime. FFmpeg and FFprobe must be available on `PATH`. Codex and DeepSeek Harness discover the Skill from `~/.agents/skills/t2av-audit`.

## Run

```bash
PYTHON=/path/from/install.py
$PYTHON scripts/prepare_review.py /absolute/video.mp4 --prompt-file /absolute/prompt.txt
$PYTHON scripts/review_workflow.py init /absolute/video.mp4.t2av-review/<run>
$PYTHON scripts/review_workflow.py freeze-plan /absolute/run /absolute/PLAN.json
$PYTHON scripts/extract_frames.py /absolute/video.mp4.t2av-review/<run> --fps 4
$PYTHON scripts/contact_sheets.py /absolute/video.mp4.t2av-review/<run>
$PYTHON scripts/extract_frames.py /absolute/run --start T0 --end T1 --all
$PYTHON scripts/continuity_board.py /absolute/run --start T0 --end T1 --roi L T R B --name R1
$PYTHON scripts/review_workflow.py freeze-initial /absolute/run /absolute/INITIAL.json
$PYTHON scripts/review_workflow.py freeze-challenge /absolute/run /absolute/CHALLENGE.json
$PYTHON scripts/analyze_audio.py /absolute/video.mp4 --output-dir /absolute/video.mp4.t2av-review/<run>/audio/primary
```

The reviewing model must inspect every required source frame. Native-size 2×2 ROI pages can cover intermediate frames, with key and ambiguous originals opened separately; strict original-by-original inspection remains available. Build `draft.json` only from the frozen checkpoints, render and review annotations, then finalize it:

Transition records must bind observations to the current action's actor and target, track distinguishable parts with visual anchors, and support occlusion explanations with explicit evidence. Invisible internal mechanisms alone do not justify a defect or uncertainty score cap. These contracts are enforced when freezing the initial review and rechecked during finalization.

```bash
$PYTHON scripts/review_result.py render /absolute/run/draft.json
$PYTHON scripts/review_result.py finalize /absolute/run/draft.json --boxes-reviewed
```

Set `GEMINI_API_KEY`, `GEMINI_BASE_URL`, and `GEMINI_MODEL` for audio evidence. Credentials are never stored in this repository.

See [SKILL.md](SKILL.md) for routing, [state_transition_protocol.md](references/state_transition_protocol.md) for strict transition checks, [review_rules.md](references/review_rules.md) for evidence boundaries, and [schema.md](references/schema.md) for the output contract.
