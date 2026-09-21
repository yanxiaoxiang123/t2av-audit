import copy
import unittest

import test_review_result
from action_evidence import validate_action_evidence


class ActionEvidenceTests(unittest.TestCase):
    def setUp(self):
        fixture = test_review_result.StagedReviewTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.check = fixture.initial_check
        self.requirement = fixture.plan["prompt_checks"][0]

    def errors(self):
        return validate_action_evidence(self.check, self.requirement)

    def test_visible_translation_passes(self):
        self.assertEqual(self.errors(), [])

    def test_wrong_actor_cannot_substitute_for_current_action(self):
        self.check["action_binding"]["subject_id"] = "other_hand"
        self.assertTrue(any("action_binding" in e for e in self.errors()))

    def test_support_of_one_object_does_not_prove_interaction(self):
        dim = self.check["dimension_checks"][6]
        dim["assessed_entity_ids"] = ["cup_1"]
        self.assertTrue(any("both current action participants" in e for e in self.errors()))

    def test_invisible_internal_mechanism_is_not_a_defect(self):
        dim = self.check["dimension_checks"][6]
        dim.update(evidence_basis="unobservable_internal", status="applicable_uncertain")
        self.assertTrue(any("internal invisibility" in e for e in self.errors()))
        dim["status"] = "not_applicable"
        self.assertEqual(self.errors(), [])

    def test_external_proxy_needs_causal_link(self):
        dim = self.check["dimension_checks"][6]
        dim["evidence_basis"] = "external_proxy"
        self.assertTrue(any("causal link" in e for e in self.errors()))
        dim["proxy_link"] = "Contact followed by joint upward motion supports the visible lifting criterion."
        self.assertEqual(self.errors(), [])

    def test_occlusion_needs_an_account(self):
        self.check["entity_ledger"][0]["status"] = "occluded"
        self.assertTrue(any("occlusion_account" in e for e in self.errors()))
        self.check["entity_ledger"][0]["occlusion_account"] = {
            "occluder_id": "person_1", "frame_indices": [2, 3],
            "visible_before": "Cup edge beside hand", "visible_after": "Same edge above hand",
            "predicted_reappearance": "Along the same upward trajectory", "outcome": "unresolved"}
        self.assertEqual(self.errors(), [])
        self.check["entity_ledger"][0]["occlusion_account"]["outcome"] = "contradicted"
        self.assertTrue(any("unexplained" in e for e in self.errors()))

    def test_parts_require_visual_correspondence(self):
        self.check["part_inventory"].update(mode="decomposed", part_ids=["handle"])
        part = copy.deepcopy(self.check["entity_ledger"][0])
        part.update(entity_id="handle", parent_id="cup_1")
        self.check["entity_ledger"].append(part)
        self.assertTrue(any("visual anchors" in e for e in self.errors()))
        part["visual_anchors"] = [{"phase": phase, "frame_index": n, "feature": "Curved handle beside rim"}
                                  for phase, n in [("before", 1), ("during", 2), ("after", 4)]]
        self.assertEqual(self.errors(), [])

    def test_uncertainty_must_compare_explanations(self):
        dim = self.check["dimension_checks"][6]
        dim.update(status="applicable_uncertain", alternative_explanations={})
        self.assertTrue(any("compare normal" in e for e in self.errors()))

    def test_unknown_frame_cannot_support_dimension(self):
        self.check["dimension_checks"][6]["frame_indices"] = [999]
        self.assertTrue(any("opened originals" in e for e in self.errors()))
