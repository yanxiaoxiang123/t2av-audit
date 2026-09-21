"""Check action-scoped evidence contracts; does not infer image semantics."""

from __future__ import annotations
from inspection_coverage import inspection_frames


def validate_action_evidence(check, requirement):
    errors = []
    binding = check.get("action_binding")
    expected = {key: requirement.get(key) for key in ("subject_id", "predicate", "object_id")}
    if binding != expected:
        errors.append("action_binding must match the frozen subject, predicate and object")
    roots = {v for k, v in expected.items() if k != "predicate" and isinstance(v, str)}
    ledger = check.get("entity_ledger", [])
    if not isinstance(ledger, list) or any(not isinstance(e, dict) for e in ledger):
        return errors + ["entity_ledger must contain objects"]
    entities = {e.get("entity_id"): e for e in ledger if isinstance(e.get("entity_id"), str)}
    if len(entities) != len(ledger) or not roots <= set(entities):
        errors.append("entity ledger must contain unique IDs including action subject and object")
    opened, coverage_errors = inspection_frames(check)
    errors.extend(coverage_errors)

    def frames(value):
        return isinstance(value, list) and bool(value) and all(type(n) is int and n in opened for n in value)

    def nonempty(value):
        return isinstance(value, str) and bool(value.strip())

    parts = check.get("part_inventory")
    if not isinstance(parts, dict) or parts.get("mode") not in {"decomposed", "whole_entity"}:
        errors.append("part_inventory must explicitly choose decomposed or whole_entity")
        parts = {}
    if not nonempty(parts.get("reason")):
        errors.append("part_inventory needs a visible-structure reason")
    part_ids = parts.get("part_ids", [])
    if (not isinstance(part_ids, list) or any(not isinstance(p, str) for p in part_ids) or
            len(set(part_ids)) != len(part_ids) or not set(part_ids) <= set(entities)):
        errors.append("part_inventory references invalid part IDs")
        part_ids = []
    if parts.get("mode") == "decomposed" and not part_ids:
        errors.append("decomposed inventory needs individually tracked parts")
    if parts.get("mode") == "whole_entity" and part_ids:
        errors.append("whole_entity inventory cannot declare parts")
    for part_id in part_ids:
        entity = entities[part_id]
        if entity.get("parent_id") not in roots:
            errors.append(f"{part_id}: parent_id must reference an action participant")
        anchors = entity.get("visual_anchors")
        if not isinstance(anchors, list) or not anchors:
            errors.append(f"{part_id}: visual anchors are required")
            continue
        phases = set()
        for anchor in anchors:
            if not isinstance(anchor, dict):
                errors.append(f"{part_id}: invalid visual anchor")
                continue
            phases.add(anchor.get("phase"))
            if not frames([anchor.get("frame_index")]) or not nonempty(anchor.get("feature")):
                errors.append(f"{part_id}: anchor needs an opened frame and visible identifying feature")
            phase, index = anchor.get("phase"), anchor.get("frame_index")
            before, after = check.get("before_frame_index"), check.get("after_frame_index")
            if all(type(n) is int for n in (index, before, after)):
                if ((phase == "before" and index != before) or
                        (phase == "after" and index != after) or
                        (phase == "during" and not before < index < after)):
                    errors.append(f"{part_id}: visual anchor phase contradicts transition boundaries")
        if not {"before", "during", "after"} <= phases:
            errors.append(f"{part_id}: anchor coverage needs before/during/after (occlusion must be explicit)")

    for entity_id, entity in entities.items():
        if entity.get("status") != "occluded":
            continue
        account = entity.get("occlusion_account")
        if not isinstance(account, dict):
            errors.append(f"{entity_id}: occluded entity requires an occlusion_account")
            continue
        if (account.get("occluder_id") not in entities or account.get("occluder_id") == entity_id or
                not frames(account.get("frame_indices")) or
                not nonempty(account.get("visible_before")) or
                not nonempty(account.get("visible_after")) or
                not nonempty(account.get("predicted_reappearance"))):
            errors.append(f"{entity_id}: incomplete occlusion evidence")
        if account.get("outcome") not in {"supported", "contradicted", "unresolved"}:
            errors.append(f"{entity_id}: invalid occlusion outcome")
        if account.get("outcome") == "contradicted":
            errors.append(f"{entity_id}: contradicted occlusion must be recorded as unexplained, not occluded")

    dimensions = check.get("dimension_checks", [])
    if not isinstance(dimensions, list):
        return errors + ["dimension_checks must be an array"]
    for dimension in dimensions:
        if not isinstance(dimension, dict):
            errors.append("dimension check must be an object")
            continue
        name = dimension.get("dimension")
        status = dimension.get("status")
        basis = dimension.get("evidence_basis")
        if basis not in {"direct_visible", "external_proxy", "unobservable_internal", "not_relevant"}:
            errors.append(f"{name}: evidence_basis is required")
        if basis in {"unobservable_internal", "not_relevant"}:
            if status != "not_applicable":
                errors.append(f"{name}: internal invisibility alone cannot create a defect or uncertainty")
            if not nonempty(dimension.get("scope_reason")):
                errors.append(f"{name}: excluded scope needs a reason")
        if status == "not_applicable":
            continue
        targets = dimension.get("assessed_entity_ids")
        if (not isinstance(targets, list) or not targets or
                any(not isinstance(t, str) or t not in entities for t in targets)):
            errors.append(f"{name}: assessed_entity_ids must reference the ledger")
            targets = []
        if name in {"contact_attachment", "motion_force_causality", "spatial_binding"} and not roots <= set(targets):
            errors.append(f"{name}: evidence must address both current action participants")
        if name in {"part_structure", "geometry_topology"} and part_ids and not set(part_ids) <= set(targets):
            errors.append(f"{name}: evidence must account for every inventoried part")
        if not frames(dimension.get("frame_indices")):
            errors.append(f"{name}: evidence must reference opened originals")
        if not nonempty(dimension.get("observable_criterion")):
            errors.append(f"{name}: action-specific observable_criterion is required")
        if basis == "external_proxy" and not nonempty(dimension.get("proxy_link")):
            errors.append(f"{name}: external proxy needs a causal link to the visible criterion")
        if status in {"applicable_uncertain", "applicable_defect"}:
            comparison = dimension.get("alternative_explanations")
            if not isinstance(comparison, dict) or any(not nonempty(comparison.get(key)) for key in (
                    "normal_explanation", "anomaly_explanation", "discriminating_observation")):
                errors.append(f"{name}: compare normal and anomalous explanations against a concrete observation")
    return errors
