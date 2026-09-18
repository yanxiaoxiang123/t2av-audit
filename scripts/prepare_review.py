#!/usr/bin/env python3
"""Create a traceable T2AV review run and probe source-frame PTS."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid


def run_probe(args: list[str]) -> dict:
    result = subprocess.run(["ffprobe", "-v", "error", *args, "-of", "json"],
                            capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise ValueError("ffprobe failed: " + result.stderr.strip()[:500])
    return json.loads(result.stdout)


def number(value):
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def ratio(value):
    try:
        a, b = value.split("/")
        return int(a) / int(b) if int(b) else None
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


def read_prompt(args):
    if args.prompt_text is not None:
        value, source = args.prompt_text, "inline"
    else:
        path = args.prompt_file or args.metadata
        value, source = path.read_text(encoding="utf-8-sig"), str(path.resolve())
    if args.metadata or (args.prompt_file and args.prompt_file.suffix.lower() == ".json"):
        obj = json.loads(value)
        if not isinstance(obj, dict):
            raise ValueError("Prompt metadata must be a JSON object")
        actual = obj.get("rewriter_prompt") or obj.get("model_input_prompt") or obj.get("prompt") or obj.get("raw_prompt")
        raw = obj.get("raw_prompt")
        selected = "rewriter_prompt" if obj.get("rewriter_prompt") else (
            "model_input_prompt" if obj.get("model_input_prompt") else (
                "prompt" if obj.get("prompt") else "raw_prompt"))
    else:
        actual, raw, selected = value, None, "supplied_prompt"
    if not isinstance(actual, str) or not actual.strip():
        raise ValueError("Actual model-input Prompt is missing")
    return actual.strip(), {"source": source, "selected_field": selected,
                            "raw_prompt": raw if isinstance(raw, str) else None}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(args):
    video = args.video.expanduser().resolve(strict=True)
    if not video.is_file():
        raise ValueError("Video must be a file")
    prompt, provenance = read_prompt(args)
    media = run_probe(["-show_format", "-show_streams", str(video)])
    streams = media.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise ValueError("No video stream found")
    frame_data = run_probe(["-select_streams", "v:0", "-show_frames",
                            "-show_entries", "frame=best_effort_timestamp_time,pkt_pts_time",
                            str(video)])
    raw_pts = [number(f.get("best_effort_timestamp_time") or f.get("pkt_pts_time"))
               for f in frame_data.get("frames", [])]
    if not raw_pts or any(t is None for t in raw_pts):
        raise ValueError("Cannot obtain PTS for all decoded video frames")
    origin = number(v.get("start_time"))
    origin = origin if origin is not None else raw_pts[0]
    pts = [{"frame_index": i, "time_sec": round(t - origin, 6),
            "source_pts_sec": t} for i, t in enumerate(raw_pts)]
    duration = number((media.get("format") or {}).get("duration"))
    if duration is None:
        duration = pts[-1]["time_sec"] + (1 / (ratio(v.get("avg_frame_rate")) or 24))
    fps = ratio(v.get("avg_frame_rate"))
    stream_fps = ratio(v.get("r_frame_rate"))
    deltas = [b - a for a, b in zip(raw_pts, raw_pts[1:]) if b > a]
    if deltas:
        median = sorted(deltas)[len(deltas) // 2]
        vfr = any(abs(delta - median) > max(0.001, median * 0.02) for delta in deltas)
    else:
        vfr = abs(fps - stream_fps) > 0.01 if fps and stream_fps else None
    v_start = number(v.get("start_time"))
    a_start = number(a.get("start_time")) if a else None
    sample_id = args.sample_id or video.stem
    root = video.parent / f"{video.stem}.t2av-review"
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    out = args.output_dir.expanduser().resolve() if args.output_dir else root / run_id
    out.mkdir(parents=True, exist_ok=False)
    for name in ("frames/original", "frames/annotated", "audio", "clips", "logs"):
        (out / name).mkdir(parents=True)
    metadata = {
        "duration_sec": duration, "width": v.get("width"), "height": v.get("height"),
        "fps": fps, "variable_frame_rate": vfr, "has_audio": a is not None,
        "sample_rate_hz": int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        "channels": a.get("channels") if a else None,
        "timeline_basis": "Video stream start_time is source zero; frame times are decoded best_effort_timestamp_time minus video start_time.",
        "av_start_offset_sec": round(a_start - v_start, 6) if a_start is not None and v_start is not None else None,
    }
    input_record = {"sample_id": sample_id, "video_path": str(video), "video_sha256": sha256(video),
                    "prompt": prompt, "prompt_provenance": provenance,
                    "media_metadata": metadata, "run_dir": str(out)}
    (out / "input.json").write_text(json.dumps(input_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "logs" / "media_probe.json").write_text(json.dumps(media, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "logs" / "frame_pts.json").write_text(json.dumps(pts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "logs" / "frame_manifest.json").write_text("[]\n", encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt-text")
    prompts.add_argument("--prompt-file", type=Path)
    prompts.add_argument("--metadata", type=Path)
    parser.add_argument("--sample-id")
    parser.add_argument("--output-dir", type=Path)
    try:
        print(prepare(parser.parse_args()))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
