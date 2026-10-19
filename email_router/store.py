"""SQLite persistence. Entity changes and local mail delivery share a transaction."""

import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from .policy import SOURCE_KINDS, cloud_context_allowed, critical_record, public_report


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def private_database_path(path):
    """Return a protected path and the file identity verified through its descriptor."""
    path = Path(os.path.abspath(path))
    # macOS system aliases are root-owned; user-provided symlink components below
    # them are still rejected by the directory-descriptor walk.
    if path.parts[1:2] in (("var",), ("tmp",)):
        alias = Path('/') / path.parts[1]
        if alias.is_symlink() and alias.lstat().st_uid == 0:
            path = alias.resolve(strict=True).joinpath(*path.parts[2:])
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parent.parts[1:]:
            parent = os.fstat(directory)
            if parent.st_uid not in (0, os.getuid()):
                raise PermissionError('Database ancestors must belong to the current user or root')
            if parent.st_mode & 0o022 and not parent.st_mode & stat.S_ISVTX:
                raise PermissionError('Database ancestors must prevent renaming by other users')
            try:
                os.mkdir(component, 0o700, dir_fd=directory)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            # Sticky shared directories protect only entries owned by us/root.
            if os.fstat(child).st_uid not in (0, os.getuid()):
                os.close(child)
                raise PermissionError('Database ancestors must belong to the current user or root')
            os.close(directory)
            directory = child
        if os.fstat(directory).st_uid != os.getuid():
            raise PermissionError('Database directory must belong to the current user')
        os.fchmod(directory, 0o700)
        for suffix in ('', '-journal', '-wal', '-shm'):
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if not suffix:
                flags |= os.O_CREAT
            try:
                fd = os.open(path.name + suffix, flags, 0o600, dir_fd=directory)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise PermissionError('Database files must be owned regular files without additional links')
                os.fchmod(fd, 0o600)
                if not suffix:
                    verified_file = info
            finally:
                os.close(fd)
    finally:
        os.close(directory)
    return path, verified_file


class Store:
    def __init__(self, path):
        path, expected = private_database_path(path)
        # Ancestor checks above prevent another user from replacing this path.
        # mode=rw also forbids SQLite from creating an unchecked replacement file.
        self.db = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=2)
        try:
            opened_path = Path(self.db.execute('PRAGMA database_list').fetchone()[2])
            for candidate in (path, opened_path):
                actual = candidate.stat(follow_symlinks=False)
                if ((actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
                        or not stat.S_ISREG(actual.st_mode)
                        or actual.st_uid != os.getuid() or actual.st_nlink != 1
                        or stat.S_IMODE(actual.st_mode) != 0o600):
                    raise PermissionError('Database location changed during connection')
        except BaseException:
            self.db.close()
            raise
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS entities(
          kind TEXT, id TEXT, payload TEXT NOT NULL,
          PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL, mode TEXT NOT NULL,
          backend TEXT NOT NULL, started REAL NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS issues(
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending');
        CREATE TABLE IF NOT EXISTS observations(
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tool TEXT NOT NULL,
          arguments TEXT NOT NULL, result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS actions(
          issue_id TEXT PRIMARY KEY, tool TEXT NOT NULL, arguments TEXT NOT NULL,
          receipt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outgoing(
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL, recipient TEXT NOT NULL,
          payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(
          id TEXT PRIMARY KEY, message_id TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
          at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS run_export_policy(
          run_id TEXT PRIMARY KEY, restricted INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS run_source_classifications(
          run_id TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
          classification TEXT NOT NULL, restricted INTEGER NOT NULL,
          PRIMARY KEY(run_id,kind,source_id,classification,restricted));
        """)
        if "metadata" not in {r[1] for r in self.db.execute("PRAGMA table_info(events)")}:
            self.db.execute("ALTER TABLE events ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
            self.db.commit()

    def close(self):
        self.db.close()

    def seed(self, filename):
        if self.db.execute("SELECT 1 FROM entities LIMIT 1").fetchone():
            return
        data = json.loads(Path(filename).read_text())
        with self.db:
            self.put("meta", {"id": "settings", "processing_date": data["processing_date"]})
            for kind, rows in data.items():
                if isinstance(rows, list):
                    for row in rows:
                        inherited = row | {"data_classification": data.get("data_classification", "restricted")}
                        individual = row if "data_classification" in row else inherited
                        # A record may tighten the dataset policy, never loosen it.
                        value = inherited if (critical_record(inherited, kind)
                                              and not critical_record(individual, kind)) else individual
                        self.put(kind, value)

    def put(self, kind, value):
        self.db.execute("INSERT INTO entities VALUES(?,?,?) ON CONFLICT(kind,id) "
                        "DO UPDATE SET payload=excluded.payload",
                        (kind, value["id"], encode(value)))

    def get(self, kind, identifier):
        row = self.db.execute("SELECT payload FROM entities WHERE kind=? AND id=?",
                              (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self, kind):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM entities WHERE kind=? ORDER BY id", (kind,))]

    def event(self, run_id, kind, payload, *, metadata=None):
        with self.db:
            sources = [(source_kind, record) for source_kind in SOURCE_KINDS for record in self.all(source_kind)]
            restrictions = [(run_id, source_kind, record['id'],
                             encode(record.get('data_classification', 'restricted')),
                             int(critical_record(record, source_kind))) for source_kind, record in sources]
            # Missing provenance on an old run cannot be reconstructed from today's sources.
            legacy = (not self.db.execute('SELECT 1 FROM run_export_policy WHERE run_id=?', (run_id,)).fetchone()
                      and self.db.execute('SELECT 1 FROM events WHERE run_id=?', (run_id,)).fetchone() is not None)
            restricted = int(legacy or any(item[-1] for item in restrictions))
            self.db.execute('INSERT INTO run_export_policy VALUES(?,?) ON CONFLICT(run_id) DO UPDATE '
                            'SET restricted=MAX(restricted,excluded.restricted)', (run_id, restricted))
            self.db.executemany('INSERT OR IGNORE INTO run_source_classifications VALUES(?,?,?,?,?)', restrictions)
            self.db.execute("INSERT INTO events(run_id,at,kind,payload,metadata) VALUES(?,?,?,?,?)",
                            (run_id, time.time(), kind, encode(payload), encode(metadata or {})))

    def issues(self, message_id):
        return [dict(row) | {"payload": json.loads(row["payload"])}
                for row in self.db.execute("SELECT * FROM issues WHERE message_id=? ORDER BY id",
                                           (message_id,))]

    def issue(self, identifier):
        row = self.db.execute("SELECT * FROM issues WHERE id=?", (identifier,)).fetchone()
        return dict(row) | {"payload": json.loads(row["payload"])} if row else None

    def register(self, message, proposals):
        existing = self.issues(message["id"])
        if existing:
            return existing
        if not isinstance(proposals, list) or not 1 <= len(proposals) <= 4:
            raise ValueError("Expected 1 to 4 issues")
        allowed = {"technical", "order", "purchase", "approval", "unknown", "forbidden"}
        for p in proposals:
            if p.get("category") not in allowed or not isinstance(p.get("quote"), str):
                raise ValueError("Invalid issue category or quote")
            if not p["quote"] or p["quote"] not in message["text"]:
                raise ValueError("Issue quote must occur in original message")
            if not isinstance(p.get("subject"), str) or not p["subject"].strip():
                raise ValueError("Issue subject is required")
        with self.db:
            for n, p in enumerate(proposals, 1):
                self.db.execute("INSERT INTO issues(id,message_id,payload) VALUES(?,?,?)",
                                (f"{message['id']}:issue-{n}", message["id"], encode(p)))
        return self.issues(message["id"])

    def report(self, run_id):
        run = dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        run["config"] = json.loads(run.pop("payload"))
        start = self.db.execute("SELECT payload FROM events WHERE run_id=? AND kind='run_start' ORDER BY seq LIMIT 1",
                                (run_id,)).fetchone()
        message = json.loads(start[0])["message"] if start else self.get("messages", run["message_id"])
        report = {
            "run": run, "message": message,
            "message_snapshot_source": "run_start" if start else "current_store_fallback",
            "issues": self.issues(message["id"]),
            "actions": [json.loads(r[0]) for r in self.db.execute(
                "SELECT a.receipt FROM actions a JOIN issues i ON i.id=a.issue_id WHERE i.message_id=?",
                (message["id"],))],
            "events": [{**json.loads(r["metadata"]), "event_index": index,
                        "run_id": run_id, "case_id": run["config"].get("case_id", message["id"]),
                        **{key: run["config"].get(key) for key in (
                            "prompt_version", "tool_version", "code_version", "dataset_version", "model_config_version")},
                        "kind": r["kind"], "at": r["at"], "payload": json.loads(r["payload"])}
                       for index, r in enumerate(self.db.execute(
                           "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)), 1)],
            "outgoing": [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM outgoing WHERE message_id=?", (message["id"],))],
            "reviews": [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM reviews WHERE message_id=?", (message["id"],))],
        }
        history = self.db.execute('SELECT restricted FROM run_export_policy WHERE run_id=?', (run_id,)).fetchone()
        safe_history = history is not None and history['restricted'] == 0
        return report if (safe_history and cloud_context_allowed(self)
                          and not critical_record(message, "messages")) else public_report(report)
