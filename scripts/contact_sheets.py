#!/usr/bin/env python3
"""Build labeled overview sheets from extracted frames; preserve originals separately."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build(review_dir, columns=4, rows=4, tile_width=400):
    from PIL import Image, ImageDraw, ImageFont
    review_dir = review_dir.expanduser().resolve(strict=True)
    frames = json.loads((review_dir / "logs" / "frame_manifest.json").read_text(encoding="utf-8"))
    if not frames:
        raise ValueError("No extracted frames")
    output = review_dir / "frames" / "contact_sheets"
    output.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default()
    paths = []
    for batch in range(0, len(frames), columns * rows):
        items = frames[batch:batch + columns * rows]
        thumb_height = int(tile_width * 9 / 16)
        canvas = Image.new("RGB", (columns * tile_width, rows * (thumb_height + 28)), "#202020")
        draw = ImageDraw.Draw(canvas)
        for i, frame in enumerate(items):
            with Image.open(frame["original_image_path"]) as source:
                preview = source.convert("RGB")
            preview.thumbnail((tile_width, thumb_height), Image.Resampling.LANCZOS)
            col, row = i % columns, i // columns
            x = col * tile_width + (tile_width - preview.width) // 2
            y = row * (thumb_height + 28) + (thumb_height - preview.height) // 2
            canvas.paste(preview, (x, y))
            label = f"frame {frame['frame_index']} | PTS {frame['time_sec']:.3f}s"
            draw.text((col * tile_width + 8, row * (thumb_height + 28) + thumb_height + 5),
                      label, fill="white", font=font)
        path = output / f"sheet_{batch // (columns * rows):03d}.jpg"
        canvas.save(path, quality=90)
        paths.append(str(path))
    (review_dir / "logs" / "contact_sheets.json").write_text(
        json.dumps({"sheets": paths, "frame_indices": [f["frame_index"] for f in frames],
                    "note": "Overview thumbnails only; inspect source PNGs for local details."},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.review_dir), ensure_ascii=False))
    except (OSError, ValueError, ImportError) as exc:
        parser.exit(1, f"Contact sheets failed: {exc}\n")


if __name__ == "__main__":
    main()
