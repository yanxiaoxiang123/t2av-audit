from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audio_review import (
    compare, compare_detailed, fulfillment, listening_items, pcm_candidates,
    requirements_from_plan, validate_reviews, windows,
)
from review_result import validate_requirement_audio_result
from analyze_audio import sha256


class AudioReviewTests(unittest.TestCase):
    def test_masked_sound_stays_uncertain(self):
        raw = {"reviews": [{"requirement_id": "A", "state": "absent", "masked": True,
                "event_count": 0, "event_times_sec": [], "time_uncertainty_sec": None,
                "observation": "音乐遮蔽", "limitations": []}]}
        review = validate_reviews(raw, [{"requirement_id": "A"}], 2)[0]
        self.assertEqual(review["state"], "uncertain")
        self.assertEqual(compare(review, [review], True)[0], "uncertain")

    def test_similar_transient_conflict_is_not_absence(self):
        full = {"state": "absent"}
        local = [{"state": "present", "event_count": 1}]
        status, conflicts = compare(full, local, True)
        self.assertEqual(status, "uncertain")
        self.assertTrue(conflicts)
        detailed = compare_detailed(full, local, True, {"event_kind": "discrete"})
        self.assertEqual(detailed[0], "uncertain")
        self.assertIn("full_absent_local_present", detailed[1])

    def test_repeated_event_count_conflict(self):
        full = {"state": "present", "event_count": 1}
        local = [{"state": "present", "event_count": 2}]
        self.assertEqual(compare(full, local, True)[0], "uncertain")
        self.assertEqual(compare_detailed(full, local, True, {"event_kind": "discrete"})[0], "uncertain")

    def test_timing_conflict_preserves_confirmed_audibility(self):
        full = {"state": "present", "event_count": 1, "sound_class": "speech",
                "source_event_times_sec": [1.2], "time_uncertainty_sec": 0.1}
        local = [{"state": "present", "event_count": 1, "sound_class": "speech",
                  "source_event_times_sec": [2.11], "time_uncertainty_sec": 0.1,
                  "source_start_time_sec": 1.0, "source_end_time_sec": 3.0,
                  "segment_id": "local_001", "audio_path": "/tmp/local.wav"}]
        req = {"event_kind": "discrete", "polarity": "required", "metric_ids": ["AF"]}
        status, conflicts, timing_status, _ = compare_detailed(full, local, True, req)
        self.assertEqual(status, "confirmed_present")
        self.assertEqual(fulfillment(req, status, full), "satisfied")
        self.assertEqual(timing_status, "conflict")
        self.assertIn("full_local_timing_conflict", conflicts)
        entry = {"requirement_id": "A", "status": status, "fulfillment": "satisfied",
                 "timing_status": timing_status, "conflicts": conflicts, "local_reviews": local}
        self.assertEqual(len(listening_items(entry, {})), 1)

    def test_masked_local_window_does_not_erase_a_clear_positive(self):
        full = {"state": "present", "event_count": 1, "sound_class": "speech"}
        local = [
            {"state": "present", "event_count": 1, "sound_class": "speech",
             "source_start_time_sec": 0.0, "source_end_time_sec": 2.5},
            {"state": "uncertain", "event_count": None, "sound_class": "unknown",
             "source_start_time_sec": 2.25, "source_end_time_sec": 5.0},
        ]
        status, conflicts, _, _ = compare_detailed(full, local, True, {"event_kind": "unknown"})
        self.assertEqual(status, "confirmed_present")
        self.assertEqual(conflicts, [])

    def test_specific_sound_class_conflict_stays_uncertain(self):
        full = {"state": "present", "event_count": 1, "sound_class": "gunshot",
                "source_event_times_sec": [1.2], "time_uncertainty_sec": 0.1}
        local = [{"state": "present", "event_count": 1, "sound_class": "metallic_impact",
                  "source_event_times_sec": [1.25], "time_uncertainty_sec": 0.1,
                  "source_start_time_sec": 1.0, "source_end_time_sec": 3.0}]
        status, conflicts, _, _ = compare_detailed(full, local, True, {"event_kind": "discrete"})
        self.assertEqual(status, "uncertain")
        self.assertIn("full_local_category_conflict", conflicts)

    def test_overlapping_local_time_conflict_is_recorded(self):
        full = {"state": "present", "event_count": 1, "sound_class": "speech",
                "source_event_times_sec": [], "time_uncertainty_sec": None}
        local = [
            {"state": "present", "event_count": 1, "sound_class": "speech",
             "source_event_times_sec": [1.2], "time_uncertainty_sec": 0.1,
             "source_start_time_sec": 0.0, "source_end_time_sec": 2.5},
            {"state": "present", "event_count": 1, "sound_class": "speech",
             "source_event_times_sec": [2.1], "time_uncertainty_sec": 0.1,
             "source_start_time_sec": 0.5, "source_end_time_sec": 3.0},
        ]
        status, conflicts, timing_status, _ = compare_detailed(
            full, local, True, {"event_kind": "discrete"})
        self.assertEqual(status, "confirmed_present")
        self.assertEqual(timing_status, "conflict")
        self.assertIn("local_local_timing_conflict", conflicts)

    def test_generic_transient_does_not_confirm_a_metal_target(self):
        raw = {"reviews": [{"requirement_id": "A", "state": "present", "masked": False,
                "sound_class": "impulse", "event_count": 1, "event_times_sec": [0.2],
                "time_uncertainty_sec": 0.1, "observation": "听到一个瞬态", "limitations": []}]}
        req = [{"requirement_id": "A", "target": "metallic clang"}]
        review = validate_reviews(raw, req, 1)[0]
        self.assertEqual(review["state"], "uncertain")

    def test_segment_boundary_has_overlap(self):
        parts = [x for x in windows(6.0, []) if x[2] == "coverage"]
        self.assertEqual(parts[0][:2], (0.0, 2.5))
        self.assertLess(parts[1][0], parts[0][1])
        self.assertEqual(parts[-1][1], 6.0)

    def test_failed_local_analysis_blocks_absence(self):
        self.assertEqual(compare({"state": "absent"}, [{"state": "analysis_failed"}], True)[0],
                         "analysis_failed")

    def test_absence_requires_agreement(self):
        self.assertEqual(compare({"state": "absent"}, [{"state": "absent"}], True)[0],
                         "confirmed_absent")
        self.assertEqual(compare({"state": "absent"}, [{"state": "uncertain"}], True)[0],
                         "uncertain")

    def test_prompt_count_and_forbidden_audio_are_separate_from_audibility(self):
        present = {"state": "present", "event_count": 2}
        self.assertEqual(fulfillment({"polarity": "required", "expected_count": 1},
                                     "confirmed_present", present), "violated")
        self.assertEqual(fulfillment({"polarity": "forbidden", "expected_count": None},
                                     "confirmed_present", present), "violated")
        self.assertEqual(fulfillment({"polarity": "forbidden", "expected_count": None},
                                     "confirmed_absent", {"state": "absent"}), "satisfied")

    def test_pcm_peak_is_only_a_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tone.wav"
            rate = 8000
            samples = bytearray(rate * 2)
            for i in range(rate // 2, rate // 2 + 80):
                samples[i*2:i*2+2] = (20000).to_bytes(2, "little", signed=True)
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
                wav.writeframes(samples)
            peaks = pcm_candidates(path)
            self.assertTrue(any(0.45 <= t <= 0.55 for t in peaks))

    def test_infers_exact_one_count_from_frozen_prompt(self):
        requirements = requirements_from_plan({'prompt_checks': [{
            'requirement_id': 'A', 'claim_type': 'audio',
            'prompt_quote': 'exactly one starting-pistol shot',
            'predicate': 'starting pistol shot', 'expected_after': None,
            'metric_ids': ['AF']} ]})
        self.assertEqual(requirements[0]['event_kind'], 'discrete')
        self.assertEqual(requirements[0]['expected_count'], 1)
        self.assertEqual(requirements[0]['polarity'], 'required')

    def test_infers_forbidden_music_without_making_all_audio_forbidden(self):
        forbidden = requirements_from_plan({'prompt_checks': [{
            'requirement_id': 'A', 'claim_type': 'audio',
            'prompt_quote': 'no non-diegetic music', 'predicate': 'music',
            'expected_after': None, 'metric_ids': ['MU']} ]})[0]
        self.assertEqual(forbidden['polarity'], 'forbidden')
        shot = requirements_from_plan({'prompt_checks': [{
            'requirement_id': 'B', 'claim_type': 'audio',
            'prompt_quote': 'no second shot after the first', 'predicate': 'shot',
            'expected_after': None, 'metric_ids': ['AF']} ]})[0]
        self.assertEqual(shot['polarity'], 'required')

    def test_finalizer_rejects_uncertain_as_missing_and_precise_offset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workflow").mkdir()
            (root / "audio" / "requirements").mkdir(parents=True)
            plan = root / "workflow" / "plan.json"
            plan.write_text("{}")
            digest = sha256(plan)
            (root / "workflow" / "state.json").write_text(json.dumps({"stages": {"plan": {
                "checkpoint_sha256": digest}}}))
            (root / "audio" / "requirements" / "audio_requirements.json").write_text(json.dumps({
                "schema_version": "t2av_audio_requirements_v1",
                "source": {"sha256": "video", "plan_sha256": digest},
                "requirements": [{"requirement_id": "A", "prompt_quote": "shot",
                                  "status": "uncertain", "conflicts": ["count mismatch"]}]}))
            checks = [{"requirement_id": "A", "claim_type": "audio", "prompt_quote": "shot",
                       "status": "未呈现"}]
            metrics = [{"metric_id": "AF", "requirement_ids": ["A"], "confidence": "高", "uncertainty": ""}]
            evidence = [{"evidence_id": "E", "synchronization": [{"audio_requirement_id": "A",
                "offset_sec": 0.02, "time_uncertainty_sec": 0.02, "measurement_method": "Gemini",
                "visual_onset_sec": 1.0}]}]
            errors = []
            validate_requirement_audio_result({}, root, {"video_sha256": "video"}, checks,
                                              metrics, evidence, lambda group, msg: errors.append(msg))
            self.assertTrue(any("unresolved audio" in e for e in errors))
            self.assertTrue(any("numeric AV offset" in e for e in errors))
            self.assertTrue(any("low confidence" in e for e in errors))

    def test_contract3_rejects_numeric_offset_from_gemini_estimates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "workflow").mkdir()
            (root / "audio" / "requirements").mkdir(parents=True)
            plan = root / "workflow" / "plan.json"
            plan.write_text("{}")
            digest = sha256(plan)
            (root / "workflow" / "state.json").write_text(json.dumps({"stages": {"plan": {
                "checkpoint_sha256": digest}}}))

            full = {"state": "present", "sound_class": "speech", "event_count": 1,
                    "source_event_times_sec": [1.2], "time_uncertainty_sec": 0.05,
                    "audio_path": "/tmp/full.wav"}
            local = [{"state": "present", "sound_class": "speech", "event_count": 1,
                      "source_event_times_sec": [1.2], "time_uncertainty_sec": 0.05,
                      "source_start_time_sec": 0.0, "source_end_time_sec": 5.0,
                      "segment_id": "local_001", "audio_path": "/tmp/local.wav"}]
            req = {"requirement_id": "A", "prompt_quote": "A spoken line is heard in shot 1.",
                   "predicate": "spoken_line", "target": "spoken line", "event_kind": "unknown",
                   "polarity": "required", "expected_count": None, "metric_ids": ["AV"]}
            status, conflicts, timing, estimates = compare_detailed(full, local, True, req)
            entry = {**req, "status": status, "audibility_status": status,
                     "fulfillment": fulfillment(req, status, full), "timing_status": timing,
                     "timing_conflicts": [], "timing_estimates": estimates,
                     "visual_binding_required": True, "coverage_complete": True,
                     "full_review": full, "local_reviews": local, "conflicts": conflicts}
            result = {"schema_version": "t2av_audio_requirements_v1", "contract_version": 3,
                      "source": {"sha256": "video", "plan_sha256": digest,
                                 "has_audio_stream": True}, "segments": [],
                      "requirements": [entry]}
            (root / "audio" / "requirements" / "audio_requirements.json").write_text(json.dumps(result))

            evidence = [{"evidence_id": "E", "requirement_ids": ["A"],
                         "frames": [{"time_sec": 1.0}],
                         "audio_segments": [{"audio_path": "/tmp/local.wav",
                                             "source_start_time_sec": 0.0,
                                             "source_end_time_sec": 5.0}],
                         "synchronization": [{"audio_requirement_id": "A", "offset_sec": 0.2,
                             "visual_onset_sec": 1.0, "audio_onset_sec": 1.2,
                             "time_uncertainty_sec": 0.05,
                             "measurement_method": "Gemini onset estimate"}]}]
            check = {"requirement_id": "A", "claim_type": "audio",
                     "prompt_quote": req["prompt_quote"], "metric_ids": ["AV"],
                     "status": "已呈现", "evidence_ids": ["E"],
                     "audio_binding": {"status": "consistent", "reason": "Both estimates fall in shot 1.",
                         "audio_time_estimates_sec": [1.2], "visual_interval_sec": [0.5, 1.5],
                         "evidence_ids": ["E"]}}
            errors = []
            validate_requirement_audio_result(
                {}, root, {"video_sha256": "video", "media_metadata": {"duration_sec": 5.0}},
                [check], [{"metric_id": "AV", "requirement_ids": ["A"],
                           "confidence": "高", "uncertainty": ""}], evidence,
                lambda group, msg: errors.append(msg))
            self.assertTrue(any("cannot use Gemini" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
