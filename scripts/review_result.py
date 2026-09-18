#!/usr/bin/env python3
"""Render evidence boxes and deterministically validate one T2AV score record."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave

METRICS = "VQ AE VF AQ AF AV TS DC LS TX PH MU PR SB HM MT PS".split()
UNIVERSAL = set(METRICS[:7])
AUDIO_METRICS = {"AQ", "AF", "AV", "DC", "LS", "MU", "PR", "SB", "MT"}


def anchors():
    rubric = (Path(__file__).resolve().parents[1] / "references" / "rubric.md").read_text(encoding="utf-8")
    result = {}
    current = None
    for line in rubric.splitlines():
        match = re.match(r"\s+([A-Z]{2})\s+\S", line)
        if match and match.group(1) in METRICS:
            current = match.group(1)
        match = re.match(r"\s+([0-5]) 分：(.*)", line)
        if match and current:
            result[(current, int(match.group(1)))] = match.group(2).strip()
    if len(result) != 102:
        raise ValueError("Rubric anchors could not be parsed")
    return result


def load_record(path):
    text = path.read_text(encoding="utf-8").strip()
    record = json.loads(text)
    if not isinstance(record, dict):
        raise ValueError("Draft must be one JSON object")
    return record


def image_size(path):
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        return image.size


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def font_for_label():
    from PIL import ImageFont
    candidates = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), 20)
            except OSError:
                continue
    return ImageFont.load_default()


def render(draft_path):
    from PIL import Image, ImageDraw
    record = load_record(draft_path)
    review_dir = draft_path.parent.resolve()
    if not (review_dir / "input.json").is_file():
        raise ValueError("Draft must be in its prepared review directory")
    font = font_for_label()
    for evidence in record.get("evidence", []):
        evidence_id = evidence.get("evidence_id", "evidence")
        for frame in evidence.get("frames", []):
            source = Path(frame["original_image_path"])
            with Image.open(source) as image:
                annotated = image.convert("RGB")
            draw = ImageDraw.Draw(annotated)
            for index, box in enumerate(frame.get("boxes", []), 1):
                xyxy = box["bbox_xyxy"]
                color = "#e53935" if box.get("color") == "red" else "#2eae49"
                draw.rectangle((xyxy[0], xyxy[1], xyxy[2] - 1, xyxy[3] - 1), outline=color, width=3)
                label = f"{evidence_id} {box.get('object_label', '')}"
                try:
                    draw.text((xyxy[0], max(0, xyxy[1] - 25)), label, fill=color, font=font,
                              stroke_width=1, stroke_fill="black")
                except UnicodeEncodeError:
                    draw.text((xyxy[0], max(0, xyxy[1] - 25)), f"{evidence_id} #{index}",
                              fill=color, font=font)
            destination = review_dir / "frames" / "annotated" / f"{evidence_id}_{frame['frame_index']:06d}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            annotated.save(destination)
            frame["annotated_image_path"] = str(destination.resolve())
    draft_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sum(len(e.get("frames", [])) for e in record.get("evidence", []))


def check_path(path, errors, label):
    if not isinstance(path, str) or not path:
        errors.append(f"{label}: missing path")
        return None
    p = Path(path)
    if not p.is_absolute() or not p.is_file():
        errors.append(f"{label}: file not found at absolute path")
        return None
    try:
        with p.open("rb") as stream:
            if not stream.read(1):
                raise ValueError("empty")
    except (OSError, ValueError):
        errors.append(f"{label}: file cannot be reopened")
        return None
    return p


def validate(record, review_dir, boxes_reviewed):
    groups = {name: [] for name in ("references", "files", "coordinates", "scores")}
    def err(group, msg):
        groups[group].append(msg)
    try:
        prepared = json.loads((review_dir / "input.json").read_text(encoding="utf-8"))
        manifest = json.loads((review_dir / "logs" / "frame_manifest.json").read_text(encoding="utf-8"))
        pts = json.loads((review_dir / "logs" / "frame_pts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Missing or unreadable prepared run") from exc
    def valid_time(value):
        return type(value) in (int, float) and math.isfinite(value)
    duration = prepared["media_metadata"].get("duration_sec")
    if not valid_time(duration) or duration <= 0:
        err("scores", "Prepared media duration is invalid")
        duration = 0
    pts_map = {p.get("frame_index"): p.get("time_sec") for p in pts if isinstance(p, dict)}
    for source in manifest:
        index = source.get("frame_index")
        if (index not in pts_map or not valid_time(source.get("time_sec")) or
                abs(source["time_sec"] - pts_map[index]) > 0.000001):
            err("references", f"Manifest frame {index} differs from source PTS")
    if record.get("schema_version") != "t2av_score_v1" or record.get("overall_score", "missing") is not None:
        err("scores", "schema_version must be t2av_score_v1 and overall_score must be null")
    for key in ("sample_id", "video_path", "prompt", "prompt_provenance", "media_metadata"):
        if record.get(key) != prepared.get(key):
            err("scores", f"{key} differs from prepared input")
    if not isinstance(record.get("summary"), str) or not record["summary"].strip():
        err("scores", "summary is required")
    metric_items = record.get("metric_scores")
    checks = record.get("prompt_checks")
    evidence = record.get("evidence")
    if not all(isinstance(x, list) for x in (metric_items, checks, evidence)):
        raise ValueError("metric_scores, prompt_checks and evidence must be arrays")
    metrics = {m.get("metric_id") for m in metric_items if isinstance(m, dict)}
    if len(metric_items) != 17 or metrics != set(METRICS):
        err("scores", "17 unique metrics are required")
    req_ids = [r.get("requirement_id") for r in checks]
    ev_ids = [e.get("evidence_id") for e in evidence]
    if any(not x or not isinstance(x, str) for x in req_ids) or len(set(req_ids)) != len(req_ids):
        err("references", "Requirement IDs must be nonempty and unique")
    if any(not x or not isinstance(x, str) for x in ev_ids) or len(set(ev_ids)) != len(ev_ids):
        err("references", "Evidence IDs must be nonempty and unique")
    req_set, ev_set = set(req_ids), set(ev_ids)
    rubric = anchors()
    for m in metric_items:
        mid = m.get("metric_id")
        if mid not in METRICS:
            continue
        if not set(m.get("requirement_ids") or []) <= req_set or not set(m.get("evidence_ids") or []) <= ev_set:
            err("references", f"{mid}: broken requirement or evidence reference")
        if m.get("metric_type") != ("通用" if mid in UNIVERSAL else "条件"):
            err("scores", f"{mid}: metric_type is wrong")
        if not isinstance(m.get("applicability_reason"), str) or not m["applicability_reason"].strip():
            err("scores", f"{mid}: applicability_reason is required")
        status, score = m.get("status"), m.get("score")
        if status == "已评分":
            if type(score) is not int or score not in range(6):
                err("scores", f"{mid}: score must be an integer from 0 to 5")
            elif m.get("rubric_anchor") != rubric[(mid, score)]:
                err("scores", f"{mid}: rubric_anchor does not match original rubric")
            if not m.get("evidence_ids") or not m.get("rationale"):
                err("scores", f"{mid}: scored metric needs evidence and rationale")
            elif prepared["media_metadata"].get("width") and not any(
                e.get("frames") for e in evidence if e.get("evidence_id") in m["evidence_ids"]
            ):
                err("scores", f"{mid}: scored metric needs saved video-frame context")
            if mid in AUDIO_METRICS and not any(
                e.get("audio_segments") or (e.get("kind") == "verified_absence" and e.get("log_paths"))
                for e in evidence if e.get("evidence_id") in (m.get("evidence_ids") or [])
            ):
                err("scores", f"{mid}: scored audio metric needs WAV or verified no-audio probe evidence")
            if type(score) is int and score <= 2 and not m.get("failure_reason"):
                err("scores", f"{mid}: 0–2 score needs failure_reason")
        elif status == "不适用" and mid not in UNIVERSAL:
            if score is not None or m.get("rubric_anchor") is not None or not m.get("rationale"):
                err("scores", f"{mid}: inapplicable metric has invalid fields")
        else:
            err("scores", f"{mid}: invalid status")
        if m.get("confidence") not in ("高", "中", "低"):
            err("scores", f"{mid}: confidence is required")
    for check in checks:
        quote = check.get("prompt_quote")
        if not isinstance(quote, str) or quote not in prepared["prompt"]:
            err("references", f"{check.get('requirement_id')}: quote is absent from actual Prompt")
        if not set(check.get("metric_ids") or []) <= metrics or not set(check.get("evidence_ids") or []) <= ev_set:
            err("references", f"{check.get('requirement_id')}: broken metric or evidence reference")
        if check.get("status") not in ("已呈现", "部分呈现", "未呈现", "与要求矛盾", "无法判断"):
            err("scores", f"{check.get('requirement_id')}: invalid prompt check status")
    frame_map = {f["frame_index"]: f for f in manifest}
    for ev in evidence:
        eid = ev.get("evidence_id")
        if not set(ev.get("metric_ids") or []) <= metrics or not set(ev.get("requirement_ids") or []) <= req_set:
            err("references", f"{eid}: broken reverse reference")
        if ev.get("kind") not in ("visual", "audio", "audiovisual", "verified_absence"):
            err("scores", f"{eid}: invalid kind")
        if not isinstance(ev.get("observation"), str) or not ev["observation"].strip():
            err("scores", f"{eid}: observation is required")
        for frame in ev.get("frames") or []:
            index = frame.get("frame_index")
            source = frame_map.get(index)
            if source is None or not isinstance(index, int) or index >= len(pts):
                err("references", f"{eid}: frame index absent from source manifest")
                continue
            if (frame.get("time_sec") != source["time_sec"] or
                    frame.get("time_sec") != pts_map.get(index) or
                    frame.get("original_image_path") != source["original_image_path"]):
                err("references", f"{eid}: frame PTS or path differs from source")
            for field in ("original_image_path", "annotated_image_path"):
                path = check_path(frame.get(field), groups["files"], f"{eid}.{field}")
                if path:
                    try:
                        if image_size(path) != (frame.get("width"), frame.get("height")):
                            err("coordinates", f"{eid}: image dimensions mismatch")
                    except (OSError, ValueError):
                        err("files", f"{eid}: unreadable image")
            if (frame.get("width"), frame.get("height")) != (source["width"], source["height"]):
                err("coordinates", f"{eid}: source frame dimensions mismatch")
            if not frame.get("boxes") and not frame.get("bbox_unavailable_reason"):
                err("coordinates", f"{eid}: empty boxes need a reason")
            for box in frame.get("boxes") or []:
                xy = box.get("bbox_xyxy")
                if not isinstance(xy, list) or len(xy) != 4 or any(type(x) is not int for x in xy):
                    err("coordinates", f"{eid}: bbox must be four integers")
                elif not (0 <= xy[0] < xy[2] <= frame["width"] and
                          0 <= xy[1] < xy[3] <= frame["height"]):
                    err("coordinates", f"{eid}: bbox outside frame")
                if box.get("region_type") not in ("object", "contact", "text", "mouth", "global", "search_area"):
                    err("coordinates", f"{eid}: invalid region_type")
                if box.get("color") not in ("red", "green"):
                    err("coordinates", f"{eid}: invalid box color")
        for segment in ev.get("audio_segments") or []:
            start, end = segment.get("source_start_time_sec"), segment.get("source_end_time_sec")
            if not (valid_time(start) and valid_time(end) and 0 <= start < end <= duration + 0.5):
                err("references", f"{eid}: audio interval is outside source timeline")
            path = check_path(segment.get("audio_path"), groups["files"], f"{eid}.audio_path")
            if path:
                try:
                    with wave.open(str(path), "rb") as wav:
                        if (wav.getframerate() != segment.get("sample_rate_hz") or
                                wav.getnchannels() != segment.get("channels") or wav.getnframes() == 0):
                            err("files", f"{eid}: WAV metadata mismatch")
                except (wave.Error, OSError):
                    err("files", f"{eid}: unreadable WAV")
            if segment.get("verified_by_listening") not in (True, False):
                err("scores", f"{eid}: verified_by_listening must be boolean")
        if ev.get("audio_segments") and not any(s.get("verified_by_listening") for s in ev["audio_segments"]):
            if ev.get("analysis_method") != "gemini_audio_understanding":
                err("scores", f"{eid}: Gemini provenance is required for unheard audio")
            analysis_path = check_path(ev.get("analysis_result_path"), groups["files"], f"{eid}.analysis_result_path")
            if analysis_path:
                try:
                    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                    analyzed = {s.get("audio_path"): s for s in analysis.get("segments", [])}
                    if analysis.get("status") != "complete":
                        err("scores", f"{eid}: audio analysis is not complete")
                    if analysis.get("source", {}).get("sha256") != prepared.get("video_sha256"):
                        err("references", f"{eid}: audio analysis source differs from video")
                    for segment in ev["audio_segments"]:
                        found = analyzed.get(segment.get("audio_path"))
                        if not found or found.get("status") != "analyzed":
                            err("references", f"{eid}: WAV is absent from completed audio analysis")
                        elif (found.get("source_start_time_sec") != segment.get("source_start_time_sec") or
                              found.get("source_end_time_sec") != segment.get("source_end_time_sec") or
                              sha256(Path(segment["audio_path"])) != found.get("audio_sha256")):
                            err("references", f"{eid}: WAV hash or source interval differs from analysis")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    err("files", f"{eid}: unreadable audio analysis ({type(exc).__name__})")
        for path in (ev.get("clip_paths") or []) + (ev.get("log_paths") or []):
            check_path(path, groups["files"], f"{eid}.asset")
        for sync in ev.get("synchronization") or []:
            offset = sync.get("offset_sec")
            if offset is not None:
                visual = sync.get("visual_onset_sec")
                audio = sync.get("audio_onset_sec")
                if not (valid_time(visual) and valid_time(audio) and valid_time(offset)
                        and 0 <= visual <= duration and 0 <= audio <= duration
                        and abs(offset - (audio - visual)) <= 0.02):
                    err("scores", f"{eid}: synchronization offset is not traceable")
    inspection = record.get("inspection") or {}
    viewed = inspection.get("viewed_frame_indices") or []
    if not viewed or not set(viewed) <= set(frame_map):
        err("scores", "inspection.viewed_frame_indices must list actually viewed saved frames")
    else:
        source_times = [f["time_sec"] for f in pts]
        required = set()
        for i in range(int(duration * 4) + 1):
            target = min(duration, i / 4)
            j = bisect.bisect_left(source_times, target)
            nearest = min((k for k in (j - 1, j) if 0 <= k < len(pts)),
                          key=lambda k: abs(source_times[k] - target))
            required.add(pts[nearest]["frame_index"])
        if not required <= set(viewed):
            err("scores", "Full-video 4-fps PTS coverage is missing from viewed frames")
    if not inspection.get("visual_inspection_intervals_sec"):
        err("scores", "inspection.visual_inspection_intervals_sec is required")
    if not inspection.get("tools"):
        err("scores", "inspection.tools is required")
    if not isinstance(record.get("uncertainties"), list) or not isinstance(record.get("inspection_limits"), list):
        err("scores", "uncertainties and inspection_limits must be arrays")
    result = {
        "json_parse_ok": True,
        "all_17_metrics_present": len(metric_items) == 17 and metrics == set(METRICS),
        "references_valid": not groups["references"],
        "files_verified": not groups["files"],
        "coordinates_verified": not groups["coordinates"] if boxes_reviewed else None,
        "score_evidence_complete": not groups["scores"],
        "errors": sum(groups.values(), []),
    }
    if not boxes_reviewed:
        result["errors"].append("Annotated boxes need visual review before finalization")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render_p = sub.add_parser("render")
    render_p.add_argument("draft", type=Path)
    final_p = sub.add_parser("finalize")
    final_p.add_argument("draft", type=Path)
    final_p.add_argument("--output", type=Path)
    final_p.add_argument("--boxes-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "render":
            print(json.dumps({"rendered_frames": render(args.draft)}, ensure_ascii=False))
            return 0
        draft = args.draft.resolve(strict=True)
        record = load_record(draft)
        validation = validate(record, draft.parent, args.boxes_reviewed)
        record["validation"] = validation
        if validation["errors"]:
            print(json.dumps(validation, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        output = args.output or draft.parent / "score.jsonl"
        if output.exists() and output.resolve() != draft:
            raise ValueError("Output exists; use a new review run")
        output.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
        if len(output.read_text(encoding="utf-8").splitlines()) != 1:
            raise ValueError("JSONL must contain exactly one line")
        print(output.resolve())
        return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Review validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
