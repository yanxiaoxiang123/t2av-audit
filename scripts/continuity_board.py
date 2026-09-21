#!/usr/bin/env python3
"""Make unlabeled, chronological source-frame crop boards for visual continuity review."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def make_boards(review_dir, start, end, roi, name, columns=2, rows=2):
    from PIL import Image, ImageDraw, ImageFont

    review_dir = Path(review_dir).expanduser().resolve(strict=True)
    if not (0 <= start < end) or not (1 <= columns <= 2 and 1 <= rows <= 2):
        raise ValueError("Invalid interval or grid dimensions")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Name must contain only letters, digits, underscores, or hyphens")
    points = json.loads((review_dir / "logs" / "frame_pts.json").read_text(encoding="utf-8"))
    manifest = json.loads((review_dir / "logs" / "frame_manifest.json").read_text(encoding="utf-8"))
    saved = {item["frame_index"]: item for item in manifest}
    selected = [item for item in points if start - 1e-6 <= item["time_sec"] <= end + 1e-6]
    if not selected:
        raise ValueError("No source frames in interval")
    missing = [item["frame_index"] for item in selected if item["frame_index"] not in saved]
    if missing:
        raise ValueError(f"Extract all source frames first; missing indices: {missing[:12]}")
    left, top, right, bottom = roi
    width, height = saved[selected[0]["frame_index"]]["width"], saved[selected[0]["frame_index"]]["height"]
    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
        raise ValueError("ROI is outside the original frame")
    output_dir = review_dir / "frames" / "continuity" / name
    output_dir.mkdir(parents=True, exist_ok=True)
    tile_width, tile_height = right - left, bottom - top
    label_height = 34
    per_page = columns * rows
    font = ImageFont.load_default()
    paths = []
    for page_start in range(0, len(selected), per_page):
        page_items = selected[page_start:page_start + per_page]
        board = Image.new("RGB", (columns * tile_width, rows * (tile_height + label_height)), "black")
        draw = ImageDraw.Draw(board)
        for offset, point in enumerate(page_items):
            source = saved[point["frame_index"]]
            with Image.open(source["original_image_path"]) as image:
                if image.size != (source["width"], source["height"]):
                    raise ValueError(f"Frame {point['frame_index']} size differs from manifest")
                crop = image.convert("RGB").crop((left, top, right, bottom))
            x = (offset % columns) * tile_width
            y = (offset // columns) * (tile_height + label_height)
            board.paste(crop, (x, y + label_height))
            draw.text((x + 8, y + 9), f"F{point['frame_index']}  PTS {point['time_sec']:.6f}s",
                      fill="white", font=font)
        path = output_dir / f"board_{page_start // per_page:03d}.png"
        board.save(path)
        paths.append(str(path.resolve()))
    def digest(raw_path):
        value = hashlib.sha256()
        with Path(raw_path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return value.hexdigest()

    report = {
        "interval_sec": [start, end], "roi_xyxy": list(roi),
        "source_frame_indices": [item["frame_index"] for item in selected],
        "columns": columns, "rows": rows,
        "original_frame_size": [width, height],
        "board_paths": paths,
        "board_sha256": {path: digest(path) for path in paths},
    }
    report_path = output_dir / "board_manifest.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"board_manifest_path": str(report_path.resolve()), **report}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--roi", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--columns", type=int, default=2)
    parser.add_argument("--rows", type=int, default=2)
    args = parser.parse_args()
    try:
        print(json.dumps(make_boards(args.review_dir, args.start, args.end, args.roi,
                                     args.name, args.columns, args.rows), ensure_ascii=False))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Board generation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
