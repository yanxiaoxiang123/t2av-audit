import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
class ConcurrentExtractionTests(unittest.TestCase):
    def test_all_frames_and_unlabeled_board(self):
        with tempfile.TemporaryDirectory(prefix="t2av-all-frames-test-") as temp:
            root = Path(temp)
            video = root / "source.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=blue:s=64x64:r=12:d=1", "-c:v", "libx264", "-pix_fmt",
                 "yuv420p", str(video)], check=True, capture_output=True,
            )
            prepared = subprocess.run(
                [sys.executable, str(SCRIPTS / "prepare_review.py"), str(video),
                 "--prompt-text", "A blue frame."], check=True, capture_output=True, text=True,
            )
            run = Path(prepared.stdout.strip())
            subprocess.run(
                [sys.executable, str(SCRIPTS / "extract_frames.py"), str(run),
                 "--start", "0", "--end", "0.5", "--all"],
                check=True, capture_output=True, text=True,
            )
            points = json.loads((run / "logs/frame_pts.json").read_text())
            expected = [p["frame_index"] for p in points if p["time_sec"] <= 0.5 + 1e-6]
            manifest = json.loads((run / "logs/frame_manifest.json").read_text())
            self.assertEqual([f["frame_index"] for f in manifest], expected)
            board = subprocess.run(
                [sys.executable, str(SCRIPTS / "continuity_board.py"), str(run),
                 "--start", "0", "--end", "0.5", "--roi", "0", "0", "32", "32",
                 "--name", "R1"], check=True, capture_output=True, text=True,
            )
            report = json.loads(board.stdout)
            self.assertEqual(report["source_frame_indices"], expected)
            self.assertTrue(all(Path(path).is_file() for path in report["board_paths"]))
            self.assertEqual(report["roi_xyxy"], [0, 0, 32, 32])
            (run / "logs/frame_manifest.json").write_text(json.dumps(manifest[:-1]))
            missing = subprocess.run(
                [sys.executable, str(SCRIPTS / "continuity_board.py"), str(run),
                 "--start", "0", "--end", "0.5", "--roi", "0", "0", "32", "32",
                 "--name", "missing"], capture_output=True, text=True,
            )
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("Extract all source frames first", missing.stderr)

    def test_parallel_intervals_preserve_manifest_union(self):
        with tempfile.TemporaryDirectory(prefix="t2av-extract-test-") as temp:
            root = Path(temp)
            video = root / "source.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi",
                 "-i", "color=c=blue:s=64x64:r=24:d=2", "-c:v", "libx264", "-pix_fmt",
                 "yuv420p", str(video)], check=True, capture_output=True,
            )
            prepared = subprocess.run(
                [sys.executable, str(SCRIPTS / "prepare_review.py"), str(video),
                 "--prompt-text", "A blue frame."], check=True, capture_output=True, text=True,
            )
            run = Path(prepared.stdout.strip())

            def extract(start, end):
                return subprocess.run(
                    [sys.executable, str(SCRIPTS / "extract_frames.py"), str(run),
                     "--start", str(start), "--end", str(end), "--fps", "12"],
                    check=True, capture_output=True, text=True,
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(extract, 0, 1), pool.submit(extract, 1, 2)]
                for future in futures:
                    future.result()

            manifest = json.loads((run / "logs/frame_manifest.json").read_text())
            selected = set()
            for log_path in (run / "logs").glob("extraction_*.json"):
                selected.update(json.loads(log_path.read_text())["selected_frame_indices"])
            self.assertEqual({item["frame_index"] for item in manifest}, selected)
            self.assertEqual(len(manifest), len(selected))
            self.assertTrue(all(Path(item["original_image_path"]).is_file() for item in manifest))


if __name__ == "__main__":
    unittest.main()
