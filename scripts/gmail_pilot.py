"""Local Gmail transport pilot. Incoming mail is quarantined, never trusted by its headers."""

import argparse
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
import imaplib
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl

from email_router.llm import ChatModel, load_env
from email_router.policy import routing_notice
from email_router.runner import Runner
from email_router.shipping_change import synthetic_seed
from email_router.store import Store


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOME = ROOT / 'artifacts/private/gmail-pilot'
MAX_BYTES = 256_000


def address(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value):
        raise ValueError('Invalid test mailbox address')
    return value


class Pilot:
    def __init__(self, home=DEFAULT_HOME):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / 'pilot.sqlite')
        (self.home / 'pilot.sqlite').chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS inbox(
              id TEXT PRIMARY KEY, raw BLOB NOT NULL, external_id TEXT NOT NULL,
              parent TEXT, state TEXT NOT NULL DEFAULT 'quarantine', run_folder TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(
              id TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, recipient_ref TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS delivery_attempts(
              id INTEGER PRIMARY KEY, delivery_id TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

    def ingest(self, raw):
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
            raise ValueError('Message size outside pilot limit')
        key = hashlib.sha256(raw).hexdigest()
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        external = str(mail.get('Message-ID', ''))
        refs = str(mail.get('References', '')) + ' ' + str(mail.get('In-Reply-To', ''))
        parent = None
        # Advisory thread relation only; never an authorization or customer lookup.
        for ref in reversed(re.findall(r'<[^<>\s]+>', refs)):
            matches = self.db.execute('SELECT id FROM inbox WHERE external_id=?', (ref,)).fetchall()
            if len(matches) == 1:
                parent = matches[0]['id']
                break
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO inbox(id,raw,external_id,parent) VALUES(?,?,?,?)',
                            (key, raw, external, parent))
        return key

    def fetch(self, account, password, folder='email-router-pilot', client_factory=imaplib.IMAP4_SSL):
        address(account)
        count = 0
        with client_factory('imap.gmail.com', 993, ssl_context=ssl.create_default_context(), timeout=30) as client:
            client.login(account, password)
            status, _ = client.select(folder, readonly=True)
            if status != 'OK':
                raise ValueError('Create the dedicated pilot label in Gmail first')
            status, data = client.uid('search', None, 'ALL')
            if status != 'OK':
                raise ValueError('Mailbox search failed')
            # Process oldest first. Repeated polls deduplicate by raw message digest.
            for uid in data[0].split():
                status, sizes = client.uid('fetch', uid, '(RFC822.SIZE)')
                size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(x for x in sizes if isinstance(x, bytes)))
                if status != 'OK' or not size or int(size[1]) > MAX_BYTES:
                    continue
                status, parts = client.uid('fetch', uid, '(BODY.PEEK[])')
                if status != 'OK':
                    raise ValueError('Mailbox fetch failed')
                bodies = [p[1] for p in parts if isinstance(p, tuple) and isinstance(p[1], bytes)]
                if len(bodies) != 1:
                    raise ValueError('Invalid mailbox fetch response')
                self.ingest(bodies[0])
                count += 1
        return count

    def process_fixture(self, key, fixture_id, model):
        """Staff explicitly maps an exact synthetic body to an existing fixture.

        The transport From/Subject/References never enter the model. This tests
        transport and fixture routing, not authentication of a real customer.
        """
        row = self.db.execute('SELECT * FROM inbox WHERE id=?', (key,)).fetchone()
        if not row or row['state'] != 'quarantine':
            raise ValueError('Expected an unprocessed quarantined message')
        seed = synthetic_seed(json.loads((ROOT / 'data/seed-held-out.json').read_text()))
        fixture = next((m for m in seed['messages'] if m['id'] == fixture_id), None)
        if not fixture:
            raise ValueError('Unknown synthetic fixture')
        mail = BytesParser(policy=policy.default).parsebytes(row['raw'])
        leaves = [p for p in mail.walk() if not p.is_multipart()]
        if len(leaves) != 1 or leaves[0].get_content_type() != 'text/plain' or leaves[0].get_filename():
            raise ValueError('Pilot accepts only one plain text part without attachments')
        body = leaves[0].get_content()
        if body.strip().replace('\r\n', '\n') != fixture['text'].strip().replace('\r\n', '\n'):
            raise ValueError('Body does not match the staff-selected synthetic fixture')
        if model.is_live and getattr(model, 'provider', None) != 'gemini':
            raise ValueError('Only configured Gemini Free Tier is allowed in this pilot')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        folder = self.home / ('run-' + stamp)
        # Claim before effects. A crash leaves a visible processing row, never an automatic replay.
        with self.db:
            updated = self.db.execute("UPDATE inbox SET state='processing',run_folder=? WHERE id=? AND state='quarantine'",
                                      (str(folder), key)).rowcount
            if updated != 1:
                raise ValueError('Message already claimed')
        folder.mkdir(exist_ok=False)
        seed_path = folder / 'seed.json'
        seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2))
        store = Store(folder / 'state.sqlite')
        try:
            store.seed(seed_path)
            report = Runner(store, model, 'workflow').run(fixture_id)
            (folder / 'trace.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            completed = report['events'][-1]['payload']['status'] == 'completed'
            with self.db:
                if completed:
                    for notice in report['outgoing']:
                        # Only fixed staff notices go to real SMTP, no clarification or source content.
                        if notice['kind'] == 'staff_routing_notice':
                            self.db.execute('INSERT INTO deliveries(id,inbox_id,recipient_ref) VALUES(?,?,?)',
                                            (notice['id'], key, notice['recipient_ref']))
                self.db.execute('UPDATE inbox SET state=? WHERE id=?',
                                ('processed' if completed else 'failed', key))
            return {'state': 'processed' if completed else 'failed', 'live_model': model.is_live,
                    'fixture': fixture_id, 'folder': str(folder)}
        finally:
            store.close()

    def send(self, identifier, account, password, recipients, allowlist, client_factory=smtplib.SMTP_SSL):
        """Explicit delivery command. SMTP acceptance is not proof of inbox delivery."""
        address(account)
        row = self.db.execute('SELECT * FROM deliveries WHERE id=?', (identifier,)).fetchone()
        if not row or row['state'] not in {'pending', 'rejected'}:
            raise ValueError('Delivery is not eligible; accepted/uncertain attempts are never retried automatically')
        target = address(recipients.get(row['recipient_ref']))
        if target not in {address(a) for a in allowlist}:
            raise ValueError('Recipient is outside the local test allowlist')
        mail = EmailMessage()
        mail['From'], mail['To'] = account, target
        mail['Subject'] = 'Email router: тестовое служебное уведомление'
        mail['Message-ID'] = '<' + identifier + '@email-router.example.test>'
        text = routing_notice('', '', '', '', '')['text']
        mail.set_content(text + '\n\nСлужебная ссылка: pilot:' + identifier)
        with self.db:
            updated = self.db.execute("UPDATE deliveries SET state='uncertain' WHERE id=? AND state IN ('pending','rejected')",
                                      (identifier,)).rowcount
            if updated != 1:
                raise ValueError('Delivery already claimed')
            attempt = self.db.execute('INSERT INTO delivery_attempts(delivery_id,at,status) VALUES(?,?,?)',
                                      (identifier, datetime.now(timezone.utc).isoformat(), 'uncertain')).lastrowid
        status = 'uncertain'
        sending_started = False
        try:
            with client_factory('smtp.gmail.com', 465, context=ssl.create_default_context(), timeout=30) as client:
                client.login(account, password)
                sending_started = True
                refused = client.send_message(mail, from_addr=account, to_addrs=[target])
                status = 'rejected' if refused else 'accepted'
                # Persist acceptance before QUIT: a QUIT failure must not authorize a duplicate.
                self._delivery_status(identifier, attempt, status)
        except smtplib.SMTPAuthenticationError:
            if not sending_started:
                status = 'rejected'
                self._delivery_status(identifier, attempt, status)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError):
            if status != 'accepted':
                status = 'rejected'
                self._delivery_status(identifier, attempt, status)
        except Exception:
            # Never log exception text: SMTP replies may contain addresses or credentials.
            pass
        return status

    def _delivery_status(self, identifier, attempt, status):
        with self.db:
            self.db.execute('UPDATE deliveries SET state=? WHERE id=?', (status, identifier))
            self.db.execute('UPDATE delivery_attempts SET status=? WHERE id=?', (status, attempt))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=DEFAULT_HOME)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('fetch')
    sub.add_parser('status')
    fixture = sub.add_parser('fixture-text')
    fixture.add_argument('--fixture', required=True)
    process = sub.add_parser('process-fixture')
    process.add_argument('--id', required=True)
    process.add_argument('--fixture', required=True)
    send = sub.add_parser('send')
    send.add_argument('--id', required=True)
    send.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'fixture-text':
        seed = synthetic_seed(json.loads((ROOT / 'data/seed-held-out.json').read_text()))
        matches = [m['text'] for m in seed['messages'] if m['id'] == args.fixture]
        if len(matches) != 1:
            parser.error('Unknown synthetic fixture')
        print(matches[0])
        return 0
    load_env()
    pilot = Pilot(args.home)
    try:
        account, password = os.getenv('GMAIL_PILOT_ACCOUNT', ''), os.getenv('GMAIL_PILOT_APP_PASSWORD', '')
        if args.command in {'fetch', 'send'} and not password:
            raise ValueError('Configure Gmail pilot account and app password locally')
        if args.command == 'fetch':
            result = {'fetched': pilot.fetch(account, password)}
            exit_code = 0
        elif args.command == 'status':
            result = {name: [dict(r) for r in pilot.db.execute(query)] for name, query in (
                ('inbox', 'SELECT id,parent,state,run_folder FROM inbox'),
                ('deliveries', 'SELECT id,inbox_id,recipient_ref,state FROM deliveries'))}
            exit_code = 0
        elif args.command == 'process-fixture':
            result = pilot.process_fixture(args.id, args.fixture, ChatModel())
            exit_code = 0 if result['state'] == 'processed' else 1
        else:
            config = json.loads(args.config.read_text())
            result = {'smtp_status': pilot.send(args.id, account, password, config['recipients'], config['allowlist'])}
            exit_code = 0 if result['smtp_status'] == 'accepted' else 1
        print(json.dumps(result, ensure_ascii=False))
        return exit_code
    except Exception:
        print('Pilot operation failed. Check local configuration and status; raw errors are suppressed.')
        return 1
    finally:
        pilot.close()


if __name__ == '__main__':
    raise SystemExit(main())
