#!/usr/bin/env python3
"""Create and freeze staged checkpoints for one T2AV review run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from workflow_common import (
    INITIAL_REQUIRED_FIELDS, PHYSICAL_CLAIM_TYPES, STATE_DIMENSIONS, VERDICTS,
    canonical_bytes, collect_asset_hashes, read_json, sha256_bytes, sha256_file,
    validate_board_manifest, validate_prompt_plan, write_json,
)


def paths(run: Path):
    root = run / "workflow"
    return root, root / "state.json"


def load_run(run: Path):
    run = run.expanduser().resolve(strict=True)
    prepared = read_json(run / "input.json")
    pts = read_json(run / "logs" / "frame_pts.json")
    return run, prepared, pts


def load_state(run: Path):
    _, state_path = paths(run)
    if not state_path.is_file():
        raise ValueError("workflow is not initialized")
    return read_json(state_path)


def init(run: Path):
    run, prepared, _ = load_run(run)
    root, state_path = paths(run)
    if state_path.exists():
        raise ValueError("workflow is already initialized")
    root.mkdir()
    template = {
        "prompt_sha256": sha256_bytes(prepared["prompt"].encode("utf-8")),
        "prompt_checks": [],
    }
    write_json(root / "plan.template.json", template)
    write_json(state_path, {
        "workflow_version": 1,
        "schema_version": "t2av_score_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "prompt_sha256": template["prompt_sha256"],
        "stages": {},
    })
    return root / "plan.template.json"


def freeze(run: Path, source: Path, stage: str):
    run, prepared, pts = load_run(run)
    root, state_path = paths(run)
    state = load_state(run)
    if stage in state["stages"]:
        raise ValueError(f"{stage} is already frozen")
    prerequisites = {"plan": (), "initial": ("plan",), "challenge": ("plan", "initial")}
    missing = [name for name in prerequisites[stage] if name not in state["stages"]]
    if missing:
        raise ValueError(f"freeze {', '.join(missing)} first")
    for name in prerequisites[stage]:
        checkpoint = state["stages"][name]
        if sha256_file(root / f"{name}.json") != checkpoint.get("checkpoint_sha256"):
            raise ValueError(f"{name} checkpoint hash mismatch")
        for raw, digest in checkpoint.get("asset_sha256", {}).items():
            if not Path(raw).is_file() or sha256_file(Path(raw)) != digest:
                raise ValueError(f"{name} asset hash mismatch: {raw}")
    value = read_json(source.expanduser().resolve(strict=True))
    if stage == "plan":
        expected = sha256_bytes(prepared["prompt"].encode("utf-8"))
        if value.get("prompt_sha256") != expected:
            raise ValueError("plan prompt_sha256 differs from prepared prompt")
        errors = validate_prompt_plan(value, prepared["prompt"])
    elif stage == "initial":
        errors = validate_initial(value, read_json(root / "plan.json"), pts)
    else:
        errors = validate_challenge(value, read_json(root / "initial.json"))
    if errors:
        raise ValueError("; ".join(errors))
    destination = root / f"{stage}.json"
    assets = collect_asset_hashes(value, run)
    write_json(destination, value)
    state["stages"][stage] = {
        "checkpoint_path": str(destination.resolve()),
        "checkpoint_sha256": sha256_file(destination),
        "asset_sha256": assets,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(state_path, state)
    return destination


def validate_initial(value, plan, pts):
    errors = []
    checks = value.get("state_transition_checks") if isinstance(value, dict) else None
    if not isinstance(checks, list):
        return ["state_transition_checks must be an array"]
    strict_requirements = {
        item["requirement_id"] for item in plan["prompt_checks"]
        if item["claim_type"] in PHYSICAL_CLAIM_TYPES
    }
    ids, covered = set(), set()
    pts_map = {p["frame_index"]: p["time_sec"] for p in pts}
    ordered_pts = sorted(pts, key=lambda p: p["time_sec"])
    for i, check in enumerate(checks):
        label = f"state_transition_checks[{i}]"
        if not isinstance(check, dict):
            errors.append(f"{label}: must be an object")
            continue
        missing = [field for field in INITIAL_REQUIRED_FIELDS if field not in check]
        if missing:
            errors.append(f"{label}: missing fields {', '.join(missing)}")
            continue
        tid, rid = check["transition_id"], check["requirement_id"]
        if not isinstance(tid, str) or not tid or tid in ids:
            errors.append(f"{label}: transition_id must be nonempty and unique")
        ids.add(tid)
        if rid not in strict_requirements:
            errors.append(f"{label}: requirement_id is not a strict physical action")
        covered.add(rid)
        interval = check["source_interval_sec"]
        if (not isinstance(interval, list) or len(interval) != 2 or
                not all(type(x) in (int, float) and math.isfinite(x) for x in interval) or
                interval[0] < 0 or interval[0] >= interval[1] or interval[1] - interval[0] > 2.000001):
            errors.append(f"{label}: transition interval must be valid and no longer than 2 seconds")
            continue
        selected = [p["frame_index"] for p in ordered_pts
                    if interval[0] - 1e-6 <= p["time_sec"] <= interval[1] + 1e-6]
        if check["source_frame_indices"] != selected or not selected:
            errors.append(f"{label}: source_frame_indices must contain every source frame in the interval")
        sequence = [check["before_frame_index"], *check["during_frame_indices"], check["after_frame_index"]]
        if (not check["during_frame_indices"] or any(type(n) is not int or n not in pts_map for n in sequence) or
                any(pts_map[a] >= pts_map[b] for a, b in zip(sequence, sequence[1:]))):
            errors.append(f"{label}: before/during/after frames must exist and be PTS ordered")
        elif not all(interval[0] - 1e-6 <= pts_map[n] <= interval[1] + 1e-6 for n in sequence):
            errors.append(f"{label}: before/during/after frames must all be inside the local interval")
        else:
            boundary = [p["frame_index"] for p in ordered_pts
                        if pts_map[sequence[0]] - 1e-6 <= p["time_sec"] <= pts_map[sequence[-1]] + 1e-6]
            if not set(boundary) <= set(check["initial_evidence_frame_indices"]):
                errors.append(f"{label}: every source frame between stable states must be individually opened")
        board_path = Path(check["board_manifest_path"])
        board_errors, _ = validate_board_manifest(board_path, interval, selected)
        errors.extend(f"{label}: {message}" for message in board_errors)
        ledger = check["entity_ledger"]
        if not isinstance(ledger, list) or not ledger:
            errors.append(f"{label}: entity_ledger must be nonempty")
            ledger = []
        else:
            for entity in ledger:
                for field in ("entity_id", "before_state", "during_state", "after_state",
                              "allowed_changes", "actual_changes", "status"):
                    if field not in entity:
                        errors.append(f"{label}: entity ledger is missing {field}")
                if entity.get("status") not in {"tracked", "occluded", "unexplained"}:
                    errors.append(f"{label}: invalid entity status")
        dimensions = check["dimension_checks"]
        if (not isinstance(dimensions, list) or
                {item.get("dimension") for item in dimensions if isinstance(item, dict)} != set(STATE_DIMENSIONS)):
            errors.append(f"{label}: all nine state dimensions must appear exactly once")
            dimensions = []
        else:
            if len(dimensions) != len(STATE_DIMENSIONS):
                errors.append(f"{label}: state dimensions must be unique")
            for item in dimensions:
                if item.get("status") not in {"not_applicable", "applicable_consistent",
                                              "applicable_uncertain", "applicable_defect"}:
                    errors.append(f"{label}: invalid dimension status")
                if not isinstance(item.get("observation"), str) or not item["observation"].strip():
                    errors.append(f"{label}: each dimension needs an observation")
                if item.get("status") != "not_applicable" and not item.get("frame_indices"):
                    errors.append(f"{label}: applicable dimensions need frame_indices")
                if item.get("status") != "not_applicable" and not isinstance(item.get("strongest_counterexample"), str):
                    errors.append(f"{label}: applicable dimensions need strongest_counterexample")
        if check["initial_verdict"] not in VERDICTS:
            errors.append(f"{label}: invalid initial_verdict")
        unresolved = any(e.get("status") in {"occluded", "unexplained"} for e in ledger if isinstance(e, dict))
        unresolved = unresolved or any(d.get("status") in {"applicable_uncertain", "applicable_defect"}
                                       for d in dimensions if isinstance(d, dict))
        if check["initial_verdict"] == "confirmed_consistent" and unresolved:
            errors.append(f"{label}: unresolved entities or dimensions contradict confirmed_consistent")
        if check["initial_verdict"] == "confirmed_defect" and not any(
                d.get("status") == "applicable_defect" for d in dimensions if isinstance(d, dict)):
            errors.append(f"{label}: confirmed_defect requires an applicable_defect dimension")
        if check["initial_verdict"] == "uncertain" and not (
                any(e.get("status") in {"occluded", "unexplained"} for e in ledger if isinstance(e, dict)) or
                any(d.get("status") == "applicable_uncertain" for d in dimensions if isinstance(d, dict))):
            errors.append(f"{label}: uncertain requires an unresolved entity or dimension")
        if not isinstance(check["strongest_counterexamples"], list) or not check["strongest_counterexamples"]:
            errors.append(f"{label}: strongest_counterexamples must be nonempty")
        if not isinstance(check["evidence_ids"], list) or not check["evidence_ids"]:
            errors.append(f"{label}: evidence_ids must be nonempty")
    for rid in sorted(strict_requirements - covered):
        errors.append(f"{rid}: missing state transition check")
    return errors


def validate_challenge(value, initial):
    errors = []
    reviews = value.get("challenge_reviews") if isinstance(value, dict) else None
    if not isinstance(reviews, list):
        return ["challenge_reviews must be an array"]
    initial_map = {item["transition_id"]: item for item in initial["state_transition_checks"]}
    seen = set()
    for i, review in enumerate(reviews):
        label = f"challenge_reviews[{i}]"
        if not isinstance(review, dict):
            errors.append(f"{label}: must be an object")
            continue
        tid = review.get("transition_id")
        if tid not in initial_map or tid in seen:
            errors.append(f"{label}: transition_id must reference one frozen initial check exactly once")
            continue
        seen.add(tid)
        if review.get("method") != "self_blind":
            errors.append(f"{label}: method must be self_blind")
        if review.get("verdict") not in VERDICTS:
            errors.append(f"{label}: invalid verdict")
        if not isinstance(review.get("observation"), str) or not review["observation"].strip():
            errors.append(f"{label}: observation is required")
        frames = review.get("frame_indices")
        if not isinstance(frames, list) or not frames or any(type(n) is not int for n in frames):
            errors.append(f"{label}: frame_indices must be a nonempty integer array")
            continue
        original = initial_map[tid]
        if review.get("verdict") in {"confirmed_defect", "uncertain"}:
            findings = review.get("dimension_findings")
            if not isinstance(findings, list) or not findings:
                errors.append(f"{label}: adverse challenge requires dimension_findings")
            else:
                for finding in findings:
                    if (not isinstance(finding, dict) or finding.get("dimension") not in STATE_DIMENSIONS or
                            finding.get("status") not in {"applicable_defect", "applicable_uncertain"} or
                            not isinstance(finding.get("observation"), str) or not finding["observation"].strip() or
                            not isinstance(finding.get("frame_indices"), list) or not finding["frame_indices"] or
                            not set(finding["frame_indices"]) <= set(frames)):
                        errors.append(f"{label}: invalid dimension finding")
        unknown_frames = set(frames) - set(original["source_frame_indices"])
        if unknown_frames:
            errors.append(f"{label}: challenge frames must belong to the frozen local interval")
        has_new_frames = bool(set(frames) - set(original["initial_evidence_frame_indices"])) and not unknown_frames
        tighter = False
        board_raw = review.get("board_manifest_path")
        if isinstance(board_raw, str):
            challenge_errors, challenge_board = validate_board_manifest(
                Path(board_raw), original["source_interval_sec"], original["source_frame_indices"])
            errors.extend(f"{label}: {message}" for message in challenge_errors)
            _, initial_board = validate_board_manifest(
                Path(original["board_manifest_path"]), original["source_interval_sec"],
                original["source_frame_indices"])
            if challenge_board and initial_board:
                a, b = initial_board["roi_xyxy"], challenge_board["roi_xyxy"]
                tighter = (b != a and b[0] >= a[0] and b[1] >= a[1] and
                           b[2] <= a[2] and b[3] <= a[3] and
                           (b[2] - b[0]) * (b[3] - b[1]) < (a[2] - a[0]) * (a[3] - a[1]))
                if Path(board_raw).resolve() == Path(original["board_manifest_path"]).resolve():
                    tighter = False
                if set(challenge_board.get("board_paths", [])) & set(initial_board.get("board_paths", [])):
                    tighter = False
        if not has_new_frames and not tighter:
            errors.append(f"{label}: self-blind challenge needs new frames or an independently tighter ROI board")
    for tid in sorted(set(initial_map) - seen):
        errors.append(f"{tid}: missing challenge review")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init_p = sub.add_parser("init")
    init_p.add_argument("run", type=Path)
    for name in ("plan", "initial", "challenge"):
        p = sub.add_parser(f"freeze-{name}")
        p.add_argument("run", type=Path)
        p.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = init(args.run)
        else:
            result = freeze(args.run, args.source, args.command.removeprefix("freeze-"))
        print(result.resolve())
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
