#!/usr/bin/env python3
"""Extract exact decoded source frames selected by presentation timestamps."""

from __future__ import annotations

import argparse
import bisect
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile


def png_size(path):
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"Invalid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def select_frames(points, start, end, fps):
    if start < 0 or end <= start or fps <= 0:
        raise ValueError("Invalid interval or rate")
    candidates = [p for p in points if start - 1e-6 <= p["time_sec"] <= end + 1e-6]
    if not candidates:
        raise ValueError("No source frames in interval")
    times = [p["time_sec"] for p in candidates]
    chosen = {candidates[0]["frame_index"], candidates[-1]["frame_index"]}
    step = 1 / fps
    count = int((end - start) * fps) + 1
    for i in range(count + 1):
        target = min(end, start + i * step)
        j = bisect.bisect_left(times, target)
        nearest = min((k for k in (j - 1, j) if 0 <= k < len(times)),
                      key=lambda k: abs(times[k] - target))
        chosen.add(candidates[nearest]["frame_index"])
    return [p for p in candidates if p["frame_index"] in chosen]


def extract(review_dir, start, end, fps):
    review_dir = review_dir.expanduser().resolve(strict=True)
    record = json.loads((review_dir / "input.json").read_text(encoding="utf-8"))
    points = json.loads((review_dir / "logs" / "frame_pts.json").read_text(encoding="utf-8"))
    duration = record["media_metadata"]["duration_sec"]
    end = min(end if end is not None else duration, duration)
    if end - start > 30.000001:
        raise ValueError("Extract in intervals of at most 30 seconds")
    selected = select_frames(points, start, end, fps)
    manifest_path = review_dir / "logs" / "frame_manifest.json"
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    known = {item["frame_index"] for item in existing}
    missing = [item for item in selected if item["frame_index"] not in known]
    if missing:
        with tempfile.TemporaryDirectory(prefix="t2av-frames-") as temp_dir:
            temp = Path(temp_dir)
            expression = "+".join(f"eq(n\\,{p['frame_index']})" for p in missing)
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-i", record["video_path"],
                       "-map", "0:v:0", "-vf", "select=" + expression, "-vsync", "0",
                       "-start_number", "0", str(temp / "frame_%06d.png")]
            result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
            if result.returncode:
                raise ValueError("ffmpeg frame extraction failed: " + result.stderr.strip()[:500])
            produced = sorted(temp.glob("frame_*.png"))
            if len(produced) != len(missing):
                raise ValueError(f"Frame extraction count mismatch: {len(produced)} vs {len(missing)}")
            for source, point in zip(produced, missing):
                target = review_dir / "frames" / "original" / f"frame_{point['frame_index']:06d}.png"
                source.replace(target)
                width, height = png_size(target)
                existing.append({"frame_index": point["frame_index"], "time_sec": point["time_sec"],
                                 "width": width, "height": height, "original_image_path": str(target)})
    existing.sort(key=lambda item: item["frame_index"])
    manifest_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log = {"interval_sec": [start, end], "requested_fps": fps,
           "selected_frame_indices": [p["frame_index"] for p in selected],
           "new_frames": len(missing), "source": record["video_path"]}
    log_path = review_dir / "logs" / f"extraction_{start:.3f}_{end:.3f}_{fps:g}fps.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_path), "log": str(log_path), "selected": len(selected), "new": len(missing)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float)
    parser.add_argument("--fps", type=float, default=4.0)
    try:
        print(json.dumps(extract(**vars(parser.parse_args())), ensure_ascii=False))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Extraction failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
