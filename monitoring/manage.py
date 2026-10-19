"""Start/check/stop the isolated local stack. Never reads the model API .env."""

import argparse
import errno
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ['docker', 'compose', '--env-file', str(ROOT / 'monitoring/compose.env'),
           '-f', str(ROOT / 'compose.monitoring.yaml')]


def prepare():
    folder = ROOT / 'monitoring/local'
    if folder.is_symlink():
        raise ValueError('Local state directory must not be a symlink')
    folder.mkdir(mode=0o700, exist_ok=True)
    folder.chmod(0o700)
    password = folder / 'grafana_admin_password'
    if password.is_symlink():
        raise ValueError('Password file must not be a symlink')
    if not password.exists():
        # The containing directory is private; the file must be readable by Grafana's UID.
        fd = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'w') as output:
            output.write(secrets.token_urlsafe(32) + '\n')
    password.chmod(0o644)


def get(url):
    # Ignore proxy environment for localhost health checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return response.read(2 * 1024 * 1024)


def check():
    results = readiness()
    if not all(item['ok'] for item in results.values()):
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1
    print('OK: exporter, live source, Prometheus scrape and Grafana are ready.')
    print('Dashboard: http://localhost:13000/d/email-router-local')
    return 0


def failure_reason(exc):
    """Classify failures without exposing arbitrary exception or response content."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, TimeoutError):
        return 'connection_timeout'
    if isinstance(reason, http.client.HTTPException):
        return 'connection_closed'
    if isinstance(reason, OSError):
        return {errno.ECONNREFUSED: 'connection_refused', errno.ECONNRESET: 'connection_reset',
                errno.ECONNABORTED: 'connection_aborted', errno.EACCES: 'permission_denied',
                errno.EPERM: 'permission_denied', errno.ETIMEDOUT: 'connection_timeout',
                errno.ENETUNREACH: 'network_unreachable', errno.EHOSTUNREACH: 'host_unreachable'}.get(
                    reason.errno, 'connection_error')
    if isinstance(exc, urllib.error.URLError):
        return 'connection_error'
    return 'invalid_response'


def readiness():
    """Check every stage; expose fixed reasons, never HTTP bodies or exception text."""
    results = {}
    query = 'http://127.0.0.1:19090/api/v1/query?query=up%7Bjob%3D%22email-router-exporter%22%7D'
    def scraped(raw):
        values = json.loads(raw)['data']['result']
        return bool(values) and all(float(row['value'][1]) == 1 for row in values)
    probes = [
        ('exporter_http', 'http://127.0.0.1:19108/healthz', lambda body: body.strip() == b'ok', 'unexpected_health'),
        ('current_source', 'http://127.0.0.1:19108/metrics',
         lambda body: 'email_router_source_loaded{role="current",source="current"} 1' in body.decode(), 'source_not_loaded'),
        ('prometheus_http', 'http://127.0.0.1:19090/-/ready', lambda body: True, 'not_ready'),
        ('grafana_database', 'http://127.0.0.1:13000/api/health', lambda body: json.loads(body).get('database') == 'ok', 'database_not_ready'),
        ('prometheus_scrape', query, scraped, 'exporter_target_not_up'),
    ]
    for component, url, predicate, reason in probes:
        try:
            ok = bool(predicate(get(url)))
            results[component] = {'ok': ok, 'reason': 'ok' if ok else reason}
        except urllib.error.HTTPError as exc:
            results[component] = {'ok': False, 'reason': 'http_error', 'http_status': exc.code}
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
            results[component] = {'ok': False, 'reason': failure_reason(exc)}
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('up', 'down', 'status', 'check', 'prepare'))
    args = parser.parse_args(argv)
    try:
        if args.command in {'prepare', 'up'}:
            prepare()
        if args.command == 'prepare':
            print('Local Grafana password prepared; its value is not printed.')
            return 0
        if args.command == 'check':
            return check()
        if args.command == 'down':
            return subprocess.call(COMPOSE + ['down'], cwd=ROOT)  # Preserve named data volumes.
        if args.command == 'status':
            return subprocess.call(COMPOSE + ['ps'], cwd=ROOT)
        subprocess.run(COMPOSE + ['config', '--quiet'], cwd=ROOT, check=True)
        subprocess.run(COMPOSE + ['up', '-d', '--build', '--wait', '--wait-timeout', '120'], cwd=ROOT, check=True)
        for _ in range(12):
            results = readiness()
            if all(item['ok'] for item in results.values()):
                print('OK: exporter, live source, Prometheus scrape and Grafana are ready.')
                print('Dashboard: http://localhost:13000/d/email-router-local')
                return 0
            time.sleep(5)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print('Containers started; readiness is not confirmed. Run monitoring/manage.py check again. Existing data was preserved.', file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print('Docker command failed. Check Docker Desktop and access to its socket. Existing data was preserved.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError):
        print('Local monitoring is unavailable; check Docker Desktop, ports and metric sources. No secret values are printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
