"""Compare three-attempt workflow and agent regressions without invoking a model."""

import argparse
from collections import Counter
import json
from pathlib import Path

from email_router.regression import aggregate


def verify(summary, mode):
    if summary['modes'] != [mode] or summary.get('live_model') is not True:
        raise ValueError('Expected a live single-mode regression for ' + mode)
    expected = {(case['id'], repetition) for case in summary['suite']['cases'] for repetition in (1, 2, 3)}
    slots = [(c['case_id'], c['repetition']) for c in summary['checks']]
    if summary['pending'] != 0 or set(slots) != expected or len(slots) != len(expected):
        raise ValueError('Complete all three attempt slots per case before comparing modes')
    if any(c['mode'] != mode for c in summary['checks']):
        raise ValueError('Mixed modes in checks')
    ids = [c.get('run_id') for c in summary['checks'] if c.get('run_id')]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate run_id')


def compare_modes(workflow, agent):
    verify(workflow, 'workflow')
    verify(agent, 'agent')
    for key in ('suite', 'seed_hash', 'source_hashes', 'model_config'):
        if workflow[key] != agent[key]:
            raise ValueError('Mode comparison requires the same ' + key)
    def actual_models(summary):
        return {m for c in summary['checks'] for m in c['metrics'].get('actual_models', [])}
    workflow_models, agent_models = actual_models(workflow), actual_models(agent)
    if len(workflow_models) != 1 or workflow_models != agent_models:
        raise ValueError('Mode comparison requires one identical actual provider model across both runs')
    result = {'live_model': True, 'workflow': aggregate(workflow)['workflow'],
              'agent': aggregate(agent)['agent'], 'actual_models': sorted(agent_models), 'cases': []}
    for case in workflow['suite']['cases']:
        row = {'case_id': case['id']}
        for mode, summary in (('workflow', workflow), ('agent', agent)):
            subset = [c for c in summary['checks'] if c['case_id'] == case['id']]
            row[mode] = {'passed': sum(c['passed'] for c in subset), 'launched': len(subset),
                         'model_calls': sum(c['metrics']['model_calls'] or 0 for c in subset),
                         'unknown_call_count_attempts': sum(c['metrics']['model_calls'] is None for c in subset),
                         'known_total_tokens': sum((c['metrics']['tokens'] or {}).get('total_tokens', 0) for c in subset),
                         'problems': dict(Counter(p for c in subset for p in c['problems'])),
                         'traces': [c['trace'] for c in subset]}
        result['cases'].append(row)
    result['note'] = 'All launched slots, including failed/interrupted attempts, remain in the denominator. Model alias may change; elapsed time includes pacing.'
    return result


def markdown(result):
    rows = ['# Gemini: workflow и agent', '',
            f"{len(result['cases'])} кейсов × 3 попытки в каждом режиме.", '',
            '| Показатель | Workflow | Agent |', '|---|---:|---:|']
    labels = {'passed': 'Правильные попытки', 'launched': 'Все запущенные',
              'pass_at_3': 'pass@3', 'pass_power_3': 'pass^3', 'model_calls': 'Известные вызовы модели',
              'unknown_call_count_attempts': 'Попытки с неизвестным числом вызовов',
              'known_prompt_tokens': 'Известные входные токены', 'known_completion_tokens': 'Известные выходные токены',
              'known_total_tokens': 'Известная сумма токенов', 'unknown_usage_attempts': 'Попытки с неизвестным usage',
              'mean_elapsed_seconds': 'Среднее время с паузами, с'}
    for key, label in labels.items():
        def formatted(mode):
            value = result[mode].get(key)
            if value is None:
                return 'н/д'
            if key in {'pass_at_3', 'pass_power_3'}:
                return f'{value:.2%}'
            if key == 'mean_elapsed_seconds':
                return f'{value:.2f}'
            return str(value)
        rows.append(f"| {label} | {formatted('workflow')} | {formatted('agent')} |")
    diagnostics = sorted(set(result['workflow']['diagnostics']) | set(result['agent']['diagnostics']))
    for key in diagnostics:
        rows.append(f"| {key} | {result['workflow']['diagnostics'].get(key, 0)} | {result['agent']['diagnostics'].get(key, 0)} |")
    rows += ['', '| Кейс | Workflow: успех / попытки | Agent: успех / попытки | Вызовы W / A | Известные токены W / A |',
             '|---|---:|---:|---:|---:|']
    for row in result['cases']:
        w, a = row['workflow'], row['agent']
        rows.append(f"| {row['case_id']} | {w['passed']}/{w['launched']} | {a['passed']}/{a['launched']} | {w['model_calls']} / {a['model_calls']} | {w['known_total_tokens']} / {a['known_total_tokens']} |")
    rows += ['', 'Подробные причины и ссылки на все три трассы каждого кейса — в JSON рядом с таблицей.',
             'Для защиты разобрать mail-09 (история), mail-07 (два вопроса) и order-missing-shipping (неполный источник).',
             'Доставка существует в SQLite; это не подтверждение доставки по SMTP.', result['note']]
    return '\n'.join(rows) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow', type=Path, required=True)
    parser.add_argument('--agent', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New .md file; JSON saved beside it')
    args = parser.parse_args(argv)
    output, machine = args.output, args.output.with_suffix('.json')
    if output.suffix != '.md':
        raise ValueError('--output must end in .md')
    for path in (output, machine):
        if path.exists() or path.is_symlink():
            raise FileExistsError('Report already exists; use a new name')
    result = compare_modes(json.loads((args.workflow / 'summary.json').read_text()),
                           json.loads((args.agent / 'summary.json').read_text()))
    result['sources'] = {'workflow': str(args.workflow), 'agent': str(args.agent)}
    with machine.open('x') as target:
        json.dump(result, target, ensure_ascii=False, indent=2)
    with output.open('x') as target:
        target.write(markdown(result))
    print('Saved: ' + str(output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
