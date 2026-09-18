# t2av-audit

Cross-harness Codex Skill for evidence-grounded review of generated text-to-audio-video clips.

It accepts a local video and the actual generation prompt, extracts source-PTS frames and original-track WAV evidence, supports a Gemini-compatible audio gateway, and validates a complete 17-metric `t2av_score_v1` JSONL result.

## Install

```bash
python scripts/install.py
```

The installer connects Claude Code to the shared Skill directory and creates a separate Pillow runtime. FFmpeg and FFprobe must be available on `PATH`. Codex and DeepSeek Harness discover the Skill from `~/.agents/skills/t2av-audit`.

## Run

```bash
PYTHON=/path/from/install.py
$PYTHON scripts/prepare_review.py /absolute/video.mp4 --prompt-file /absolute/prompt.txt
$PYTHON scripts/extract_frames.py /absolute/video.mp4.t2av-review/<run> --fps 4
$PYTHON scripts/contact_sheets.py /absolute/video.mp4.t2av-review/<run>
$PYTHON scripts/analyze_audio.py /absolute/video.mp4 --output-dir /absolute/video.mp4.t2av-review/<run>/audio/primary
```

The reviewing model must actually inspect the saved images. Build `draft.json` according to `references/schema.md`, render and review annotations, then finalize it:

```bash
$PYTHON scripts/review_result.py render /absolute/run/draft.json
$PYTHON scripts/review_result.py finalize /absolute/run/draft.json --boxes-reviewed
```

Set `GEMINI_API_KEY`, `GEMINI_BASE_URL`, and `GEMINI_MODEL` for audio evidence. Credentials are never stored in this repository.

See [SKILL.md](SKILL.md) for the complete workflow, [review_rules.md](references/review_rules.md) for evidence boundaries, and [schema.md](references/schema.md) for the output contract.
