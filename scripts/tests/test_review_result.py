import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_result import validate_continuity


class ContinuityValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="t2av-continuity-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        board_image = root / "board_000.png"
        Image.new("RGB", (8, 8)).save(board_image)
        self.board_manifest = root / "board_manifest.json"
        self.board_manifest.write_text(json.dumps({
            "interval_sec": [1 / 24, 2 / 24], "source_frame_indices": [1, 2],
            "roi_xyxy": [0, 0, 8, 8], "board_paths": [str(board_image)],
        }))
        self.requirements = [{"requirement_id": "R1", "metric_ids": ["PH", "HM"]}]
        self.evidence = [{"evidence_id": "E1", "frames": [{"frame_index": n} for n in range(4)]}]
        self.metrics = [
            {"metric_id": "PH", "status": "已评分", "score": 5},
            {"metric_id": "HM", "status": "已评分", "score": 5},
        ]
        self.frames = {n: {"frame_index": n, "time_sec": n / 24} for n in range(4)}
        self.check = {
            "requirement_id": "R1", "object_label": "lock shackle",
            "expected_invariants": "One rigid U-shaped shackle stays attached to the same lock.",
            "before_frame_index": 0, "during_frame_indices": [1, 2], "after_frame_index": 3,
            "before_observation": "The shackle is U-shaped.",
            "transition_observation": "The shape remains visible while moving.",
            "after_observation": "The same shackle is outside the body.",
            "source_interval_sec": [1 / 24, 2 / 24], "source_frame_indices": [1, 2],
            "board_manifest_path": str(self.board_manifest),
            "part_correspondence": [{"part_id": "shackle", "before_state": "open U with two ends",
                                     "after_state": "same two ends outside the lock",
                                     "trajectory_explanation": "Both ends can be followed through the turn.",
                                     "status": "tracked"}],
            "unexplained_changes": [], "initial_verdict": "confirmed_consistent",
            "topology_search": {
                "part_disappearance": {"status": "not_seen", "observation": "No part disappears.", "frame_indices": [1, 2]},
                "new_closed_loop": {"status": "not_seen", "observation": "No new closed loop appears.", "frame_indices": [1, 2]},
                "connection_change": {"status": "not_seen", "observation": "Connections remain stable.", "frame_indices": [1, 2]},
            },
            "challenge_review": {"method": "self_blind", "verdict": "confirmed_consistent",
                                 "observation": "Both shackle ends remain identifiable.",
                                 "frame_indices": [1, 2]},
            "conflict_resolution": None,
            "verdict": "confirmed_consistent", "affected_metric_ids": [], "evidence_ids": ["E1"],
        }

    def errors(self, checks, opened=None):
        result = []
        validate_continuity(
            self.requirements, self.evidence, self.metrics,
            {"object_continuity_checks": checks}, self.frames, [0, 1, 2, 3],
            [0, 1, 2, 3] if opened is None else opened, list(self.frames.values()),
            lambda group, message: result.append((group, message)),
        )
        return [message for _, message in result]

    def test_complete_consistent_check_passes(self):
        self.assertEqual(self.errors([self.check]), [])

    def test_missing_check_for_physical_requirement_fails(self):
        self.assertTrue(any("missing object continuity check" in error for error in self.errors([])))

    def test_unknown_and_out_of_order_frames_fail(self):
        bad = dict(self.check, during_frame_indices=[99])
        self.assertTrue(any("absent from source manifest" in error for error in self.errors([bad])))
        bad = dict(self.check, during_frame_indices=[2, 1])
        self.assertTrue(any("strictly ordered" in error for error in self.errors([bad])))

    def test_frames_must_be_in_referenced_evidence(self):
        self.evidence[0]["frames"] = [{"frame_index": n} for n in (0, 1, 3)]
        self.assertTrue(any("not covered by evidence_ids" in error for error in self.errors([self.check])))

    def test_confirmed_defect_contradicts_five(self):
        defect = dict(self.check, initial_verdict="confirmed_defect", verdict="confirmed_defect",
                      challenge_review=dict(self.check["challenge_review"], verdict="confirmed_defect"),
                      affected_metric_ids=["PH"], topology_search=dict(
                          self.check["topology_search"],
                          new_closed_loop={"status": "seen", "observation": "A second loop appears.", "frame_indices": [1, 2]}))
        self.assertTrue(any("contradicts PH score 5" in error for error in self.errors([defect])))
        self.metrics[0]["score"] = 2
        self.assertEqual(self.errors([defect]), [])

    def test_uncertain_check_does_not_create_a_defect(self):
        uncertain = dict(self.check, initial_verdict="uncertain", verdict="uncertain",
                         challenge_review=dict(self.check["challenge_review"], verdict="uncertain"),
                         affected_metric_ids=[])
        self.assertEqual(self.errors([uncertain]), [])

    def test_source_interval_cannot_skip_a_frame(self):
        bad = dict(self.check, source_frame_indices=[1])
        self.assertTrue(any("every extracted source frame" in error for error in self.errors([bad])))

    def test_key_original_must_be_opened(self):
        self.assertTrue(any("individually opened" in error for error in
                            self.errors([self.check], opened=[0, 1, 3])))

    def test_board_manifest_must_match_interval(self):
        bad = dict(self.check, source_interval_sec=[0, 2 / 24], source_frame_indices=[0, 1, 2])
        self.assertTrue(any("board manifest differs" in error for error in self.errors([bad])))

    def test_unexplained_part_cannot_be_consistent(self):
        bad = dict(self.check, unexplained_changes=["A second closed loop appeared."])
        self.assertTrue(any("contradict confirmed_consistent" in error for error in self.errors([bad])))

    def test_unresolved_challenge_conflict_cannot_be_consistent(self):
        bad = dict(self.check, challenge_review=dict(self.check["challenge_review"],
                                                      verdict="confirmed_defect"))
        self.assertTrue(any("reviewer conflict" in error for error in self.errors([bad])))

    def test_conflict_resolution_requires_disagreement(self):
        bad = dict(self.check, conflict_resolution={"verdict": "confirmed_consistent",
                                                     "observation": "Same conclusion.",
                                                     "frame_indices": [1, 2]})
        self.assertTrue(any("only allowed for reviewer disagreement" in error for error in self.errors([bad])))

    def test_source_frame_indices_must_be_pts_ordered(self):
        bad = dict(self.check, source_frame_indices=[2, 1])
        self.assertTrue(any("every extracted source frame" in error for error in self.errors([bad])))


if __name__ == "__main__":
    unittest.main()
