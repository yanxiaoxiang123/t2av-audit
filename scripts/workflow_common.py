#!/usr/bin/env python3
"""Shared deterministic rules for the staged T2AV review workflow."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re


CLAIM_TYPES = {
    "identity", "count", "attribute", "spatial_relation", "physical_action",
    "human_object_action", "text", "dialogue", "audio", "temporal",
}
PHYSICAL_CLAIM_TYPES = {"physical_action", "human_object_action"}
STATE_DIMENSIONS = (
    "identity_count", "part_structure", "geometry_topology",
    "contact_attachment", "containment_occlusion", "pose_anatomy",
    "motion_force_causality", "material_state", "spatial_binding",
)
DIMENSION_STATUSES = {
    "not_applicable", "applicable_consistent", "applicable_uncertain",
    "applicable_defect",
}
VERDICTS = {"confirmed_consistent", "confirmed_defect", "uncertain"}
SEVERITY_CAPS = {"minor": 4, "major": 3, "critical": 2}
ISSUE_METRICS = {
    "visible_structure": {"VQ"},
    "geometry_topology": {"VQ", "PH"},
    "anatomy": {"VQ", "HM"},
    "prompt_identity": {"VF"},
    "prompt_attribute": {"VF"},
    "prompt_action": {"VF"},
    "sequence": {"TS"},
    "duration": {"TS"},
    "completion_state": {"TS"},
    "contact_attachment": {"PH"},
    "force_causality": {"PH"},
    "material_state": {"PH"},
    "human_object_relation": {"HM"},
    "multi_step_process": {"PS"},
}
DIMENSION_ISSUE_CATEGORY = {
    "identity_count": "visible_structure",
    "part_structure": "visible_structure",
    "geometry_topology": "geometry_topology",
    "contact_attachment": "contact_attachment",
    "containment_occlusion": "visible_structure",
    "pose_anatomy": "anatomy",
    "motion_force_causality": "force_causality",
    "material_state": "material_state",
    "spatial_binding": "contact_attachment",
}
INITIAL_REQUIRED_FIELDS = (
    "transition_id", "requirement_id", "source_interval_sec",
    "source_frame_indices", "board_manifest_path", "before_frame_index",
    "during_frame_indices", "after_frame_index", "before_observation",
    "transition_observation", "after_observation", "entity_ledger",
    "dimension_checks", "strongest_counterexamples", "initial_verdict",
    "initial_evidence_frame_indices", "evidence_ids",
)


def canonical_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def is_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_prompt_plan(plan, prompt: str) -> list[str]:
    errors = []
    checks = plan.get("prompt_checks") if isinstance(plan, dict) else None
    if not isinstance(checks, list) or not checks:
        return ["prompt_checks must be a nonempty array"]
    seen_ids = set()
    sequence_values = []
    for i, check in enumerate(checks):
        label = f"prompt_checks[{i}]"
        if not isinstance(check, dict):
            errors.append(f"{label}: must be an object")
            continue
        required = ("requirement_id", "claim_type", "subject_id", "predicate",
                    "object_id", "expected_before", "expected_after",
                    "sequence_index", "prompt_span", "metric_ids")
        missing = [field for field in required if field not in check]
        if missing:
            errors.append(f"{label}: missing fields {', '.join(missing)}")
            continue
        rid = check["requirement_id"]
        if not isinstance(rid, str) or not rid.strip() or rid in seen_ids:
            errors.append(f"{label}: requirement_id must be nonempty and unique")
        seen_ids.add(rid)
        if check["claim_type"] not in CLAIM_TYPES:
            errors.append(f"{label}: invalid claim_type")
        if not isinstance(check["subject_id"], str) or not check["subject_id"].strip():
            errors.append(f"{label}: subject_id is required")
        if check["object_id"] is not None and (not isinstance(check["object_id"], str) or not check["object_id"].strip()):
            errors.append(f"{label}: object_id must be null or a nonempty string")
        for field in ("expected_before", "expected_after"):
            if check[field] is not None and (not isinstance(check[field], str) or not check[field].strip()):
                errors.append(f"{label}: {field} must be null or a nonempty string")
        if check["expected_before"] is None and check["expected_after"] is None:
            errors.append(f"{label}: at least one expected state is required")
        predicate = check["predicate"]
        if not isinstance(predicate, str) or not re.fullmatch(r"[a-z][a-z0-9_:-]*", predicate):
            errors.append(f"{label}: predicate must be one atomic snake_case token")
        elif any(token in f"_{predicate}_" for token in ("_and_", "_then_", "_plus_", "_or_")):
            errors.append(f"{label}: predicate contains a multi-claim connector")
        if type(check["sequence_index"]) is not int or check["sequence_index"] < 0:
            errors.append(f"{label}: sequence_index must be a nonnegative integer")
        else:
            sequence_values.append(check["sequence_index"])
        span = check["prompt_span"]
        if (not isinstance(span, list) or len(span) != 2 or
                any(type(n) is not int for n in span) or not (0 <= span[0] < span[1] <= len(prompt))):
            errors.append(f"{label}: invalid prompt_span")
        elif check.get("prompt_quote") != prompt[span[0]:span[1]]:
            errors.append(f"{label}: prompt_quote must exactly match prompt_span")
        if not isinstance(check["metric_ids"], list) or not check["metric_ids"]:
            errors.append(f"{label}: metric_ids must be nonempty")
        strict = check.get("strict_transition_required")
        if check["claim_type"] in PHYSICAL_CLAIM_TYPES and strict is not True:
            errors.append(f"{label}: physical and hand-object actions require strict_transition_required=true")
        if check["claim_type"] not in PHYSICAL_CLAIM_TYPES and strict not in (False, None):
            errors.append(f"{label}: strict_transition_required is only valid for physical actions")
    if any(a > b for a, b in zip(sequence_values, sequence_values[1:])):
        errors.append("prompt_checks must be ordered by nondecreasing sequence_index")
    return errors


def validate_board_manifest(path: Path, interval, source_indices) -> tuple[list[str], dict | None]:
    errors = []
    try:
        board = read_json(path)
    except (OSError, ValueError, TypeError) as exc:
        return [f"unreadable board manifest ({type(exc).__name__})"], None
    if board.get("interval_sec") != interval or board.get("source_frame_indices") != source_indices:
        errors.append("board manifest differs from transition interval")
    columns, rows = board.get("columns"), board.get("rows")
    if type(columns) is not int or type(rows) is not int or not (1 <= columns <= 2 and 1 <= rows <= 2):
        errors.append("strict transition board grid must be at most 2x2")
    roi = board.get("roi_xyxy")
    size = board.get("original_frame_size")
    if not isinstance(roi, list) or len(roi) != 4 or any(type(n) is not int for n in roi):
        errors.append("board ROI is missing")
    if (not isinstance(size, list) or len(size) != 2 or
            any(type(n) is not int or n <= 0 for n in size)):
        errors.append("board original_frame_size is missing")
    elif isinstance(roi, list) and len(roi) == 4 and all(type(n) is int for n in roi):
        if not (0 <= roi[0] < roi[2] <= size[0] and 0 <= roi[1] < roi[3] <= size[1]):
            errors.append("board ROI is outside original_frame_size")
    board_paths = board.get("board_paths")
    hashes = board.get("board_sha256")
    if not isinstance(board_paths, list) or not board_paths or not isinstance(hashes, dict):
        errors.append("board paths and hashes are required")
    else:
        for raw in board_paths:
            p = Path(raw)
            if not p.is_absolute() or not p.is_file():
                errors.append("board page is missing")
            elif hashes.get(raw) != sha256_file(p):
                errors.append("board page hash mismatch")
    return errors, board


def collect_asset_hashes(value, review_dir: Path | None = None) -> dict[str, str]:
    """Hash explicitly referenced checkpoint assets, including board pages."""
    paths = set()
    for item in value.get("state_transition_checks", []) if isinstance(value, dict) else []:
        raw = item.get("board_manifest_path")
        if isinstance(raw, str):
            paths.add(raw)
    for item in value.get("challenge_reviews", []) if isinstance(value, dict) else []:
        raw = item.get("board_manifest_path")
        if isinstance(raw, str):
            paths.add(raw)
    if review_dir is not None:
        manifest = read_json(review_dir / "logs" / "frame_manifest.json")
        frame_paths = {item.get("frame_index"): item.get("original_image_path") for item in manifest}
        frame_indices = set()
        for item in value.get("state_transition_checks", []) if isinstance(value, dict) else []:
            frame_indices.update(item.get("initial_evidence_frame_indices") or [])
            frame_indices.update(item.get("board_reviewed_frame_indices") or [])
        for item in value.get("challenge_reviews", []) if isinstance(value, dict) else []:
            frame_indices.update(item.get("frame_indices") or [])
        for index in frame_indices:
            raw = frame_paths.get(index)
            if not isinstance(raw, str):
                raise ValueError(f"checkpoint source frame is absent from manifest: {index}")
            paths.add(raw)
    result = {}
    for raw in sorted(paths):
        path = Path(raw)
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"checkpoint asset is missing: {raw}")
        result[raw] = sha256_file(path)
        if path.name == "board_manifest.json":
            board = read_json(path)
            for board_raw in board.get("board_paths", []):
                board_path = Path(board_raw)
                if not board_path.is_absolute() or not board_path.is_file():
                    raise ValueError(f"checkpoint board page is missing: {board_raw}")
                result[board_raw] = sha256_file(board_path)
    return result


def required_metrics(issue_categories) -> set[str]:
    result = set()
    for category in issue_categories or []:
        result.update(ISSUE_METRICS.get(category, set()))
    return result
