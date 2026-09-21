"""Native-size crop inspection can cover frames without claiming originals were opened."""
import json
from pathlib import Path


def inspection_frames(check):
    opened = check.get("initial_evidence_frame_indices", [])
    covered = set(opened)
    if check.get("inspection_mode", "originals") == "originals":
        return covered, []
    if check.get("inspection_mode") != "native_board":
        return covered, ["unknown inspection_mode"]
    try:
        board = json.loads(Path(check["board_manifest_path"]).read_text(encoding="utf-8"))
        pages = board["board_paths"]
        if not pages or check.get("board_pages_viewed") != pages:
            return covered, ["native_board requires every board page actually viewed in order"]
        if check.get("native_resolution_verified") is not True:
            return covered, ["native_board requires verification that displayed crops were not downscaled"]
        from PIL import Image
        for raw in pages:
            with Image.open(raw) as im:
                if max(im.size) > 1600:
                    return covered, ["native_board pages exceed 1600 pixels; use smaller ROI or originals"]
        reviewed = check.get("board_reviewed_frame_indices")
        if reviewed != board["source_frame_indices"]:
            return covered, ["native_board must account for every source frame on the viewed pages"]
        covered.update(reviewed)
        return covered, []
    except (OSError, ValueError, KeyError, TypeError):
        return covered, ["native_board inspection manifest is invalid"]
