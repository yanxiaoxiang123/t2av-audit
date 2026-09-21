import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

from review_result import validate_audio_boundaries, validate_state_transitions, verify_workflow
from review_workflow import freeze, init, validate_challenge, validate_initial
from workflow_common import STATE_DIMENSIONS, sha256_bytes, validate_prompt_plan


class StagedReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="t2av-staged-review-")
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "run"
        (self.run / "logs").mkdir(parents=True)
        self.prompt = "A person lifts a red cup, places it in a box, and says hello. A bell rings."
        self.pts = [{"frame_index": n, "time_sec": n / 10} for n in range(8)]
        (self.run / "input.json").write_text(json.dumps({
            "prompt": self.prompt, "schema_version": "t2av_score_v1",
            "media_metadata": {"duration_sec": 0.7},
        }))
        (self.run / "logs" / "frame_pts.json").write_text(json.dumps(self.pts))
        frame_dir = self.run / "frames" / "original"
        frame_dir.mkdir(parents=True)
        manifest = []
        for point in self.pts:
            path = frame_dir / f"frame_{point['frame_index']:06d}.png"
            Image.new("RGB", (16, 16), (point["frame_index"], 0, 0)).save(path)
            manifest.append({**point, "width": 16, "height": 16,
                             "original_image_path": str(path.resolve())})
        (self.run / "logs" / "frame_manifest.json").write_text(json.dumps(manifest))
        self.board_image = self.run / "board.png"
        Image.new("RGB", (16, 16), "white").save(self.board_image)
        self.board = self.run / "board_manifest.json"
        self.write_board(self.board, [0.1, 0.4], [1, 2, 3, 4], [0, 0, 16, 16])
        self.plan = {
            "prompt_sha256": sha256_bytes(self.prompt.encode()),
            "prompt_checks": [self.prompt_check("R1", "human_object_action", "lift", 0, 16, 0)],
        }
        self.initial_check = self.make_initial()

    def prompt_check(self, rid, claim_type, predicate, start, end, sequence):
        return {
            "requirement_id": rid, "claim_type": claim_type, "subject_id": "person_1",
            "predicate": predicate, "object_id": "cup_1", "expected_before": "not_done",
            "expected_after": "done", "sequence_index": sequence,
            "prompt_span": [start, end], "prompt_quote": self.prompt[start:end],
            "metric_ids": ["VF", "TS", "PH", "HM", "PS"],
            "strict_transition_required": claim_type in {"physical_action", "human_object_action"},
        }

    def write_board(self, path, interval, indices, roi, columns=2, rows=2):
        import hashlib
        digest = hashlib.sha256(self.board_image.read_bytes()).hexdigest()
        path.write_text(json.dumps({
            "interval_sec": interval, "source_frame_indices": indices,
            "roi_xyxy": roi, "columns": columns, "rows": rows,
            "original_frame_size": [16, 16],
            "board_paths": [str(self.board_image.resolve())],
            "board_sha256": {str(self.board_image.resolve()): digest},
        }))

    def dimensions(self, status="applicable_consistent"):
        return [{
            "dimension": name,
            "status": status if name == "motion_force_causality" else "not_applicable",
            "observation": "Checked from original frames.",
            "frame_indices": [1, 2, 3, 4] if name == "motion_force_causality" else [],
            "strongest_counterexample": "No unexplained reversal found." if name == "motion_force_causality" else None,
        } for name in STATE_DIMENSIONS]

    def make_initial(self):
        return {
            "transition_id": "T1", "requirement_id": "R1",
            "source_interval_sec": [0.1, 0.4], "source_frame_indices": [1, 2, 3, 4],
            "board_manifest_path": str(self.board.resolve()),
            "before_frame_index": 1, "during_frame_indices": [2, 3], "after_frame_index": 4,
            "before_observation": "Cup is on the table.",
            "transition_observation": "Hand contacts and lifts the cup.",
            "after_observation": "Cup is held above the table.",
            "entity_ledger": [{
                "entity_id": "cup_1", "before_state": "on table", "during_state": "in hand",
                "after_state": "above table", "allowed_changes": ["position"],
                "actual_changes": ["position"], "status": "tracked",
            }],
            "dimension_checks": self.dimensions(),
            "strongest_counterexamples": ["Identity replacement was searched and not observed."],
            "initial_verdict": "confirmed_consistent",
            "initial_evidence_frame_indices": [1, 2, 3, 4], "evidence_ids": ["E1"],
        }

    def test_non_atomic_predicate_is_rejected(self):
        bad = json.loads(json.dumps(self.plan))
        bad["prompt_checks"][0]["predicate"] = "lift_and_place"
        self.assertTrue(any("multi-claim" in error for error in validate_prompt_plan(bad, self.prompt)))

    def test_structural_positive_claim_types(self):
        claim_types = ["identity", "count", "attribute", "spatial_relation", "physical_action",
                       "human_object_action", "text", "dialogue", "audio", "temporal"]
        plan = {"prompt_checks": [self.prompt_check(f"R{i}", kind, f"claim_{i}", 0, 1, i)
                                   for i, kind in enumerate(claim_types)]}
        self.assertEqual(validate_prompt_plan(plan, self.prompt), [])

    def test_structural_positive_video_scenarios(self):
        scenarios = [
            ("static_scene", "identity", "remain_visible"),
            ("person_action", "physical_action", "walk"),
            ("container_operation", "human_object_action", "open"),
            ("object_transfer", "human_object_action", "hand_over"),
            ("vehicle_motion", "physical_action", "drive"),
            ("on_screen_text", "text", "display"),
            ("dialogue", "dialogue", "speak"),
            ("audio_requirement", "audio", "ring"),
        ]
        plan = {"prompt_checks": [
            self.prompt_check(f"R{i}-{name}", kind, predicate, 0, 1, i)
            for i, (name, kind, predicate) in enumerate(scenarios)
        ]}
        self.assertEqual(validate_prompt_plan(plan, self.prompt), [])

    def test_wide_window_and_outside_boundary_are_rejected(self):
        bad = json.loads(json.dumps(self.initial_check))
        bad["source_interval_sec"] = [0.0, 2.1]
        self.assertTrue(any("no longer than 2 seconds" in e for e in
                            validate_initial({"state_transition_checks": [bad]}, self.plan, self.pts)))
        bad = json.loads(json.dumps(self.initial_check))
        bad["before_frame_index"] = 0
        self.assertTrue(any("inside the local interval" in e for e in
                            validate_initial({"state_transition_checks": [bad]}, self.plan, self.pts)))

    def test_board_over_2x2_is_rejected(self):
        self.write_board(self.board, [0.1, 0.4], [1, 2, 3, 4], [0, 0, 16, 16], columns=3)
        errors = validate_initial({"state_transition_checks": [self.initial_check]}, self.plan, self.pts)
        self.assertTrue(any("at most 2x2" in e for e in errors))

    def test_skipped_boundary_frame_is_rejected(self):
        bad = json.loads(json.dumps(self.initial_check))
        bad["initial_evidence_frame_indices"] = [1, 2, 4]
        errors = validate_initial({"state_transition_checks": [bad]}, self.plan, self.pts)
        self.assertTrue(any("every source frame between stable states" in e for e in errors))

    def test_challenge_requires_frozen_initial_and_new_evidence(self):
        challenge = {"challenge_reviews": [{
            "transition_id": "T1", "method": "self_blind", "verdict": "confirmed_consistent",
            "observation": "Rechecked identity and trajectory.", "frame_indices": [1, 2, 3, 4],
        }]}
        self.assertTrue(any("new frames" in e for e in
                            validate_challenge(challenge, {"state_transition_checks": [self.initial_check]})))
        init(self.run)
        initial_path = self.run / "initial-source.json"
        initial_path.write_text(json.dumps({"state_transition_checks": [self.initial_check]}))
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "review_workflow.py"), "freeze-initial", str(self.run), str(initial_path)],
            capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("freeze plan first", result.stderr)

    def test_tighter_roi_is_independent_challenge(self):
        challenge_board = self.run / "challenge_board_manifest.json"
        initial_image = self.board_image
        self.board_image = self.run / "challenge_board.png"
        Image.new("RGB", (12, 12), "gray").save(self.board_image)
        self.write_board(challenge_board, [0.1, 0.4], [1, 2, 3, 4], [2, 2, 14, 14])
        self.board_image = initial_image
        challenge = {"challenge_reviews": [{
            "transition_id": "T1", "method": "self_blind", "verdict": "confirmed_consistent",
            "observation": "Tighter crop retains the same entity.", "frame_indices": [1, 2, 3, 4],
            "board_manifest_path": str(challenge_board.resolve()),
        }]}
        self.assertEqual(validate_challenge(challenge, {"state_transition_checks": [self.initial_check]}), [])

    def transition_errors(self, verdict, categories, affected, score, severity=None,
                          status="applicable_uncertain"):
        check = json.loads(json.dumps(self.initial_check))
        check["dimension_checks"] = self.dimensions(status)
        check.update({
            "challenge_review": {"transition_id": "T1", "method": "self_blind",
                                 "verdict": verdict, "observation": "Independent evidence checked.",
                                 "frame_indices": [5]},
            "conflict_resolution": None, "verdict": verdict,
            "issue_categories": categories, "severity": severity,
            "affected_metric_ids": affected,
        })
        check["initial_verdict"] = verdict
        metrics = [{"metric_id": mid, "status": "已评分", "score": score}
                   for mid in ("VQ", "VF", "TS", "PH", "HM", "PS")]
        evidence = [{"evidence_id": "E1", "frames": [{"frame_index": n} for n in [1, 2, 3, 4]]}]
        frames = {n: {"frame_index": n, "time_sec": n / 10} for n in range(8)}
        errors = []
        validate_state_transitions(
            self.plan["prompt_checks"], evidence, metrics,
            {"state_transition_checks": [check]}, frames, list(frames), list(frames), self.pts,
            {"R1": {"status": "部分呈现"}}, lambda group, message: errors.append(message))
        return errors

    def test_uncertain_score_cap_and_defect_propagation(self):
        errors = self.transition_errors("uncertain", ["prompt_action"], ["VF"], 4)
        self.assertTrue(any("caps VF at 3" in e for e in errors))
        errors = self.transition_errors("confirmed_defect", ["geometry_topology"], ["VQ"], 2,
                                        severity="critical", status="applicable_defect")
        self.assertTrue(any("not propagated" in e and "PH" in e for e in errors))

    def test_defect_severity_cap(self):
        errors = self.transition_errors("confirmed_defect", ["prompt_action"], ["VF"], 4,
                                        severity="major", status="applicable_defect")
        self.assertTrue(any("caps VF at 3" in e for e in errors))

    def test_af_cannot_penalize_unrequested_natural_sound(self):
        errors = []
        validate_audio_boundaries(
            self.plan["prompt_checks"], [{"metric_id": "AF", "status": "已评分", "score": 3}],
            lambda group, message: errors.append(message))
        self.assertTrue(any("no explicit audio requirement" in e for e in errors))

    def test_checkpoint_mutation_is_detected(self):
        init(self.run)
        plan_source = self.run / "plan-source.json"
        plan_source.write_text(json.dumps(self.plan))
        freeze(self.run, plan_source, "plan")
        (self.run / "workflow" / "plan.json").write_text("{}")
        errors = []
        verify_workflow({"prompt_checks": [], "inspection": {}}, self.run,
                        {"prompt": self.prompt}, lambda group, message: errors.append(message))
        self.assertTrue(any("checkpoint hash mismatch" in e for e in errors))

    def test_frozen_evidence_mutation_is_detected(self):
        init(self.run)
        plan_source = self.run / "plan-source.json"
        initial_source = self.run / "initial-source.json"
        plan_source.write_text(json.dumps(self.plan))
        initial_source.write_text(json.dumps({"state_transition_checks": [self.initial_check]}))
        freeze(self.run, plan_source, "plan")
        freeze(self.run, initial_source, "initial")
        self.board_image.write_bytes(self.board_image.read_bytes() + b"changed")
        errors = []
        verify_workflow({"prompt_checks": [], "inspection": {}}, self.run,
                        {"prompt": self.prompt}, lambda group, message: errors.append(message))
        self.assertTrue(any("asset hash mismatch" in e for e in errors))

    def test_next_stage_rejects_modified_previous_checkpoint(self):
        init(self.run)
        source = self.run / "plan-source.json"
        source.write_text(json.dumps(self.plan))
        freeze(self.run, source, "plan")
        (self.run / "workflow" / "plan.json").write_text("{}")
        initial = self.run / "initial-source.json"
        initial.write_text(json.dumps({"state_transition_checks": [self.initial_check]}))
        with self.assertRaisesRegex(ValueError, "checkpoint hash mismatch"):
            freeze(self.run, initial, "initial")


if __name__ == "__main__":
    unittest.main()
