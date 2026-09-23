#!/usr/bin/env python3
"""Render evidence boxes and deterministically validate one T2AV score record."""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave
from inspection_coverage import inspection_frames
from audio_review import (
    TIMING_CONFLICTS, compare as compare_audio_reviews,
    compare_detailed as compare_audio_reviews_detailed, fulfillment as audio_fulfillment,
    is_audio_claim, requires_visual_binding,
)

from workflow_common import (
    DIMENSION_ISSUE_CATEGORY, ISSUE_METRICS, PHYSICAL_CLAIM_TYPES, SEVERITY_CAPS,
    STATE_DIMENSIONS, VERDICTS, read_json, required_metrics, sha256_bytes,
    sha256_file, validate_board_manifest, validate_prompt_plan, collect_asset_hashes,
)

METRICS = "VQ AE VF AQ AF AV TS DC LS TX PH MU PR SB HM MT PS".split()
UNIVERSAL = set(METRICS[:7])
AUDIO_METRICS = {"AQ", "AF", "AV", "DC", "LS", "MU", "PR", "SB", "MT"}


def anchors():
    rubric = (Path(__file__).resolve().parents[1] / "references" / "rubric.md").read_text(encoding="utf-8")
    result = {}
    current = None
    for line in rubric.splitlines():
        match = re.match(r"\s+([A-Z]{2})\s+\S", line)
        if match and match.group(1) in METRICS:
            current = match.group(1)
        match = re.match(r"\s+([0-5]) 分：(.*)", line)
        if match and current:
            result[(current, int(match.group(1)))] = match.group(2).strip()
    if len(result) != 102:
        raise ValueError("Rubric anchors could not be parsed")
    return result


def load_record(path):
    text = path.read_text(encoding="utf-8").strip()
    record = json.loads(text)
    if not isinstance(record, dict):
        raise ValueError("Draft must be one JSON object")
    return record


def image_size(path):
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        return image.size


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def font_for_label():
    from PIL import ImageFont
    candidates = [
        Path("/System/Library/Fonts/PingFang.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), 20)
            except OSError:
                continue
    return ImageFont.load_default()


def render(draft_path):
    from PIL import Image, ImageDraw
    record = load_record(draft_path)
    review_dir = draft_path.parent.resolve()
    if not (review_dir / "input.json").is_file():
        raise ValueError("Draft must be in its prepared review directory")
    font = font_for_label()
    for evidence in record.get("evidence", []):
        evidence_id = evidence.get("evidence_id", "evidence")
        for frame in evidence.get("frames", []):
            source = Path(frame["original_image_path"])
            with Image.open(source) as image:
                annotated = image.convert("RGB")
            draw = ImageDraw.Draw(annotated)
            for index, box in enumerate(frame.get("boxes", []), 1):
                xyxy = box["bbox_xyxy"]
                color = "#e53935" if box.get("color") == "red" else "#2eae49"
                draw.rectangle((xyxy[0], xyxy[1], xyxy[2] - 1, xyxy[3] - 1), outline=color, width=3)
                label = f"{evidence_id} {box.get('object_label', '')}"
                try:
                    draw.text((xyxy[0], max(0, xyxy[1] - 25)), label, fill=color, font=font,
                              stroke_width=1, stroke_fill="black")
                except UnicodeEncodeError:
                    draw.text((xyxy[0], max(0, xyxy[1] - 25)), f"{evidence_id} #{index}",
                              fill=color, font=font)
            destination = review_dir / "frames" / "annotated" / f"{evidence_id}_{frame['frame_index']:06d}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            annotated.save(destination)
            frame["annotated_image_path"] = str(destination.resolve())
    draft_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sum(len(e.get("frames", [])) for e in record.get("evidence", []))


def check_path(path, errors, label):
    if not isinstance(path, str) or not path:
        errors.append(f"{label}: missing path")
        return None
    p = Path(path)
    if not p.is_absolute() or not p.is_file():
        errors.append(f"{label}: file not found at absolute path")
        return None
    try:
        with p.open("rb") as stream:
            if not stream.read(1):
                raise ValueError("empty")
    except (OSError, ValueError):
        errors.append(f"{label}: file cannot be reopened")
        return None
    return p


def verify_workflow(record, review_dir, prepared, err):
    """Verify immutable stage checkpoints and return their decoded contents."""
    root = review_dir / "workflow"
    try:
        state = read_json(root / "state.json")
    except (OSError, ValueError, TypeError):
        err("files", "staged workflow is missing or unreadable")
        return None, None, None
    if not isinstance(state, dict):
        err("files", "staged workflow state must be an object")
        return None, None, None
    if state.get("schema_version") != "t2av_score_v1" or state.get("workflow_version") != 1:
        err("scores", "unsupported staged workflow version")
    expected_prompt_hash = sha256_bytes(prepared["prompt"].encode("utf-8"))
    if state.get("prompt_sha256") != expected_prompt_hash:
        err("references", "workflow prompt hash differs from prepared input")
    decoded = {}
    for stage in ("plan", "initial", "challenge"):
        item = (state.get("stages") or {}).get(stage)
        path = root / f"{stage}.json"
        if not isinstance(item, dict) or not path.is_file():
            err("files", f"workflow stage {stage} is not frozen")
            continue
        try:
            if item.get("checkpoint_path") != str(path.resolve()) or item.get("checkpoint_sha256") != sha256_file(path):
                err("references", f"workflow stage {stage} checkpoint hash mismatch")
            decoded[stage] = read_json(path)
            if collect_asset_hashes(decoded[stage], review_dir) != item.get("asset_sha256"):
                err("references", f"workflow stage {stage} asset hash mismatch or incomplete inventory")
        except (OSError, ValueError, TypeError, AttributeError):
            err("files", f"workflow stage {stage} cannot be verified")
    plan, initial, challenge = (decoded.get(name) for name in ("plan", "initial", "challenge"))
    from review_workflow import validate_initial, validate_challenge
    if isinstance(plan, dict) and isinstance(initial, dict):
        try:
            points = read_json(review_dir / "logs" / "frame_pts.json")
            for message in validate_initial(initial, plan, points):
                err("scores", f"frozen initial: {message}")
            if isinstance(challenge, dict):
                for message in validate_challenge(challenge, initial):
                    err("scores", f"frozen challenge: {message}")
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            err("scores", "invalid frozen stage structure")
    if isinstance(plan, dict):
        for message in validate_prompt_plan(plan, prepared["prompt"]):
            err("scores", f"frozen plan: {message}")
        final_map = {item.get("requirement_id"): item for item in record.get("prompt_checks", [])}
        for planned in plan.get("prompt_checks", []):
            final = final_map.get(planned.get("requirement_id"), {})
            for key, value in planned.items():
                if final.get(key) != value:
                    err("references", f"{planned.get('requirement_id')}: final prompt check differs from frozen plan field {key}")
    final_transitions = {
        item.get("transition_id"): item
        for item in (record.get("inspection") or {}).get("state_transition_checks", [])
        if isinstance(item, dict)
    }
    if isinstance(initial, dict):
        frozen = initial.get("state_transition_checks", [])
        if set(final_transitions) != {item.get("transition_id") for item in frozen}:
            err("references", "final state transitions differ from frozen initial transition IDs")
        for item in frozen:
            final = final_transitions.get(item.get("transition_id"), {})
            for key, value in item.items():
                if final.get(key) != value:
                    err("references", f"{item.get('transition_id')}: final transition differs from frozen initial field {key}")
    if isinstance(challenge, dict):
        frozen_reviews = challenge.get("challenge_reviews", [])
        if set(final_transitions) != {item.get("transition_id") for item in frozen_reviews}:
            err("references", "final state transitions differ from frozen challenge transition IDs")
        for item in frozen_reviews:
            if final_transitions.get(item.get("transition_id"), {}).get("challenge_review") != item:
                err("references", f"{item.get('transition_id')}: final challenge differs from frozen checkpoint")
    if isinstance(plan, dict):
        planned_ids = {item.get("requirement_id") for item in plan.get("prompt_checks", [])}
        final_ids = {item.get("requirement_id") for item in record.get("prompt_checks", [])}
        if planned_ids != final_ids:
            err("references", "final prompt check IDs differ from frozen plan")
    return plan, initial, challenge


def validate_state_transitions(checks, evidence, metric_items, inspection, frame_map,
                               viewed, opened, pts, prompt_check_map, err):
    """Validate local state-transition coverage and deterministic score propagation."""
    transitions = inspection.get("state_transition_checks")
    if not isinstance(transitions, list):
        err("scores", "inspection.state_transition_checks must be an array")
        return
    metric_map = {item.get("metric_id"): item for item in metric_items}
    evidence_map = {item.get("evidence_id"): item for item in evidence}
    pts_map = {p.get("frame_index"): p.get("time_sec") for p in pts}
    strict_requirements = {
        item.get("requirement_id") for item in checks
        if item.get("claim_type") in PHYSICAL_CLAIM_TYPES
    }
    requirement_map = {item.get("requirement_id"): item for item in checks}
    covered = set()
    for index, check in enumerate(transitions):
        label = f"state_transition_checks[{index}]"
        if not isinstance(check, dict):
            err("scores", f"{label}: must be an object")
            continue
        rid = check.get("requirement_id")
        if rid not in strict_requirements:
            err("references", f"{label}: does not reference a strict physical action")
        else:
            covered.add(rid)
        interval = check.get("source_interval_sec")
        source_indices = check.get("source_frame_indices")
        if (not isinstance(interval, list) or len(interval) != 2 or
                any(type(t) not in (int, float) or not math.isfinite(t) for t in interval) or
                interval[0] < 0 or interval[0] >= interval[1] or interval[1] - interval[0] > 2.000001):
            err("references", f"{label}: local interval must be valid and no longer than 2 seconds")
            selected = []
        else:
            selected = [p["frame_index"] for p in pts
                        if interval[0] - 1e-6 <= p["time_sec"] <= interval[1] + 1e-6]
            if source_indices != selected or not selected or not set(selected) <= set(frame_map):
                err("references", f"{label}: interval must include every extracted source frame")
        sequence = [check.get("before_frame_index"), *(check.get("during_frame_indices") or []),
                    check.get("after_frame_index")]
        valid = (len(sequence) >= 3 and all(type(n) is int and n in frame_map for n in sequence))
        if not valid:
            err("references", f"{label}: before/during/after frames are invalid")
        elif (any(pts_map[a] >= pts_map[b] for a, b in zip(sequence, sequence[1:])) or
              any(n not in viewed for n in sequence)):
            err("scores", f"{label}: before/during/after frames must be viewed and PTS ordered")
        elif not all(interval[0] - 1e-6 <= pts_map[n] <= interval[1] + 1e-6 for n in sequence):
            err("references", f"{label}: before/during/after frames must be inside the local interval")
        else:
            boundary = [p["frame_index"] for p in pts
                        if pts_map[sequence[0]] - 1e-6 <= p["time_sec"] <= pts_map[sequence[-1]] + 1e-6]
            initial_opened = check.get("initial_evidence_frame_indices") or []
            inspected, coverage_errors = inspection_frames(check)
            for message in coverage_errors:
                err("scores", f"{label}: {message}")
            if not set(boundary) <= inspected or not set(initial_opened) <= set(opened):
                err("scores", f"{label}: every source frame between stable states must be individually opened")
            if not set(sequence) <= set(opened):
                err("scores", f"{label}: before/during/after key originals must still be opened")
        board_path = Path(check.get("board_manifest_path", ""))
        board_errors, _ = validate_board_manifest(board_path, interval, source_indices)
        for message in board_errors:
            err("files" if "missing" in message or "unreadable" in message else "references",
                f"{label}: {message}")
        evidence_ids = check.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids or any(eid not in evidence_map for eid in evidence_ids):
            err("references", f"{label}: evidence_ids must reference saved evidence")
        elif valid:
            evidenced = {f.get("frame_index") for eid in evidence_ids
                         for f in evidence_map[eid].get("frames", [])}
            if not set(sequence) <= evidenced:
                err("references", f"{label}: before/during/after frames are not covered by evidence")
        dimensions = check.get("dimension_checks") or []
        statuses = {item.get("status") for item in dimensions if isinstance(item, dict)}
        if {item.get("dimension") for item in dimensions if isinstance(item, dict)} != set(STATE_DIMENSIONS):
            err("scores", f"{label}: all nine state dimensions are required")
        challenge = check.get("challenge_review") or {}
        dimensions = dimensions + (challenge.get("dimension_findings") or [])
        statuses = {item.get("status") for item in dimensions if isinstance(item, dict)}
        initial_verdict, challenge_verdict = check.get("initial_verdict"), challenge.get("verdict")
        verdict = check.get("verdict")
        if initial_verdict not in VERDICTS or challenge_verdict not in VERDICTS or verdict not in VERDICTS:
            err("scores", f"{label}: invalid initial, challenge, or final verdict")
        challenge_frames = challenge.get("frame_indices")
        if (not isinstance(challenge_frames, list) or not challenge_frames or
                any(type(n) is not int or n not in frame_map or n not in opened for n in challenge_frames)):
            err("references", f"{label}: challenge frames must be individually opened source frames")
        resolution = check.get("conflict_resolution")
        if initial_verdict != challenge_verdict and resolution is None and verdict != "uncertain":
            err("scores", f"{label}: unresolved reviewer conflict must remain uncertain")
        if initial_verdict == challenge_verdict and resolution is None and verdict != initial_verdict:
            err("scores", f"{label}: final verdict differs from agreeing frozen reviews")
        if resolution is not None:
            resolution_frames = resolution.get("frame_indices") if isinstance(resolution, dict) else None
            if (not isinstance(resolution, dict) or resolution.get("verdict") not in VERDICTS or
                    verdict != resolution.get("verdict") or
                    not isinstance(resolution.get("observation"), str) or not resolution["observation"].strip() or
                    not isinstance(resolution_frames, list) or not resolution_frames or
                    any(type(n) is not int or n not in frame_map or n not in opened for n in resolution_frames)):
                err("scores", f"{label}: invalid conflict_resolution")
        unresolved = ("applicable_uncertain" in statuses or
                      any(e.get("status") in {"occluded", "unexplained"}
                          for e in check.get("entity_ledger", []) if isinstance(e, dict)))
        defective = "applicable_defect" in statuses
        if verdict == "confirmed_consistent" and (unresolved or defective):
            err("scores", f"{label}: unresolved or defective state contradicts confirmed_consistent")
        if verdict == "confirmed_defect" and not defective:
            err("scores", f"{label}: confirmed_defect requires an applicable_defect dimension")
        if verdict == "uncertain" and not (unresolved or initial_verdict != challenge_verdict):
            err("scores", f"{label}: uncertain requires unresolved state or reviewer conflict")
        categories = check.get("issue_categories")
        if not isinstance(categories, list) or any(category not in ISSUE_METRICS for category in categories):
            err("scores", f"{label}: issue_categories are invalid")
            categories = []
        affected = check.get("affected_metric_ids")
        if not isinstance(affected, list) or any(mid not in metric_map for mid in affected):
            err("references", f"{label}: affected_metric_ids are invalid")
            affected = []
        mandatory = required_metrics(categories)
        if verdict in {"uncertain", "confirmed_defect"}:
            derived_categories = {
                DIMENSION_ISSUE_CATEGORY[item["dimension"]]
                for item in dimensions if isinstance(item, dict) and
                item.get("dimension") in DIMENSION_ISSUE_CATEGORY and
                item.get("status") in {"applicable_uncertain", "applicable_defect"}
            }
            derived_categories.update({"prompt_action", "sequence"})
            if requirement_map.get(rid, {}).get("claim_type") == "human_object_action":
                derived_categories.add("human_object_relation")
            if len(strict_requirements) > 1:
                derived_categories.add("multi_step_process")
            if not derived_categories <= set(categories):
                err("scores", f"{label}: issue_categories omit derived categories {sorted(derived_categories - set(categories))}")
            mandatory.update(required_metrics(derived_categories))
            if not categories or not affected:
                err("scores", f"{label}: unresolved findings need categories and affected metrics")
            if not mandatory <= set(affected):
                err("scores", f"{label}: defect categories were not propagated to {sorted(mandatory - set(affected))}")
            for mid in affected:
                metric = metric_map[mid]
                if metric.get("status") != "已评分":
                    err("scores", f"{label}: affected metric {mid} is not scored")
                elif verdict == "uncertain" and metric.get("score", 99) > 3:
                    err("scores", f"{label}: uncertain finding caps {mid} at 3")
            if verdict == "confirmed_defect":
                severity = check.get("severity")
                if severity not in SEVERITY_CAPS:
                    err("scores", f"{label}: confirmed defect needs minor, major, or critical severity")
                else:
                    cap = SEVERITY_CAPS[severity]
                    for mid in affected:
                        if metric_map[mid].get("status") == "已评分" and metric_map[mid].get("score", 99) > cap:
                            err("scores", f"{label}: {severity} defect caps {mid} at {cap}")
                if prompt_check_map.get(rid, {}).get("status") == "已呈现":
                    err("scores", f"{label}: confirmed defect is not reflected in its prompt check")
            elif check.get("severity") is not None:
                err("scores", f"{label}: only confirmed defects have severity")
        else:
            if categories or affected or check.get("severity") is not None:
                err("scores", f"{label}: consistent transition cannot carry issue fields")
    for rid in sorted(strict_requirements - covered):
        err("scores", f"{rid}: missing state transition check")


def validate_audio_boundaries(checks, metric_items, err):
    metric_map = {item.get("metric_id"): item for item in metric_items}
    explicit_af = any(is_audio_claim(check) and "AF" in (check.get("metric_ids") or [])
                      for check in checks)
    af_metric = metric_map.get("AF", {})
    if not explicit_af and af_metric.get("status") == "已评分" and af_metric.get("score") != 5:
        err("scores", "AF cannot be reduced when the Prompt has no explicit audio requirement")


def validate_requirement_audio_result(record, review_dir, prepared, checks, metric_items, evidence, err):
    """Apply the stronger gates only when the optional new evidence exists."""
    path = review_dir / "audio" / "requirements" / "audio_requirements.json"
    if not path.is_file():
        return  # Historical reviews retain their existing validation contract.
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        plan_path = review_dir / "workflow" / "plan.json"
        state = json.loads((review_dir / "workflow" / "state.json").read_text(encoding="utf-8"))
        frozen_sha = state["stages"]["plan"]["checkpoint_sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        err("files", "Audio requirement result or frozen plan is unreadable")
        return
    if (result.get("schema_version") != "t2av_audio_requirements_v1" or
            result.get("source", {}).get("sha256") != prepared.get("video_sha256") or
            result.get("source", {}).get("plan_sha256") != frozen_sha or
            sha256(plan_path) != frozen_sha):
        err("references", "Audio requirement result does not match the video and frozen plan")
        return
    contract_version = result.get("contract_version", 1)
    if type(contract_version) is not int or contract_version not in {1, 2, 3}:
        err("references", "Audio requirement result has an unsupported contract version")
        return
    audio_checks = {x.get("requirement_id"): x for x in checks if is_audio_claim(x)}
    entries = result.get("requirements")
    if (not isinstance(entries, list) or len(entries) != len(audio_checks) or
            len({x.get("requirement_id") for x in entries if isinstance(x, dict)}) != len(entries)):
        err("references", "Audio requirement result must cover every frozen audio claim exactly once")
        return
    mapped = {x.get("requirement_id"): x for x in entries if isinstance(x, dict)}
    if set(mapped) != set(audio_checks):
        err("references", "Audio requirement IDs differ from frozen plan")
        return
    for segment in result.get("segments", []):
        path_value = segment.get("audio_path")
        if not isinstance(path_value, str) or not Path(path_value).is_file():
            err("files", "Audio requirement segment is missing")
        elif sha256(Path(path_value)) != segment.get("audio_sha256"):
            err("files", "Audio requirement WAV hash mismatch")
    uncertain_ids = set()
    binding_uncertain_ids = set()
    evidence_map = {x.get("evidence_id"): x for x in evidence if isinstance(x, dict)}
    for rid, entry in mapped.items():
        check = audio_checks[rid]
        status = entry.get("audibility_status", entry.get("status"))
        if contract_version >= 3 and (entry.get("status") != status or entry.get("audibility_status") != status):
            err("scores", f"{rid}: status and audibility_status must match")
        if contract_version >= 3 and entry.get("visual_binding_required") is not requires_visual_binding(entry):
            err("scores", f"{rid}: visual_binding_required differs from the frozen claim")
        if entry.get("full_review") is not None:
            try:
                if contract_version >= 3:
                    expected_status, expected_conflicts, expected_timing, expected_estimates = compare_audio_reviews_detailed(
                        entry["full_review"], entry["local_reviews"],
                        bool(entry.get("coverage_complete")), entry)
                    if (status != expected_status or entry.get("conflicts") != expected_conflicts or
                            entry.get("timing_status") != expected_timing or
                            entry.get("timing_estimates") != expected_estimates):
                        err("scores", f"{rid}: audio audibility/timing result differs from saved full/local reviews")
                else:
                    expected_status, expected_conflicts = compare_audio_reviews(
                        entry["full_review"], entry["local_reviews"], bool(entry["local_reviews"]), entry)
                    if status != expected_status or entry.get("conflicts") != expected_conflicts:
                        err("scores", f"{rid}: audio verdict differs from saved full/local reviews")
            except (KeyError, TypeError, ValueError):
                err("scores", f"{rid}: invalid full/local audio reviews")
        elif result.get("source", {}).get("has_audio_stream"):
            err("scores", f"{rid}: audio stream requires a saved full/local analysis")
        if entry.get("prompt_quote") != check.get("prompt_quote"):
            err("references", f"{rid}: audio quote differs from frozen plan")
        if status not in {"confirmed_present", "confirmed_absent", "uncertain", "analysis_failed"}:
            err("scores", f"{rid}: invalid audio verdict")
            continue
        fulfillment = entry.get("fulfillment", "uncertain")
        if contract_version >= 3:
            try:
                expected_fulfillment = audio_fulfillment(entry, status, entry.get("full_review") or {})
                if fulfillment != expected_fulfillment:
                    err("scores", f"{rid}: audio fulfillment differs from its status and full review")
            except (KeyError, TypeError, ValueError):
                err("scores", f"{rid}: invalid audio fulfillment inputs")
        if status in {"uncertain", "analysis_failed"} or fulfillment == "uncertain":
            uncertain_ids.add(rid)
            if check.get("status") != "无法判断":
                err("scores", f"{rid}: unresolved audio cannot be called absent or present")
        elif status == "confirmed_present" and fulfillment == "violated" and check.get("status") in {"已呈现", "未呈现", "部分呈现"}:
            err("scores", f"{rid}: audible event does not satisfy its count/prohibition constraint")
        elif status == "confirmed_present" and fulfillment == "satisfied" and check.get("status") in {"未呈现", "与要求矛盾"}:
            err("scores", f"{rid}: audible requirement cannot be called missing")
        elif status == "confirmed_absent" and check.get("status") in {"已呈现", "部分呈现"}:
            err("scores", f"{rid}: absent audio cannot be called present")
        semantic_conflicts = [x for x in entry.get("conflicts", []) if x not in TIMING_CONFLICTS]
        if contract_version >= 3:
            if semantic_conflicts and status not in {"uncertain", "analysis_failed"}:
                err("scores", f"{rid}: content/count conflict must remain uncertain")
            if entry.get("timing_conflicts") != [x for x in entry.get("conflicts", []) if x in TIMING_CONFLICTS]:
                err("scores", f"{rid}: timing_conflicts do not match conflict records")
        elif entry.get("conflicts") and status != "uncertain":
            err("scores", f"{rid}: legacy audio conflicts must remain uncertain")

        if contract_version >= 3 and entry.get("visual_binding_required"):
            binding = check.get("audio_binding")
            if check.get("status") == "无法判断":
                binding_uncertain_ids.add(rid)
            if not isinstance(binding, dict) and check.get("status") != "无法判断":
                err("scores", f"{rid}: shot-bound audio claim needs an audio_binding record")
                binding_uncertain_ids.add(rid)
            elif isinstance(binding, dict):
                binding_status = binding.get("status")
                if binding_status not in {"consistent", "conflict", "uncertain"} or not str(binding.get("reason", "")).strip():
                    err("scores", f"{rid}: audio_binding needs a supported status and reason")
                    binding_uncertain_ids.add(rid)
                if binding_status == "uncertain":
                    binding_uncertain_ids.add(rid)
                    if check.get("status") != "无法判断":
                        err("scores", f"{rid}: uncertain audio binding must remain unresolved")
                elif binding_status == "consistent" and check.get("status") not in {"已呈现", "部分呈现"}:
                    err("scores", f"{rid}: consistent audio binding is not reflected in prompt check")
                elif binding_status == "conflict" and check.get("status") not in {"与要求矛盾", "部分呈现"}:
                    err("scores", f"{rid}: conflicting audio binding is not reflected in prompt check")
                interval = binding.get("visual_interval_sec")
                duration = prepared["media_metadata"].get("duration_sec")
                valid_visual_interval = (
                    isinstance(interval, list) and len(interval) == 2 and
                    all(type(x) in (int, float) and math.isfinite(x) for x in interval) and
                    type(duration) in (int, float) and 0 <= interval[0] < interval[1] <= duration
                )
                if not valid_visual_interval:
                    err("scores", f"{rid}: audio_binding visual interval is invalid")
                estimate_times = (entry.get("timing_estimates") or {}).get("all_source_times_sec", [])
                supplied_times = binding.get("audio_time_estimates_sec", [])
                if (binding_status in {"consistent", "conflict"} and
                        (not isinstance(supplied_times, list) or not supplied_times)):
                    err("scores", f"{rid}: resolved audio binding needs at least one saved time estimate")
                if (not isinstance(supplied_times, list) or not isinstance(estimate_times, list) or
                        any(type(x) not in (int, float) or not math.isfinite(x) or
                            not any(abs(x - y) <= 1e-6 for y in estimate_times) for x in supplied_times)):
                    err("scores", f"{rid}: audio_binding times must cite saved source-time estimates")
                binding_refs = binding.get("evidence_ids") or []
                check_refs = check.get("evidence_ids") or []
                if (not isinstance(binding_refs, list) or not binding_refs or
                        not all(isinstance(ref, str) for ref in binding_refs) or
                        not isinstance(check_refs, list) or not all(isinstance(ref, str) for ref in check_refs) or
                        not set(binding_refs) <= set(check_refs) or
                        any(ref not in evidence_map for ref in binding_refs)):
                    err("references", f"{rid}: audio_binding must reference the check's saved evidence")
                else:
                    cited = [evidence_map[ref] for ref in binding_refs]
                    if not any(e.get("frames") for e in cited) or not any(e.get("audio_segments") for e in cited):
                        err("scores", f"{rid}: audio_binding evidence needs both visual frames and WAV context")
                    if any(rid not in (e.get("requirement_ids") or []) for e in cited):
                        err("references", f"{rid}: audio_binding evidence must cite this requirement")
                    expected_audio_paths = {
                        row.get("audio_path") for row in
                        [entry.get("full_review") or {}, *(entry.get("local_reviews") or [])]
                        if isinstance(row, dict) and isinstance(row.get("audio_path"), str)
                    }
                    cited_audio = [audio for e in cited for audio in e.get("audio_segments") or []
                                   if isinstance(audio, dict) and audio.get("audio_path") in expected_audio_paths]
                    cited_audio_paths = {audio.get("audio_path") for audio in cited_audio}
                    if not expected_audio_paths.intersection(cited_audio_paths):
                        err("references", f"{rid}: audio_binding WAV must belong to this requirement result")
                    if (not valid_visual_interval or not any(
                            type(frame.get("time_sec")) in (int, float) and
                            interval[0] <= frame["time_sec"] <= interval[1]
                            for e in cited for frame in e.get("frames") or [])):
                        err("references", f"{rid}: audio_binding needs a cited frame in its visual interval")
                    if supplied_times and not any(
                            type(audio.get("source_start_time_sec")) in (int, float) and
                            type(audio.get("source_end_time_sec")) in (int, float) and
                            audio["source_start_time_sec"] <= estimate <= audio["source_end_time_sec"]
                            for estimate in supplied_times for audio in cited_audio):
                        err("references", f"{rid}: audio_binding times must fall in a cited requirement WAV")
                    if (binding_status == "consistent" and valid_visual_interval and
                            isinstance(supplied_times, list) and
                            any(type(estimate) in (int, float) and
                                not interval[0] <= estimate <= interval[1] for estimate in supplied_times)):
                        err("scores", f"{rid}: consistent audio binding has an onset outside its visual interval")
    for metric in metric_items:
        if metric.get("metric_id") in AUDIO_METRICS and uncertain_ids.intersection(metric.get("requirement_ids") or []):
            if metric.get("confidence") != "低" or not metric.get("uncertainty"):
                err("scores", f"{metric.get('metric_id')}: unresolved audio needs low confidence and uncertainty")
        timing_sensitive = metric.get("metric_id") in {"AF", "AV", "MU", "SB", "TS"}
        if timing_sensitive and binding_uncertain_ids.intersection(metric.get("requirement_ids") or []):
            if metric.get("confidence") != "低" or not metric.get("uncertainty"):
                err("scores", f"{metric.get('metric_id')}: unresolved audio binding needs low confidence and uncertainty")
    for ev in evidence:
        for sync in ev.get("synchronization") or []:
            if sync.get("offset_sec") is None:
                continue
            rid = sync.get("audio_requirement_id")
            entry = mapped.get(rid)
            uncertainty = sync.get("time_uncertainty_sec")
            if not entry or entry.get("status") != "confirmed_present":
                err("scores", f"{ev.get('evidence_id')}: numeric AV offset needs confirmed audio requirement")
            if (entry and contract_version >= 3 and entry.get("timing_status") == "conflict" and
                    sync.get("independent_measurement") is not True):
                err("scores", f"{ev.get('evidence_id')}: conflicting model times need an independent measurement")
            if (entry and contract_version >= 3 and
                    "gemini" in str(sync.get("measurement_method", "")).lower()):
                err("scores", f"{ev.get('evidence_id')}: numeric AV offset cannot use Gemini time estimates")
            if (type(uncertainty) not in (int, float) or not math.isfinite(uncertainty) or
                    uncertainty <= 0 or uncertainty > 0.1):
                err("scores", f"{ev.get('evidence_id')}: AV offset lacks sufficient time precision")
            if (not sync.get("measurement_method") or sync.get("visual_onset_sec") is None or
                    sync.get("audio_onset_sec") is None or sync.get("audio_requirement_id") != rid):
                err("scores", f"{ev.get('evidence_id')}: AV offset lacks traceable common-timeline timing")
            if rid and entry:
                if rid not in (ev.get("requirement_ids") or []):
                    err("references", f"{ev.get('evidence_id')}: AV evidence does not cite its audio requirement")
                check = audio_checks.get(rid)
                binding = check.get("audio_binding") if check else None
                if (contract_version >= 3 and
                        (not isinstance(binding, dict) or binding.get("status") == "uncertain" or
                         (check and check.get("status") == "无法判断"))):
                    err("scores", f"{ev.get('evidence_id')}: numeric AV offset needs a resolved visual binding")
                if contract_version >= 3:
                    audio_onset = sync.get("audio_onset_sec")
                    visual_onset = sync.get("visual_onset_sec")
                    requirement_paths = {
                        row.get("audio_path") for row in
                        [entry.get("full_review") or {}, *(entry.get("local_reviews") or [])]
                        if isinstance(row, dict) and isinstance(row.get("audio_path"), str)
                    }
                    audio_segments = [segment for segment in ev.get("audio_segments") or []
                                      if segment.get("audio_path") in requirement_paths]
                    valid_audio_onset = (type(audio_onset) in (int, float) and math.isfinite(audio_onset))
                    valid_visual_onset = (type(visual_onset) in (int, float) and math.isfinite(visual_onset))
                    if (not valid_audio_onset or not any(
                            type(segment.get("source_start_time_sec")) in (int, float) and
                            type(segment.get("source_end_time_sec")) in (int, float) and
                            segment["source_start_time_sec"] <= audio_onset <= segment["source_end_time_sec"]
                            for segment in audio_segments)):
                        err("references", f"{ev.get('evidence_id')}: measured audio onset is outside cited requirement WAVs")
                    if (not valid_visual_onset or not any(
                            type(frame.get("time_sec")) in (int, float) and
                            abs(frame["time_sec"] - visual_onset) <= .25
                            for frame in ev.get("frames") or [])):
                        err("references", f"{ev.get('evidence_id')}: measured visual onset is not tied to a cited source frame")


def validate(record, review_dir, boxes_reviewed):
    groups = {name: [] for name in ("references", "files", "coordinates", "scores")}
    def err(group, msg):
        groups[group].append(msg)
    try:
        prepared = json.loads((review_dir / "input.json").read_text(encoding="utf-8"))
        manifest = json.loads((review_dir / "logs" / "frame_manifest.json").read_text(encoding="utf-8"))
        pts = json.loads((review_dir / "logs" / "frame_pts.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Missing or unreadable prepared run") from exc
    def valid_time(value):
        return type(value) in (int, float) and math.isfinite(value)
    duration = prepared["media_metadata"].get("duration_sec")
    if not valid_time(duration) or duration <= 0:
        err("scores", "Prepared media duration is invalid")
        duration = 0
    pts_map = {p.get("frame_index"): p.get("time_sec") for p in pts if isinstance(p, dict)}
    for source in manifest:
        index = source.get("frame_index")
        if (index not in pts_map or not valid_time(source.get("time_sec")) or
                abs(source["time_sec"] - pts_map[index]) > 0.000001):
            err("references", f"Manifest frame {index} differs from source PTS")
    if record.get("schema_version") != "t2av_score_v1" or record.get("overall_score", "missing") is not None:
        err("scores", "schema_version must be t2av_score_v1 and overall_score must be null")
    for key in ("sample_id", "video_path", "prompt", "prompt_provenance", "media_metadata"):
        if record.get(key) != prepared.get(key):
            err("scores", f"{key} differs from prepared input")
    if not isinstance(record.get("summary"), str) or not record["summary"].strip():
        err("scores", "summary is required")
    metric_items = record.get("metric_scores")
    checks = record.get("prompt_checks")
    evidence = record.get("evidence")
    if not all(isinstance(x, list) for x in (metric_items, checks, evidence)):
        raise ValueError("metric_scores, prompt_checks and evidence must be arrays")
    metrics = {m.get("metric_id") for m in metric_items if isinstance(m, dict)}
    if len(metric_items) != 17 or metrics != set(METRICS):
        err("scores", "17 unique metrics are required")
    req_ids = [r.get("requirement_id") for r in checks]
    ev_ids = [e.get("evidence_id") for e in evidence]
    if any(not x or not isinstance(x, str) for x in req_ids) or len(set(req_ids)) != len(req_ids):
        err("references", "Requirement IDs must be nonempty and unique")
    if any(not x or not isinstance(x, str) for x in ev_ids) or len(set(ev_ids)) != len(ev_ids):
        err("references", "Evidence IDs must be nonempty and unique")
    req_set, ev_set = set(req_ids), set(ev_ids)
    verify_workflow(record, review_dir, prepared, err)
    rubric = anchors()
    for m in metric_items:
        mid = m.get("metric_id")
        if mid not in METRICS:
            continue
        if not set(m.get("requirement_ids") or []) <= req_set or not set(m.get("evidence_ids") or []) <= ev_set:
            err("references", f"{mid}: broken requirement or evidence reference")
        if m.get("metric_type") != ("通用" if mid in UNIVERSAL else "条件"):
            err("scores", f"{mid}: metric_type is wrong")
        if not isinstance(m.get("applicability_reason"), str) or not m["applicability_reason"].strip():
            err("scores", f"{mid}: applicability_reason is required")
        status, score = m.get("status"), m.get("score")
        if status == "已评分":
            if type(score) is not int or score not in range(6):
                err("scores", f"{mid}: score must be an integer from 0 to 5")
            elif m.get("rubric_anchor") != rubric[(mid, score)]:
                err("scores", f"{mid}: rubric_anchor does not match original rubric")
            if not m.get("evidence_ids") or not m.get("rationale"):
                err("scores", f"{mid}: scored metric needs evidence and rationale")
            elif prepared["media_metadata"].get("width") and not any(
                e.get("frames") for e in evidence if e.get("evidence_id") in m["evidence_ids"]
            ):
                err("scores", f"{mid}: scored metric needs saved video-frame context")
            if mid in AUDIO_METRICS and not any(
                e.get("audio_segments") or (e.get("kind") == "verified_absence" and e.get("log_paths"))
                for e in evidence if e.get("evidence_id") in (m.get("evidence_ids") or [])
            ):
                err("scores", f"{mid}: scored audio metric needs WAV or verified no-audio probe evidence")
            if type(score) is int and score <= 2 and not m.get("failure_reason"):
                err("scores", f"{mid}: 0–2 score needs failure_reason")
        elif status == "不适用" and mid not in UNIVERSAL:
            if score is not None or m.get("rubric_anchor") is not None or not m.get("rationale"):
                err("scores", f"{mid}: inapplicable metric has invalid fields")
        else:
            err("scores", f"{mid}: invalid status")
        if m.get("confidence") not in ("高", "中", "低"):
            err("scores", f"{mid}: confidence is required")
    for check in checks:
        quote = check.get("prompt_quote")
        if not isinstance(quote, str) or quote not in prepared["prompt"]:
            err("references", f"{check.get('requirement_id')}: quote is absent from actual Prompt")
        if not set(check.get("metric_ids") or []) <= metrics or not set(check.get("evidence_ids") or []) <= ev_set:
            err("references", f"{check.get('requirement_id')}: broken metric or evidence reference")
        if check.get("status") not in ("已呈现", "部分呈现", "未呈现", "与要求矛盾", "无法判断"):
            err("scores", f"{check.get('requirement_id')}: invalid prompt check status")
    validate_audio_boundaries(checks, metric_items, err)
    validate_requirement_audio_result(record, review_dir, prepared, checks, metric_items, evidence, err)
    frame_map = {f["frame_index"]: f for f in manifest}
    for ev in evidence:
        eid = ev.get("evidence_id")
        if not set(ev.get("metric_ids") or []) <= metrics or not set(ev.get("requirement_ids") or []) <= req_set:
            err("references", f"{eid}: broken reverse reference")
        if ev.get("kind") not in ("visual", "audio", "audiovisual", "verified_absence"):
            err("scores", f"{eid}: invalid kind")
        if not isinstance(ev.get("observation"), str) or not ev["observation"].strip():
            err("scores", f"{eid}: observation is required")
        for frame in ev.get("frames") or []:
            index = frame.get("frame_index")
            source = frame_map.get(index)
            if source is None or not isinstance(index, int) or index >= len(pts):
                err("references", f"{eid}: frame index absent from source manifest")
                continue
            if (frame.get("time_sec") != source["time_sec"] or
                    frame.get("time_sec") != pts_map.get(index) or
                    frame.get("original_image_path") != source["original_image_path"]):
                err("references", f"{eid}: frame PTS or path differs from source")
            for field in ("original_image_path", "annotated_image_path"):
                path = check_path(frame.get(field), groups["files"], f"{eid}.{field}")
                if path:
                    try:
                        if image_size(path) != (frame.get("width"), frame.get("height")):
                            err("coordinates", f"{eid}: image dimensions mismatch")
                    except (OSError, ValueError):
                        err("files", f"{eid}: unreadable image")
            if (frame.get("width"), frame.get("height")) != (source["width"], source["height"]):
                err("coordinates", f"{eid}: source frame dimensions mismatch")
            if not frame.get("boxes") and not frame.get("bbox_unavailable_reason"):
                err("coordinates", f"{eid}: empty boxes need a reason")
            for box in frame.get("boxes") or []:
                xy = box.get("bbox_xyxy")
                if not isinstance(xy, list) or len(xy) != 4 or any(type(x) is not int for x in xy):
                    err("coordinates", f"{eid}: bbox must be four integers")
                elif not (0 <= xy[0] < xy[2] <= frame["width"] and
                          0 <= xy[1] < xy[3] <= frame["height"]):
                    err("coordinates", f"{eid}: bbox outside frame")
                if box.get("region_type") not in ("object", "contact", "text", "mouth", "global", "search_area"):
                    err("coordinates", f"{eid}: invalid region_type")
                if box.get("color") not in ("red", "green"):
                    err("coordinates", f"{eid}: invalid box color")
        for segment in ev.get("audio_segments") or []:
            start, end = segment.get("source_start_time_sec"), segment.get("source_end_time_sec")
            if not (valid_time(start) and valid_time(end) and 0 <= start < end <= duration + 0.5):
                err("references", f"{eid}: audio interval is outside source timeline")
            path = check_path(segment.get("audio_path"), groups["files"], f"{eid}.audio_path")
            if path:
                try:
                    with wave.open(str(path), "rb") as wav:
                        if (wav.getframerate() != segment.get("sample_rate_hz") or
                                wav.getnchannels() != segment.get("channels") or wav.getnframes() == 0):
                            err("files", f"{eid}: WAV metadata mismatch")
                except (wave.Error, OSError):
                    err("files", f"{eid}: unreadable WAV")
            if segment.get("verified_by_listening") not in (True, False):
                err("scores", f"{eid}: verified_by_listening must be boolean")
        if ev.get("audio_segments") and not any(s.get("verified_by_listening") for s in ev["audio_segments"]):
            if ev.get("analysis_method") != "gemini_audio_understanding":
                err("scores", f"{eid}: Gemini provenance is required for unheard audio")
            analysis_path = check_path(ev.get("analysis_result_path"), groups["files"], f"{eid}.analysis_result_path")
            if analysis_path:
                try:
                    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                    analyzed = {s.get("audio_path"): s for s in analysis.get("segments", [])}
                    if analysis.get("status") != "complete":
                        err("scores", f"{eid}: audio analysis is not complete")
                    if analysis.get("source", {}).get("sha256") != prepared.get("video_sha256"):
                        err("references", f"{eid}: audio analysis source differs from video")
                    for segment in ev["audio_segments"]:
                        found = analyzed.get(segment.get("audio_path"))
                        if not found or found.get("status") != "analyzed":
                            err("references", f"{eid}: WAV is absent from completed audio analysis")
                        elif (found.get("source_start_time_sec") != segment.get("source_start_time_sec") or
                              found.get("source_end_time_sec") != segment.get("source_end_time_sec") or
                              sha256(Path(segment["audio_path"])) != found.get("audio_sha256")):
                            err("references", f"{eid}: WAV hash or source interval differs from analysis")
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    err("files", f"{eid}: unreadable audio analysis ({type(exc).__name__})")
        for path in (ev.get("clip_paths") or []) + (ev.get("log_paths") or []):
            check_path(path, groups["files"], f"{eid}.asset")
        for sync in ev.get("synchronization") or []:
            offset = sync.get("offset_sec")
            if offset is not None:
                visual = sync.get("visual_onset_sec")
                audio = sync.get("audio_onset_sec")
                if not (valid_time(visual) and valid_time(audio) and valid_time(offset)
                        and 0 <= visual <= duration and 0 <= audio <= duration
                        and abs(offset - (audio - visual)) <= 0.02):
                    err("scores", f"{eid}: synchronization offset is not traceable")
    inspection = record.get("inspection") or {}
    viewed = inspection.get("viewed_frame_indices") or []
    if not viewed or not set(viewed) <= set(frame_map):
        err("scores", "inspection.viewed_frame_indices must list actually viewed saved frames")
    else:
        source_times = [f["time_sec"] for f in pts]
        required = set()
        for i in range(int(duration * 4) + 1):
            target = min(duration, i / 4)
            j = bisect.bisect_left(source_times, target)
            nearest = min((k for k in (j - 1, j) if 0 <= k < len(pts)),
                          key=lambda k: abs(source_times[k] - target))
            required.add(pts[nearest]["frame_index"])
        if not required <= set(viewed):
            err("scores", "Full-video 4-fps PTS coverage is missing from viewed frames")
    opened = inspection.get("original_frames_opened") or []
    if not isinstance(opened, list) or any(type(n) is not int or n not in frame_map for n in opened):
        err("scores", "inspection.original_frames_opened must list saved source frames")
        opened = []
    prompt_check_map = {item.get("requirement_id"): item for item in checks}
    validate_state_transitions(checks, evidence, metric_items, inspection, frame_map,
                               viewed, opened, pts, prompt_check_map, err)
    if not inspection.get("visual_inspection_intervals_sec"):
        err("scores", "inspection.visual_inspection_intervals_sec is required")
    if not inspection.get("tools"):
        err("scores", "inspection.tools is required")
    if not isinstance(record.get("uncertainties"), list) or not isinstance(record.get("inspection_limits"), list):
        err("scores", "uncertainties and inspection_limits must be arrays")
    result = {
        "json_parse_ok": True,
        "all_17_metrics_present": len(metric_items) == 17 and metrics == set(METRICS),
        "references_valid": not groups["references"],
        "files_verified": not groups["files"],
        "coordinates_verified": not groups["coordinates"] if boxes_reviewed else None,
        "score_evidence_complete": not groups["scores"],
        "errors": sum(groups.values(), []),
    }
    if not boxes_reviewed:
        result["errors"].append("Annotated boxes need visual review before finalization")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render_p = sub.add_parser("render")
    render_p.add_argument("draft", type=Path)
    final_p = sub.add_parser("finalize")
    final_p.add_argument("draft", type=Path)
    final_p.add_argument("--output", type=Path)
    final_p.add_argument("--boxes-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "render":
            print(json.dumps({"rendered_frames": render(args.draft)}, ensure_ascii=False))
            return 0
        draft = args.draft.resolve(strict=True)
        record = load_record(draft)
        validation = validate(record, draft.parent, args.boxes_reviewed)
        record["validation"] = validation
        if validation["errors"]:
            print(json.dumps(validation, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        output = args.output or draft.parent / "score.jsonl"
        if output.exists() and output.resolve() != draft:
            raise ValueError("Output exists; use a new review run")
        output.write_text(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
        if len(output.read_text(encoding="utf-8").splitlines()) != 1:
            raise ValueError("JSONL must contain exactly one line")
        print(output.resolve())
        return 0
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Review validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
