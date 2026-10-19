"""Read-only, allowlisted numeric projection of regression summaries. No model calls."""

import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import time

MAX_BYTES = 8 * 1024 * 1024
DIAGNOSTICS = ('blocked_attempts', 'budget_stops', 'false_routes', 'false_refusals',
               'false_handoffs', 'forbidden_content_effects', 'human_handoffs',
               'justified_handoffs', 'planned_technical_retries', 'repeated_argument_calls', 'invalid_calls', 'routes')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('Invalid numeric metric')
    return value


def read_json(path):
    with path.open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Summary too large')
    return json.loads(raw)


def config(path):
    sources = read_json(path)['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 8:
        raise ValueError('Expected 1..8 metric sources')
    names = set()
    for source in sources:
        name = source['id']
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name) or name in names:
            raise ValueError('Invalid source ID')
        names.add(name)
        if source['role'] not in {'current', 'baseline', 'offline'}:
            raise ValueError('Invalid source role')
        if source.get('latest_live') is True:
            if 'directory' in source or source['role'] != 'current':
                raise ValueError('Invalid live selector')
        elif not re.fullmatch(r'[a-zA-Z0-9_-]+', source.get('directory', '')):
            raise ValueError('Invalid source directory')
    return sources


def summary_path(root, source):
    root = root.resolve()
    if source.get('latest_live'):
        candidates = [p for p in root.glob('live-regression-*')
                      if re.fullmatch(r'live-regression-\d{8}T\d{12}Z', p.name)
                      and (p.is_dir() or p.is_symlink())]
        if not candidates:
            raise FileNotFoundError('No live run directory')
        # A new run without a summary is unavailable, not permission to show an older success.
        path = max(candidates, key=lambda p: p.name) / 'summary.json'
    else:
        path = root / source['directory'] / 'summary.json'
    if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('Source must stay inside artifacts')
    return path


def project(summary):
    """Ignore aggregate and all free text; calculate bounded metrics from checks."""
    if not isinstance(summary, dict) or type(summary.get('live_model')) is not bool:
        raise ValueError('Missing execution classification')
    checks = summary['checks']
    if not isinstance(checks, list) or len(checks) > 10000:
        raise ValueError('Invalid checks')
    seen, values, groups = set(), {}, {}
    modes = summary['modes']
    if not isinstance(modes, list) or not modes or len(set(modes)) != len(modes) or any(m not in {'workflow', 'agent'} for m in modes):
        raise ValueError('Invalid modes')
    for mode in modes:
        values[mode] = Counter({k: 0 for k in (*DIAGNOSTICS, 'attempts', 'passed', 'failed', 'incomplete',
                                             'model_calls', 'unknown_call_count_attempts', 'unknown_usage_attempts',
                                             'unknown_diagnostics_attempts',
                                             'prompt_tokens', 'completion_tokens', 'total_tokens',
                                             'elapsed_seconds_sum', 'elapsed_seconds_count')})
        groups[mode] = {}
    declared = summary['suite']['cases']
    case_ids = {c['id'] for c in declared}
    if not case_ids or len(case_ids) != len(declared):
        raise ValueError('Invalid suite cases')
    for check in checks:
        if not isinstance(check, dict) or check.get('case_id') not in case_ids:
            raise ValueError('Unknown case')
        mode, run = check['mode'], (check['case_id'], check['repetition'])
        if (mode not in values or type(check['repetition']) is not int or check['repetition'] not in {1, 2, 3}
                or type(check['passed']) is not bool or (mode, *run) in seen
                or check.get('state') not in {'started', 'finished'}
                or check['passed'] and check['state'] != 'finished'):
            raise ValueError('Invalid attempt')
        seen.add((mode, *run))
        v = values[mode]
        v['attempts'] += 1
        v['passed'] += check['passed']
        v['failed'] += not check['passed']
        v['incomplete'] += check.get('state') != 'finished'
        groups[mode].setdefault(check['case_id'], []).append(check['passed'])
        measured = check['metrics']
        if not isinstance(measured, dict):
            raise ValueError('Invalid metrics')
        calls = measured.get('model_calls')
        if calls is None:
            v['unknown_call_count_attempts'] += 1
        else:
            v['model_calls'] += number(calls)
        elapsed = measured.get('elapsed_seconds')
        if elapsed is not None:
            v['elapsed_seconds_sum'] += number(elapsed)
            v['elapsed_seconds_count'] += 1
        usage = measured.get('tokens')
        if usage is None:
            v['unknown_usage_attempts'] += calls != 0
        elif not isinstance(usage, dict):
            raise ValueError('Invalid usage')
        else:
            v['unknown_usage_attempts'] += calls != 0 and not all(k in usage for k in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            for field in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                if field in usage:
                    v[field] += number(usage[field])
        diagnostics = check.get('diagnostics', {})
        if not isinstance(diagnostics, dict):
            raise ValueError('Invalid diagnostics')
        missing_diagnostics = set(DIAGNOSTICS) - diagnostics.keys()
        if missing_diagnostics and check['state'] == 'finished':
            raise ValueError('Finished attempt requires complete diagnostics')
        v['unknown_diagnostics_attempts'] += bool(missing_diagnostics)
        for name in DIAGNOSTICS:
            if name in diagnostics:
                v[name] += number(diagnostics[name])
    case_count = len(case_ids)
    if not case_count:
        raise ValueError('Empty suite')
    for mode, v in values.items():
        v['planned_attempts'] = case_count * 3
        if v['attempts'] > v['planned_attempts'] or len(groups[mode]) > case_count:
            raise ValueError('Too many attempts')
        v['pending_attempts'] = v['planned_attempts'] - v['attempts']
        v['complete_case_groups'] = sum(len(g) == 3 for g in groups[mode].values())
        if v['complete_case_groups'] == case_count:
            v['pass_at_3_ratio'] = sum(any(g) for g in groups[mode].values()) / case_count
            v['pass_power_3_ratio'] = sum(all(g) for g in groups[mode].values()) / case_count
    return values


def collect(root, sources):
    lines = ['# All email_router_* values are regression snapshot gauges, not production delivery counters.']
    def emit(name, value, **labels):
        # Labels come only from validated configuration and fixed enums, never reports' free text.
        tag = ','.join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        lines.append(f'email_router_{name}{{{tag}}} {value}')
    emit('exporter_scrape_timestamp_seconds', time.time())
    for source in sources:
        labels = {'source': source['id'], 'role': source['role']}
        try:
            path = summary_path(root, source)
            summary = read_json(path)
            values = project(summary)
            execution = 'live' if summary['live_model'] else 'offline'
            if (source['role'] in {'current', 'baseline'} and execution != 'live'
                    or source['role'] == 'offline' and execution != 'offline'):
                raise ValueError('Wrong source execution class')
            # Build each source atomically: no partial success if any metric is invalid.
            modified = path.stat().st_mtime
            emit('source_loaded', 1, **labels)
            emit('snapshot_modified_timestamp_seconds', modified, **labels)
            for mode, measured in values.items():
                for name, value in sorted(measured.items()):
                    emit('snapshot_' + name, value, **labels, execution=execution, mode=mode)
        except (OSError, ValueError, KeyError, TypeError, OverflowError, AttributeError, RecursionError):
            emit('source_loaded', 0, **labels)
    return '\n'.join(lines) + '\n'


def handler(root, sources):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/metrics':
                body, status = collect(root, sources).encode(), 200
                content_type = 'text/plain; version=0.0.4; charset=utf-8'
            elif self.path == '/healthz':
                body, status, content_type = b'ok\n', 200, 'text/plain'
            else:
                body, status, content_type = b'not found\n', 404, 'text/plain'
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # Never echo URLs, report fragments or exception text.
    return Handler


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9108)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    sources = config(args.config)
    if args.once:
        print(collect(args.artifacts, sources), end='')
    else:
        server = ThreadingHTTPServer((args.host, args.port), handler(args.artifacts, sources))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
