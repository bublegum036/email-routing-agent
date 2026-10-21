"""Independent raw extraction oracle and live benchmark; no routing actions."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from .__main__ import ROOT
from .llm import ChatModel, ModelError, load_env
from .runner import BUSINESS, EXTRACT
from .schemas import ISSUE_SCHEMA, validate


def assess_extraction(expected, proposals, message, history=None):
    """Score raw output, before controller corrections, without model-based judging.

    Category and reference are matched jointly; issue order and subject wording
    are immaterial. Empty reference is meaningful, never a wildcard.
    """
    problems = []
    if Counter(p['category'] for p in proposals) != Counter(p['category'] for p in expected):
        problems.append('wrong_extracted_categories_or_count')
    remaining = list(proposals)
    for wanted in expected:
        match = next((p for p in remaining if p['category'] == wanted['category']
                      and p['reference_id'] in wanted['references']
                      and (not wanted.get('quote_any') or any(
                          part in p['quote'] for part in wanted['quote_any']))), None)
        if match is None:
            problems.append('wrong_extracted_reference_or_issue_link')
        else:
            remaining.remove(match)
    observed = message['text']
    if history and history.get('status') == 'found':
        observed += '\n' + '\n'.join(m['text'] for m in history['data']['messages'])
    for p in proposals:
        if not p['quote'] or p['quote'] not in message['text']:
            problems.append('ungrounded_extracted_quote')
        if not p['subject'].strip():
            problems.append('empty_extracted_subject')
        if p['company_hint'] and p['company_hint'].casefold() not in observed.casefold():
            problems.append('ungrounded_extracted_company')
        if p['reference_id'] and (not re.fullmatch(r'[DOT]-[0-9]+', p['reference_id'])
                                  or p['reference_id'] not in re.findall(r'\b[DOT]-[0-9]+\b', observed)):
            problems.append('ungrounded_extracted_reference')
    return sorted(set(problems))


def assess_report_extraction(case, report):
    if 'extraction' not in case:
        return []
    calls = [c for e in report['events'] if e['kind'] == 'model_response'
             for c in e['payload']['message'].get('tool_calls', [])
             if c.get('function', {}).get('name') == 'register_issues']
    if len(calls) != 1:
        return ['missing_or_repeated_extraction']
    try:
        args = json.loads(calls[0]['function']['arguments'])
        validate(args, ISSUE_SCHEMA)
    except (ValueError, KeyError, TypeError):
        return ['invalid_raw_extraction']
    # Use only history actually supplied to the extractor, not later planner reads.
    requests = [e for e in report['events'] if e['kind'] == 'model_request'
                and e['payload']['tools'][0]['function']['name'] == 'register_issues']
    history = json.loads(requests[0]['payload']['messages'][-1]['content']).get('history') if requests else None
    expected = [dict(p, references=p.get('references_by_mode', {}).get(
        report['run']['mode'], p['references'])) for p in case['extraction']]
    return assess_extraction(expected, args['issues'], report['message'], history)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=str, default=str(ROOT / 'data/extraction-challenge-v1.json'))
    parser.add_argument('--model', required=True)
    args = parser.parse_args(argv)
    from .shipping_change import synthetic_seed
    from .evaluate import PacedModel
    suite = synthetic_seed(json.loads(Path(args.suite).read_text()))
    if not suite.get('cases') or len({c['id'] for c in suite['cases']}) != len(suite['cases']):
        raise ValueError('Expected unique nonempty challenge cases')
    for case in suite['cases']:
        if not re.fullmatch(r'[a-z0-9-]+', case['id']):
            raise ValueError('Unsafe case ID')
        for expected in case['expected']:
            if (not expected.get('references') or expected['category'] not in
                    ISSUE_SCHEMA['properties']['issues']['items']['properties']['category']['enum']):
                raise ValueError('Invalid extraction expectation')
            if not all(part in case['message']['text'] for part in expected.get('quote_any', [])):
                raise ValueError('Expected quote anchor missing from message')
    load_env()
    previous = os.environ.get('LLM_MODEL')
    try:
        os.environ['LLM_MODEL'] = args.model
        model = PacedModel(ChatModel(), 5)
    finally:
        if previous is None:
            os.environ.pop('LLM_MODEL', None)
        else:
            os.environ['LLM_MODEL'] = previous
    directory = ROOT / 'artifacts' / ('live-extraction-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'suite.json').write_text(json.dumps(suite, ensure_ascii=False, indent=2))
    summary = {'benchmark': 'raw_extraction_only', 'live_model': True, 'model': model.model,
               'provider': model.provider, 'generation_config': model.generation_config,
               'suite_hash': hashlib.sha256(Path(args.suite).read_bytes()).hexdigest(),
               'prompt_hash': hashlib.sha256((BUSINESS + EXTRACT).encode()).hexdigest(),
               'schema_hash': hashlib.sha256(json.dumps(ISSUE_SCHEMA, sort_keys=True).encode()).hexdigest(),
               'checks': [], 'pending': len(suite['cases']), 'stopped': False}
    def save():
        (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    save()
    for case in suite['cases']:
        history = case.get('history')
        messages = [{'role': 'system', 'content': BUSINESS + EXTRACT},
                    {'role': 'user', 'content': json.dumps({'message': case['message'], 'history': history}, ensure_ascii=False)}]
        tools = [{'type': 'function', 'function': {'name': 'register_issues',
                  'description': 'Register all issues before any actions', 'parameters': ISSUE_SCHEMA}}]
        trace = {'request': {'messages': messages, 'tools': tools}}
        # Persist the attempt before contacting the provider; gold labels are separate.
        path = directory / (case['id'] + '.json')
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2))
        try:
            answer = model.complete(messages, tools, timeout=30)
            trace['response'] = answer
            calls = answer['message'].get('tool_calls', [])
            if len(calls) != 1 or calls[0]['function']['name'] != 'register_issues':
                raise ValueError('Expected one register_issues call')
            result = json.loads(calls[0]['function']['arguments'])
            validate(result, ISSUE_SCHEMA)
            problems = assess_extraction(case['expected'], result['issues'], case['message'], history)
        except ModelError as exc:
            trace['error'] = str(exc)
            problems = ['model_error']
            summary['stopped'] = True
        except (ValueError, KeyError, TypeError):
            problems = ['invalid_raw_extraction']
        path.write_text(json.dumps(trace, ensure_ascii=False, indent=2))
        summary['checks'].append({'id': case['id'], 'passed': not problems, 'problems': problems, 'trace': path.name})
        summary['pending'] -= 1
        save()
        print(json.dumps(summary['checks'][-1]), flush=True)
        if summary['stopped']:
            break
    print('Результаты: ' + str(directory), flush=True)
    return int(summary['stopped'] or any(not c['passed'] for c in summary['checks']))


if __name__ == '__main__':
    raise SystemExit(main())
