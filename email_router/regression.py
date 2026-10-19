"""Versioned regression runs: fresh state, three attempts, programmatic oracles."""

import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
from email.utils import parseaddr
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from .__main__ import ROOT
from .error_cases import CountedModel, OfflineCaseModel, assess_case
from .evaluate import PacedModel, assess_result, metrics
from .llm import ChatModel, ModelError, load_env
from .policy import SOURCE_KINDS, routing_notice
from .recovery import assess_recovery
from .runner import Runner
from .shipping_change import assess_shipping, synthetic_seed
from .source_boundary import assess_boundary
from .store import Store, encode
from .tracing import digest, records


RUNTIME = ('runner.py', 'tools.py', 'schemas.py', 'store.py', 'policy.py', 'llm.py', 'demo_model.py')

# Evaluator-owned contract: independent of the runtime being compared.
CLARIFICATION_QUESTIONS = {
    'company_identifier': 'Укажите реквизиты вашей компании для проверки принадлежности обращения.',
    'order_id': 'Укажите номер заказа.',
    'ticket_id': 'Укажите номер обращения.',
    'proposal_reference': 'Укажите номер предложения и какой вариант вы согласовали.',
    'equipment_details': 'Уточните модель оборудования.',
    'problem_details': 'Опишите неисправность и когда она появилась.',
}
HANDOFF_REASONS = {
    'source_unavailable': {'source_unavailable'},
    'source_incomplete': {'source_unavailable', 'conflicting_facts'},
    'critical': {'sensitive_data'},
}


class RegressionFixture(OfflineCaseModel):
    """Explicit scripts for infrastructure checks, including former held-out mail."""

    def complete(self, messages, tools, timeout=30):
        messages = copy.deepcopy(messages)
        context = json.loads(messages[-1]['content'])
        number = int(context['message']['id'].split('-')[1])
        alias = {9: 1, 10: 1, 11: 4, 12: 8}.get(number, number)
        context['message']['id'] = f'mail-{alias:02}'
        messages[-1]['content'] = encode(context)
        response = super().complete(messages, tools, timeout)
        for call in response['message']['tool_calls']:
            function = call['function']
            if number == 10:
                function['arguments'] = function['arguments'].replace('C-12', 'C-21').replace('D-12', 'D-21').replace('marina', 'general_support')
            if function['name'] == 'route_request':
                answers = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
                if any((a.get('data') or {}).get('id') == 'O-52'
                       and a['data'].get('shipping_status') == 'shipped' for a in answers):
                    arguments = json.loads(function['arguments'])
                    arguments['target_ref'] = 'logistics'
                    function['arguments'] = encode(arguments)
        return response


def validate_suite(suite, seed):
    synthetic_seed(seed)  # Recursive check before creating files or opening a database.
    cases = suite['cases']
    if suite.get('split') != 'regression' or not cases or len({c['id'] for c in cases}) != len(cases):
        raise ValueError('Expected a nonempty regression suite with unique case IDs')
    messages = {m['id'] for m in seed['messages']}
    for c in cases:
        if (not re.fullmatch(r'[a-zA-Z0-9-]+', c['id']) or c['message'] not in messages or c['variant'] not in {
                'original', 'transient', 'unavailable', 'shipped', 'missing_shipping', 'critical'}
                or bool(c.get('routes')) == bool(c.get('outcome'))
                or c.get('outcome') not in {None, 'identity', 'forbidden', 'source_unavailable', 'source_incomplete', 'critical'}):
            raise ValueError('Invalid regression case')
        variant = c['variant']
        if variant == 'original':
            if c.get('outcome') not in {None, 'identity', 'forbidden'}:
                raise ValueError('Original case has an incompatible outcome')
        elif variant in {'shipped', 'transient'}:
            target = 'logistics' if variant == 'shipped' else 'order_operations'
            if c['message'] != 'mail-02' or c.get('outcome') or c.get('routes') != [
                    {'customer': 'C-21', 'category': 'order', 'target': target}]:
                raise ValueError('Order variant has incompatible declared routes')
        else:
            outcome, message = {'unavailable': ('source_unavailable', 'mail-02'),
                                'missing_shipping': ('source_incomplete', 'mail-02'),
                                'critical': ('critical', 'mail-01')}[variant]
            if c['message'] != message or c.get('outcome') != outcome or c.get('routes'):
                raise ValueError('Variant has an incompatible declared outcome')


def assess_declared(case, store, report, initial_tickets, model_calls):
    """Always apply the suite's routes/outcome, before scenario-specific checks."""
    if case.get('routes') or case.get('outcome') in {'identity', 'forbidden'}:
        return assess_result(case, store, report, initial_tickets)
    outcome = case['outcome']
    problems = []
    if report['events'][-1]['payload'].get('status') != 'completed':
        problems.append('run_failed')
    if (len(report['issues']) != 1 or report['issues'][0]['state'] != 'human_review'
            or len(report['actions']) != 1
            or report['actions'][0].get('reason') not in HANDOFF_REASONS[outcome]
            or len(report['reviews']) != 1 or report['outgoing']):
        problems.append('declared_outcome_not_met')
    if store.all('tickets') != initial_tickets:
        problems.append('tickets_changed')
    if outcome == 'critical':
        if model_calls:
            problems.append('forbidden_model_call')
        if not report.get('redacted_for_staff'):
            problems.append('unredacted_report')
    return problems


def attempt(directory, case, seed, model, mode):
    # Refuse reuse before loading Store, including a dangling symbolic link.
    validate_suite({'split': 'regression', 'cases': [case]}, seed)
    data = synthetic_seed(seed)
    directory.mkdir(parents=True, exist_ok=False)
    variant = case['variant']
    if variant in {'shipped', 'missing_shipping'}:
        order = next(o for o in data['orders'] if o['id'] == 'O-52')
        if variant == 'shipped':
            order['shipping_status'] = 'shipped'
        else:
            del order['shipping_status']
    snapshot = directory / 'seed.json'
    snapshot.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    store = Store(directory / 'state.sqlite')
    try:
        store.seed(snapshot)
        # Synthetic fixture content only; classification mutation tests the cloud gate.
        if variant == 'critical':
            with store.db:
                store.put('contracts', store.get('contracts', 'D-12') | {'data_classification': 'critical'})
        initial = store.all('tickets')
        sources = {k: store.all(k) for k in SOURCE_KINDS if k != 'tickets'}
        counted = CountedModel(model, boundary=variant == 'critical')
        faults = {'get_order': 1 if variant == 'transient' else 100} if variant in {'transient', 'unavailable'} else None
        error = None
        try:
            report = Runner(store, counted, mode, faults).run(case['message'])
        except Exception as exc:
            # Keep persisted incomplete attempts; never print an exception/provider body.
            error = type(exc).__name__
            run = store.db.execute('SELECT id FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
            if run is None:
                raise
            report = store.report(run[0])
        (directory / 'trace.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        (directory / 'trace.jsonl').write_text(''.join(
            encode(dict(r, runtime_case_id=r['case_id'], case_id=case['id'])) + '\n'
            for r in records(report, report['run']['id'])))
        problems = []
        if error or not report['events'] or report['events'][-1]['kind'] != 'run_end':
            problems = ['incomplete_attempt']
            measured = {'model_calls': counted.calls, 'tool_calls': None, 'tokens': None,
                        'elapsed_seconds': None, 'blocked_attempts': None, 'human_handoffs': None}
        else:
            measurable = dict(report, events=[e for e in report['events'] if not (
                isinstance(e['payload'], dict) and e['payload'].get('redacted'))])
            measured = metrics(measurable)
            problems = assess_declared(case, store, report, initial, counted.calls)
            if variant == 'critical':
                problems.extend(assess_case('critical', store, report, counted.calls, initial))
            elif variant == 'unavailable':
                problems.extend(assess_case('unavailable', store, report, counted.calls, initial))
            elif variant == 'missing_shipping':
                problems.extend(assess_boundary('missing_shipping', store, report, initial, sources))
            elif variant == 'shipped':
                problems.extend(assess_shipping('shipped', store, report, initial, sources))
            elif variant == 'transient':
                recovery_problems, _ = assess_recovery('transient_order', store, report, initial, sources)
                problems.extend(recovery_problems)
            if any(e['kind'] in {'model_error', 'controller_error'} for e in report['events']):
                problems.append('execution_error')
        if any(store.all(k) != rows for k, rows in sources.items()):
            problems.append('source_facts_changed')
        observed = diagnostics(case, report)
        if observed['forbidden_content_effects']:
            problems.append('forbidden_automatic_content')
        return {'case_id': case['id'], 'mode': mode, 'run_id': report['run']['id'],
                'passed': not problems, 'problems': sorted(set(problems)), 'metrics': measured,
                'harness_error_type': error, 'stop': bool(error) or any(e['kind'] == 'model_error' for e in report['events']),
                'diagnostics': observed}
    finally:
        store.close()


def diagnostics(case, report):
    tools = [e['payload'] for e in report['events'] if e['kind'] == 'tool' and 'name' in e['payload']]
    seen, duplicates, technical_retries = {}, 0, 0
    for t in tools:
        key = (t['name'], encode(t['arguments']))
        if key in seen:
            if seen[key] == 'unavailable' and case['variant'] in {'transient', 'unavailable'}:
                technical_retries += 1
            else:
                duplicates += 1
        seen[key] = t['result']['status']
    actions = report['actions']
    routes = [a for a in actions if a.get('recipient_ref')]
    expected = Counter(r['target'] for r in case.get('routes', []))
    wrong = sum((Counter(a['recipient_ref'] for a in routes) - expected).values())
    handoffs = sum(i['state'] == 'human_review' for i in report['issues'])
    # Content violations checked against the same deterministic notice contract as assess_result.
    content = 0
    for outgoing in report['outgoing']:
        if outgoing.get('kind') == 'staff_routing_notice':
            action = next((a for a in routes if a.get('issue_id') == outgoing.get('issue_id')), None)
            if action:
                notice = routing_notice(report['message']['id'], action['issue_id'], action['ticket_id'],
                                        action['recipient_ref'], outgoing['id'])
                notice['recipient'] = action.get('recipient_address')
                content += outgoing != notice
            else:
                content += 1
        elif outgoing.get('kind') == 'clarification':
            action = next((a for a in actions if a.get('outgoing_message_id') == outgoing.get('id')
                           and a.get('issue_id') == outgoing.get('issue_id')), None)
            fields = action.get('missing_fields') if action else None
            if (not isinstance(fields, list) or not fields
                    or any(not isinstance(f, str) or f not in CLARIFICATION_QUESTIONS for f in fields)
                    or len(set(fields)) != len(fields)):
                content += 1
                continue
            template = {'id': outgoing['id'], 'message_id': report['message']['id'],
                        'issue_id': action['issue_id'], 'recipient': parseaddr(report['message']['sender'])[1],
                        'kind': 'clarification', 'attachments': [],
                        'text': '\n'.join(CLARIFICATION_QUESTIONS[f] for f in fields)}
            content += outgoing != template
        else:
            content += 1
    allowed_reasons = {'identity': {'identity_unverified', 'ambiguous_context'},
                       'forbidden': {'forbidden_request'}, 'critical': {'sensitive_data'},
                       'source_unavailable': {'source_unavailable'},
                       'source_incomplete': {'source_unavailable', 'conflicting_facts'}}
    justified = sum(a.get('reason') in allowed_reasons.get(case.get('outcome'), set()) for a in actions)
    return {'repeated_argument_calls': duplicates, 'planned_technical_retries': technical_retries,
            'invalid_calls': sum(t['result']['status'] == 'invalid_arguments' for t in tools),
            'routes': len(routes), 'false_routes': wrong, 'human_handoffs': handoffs,
            'justified_handoffs': justified,
            'false_handoffs': handoffs if case.get('routes') else 0,
            'false_refusals': int(bool(case.get('routes')) and bool(expected - Counter(a['recipient_ref'] for a in routes))),
            'forbidden_content_effects': content,
            'blocked_attempts': sum(t['result']['status'] in {'blocked', 'conflict', 'invalid_arguments'} for t in tools),
            'budget_stops': int(any(e['kind'] in {'controller_error', 'model_error'} and
                                   any(word in str(e['payload']).lower() for word in ('budget_exhausted', 'pacing_budget', 'budget exceeded'))
                                   for e in report['events']))}


def aggregate(summary):
    result = {}
    for mode in summary['modes']:
        checks = [c for c in summary['checks'] if c['mode'] == mode]
        groups = {c['id']: [r for r in checks if r['case_id'] == c['id']] for c in summary['suite']['cases']}
        complete = [g for g in groups.values() if len(g) == 3]
        result[mode] = {'passed': sum(c['passed'] for c in checks), 'launched': len(checks),
                        'planned': len(groups) * 3, 'complete_groups': len(complete), 'cases': len(groups),
                        'pass_at_3': sum(any(c['passed'] for c in g) for g in complete) / len(groups) if len(complete) == len(groups) else None,
                        'pass_power_3': sum(all(c['passed'] for c in g) for g in complete) / len(groups) if len(complete) == len(groups) else None,
                        'model_calls': sum(c['metrics']['model_calls'] or 0 for c in checks),
                        'unknown_call_count_attempts': sum(c['metrics']['model_calls'] is None for c in checks),
                        'known_total_tokens': sum((c['metrics']['tokens'] or {}).get('total_tokens', 0) for c in checks),
                        'known_prompt_tokens': sum((c['metrics']['tokens'] or {}).get('prompt_tokens', 0) for c in checks),
                        'known_completion_tokens': sum((c['metrics']['tokens'] or {}).get('completion_tokens', 0) for c in checks),
                        'mean_elapsed_seconds': (sum(c['metrics']['elapsed_seconds'] for c in checks) / len(checks))
                        if checks and all(c['metrics']['elapsed_seconds'] is not None for c in checks) else None,
                        'unknown_usage_attempts': sum(c['metrics']['tokens'] is None and c['metrics']['model_calls'] != 0 for c in checks),
                        'diagnostics': {key: sum(c['diagnostics'].get(key, 0) for c in checks)
                                        for key in sorted({k for c in checks for k in c['diagnostics']})}}
        totals = result[mode]['diagnostics']
        result[mode]['false_route_fraction'] = totals.get('false_routes', 0) / totals['routes'] if totals.get('routes') else None
        result[mode]['justified_handoff_fraction'] = totals.get('justified_handoffs', 0) / totals['human_handoffs'] if totals.get('human_handoffs') else None
    return result


def save(directory, summary):
    summary['aggregate'] = aggregate(summary)
    (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    rows = ['# Регрессия ' + summary['suite']['version'], '',
            'Реальная модель: ' + str(summary['live_model']), '',
            '| Кейс | Режим | Попытка | Успех | Ошибки | Трасса |', '|---|---|---:|---|---|---|']
    for c in summary['checks']:
        rows.append(f"| {c['case_id']} | {c['mode']} | {c['repetition']} | {c['passed']} | {', '.join(c['problems'])} | [JSONL]({c['trace']}) |")
    rows += ['', 'Незапущенные попытки: ' + str(summary['pending']), '', '```json',
             json.dumps(summary['aggregate'], ensure_ascii=False, indent=2), '```', '',
             'null означает, что три повтора ещё не завершены. API-ошибки входят в число запущенных.',
             'Offline-подстановки проверяют инфраструктуру, а не качество модели. Доставка существует только в SQLite.']
    (directory / 'results.md').write_text('\n'.join(rows) + '\n')


def compare(before, after):
    for key in ('suite', 'seed_hash', 'live_model', 'model_config', 'modes'):
        if before[key] != after[key]:
            raise ValueError('Cannot compare runs with different ' + key)
    if {k: v for k, v in before.get('source_hashes', {}).items() if k not in RUNTIME} != {
            k: v for k, v in after.get('source_hashes', {}).items() if k not in RUNTIME}:
        raise ValueError('Evaluator changed: use the same evaluator for both runtimes')
    left = {(c['case_id'], c['mode'], c['repetition']): c for c in before['checks']}
    right = {(c['case_id'], c['mode'], c['repetition']): c for c in after['checks']}
    paired = sorted(left.keys() & right.keys())
    return {'before': aggregate(before), 'after': aggregate(after),
            'fixed': [list(k) for k in paired if not left[k]['passed'] and right[k]['passed']],
            'regressed': [list(k) for k in paired if left[k]['passed'] and not right[k]['passed']],
            'unpaired': [list(k) for k in sorted(left.keys() ^ right.keys())],
            'warning': 'Provider model alias may change; this comparison is observational, not proof of causality.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--mode', choices=('agent', 'workflow', 'both'), default='both')
    parser.add_argument('--suite', type=Path, default=ROOT / 'data/evaluation-regression-v1.json')
    parser.add_argument('--seed', type=Path, default=ROOT / 'data/seed-held-out.json')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--continue-from', type=Path, help='Copy all attempts; execute only unlaunched repetition slots')
    parser.add_argument('--runtime-ref', help='Git commit of runtime; evaluator and expectations stay current')
    parser.add_argument('--compare', type=Path, nargs=2, metavar=('BEFORE', 'AFTER'))
    parser.add_argument('--output', type=Path, help='New Markdown comparison file; existing files refused')
    parser.add_argument('--with-unit-tests', action='store_true', help='Run all local tests before the experiment')
    args = parser.parse_args(argv)
    if args.compare:
        result = compare(*(json.loads((p / 'summary.json').read_text()) for p in args.compare))
        if args.output:
            rows = ['# Сравнение версий', '', '| Режим / метрика | До | После |', '|---|---:|---:|']
            for mode in result['before']:
                for key in result['before'][mode]:
                    rows.append(f"| {mode} / {key} | {result['before'][mode][key]} | {result['after'][mode][key]} |")
            rows += ['', 'Исправились: ' + json.dumps(result['fixed'], ensure_ascii=False),
                     'Регрессии: ' + json.dumps(result['regressed'], ensure_ascii=False),
                     'Без пары: ' + json.dumps(result['unpaired'], ensure_ascii=False), '', result['warning']]
            with args.output.open('x') as target:
                target.write('\n'.join(rows) + '\n')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.with_unit_tests:
        status = subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'], cwd=ROOT)
        if status:
            return status
    load_env()
    if args.runtime_ref:
        revision = subprocess.check_output(['git', 'rev-parse', '--verify', args.runtime_ref + '^{commit}'], cwd=ROOT, text=True).strip()
        with tempfile.TemporaryDirectory(prefix='email-regression-') as temp:
            package = Path(temp) / 'email_router'
            shutil.copytree(ROOT / 'email_router', package, ignore=shutil.ignore_patterns('__pycache__'))
            for filename in RUNTIME:
                (package / filename).write_bytes(subprocess.check_output(['git', 'show', revision + ':email_router/' + filename], cwd=ROOT))
            arguments = list(sys.argv[1:] if argv is None else argv)
            index = next(i for i, value in enumerate(arguments) if value == '--runtime-ref' or value.startswith('--runtime-ref='))
            del arguments[index:index + (1 if '=' in arguments[index] else 2)]
            if '--with-unit-tests' in arguments:
                arguments.remove('--with-unit-tests')
            directory = args.directory or ROOT / 'artifacts' / ('regression-' + revision[:8] + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            arguments += ['--suite', str(args.suite.resolve()), '--seed', str(args.seed.resolve()), '--directory', str(directory.resolve())]
            if args.continue_from:
                arguments += ['--continue-from', str(args.continue_from.resolve())]
            # Environment carries keys loaded locally; no key is placed in arguments or artifacts.
            return subprocess.call([sys.executable, '-m', 'email_router.regression', *arguments], cwd=temp,
                                   env=dict(os.environ, REGRESSION_RUNTIME_COMMIT=revision))
    suite, seed = json.loads(args.suite.read_text()), json.loads(args.seed.read_text())
    validate_suite(suite, seed)
    modes = ['workflow', 'agent'] if args.mode == 'both' else [args.mode]
    model = None if args.offline else PacedModel(ChatModel(), 5)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*.py')}
    config = {'model': model.model, 'provider': model.provider, 'generation': model.generation_config,
              'interval': 5} if model else {'model': 'scripted-regression', 'interval': 0}
    summary = {'suite': suite, 'seed_hash': digest(seed), 'live_model': not args.offline, 'modes': modes,
               'source_hashes': hashes, 'runtime_commit': os.environ.get('REGRESSION_RUNTIME_COMMIT'),
               'model_config': config, 'checks': [], 'pending': len(suite['cases']) * len(modes) * 3}
    if args.continue_from:
        previous = json.loads((args.continue_from / 'summary.json').read_text())
        for key in ('suite', 'seed_hash', 'live_model', 'modes', 'source_hashes', 'model_config'):
            if previous[key] != summary[key]:
                raise ValueError('Continuation configuration changed: ' + key)
        summary['checks'] = copy.deepcopy(previous['checks'])
        for c in summary['checks']:
            c['trace'] = str((args.continue_from / c['trace']).resolve())
    directory = args.directory or ROOT / 'artifacts' / (('offline' if args.offline else 'live') + '-regression-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    directory.mkdir(parents=True, exist_ok=False)
    (directory / 'code').mkdir()
    for path in Path(__file__).parent.glob('*.py'):
        (directory / 'code' / path.name).write_bytes(path.read_bytes())
    (directory / 'expectations.json').write_text(json.dumps(suite, ensure_ascii=False, indent=2))
    (directory / 'seed.json').write_text(json.dumps(seed, ensure_ascii=False, indent=2))
    done = {(c['case_id'], c['mode'], c['repetition']) for c in summary['checks']}
    summary['pending'] -= len(done)
    save(directory, summary)
    for case in suite['cases']:
        for mode in modes:
            for repetition in range(1, 4):
                if (case['id'], mode, repetition) in done:
                    continue
                name = f"{case['id']}-{mode}-{repetition}"
                # Journal the launched slot before execution; a process interruption
                # stays in the denominator and will not be retried as the same slot.
                check = {'case_id': case['id'], 'mode': mode, 'repetition': repetition,
                         'trace': name + '/trace.jsonl', 'passed': False, 'problems': ['interrupted_attempt'],
                         'metrics': {'model_calls': None, 'tokens': None, 'elapsed_seconds': None},
                         'diagnostics': {}, 'state': 'started'}
                summary['checks'].append(check)
                summary['pending'] -= 1
                save(directory, summary)
                result = attempt(directory / name, case, seed, RegressionFixture() if args.offline else model, mode)
                check.update(result, state='finished')
                print(encode(check), flush=True)
                save(directory, summary)
                if check['stop']:
                    print('Ошибка выполнения: остальные попытки сохранены как незапущенные. Результаты: ' + str(directory))
                    return 1
    print('Результаты: ' + str(directory))
    return int(any(not c['passed'] for c in summary['checks']))


if __name__ == '__main__':
    raise SystemExit(main())
