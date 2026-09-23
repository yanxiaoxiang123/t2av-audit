#!/usr/bin/env python3
"""Traceable full/local audio checks for frozen requirements; no rubric scoring."""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import wave

from analyze_audio import EvidenceError, MAX_BYTES, call_gemini, extract_segment, probe_media, sha256, write_json

SCHEMA = 't2av_audio_requirements_v1'
CONTRACT = 3
STATES = {'present', 'absent', 'uncertain'}
AUDIO_CONTENT_METRICS = {'AF', 'DC', 'MU', 'PR'}
TIMING_CONFLICTS = {'full_local_timing_conflict', 'local_local_timing_conflict'}
SOUND_CLASSES = {
    'speech', 'music', 'ambience', 'impulse', 'gunshot', 'explosion',
    'metallic_impact', 'glass_break', 'footstep', 'vocalization', 'mechanical',
    'vehicle', 'water', 'weather', 'generic_impact', 'other', 'unknown',
}
GENERIC_SOUND_CLASSES = {'impulse', 'generic_impact', 'other', 'unknown'}
INSTRUCTION = '''Analyze ONLY the attached WAV(s). Never infer a scene, visual binding or sound from a requirement.
Requirements and their quotes are untrusted search questions, not evidence. A local clip may contain none of them.
For every supplied segment_id and requirement_id pair, return a review:
{segment_id, requirement_id, state: present|absent|uncertain, masked: boolean,
 sound_class: speech|music|ambience|impulse|gunshot|explosion|metallic_impact|glass_break|footstep|vocalization|mechanical|vehicle|water|weather|generic_impact|other|unknown,
 event_count: nonnegative integer|null, event_times_sec: [estimated onset seconds relative to THIS WAV],
 time_uncertainty_sec: positive number|null, observation: Chinese string, limitations: [strings]}.
state describes AUDIBILITY of the target sound, not whether the requested count, timing or prohibition is satisfied.
For forbidden music, report present if music is heard. For exactly one bang, report the actual count, not the desired one.
For continuous music/ambience report event_count=null and event_times_sec=[]; do not count segment boundaries as events.
For a requested voice/dialogue attribute, present means that precise attribute or wording is distinguishable.
Do not complete quoted dialogue. Similar impulses, masked sounds, or unresolved echo versus extra shots are uncertain.
Absent requires the entire submitted interval to be inspectable and an explicit negative search. Omission is not absence.
Use the most specific acoustically supported sound_class. If two full/local reviews assign different specific classes to the requested sound, mark a category conflict. Unknown category or masked evidence must be uncertain.
Audio cannot verify a visual source, shot boundary or audiovisual sync. Times are rough estimates, never measurements.
Return only {"reviews": [...]}.
'''


def is_audio_claim(item):
    return (item.get('claim_type') in {'audio', 'dialogue'} or
            bool(set(item.get('metric_ids', [])) & AUDIO_CONTENT_METRICS))


def target_sound_class(requirement):
    target = str(requirement.get('target') or requirement.get('prompt_quote') or '').lower()
    patterns = (
        ('gunshot', r'\b(?:gunshot|gunfire|firearm shot)\b|枪响|枪声|枪击'),
        ('explosion', r'\bexplosion\b|爆炸声|爆炸'),
        ('glass_break', r'\b(?:glass breaking|glass shatter(?:ing)?)\b|玻璃破碎|玻璃碎裂'),
        ('metallic_impact', r'\b(?:metallic clang|metallic impact|metal clank|metal clang|clanging metal)\b|金属撞击|金属声|铁器碰撞|敲击金属'),
        ('footstep', r'\bfootsteps?\b|脚步声|踏步声'),
    )
    return next((sound_class for sound_class, pattern in patterns if re.search(pattern, target)), None)


def infer_audio_spec(item, spec):
    """Fill only conservative count/polarity cues stated literally in the quote."""
    quote = f"{item.get('prompt_quote', '')} {item.get('expected_after', '')}".lower()
    if spec.get('polarity') is None:
        if re.search(r"\bno\s+(?:non[- ]diegetic\s+)?music\b|禁止配乐|没有音乐", quote):
            spec['polarity'] = 'forbidden'
        else:
            spec['polarity'] = 'required'
    if spec.get('event_kind') is None:
        spec['event_kind'] = 'discrete' if re.search(
            r"\b(?:exactly\s+one|single|once|one)\b|恰好一|单次|一次|一声", quote) else 'unknown'
    if spec.get('expected_count') is None and spec.get('event_kind') == 'discrete' and re.search(
            r"\b(?:exactly\s+one|single|once|one)\b|恰好一|单次|一次|一声", quote):
        spec['expected_count'] = 1
    return spec


def requirements_from_plan(plan):
    result = []
    for item in plan['prompt_checks']:
        if not is_audio_claim(item):
            continue
        raw_spec = item.get('audio_spec')
        if raw_spec is None:
            spec = {}
        elif not isinstance(raw_spec, dict):
            raise EvidenceError('audio_spec must be an object')
        else:
            spec = dict(raw_spec)
        spec = infer_audio_spec(item, spec)
        polarity = spec.get('polarity', 'required')
        kind = spec.get('event_kind', 'unknown')
        count = spec.get('expected_count')
        interval = spec.get('search_interval_sec')
        if polarity not in {'required', 'forbidden'} or kind not in {'discrete', 'continuous', 'unknown'}:
            raise EvidenceError('Invalid audio polarity or event kind')
        if count is not None and (type(count) is not int or count < 0 or kind != 'discrete'):
            raise EvidenceError('expected_count requires a discrete sound and nonnegative integer')
        if interval is not None and (not isinstance(interval, list) or len(interval) != 2 or
                                    not all(finite(x) for x in interval) or not 0 <= interval[0] < interval[1]):
            raise EvidenceError('Invalid audio search interval')
        result.append({'requirement_id': item['requirement_id'], 'prompt_quote': item['prompt_quote'],
                       'predicate': item['predicate'], 'expected_after': item.get('expected_after'),
                       'target': spec.get('target') or item.get('expected_after') or item['prompt_quote'],
                       'polarity': polarity, 'event_kind': kind, 'expected_count': count,
                       'search_interval_sec': interval, 'metric_ids': item['metric_ids']})
    return result


def frozen_audio_requirements(run):
    state = json.loads((run / 'workflow/state.json').read_text())
    plan_path = run / 'workflow/plan.json'
    if sha256(plan_path) != state.get('stages', {}).get('plan', {}).get('checkpoint_sha256'):
        raise EvidenceError('A matching frozen plan is required')
    plan = json.loads(plan_path.read_text())
    prepared = json.loads((run / 'input.json').read_text())
    media = Path(prepared['video_path']).resolve(strict=True)
    if plan.get('prompt_sha256') != hashlib.sha256(prepared['prompt'].encode()).hexdigest():
        raise EvidenceError('Frozen plan prompt differs from prepared prompt')
    if sha256(media) != prepared['video_sha256']:
        raise EvidenceError('Source video changed after preparation')
    return requirements_from_plan(plan), {'media': media, 'plan_sha256': sha256(plan_path)}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def pcm_candidates(path, max_events=2):
    """20 ms channel-energy windows / 10 ms hop; no semantic sound labels."""
    with wave.open(str(path), 'rb') as source:
        rate, channels = source.getframerate(), source.getnchannels()
        if source.getsampwidth() != 2:
            raise EvidenceError('PCM detector requires 16-bit WAV')
        values = array('h', source.readframes(source.getnframes()))
    if sys.byteorder != 'little':
        values.byteswap()
    hop, levels = max(1, round(rate * .01)), []
    for i in range(0, len(values) // channels, hop):
        chunk = values[i*channels:(i+2*hop)*channels]
        rms = math.sqrt(sum(v*v for v in chunk) / max(1, len(chunk))) / 32768
        levels.append(20 * math.log10(max(rms, 1e-9)))
    rises = [(levels[i] - sum(levels[max(0, i-5):i])/min(5, i), i*hop/rate)
             for i in range(1, len(levels))]
    chosen = []
    for rise, time_sec in sorted(rises, reverse=True):
        if rise < 4 or len(chosen) >= max_events:
            break
        if all(abs(time_sec-other) >= .4 for other in chosen):
            chosen.append(time_sec)
    return sorted(chosen)


def windows(duration, peaks):
    result, cursor = [], 0.0
    while cursor < duration - 1e-6:
        end = min(duration, cursor+2.5)
        result.append((cursor, end, 'coverage'))
        if end >= duration:
            break
        cursor = end-.25
    for peak in peaks:
        a, b = max(0., peak-.8), min(duration, peak+1.)
        if b-a >= .25 and not any(abs(a-x)<.1 and abs(b-y)<.1 for x,y,_ in result):
            result.append((a,b,'pcm_candidate'))
    return sorted(result)


def validate_reviews(raw, requirements, duration):
    expected = {x['requirement_id'] for x in requirements}
    rows = raw.get('reviews') if isinstance(raw, dict) else None
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise EvidenceError('Gemini omitted requirement reviews')
    seen, output = set(), []
    for item in rows:
        if not isinstance(item, dict) or item.get('requirement_id') not in expected or item['requirement_id'] in seen:
            raise EvidenceError('Invalid or duplicate requirement ID')
        seen.add(item['requirement_id'])
        state, masked = item.get('state'), item.get('masked')
        count, times, uncertainty = item.get('event_count'), item.get('event_times_sec'), item.get('time_uncertainty_sec')
        if state not in STATES or type(masked) is not bool or not isinstance(times, list):
            raise EvidenceError('Invalid requirement state')
        if count is not None and (type(count) is not int or count < 0):
            raise EvidenceError('Invalid event count')
        if any(not finite(t) or not 0 <= t <= duration for t in times) or times != sorted(set(times)):
            raise EvidenceError('Invalid event timestamps')
        if uncertainty is not None and (not finite(uncertainty) or uncertainty <= 0):
            raise EvidenceError('Invalid time uncertainty')
        if not isinstance(item.get('observation'), str) or not item['observation'].strip():
            raise EvidenceError('Missing observation')
        limits = item.get('limitations')
        if not isinstance(limits, list) or not all(isinstance(x, str) for x in limits):
            raise EvidenceError('Invalid limitations')
        req = next(x for x in requirements if x['requirement_id'] == item['requirement_id'])
        sound_class = item.get('sound_class', 'unknown')
        if not isinstance(sound_class, str) or sound_class not in SOUND_CLASSES:
            raise EvidenceError('Invalid sound category')
        expected_class = target_sound_class(req)
        if masked or (state == 'present' and (
                sound_class == 'unknown' or (expected_class and sound_class != expected_class) or count == 0 or
                (count is not None and len(times) > count))):
            state = 'uncertain'
        if state == 'absent' and (times or count not in (0, None)):
            state = 'uncertain'
        if req.get('event_kind') == 'continuous':
            count, times, uncertainty = None, [], None
        output.append({**item, 'state': state, 'sound_class': sound_class,
                       'event_count': count, 'event_times_sec': times, 'time_uncertainty_sec': uncertainty})
    return output


def covers(rows, interval):
    a, b = interval
    cursor = a
    for row in sorted(rows, key=lambda x:x['source_start_time_sec']):
        left, right = row['source_start_time_sec'], row['source_end_time_sec']
        if right <= cursor:
            continue
        if left > cursor + 1e-5:
            return False
        cursor = max(cursor, right)
    return cursor >= b-1e-5


def compare(full, local, complete_coverage, requirement=None):
    """An audibility verdict; never a rubric score or visual binding verdict."""
    requirement = requirement or {}
    conflicts = []
    positives = [x for x in local if x['state'] == 'present']
    unknown = [x for x in local if x['state'] == 'uncertain']
    if full['state'] == 'absent' and positives:
        conflicts.append('full_absent_local_present')
    if full['state'] == 'present' and not positives and not unknown and complete_coverage:
        conflicts.append('full_present_local_absent')
    discrete = requirement.get('event_kind') == 'discrete'
    if full['state'] == 'present' and positives and (discrete or not requirement):
        full_count = full.get('event_count')
        if full_count is not None and any(x.get('event_count') is not None and x['event_count'] > full_count for x in positives):
            conflicts.append('local_count_exceeds_full')
        if discrete:
            full_times, full_u = full.get('source_event_times_sec', []), full.get('time_uncertainty_sec')
            if full_times and finite(full_u):
                for x in positives:
                    if finite(x.get('time_uncertainty_sec')) and any(
                        all(abs(t-ref) > max(.25, full_u+x['time_uncertainty_sec']) for ref in full_times)
                        for t in x.get('source_event_times_sec', [])):
                        conflicts.append('full_local_timing_conflict')
                        break
            # Count on disjoint source windows only; overlapping crops never add votes/counts.
            chosen, last_end = [], -math.inf
            for x in sorted(positives, key=lambda x:x.get('source_end_time_sec', math.inf)):
                if x.get('source_start_time_sec', -math.inf) >= last_end:
                    chosen.append(x); last_end = x.get('source_end_time_sec', math.inf)
            if full_count is not None and sum(x.get('event_count') or 0 for x in chosen) > full_count:
                conflicts.append('disjoint_local_count_exceeds_full')
    failed = (full['state'] == 'analysis_failed' or not complete_coverage or
              any(x['state'] == 'analysis_failed' for x in local))
    # Preserve discovered contradictions even when another request failed.
    if failed:
        return 'analysis_failed', conflicts
    if conflicts or unknown or full['state'] == 'uncertain':
        return 'uncertain', conflicts
    if full['state'] == 'present' and positives:
        return 'confirmed_present', []
    if full['state'] == 'absent' and local and all(x['state'] == 'absent' for x in local):
        return 'confirmed_absent', []
    return 'uncertain', []


def compare_detailed(full, local, complete_coverage, requirement=None):
    """Separate sound audibility from estimated-onset disagreement.

    The two-value ``compare`` API remains unchanged for historical contract-v2
    results. New contract-v3 results use this function so timing estimates can
    conflict without erasing a sound whose presence/content both analyses found.
    """
    requirement = requirement or {}
    conflicts = []
    semantic_conflicts = []
    timing_conflicts = []
    positives = [x for x in local if x['state'] == 'present']
    unknown = [x for x in local if x['state'] == 'uncertain']

    if full['state'] == 'absent' and positives:
        semantic_conflicts.append('full_absent_local_present')
    if full['state'] == 'present' and not positives and not unknown and complete_coverage:
        semantic_conflicts.append('full_present_local_absent')

    specific_classes = {
        row.get('sound_class') for row in [full, *positives]
        if isinstance(row.get('sound_class'), str) and
        row.get('sound_class') not in GENERIC_SOUND_CLASSES
    }
    if len(specific_classes) > 1:
        semantic_conflicts.append('full_local_category_conflict')

    discrete = requirement.get('event_kind') == 'discrete'
    if full['state'] == 'present' and positives:
        full_count = full.get('event_count')
        if discrete or not requirement:
            if full_count is not None and any(
                    x.get('event_count') is not None and x['event_count'] > full_count
                    for x in positives):
                semantic_conflicts.append('local_count_exceeds_full')
        if requirement.get('event_kind') != 'continuous':
            full_times = full.get('source_event_times_sec', [])
            full_uncertainty = full.get('time_uncertainty_sec')
            if full_times:
                for row in positives:
                    if estimates_disagree(full_times, row.get('source_event_times_sec', []),
                                          full_uncertainty, row.get('time_uncertainty_sec')):
                        timing_conflicts.append('full_local_timing_conflict')
                        break
            for index, left in enumerate(positives):
                left_start, left_end = left.get('source_start_time_sec'), left.get('source_end_time_sec')
                for right in positives[index + 1:]:
                    overlap_start = max(left_start, right.get('source_start_time_sec', math.inf))
                    overlap_end = min(left_end, right.get('source_end_time_sec', -math.inf))
                    if overlap_start >= overlap_end:
                        continue
                    left_times = [t for t in left.get('source_event_times_sec', [])
                                  if overlap_start <= t <= overlap_end]
                    right_times = [t for t in right.get('source_event_times_sec', [])
                                   if overlap_start <= t <= overlap_end]
                    if estimates_disagree(left_times, right_times,
                                          left.get('time_uncertainty_sec'),
                                          right.get('time_uncertainty_sec')):
                        timing_conflicts.append('local_local_timing_conflict')
                        break
                if 'local_local_timing_conflict' in timing_conflicts:
                    break
        if discrete or not requirement:
            # Counts from disjoint source intervals only; overlapping crops are
            # not independent observations and must not inflate an event count.
            chosen, last_end = [], -math.inf
            for row in sorted(positives, key=lambda x: x.get('source_end_time_sec', math.inf)):
                if row.get('source_start_time_sec', -math.inf) >= last_end:
                    chosen.append(row)
                    last_end = row.get('source_end_time_sec', math.inf)
            if full_count is not None and sum(row.get('event_count') or 0 for row in chosen) > full_count:
                semantic_conflicts.append('disjoint_local_count_exceeds_full')

    conflicts = semantic_conflicts + timing_conflicts
    failed = (full['state'] == 'analysis_failed' or not complete_coverage or
              any(row['state'] == 'analysis_failed' for row in local))
    if failed:
        status = 'analysis_failed'
    elif semantic_conflicts or full['state'] == 'uncertain':
        status = 'uncertain'
    elif full['state'] == 'present' and positives:
        # Other masked windows do not negate a positive observation elsewhere.
        status = 'confirmed_present'
    elif full['state'] == 'absent' and local and all(row['state'] == 'absent' for row in local):
        status = 'confirmed_absent'
    else:
        status = 'uncertain'

    estimates = timing_estimates(full, local)
    if timing_conflicts:
        timing_status = 'conflict'
    elif estimates['all_source_times_sec']:
        timing_status = 'estimated'
    elif requirement.get('event_kind') == 'continuous':
        timing_status = 'not_applicable'
    else:
        timing_status = 'not_reported'
    return status, conflicts, timing_status, estimates


def estimates_disagree(left_times, right_times, left_uncertainty, right_uncertainty):
    if not left_times or not right_times:
        return False
    tolerance = (left_uncertainty + right_uncertainty
                 if finite(left_uncertainty) and finite(right_uncertainty) else .5)
    tolerance = max(.5, tolerance)
    return (any(all(abs(t - other) > tolerance for other in right_times) for t in left_times) or
            any(all(abs(t - other) > tolerance for other in left_times) for t in right_times))


def timing_estimates(full, local):
    full_times = full.get('source_event_times_sec', [])
    local_rows = [{
        'segment_id': row.get('segment_id'),
        'source_interval_sec': [row.get('source_start_time_sec'), row.get('source_end_time_sec')],
        'event_times_sec': row.get('source_event_times_sec', []),
        'time_uncertainty_sec': row.get('time_uncertainty_sec'),
    } for row in local if row.get('source_event_times_sec')]
    all_times = sorted({t for t in full_times + [t for row in local_rows for t in row['event_times_sec']]
                        if finite(t)})
    return {
        'times_are_estimates': True,
        'full_source_times_sec': full_times,
        'local_estimates': local_rows,
        'all_source_times_sec': all_times,
    }


def requires_visual_binding(requirement):
    quote = str(requirement.get('prompt_quote') or '').lower()
    shot_reference = bool(re.search(
        r'\b(?:shot|scene)\s*\d+\b|\b(?:in|during|through)\s+(?:shot|scene)\b|镜头|画面|声画|同步', quote))
    return shot_reference or bool(set(requirement.get('metric_ids') or []) & {'AV', 'LS'})


def fulfillment(req, status, full):
    if status in {'uncertain', 'analysis_failed'}:
        return 'uncertain'
    if req.get('polarity') == 'forbidden':
        return 'satisfied' if status == 'confirmed_absent' else 'violated'
    if status == 'confirmed_absent':
        return 'violated'
    count = req.get('expected_count')
    if count is not None:
        if full.get('event_count') is None:
            return 'uncertain'
        return 'satisfied' if full['event_count'] == count else 'violated'
    return 'satisfied'


def assess(req, segments, interval):
    rows = [r for s in segments for r in s.get('reviews', []) if r['requirement_id'] == req['requirement_id']]
    full = next((r for r in rows if r['window_kind'] == 'full'), {'state':'analysis_failed'})
    local = [r for r in rows if r['window_kind'] != 'full']
    coverage = [r for r in local if r['window_kind'] == 'coverage' and r['state'] != 'analysis_failed']
    complete = covers(coverage, interval)
    status, conflicts, timing_status, estimates = compare_detailed(full, local, complete, req)
    binding_required = requires_visual_binding(req)
    return {**req, 'status':status, 'audibility_status':status,
            'fulfillment':fulfillment(req,status,full),
            'timing_status':timing_status, 'timing_conflicts':[x for x in conflicts if x in TIMING_CONFLICTS],
            'timing_estimates':estimates,
            'visual_binding_required':binding_required,
            'visual_binding_status':'pending_visual_review' if binding_required else 'not_required',
            'coverage_complete':complete, 'full_review':full, 'local_reviews':local,
            'conflicts':conflicts, 'verified_by_listening':False}


def listening_items(entry, full_segment):
    needs_review = (entry['fulfillment'] == 'uncertain' or entry['status'] == 'analysis_failed' or
                    entry.get('timing_status') == 'conflict')
    if not needs_review:
        return []
    rows = entry['local_reviews']
    # Preserve every contested/failed interval. An unrelated early short clip cannot resolve a later conflict.
    selected = [r for r in rows if r['state'] in {'uncertain','analysis_failed'}]
    if entry['conflicts']:
        selected += [r for r in rows if r['state'] == 'present']
    if not selected:
        selected = rows or [full_segment]
    unique = {r['audio_path']:r for r in selected}
    priority = 'high' if entry['conflicts'] else 'medium'
    return [{'requirement_id':entry['requirement_id'], 'reason':entry['conflicts'] or [entry['status']],
             'priority':priority, 'audio_path':r['audio_path'],
             'source_interval_sec':[r['source_start_time_sec'],r['source_end_time_sec']]}
            for r in sorted(unique.values(), key=lambda x:x['source_end_time_sec']-x['source_start_time_sec'])]


def aggregate_listening_clips(queue):
    """Provide one prioritized player entry per WAV while retaining item-level traceability."""
    grouped = {}
    for item in queue:
        path = item['audio_path']
        clip = grouped.setdefault(path, {
            'audio_path':path,
            'source_interval_sec':item['source_interval_sec'],
            'requirement_ids':[],
            'reason_codes':[],
            'priority':'medium',
        })
        if item['requirement_id'] not in clip['requirement_ids']:
            clip['requirement_ids'].append(item['requirement_id'])
        for reason in item.get('reason') or []:
            if reason not in clip['reason_codes']:
                clip['reason_codes'].append(reason)
        if item.get('priority') == 'high':
            clip['priority'] = 'high'
    return sorted(grouped.values(), key=lambda x: (x['priority'] != 'high', x['source_interval_sec'][0]))


def analyze_group(group, requirements, output_dir, timeout, batch_id):
    ids = [s['segment_id'] for s in group]
    instruction = INSTRUCTION + '\nRequirements: ' + json.dumps(requirements,ensure_ascii=False)
    instruction += '\nSegments (local zero for each WAV): ' + json.dumps([
        {'segment_id':s['segment_id'],'duration_sec':s['duration_sec']} for s in group])
    request = {'instruction':instruction,'segments':group,'temperature':.1,'max_tokens':12000}
    request_path = output_dir / f'{batch_id}_request.json'
    write_json(request_path, request)
    response, attempts = None, []
    for attempt in range(2):
        try:
            response = call_gemini([(s['segment_id'],Path(s['audio_path'])) for s in group], timeout,
                                  instruction_override=instruction, max_tokens=12000, stream=attempt == 0)
            raw_path = output_dir / f'{batch_id}_response_{attempt}.json'
            write_json(raw_path,response)
            attempts.append({'response_path':str(raw_path),'sha256':sha256(raw_path)})
            break
        except EvidenceError as exc:
            attempts.append({'error':str(exc),'transport':'stream' if attempt == 0 else 'non_stream'})
    raw = response['evidence'].get('reviews') if response else []
    raw = raw if isinstance(raw,list) else []
    if len(group) == 1:
        raw = [{**x,'segment_id':x.get('segment_id',ids[0])} if isinstance(x,dict) else x for x in raw]
    for segment in group:
        segment['request_path'], segment['request_sha256'] = str(request_path), sha256(request_path)
        segment['attempts'] = attempts
        segment['model'] = response['model'] if response else None
        rows = []
        for req in requirements:
            selected = [x for x in raw if isinstance(x,dict) and x.get('segment_id') == segment['segment_id']
                        and x.get('requirement_id') == req['requirement_id']]
            try:
                row = validate_reviews({'reviews':selected},[req],segment['duration_sec'])[0]
            except EvidenceError as exc:
                row = {'requirement_id':req['requirement_id'],'state':'analysis_failed','observation':str(exc),
                       'event_times_sec':[],'event_count':None,'time_uncertainty_sec':None}
            row.update({k:segment[k] for k in ('segment_id','audio_path','source_start_time_sec','source_end_time_sec')})
            row['window_kind'] = segment['kind']
            row['source_event_times_sec'] = [segment['source_start_time_sec']+t for t in row['event_times_sec']]
            rows.append(row)
        segment['reviews'] = rows
        segment['status'] = 'analyzed' if all(r['state'] != 'analysis_failed' for r in rows) else 'partial'


def review(run, output_dir, timeout=120, resume=False, batch_size=2):
    if not finite(timeout) or timeout <= 0 or type(batch_size) is not int or not 1 <= batch_size <= 4:
        raise EvidenceError('Invalid timeout or batch size')
    run, output_dir = run.expanduser().resolve(strict=True), output_dir.expanduser().resolve()
    requirements, context = frozen_audio_requirements(run)
    media, output = context['media'], output_dir/'audio_requirements.json'
    source_hash = sha256(media)
    if output_dir.exists() and any(output_dir.iterdir()) and not resume:
        raise EvidenceError('Use a new output directory, or --resume to retain successful batches')
    output_dir.mkdir(parents=True,exist_ok=True)
    if resume and output.exists():
        result = json.loads(output.read_text())
        if (result.get('contract_version') not in {2, CONTRACT} or result['source']['sha256'] != source_hash or
                result['source']['plan_sha256'] != context['plan_sha256'] or result.get('requested_requirements') != requirements):
            raise EvidenceError('Resume source, plan or contract mismatch')
        for s in result['segments']:
            if sha256(Path(s['audio_path'])) != s['audio_sha256']:
                raise EvidenceError('Resume WAV hash mismatch')
    else:
        info = probe_media(media)
        result = {'schema_version':SCHEMA,'contract_version':CONTRACT,'status':'incomplete',
                  'source':{'path':str(media),'sha256':source_hash,'plan_sha256':context['plan_sha256'],**info},
                  'requested_requirements':requirements,'requirements':[],'segments':[],'conflicts':[],
                  'listening_queue':[],'listening_clips':[],'visual_binding_queue':[],
                  'analysis':{'method':'gemini_full_and_local_audio','times_are_estimates':True,
                              'verified_by_listening':False,'pcm_is_semantic_evidence':False,
                              'timing_conflict_tolerance_sec':0.5}}
        write_json(output_dir/'media_probe.json',info)
        result['probe_path'],result['probe_sha256'] = str(output_dir/'media_probe.json'),sha256(output_dir/'media_probe.json')
        if info['has_audio_stream']:
            a,b = info['audio_interval_sec']
            a,b = max(0.,a),min(info['duration_sec'],b)
            if b <= a:
                raise EvidenceError('Audio track does not overlap the video')
            audio_dir = output_dir/'audio'; audio_dir.mkdir(exist_ok=True)
            full = extract_segment(media,audio_dir/'full.wav',info,a,b)
            full.update({'segment_id':'full','kind':'full'})
            result['segments'] = [full]
            peaks = pcm_candidates(Path(full['audio_path']))
            result['pcm_candidates_sec'] = [a+t for t in peaks]
            result['pcm_method'] = '20 ms RMS / 10 ms hop; channel energy, not averaged waveforms'
            planned = windows(b-a,peaks)
            # Prompt-specified timing windows are search coverage, never positive sound evidence.
            for req in requirements:
                interval = req.get('search_interval_sec')
                if interval:
                    left,right=max(a,interval[0]-.3),min(b,interval[1]+.3)
                    if right>left: planned.append((left-a,right-a,'requirement_window'))
            seen=set()
            for x,y,kind in planned:
                key=(round(x,6),round(y,6),kind)
                if key in seen:continue
                seen.add(key)
                sid=f'local_{len(result["segments"]):03d}'
                seg=extract_segment(media,audio_dir/f'{sid}.wav',info,a+x,a+y)
                seg.update({'segment_id':sid,'kind':kind});result['segments'].append(seg)
        write_json(output,result)
    if not result['source']['has_audio_stream']:
        empty_full = {'source_event_times_sec':[], 'time_uncertainty_sec':None}
        result['requirements']=[{
            **r, 'status':'confirmed_absent', 'audibility_status':'confirmed_absent',
            'fulfillment':fulfillment(r,'confirmed_absent',{}),
            'timing_status':'not_reported', 'timing_conflicts':[],
            'timing_estimates':timing_estimates(empty_full, []),
            'visual_binding_required':requires_visual_binding(r),
            'visual_binding_status':'not_assessable_no_audio' if requires_visual_binding(r) else 'not_required',
            'conflicts':[], 'verified_by_listening':False
        } for r in requirements]
        result['contract_version'] = CONTRACT
        result['listening_queue'] = []
        result['listening_clips'] = []
        result['visual_binding_queue'] = []
        result['status']='complete';write_json(output,result);return result
    pending=[s for s in result['segments'] if s.get('status') != 'analyzed']
    groups=[]
    for kind in ('full','coverage','pcm_candidate','requirement_window'):
        selected=[s for s in pending if s['kind']==kind]
        groups.extend(selected[i:i+batch_size] for i in range(0,len(selected),batch_size))
    attempt_number=len(list(output_dir.glob('batch_*_request.json')))
    for i,group in enumerate(groups):
        if not requirements:break
        analyze_group(group,requirements,output_dir,timeout,f'batch_{attempt_number+i:03d}')
        write_json(output,result)
    full=result['segments'][0]
    interval=[full['source_start_time_sec'],full['source_end_time_sec']]
    result['requirements']=[assess(req,result['segments'],interval) for req in requirements]
    result['conflicts']=[{'requirement_id':r['requirement_id'],'reasons':r['conflicts']} for r in result['requirements'] if r['conflicts']]
    result['listening_queue']=[x for r in result['requirements'] for x in listening_items(r,full)]
    result['listening_clips']=aggregate_listening_clips(result['listening_queue'])
    result['visual_binding_queue']=[{
        'requirement_id':r['requirement_id'],
        'audibility_status':r['audibility_status'],
        'timing_status':r['timing_status'],
        'timing_estimates':r['timing_estimates'],
        'status':'pending_visual_review',
    } for r in result['requirements'] if r['visual_binding_required']]
    result['contract_version'] = CONTRACT
    result['status']='incomplete' if any(r['status']=='analysis_failed' for r in result['requirements']) else 'complete'
    write_json(output,result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run',type=Path)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--timeout',type=float,default=120)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--batch-size',type=int,default=2)
    args=parser.parse_args()
    try:
        result=review(args.run,args.output_dir,args.timeout,args.resume,args.batch_size)
        print(json.dumps({'status':result['status'],'requirements':len(result['requirements']),
                          'listening_queue':len(result['listening_queue']),
                          'listening_clips':len(result.get('listening_clips', [])),
                          'visual_binding_queue':len(result.get('visual_binding_queue', [])),
                          'output':str(args.output_dir.resolve()/'audio_requirements.json')},ensure_ascii=False))
        return 0 if result['status']=='complete' else 2
    except (EvidenceError,OSError,KeyError,ValueError,TypeError,wave.Error) as exc:
        print(f'Audio review failed: {exc}',file=sys.stderr);return 1

if __name__=='__main__':
    raise SystemExit(main())
