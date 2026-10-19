"""SQLite persistence. Entity changes and local mail delivery share a transaction."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from .policy import cloud_context_allowed, public_report


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
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
          at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        """)

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
                        self.put(kind, row | {"data_classification": data.get("data_classification", "restricted")})

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

    def event(self, run_id, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO events(run_id,at,kind,payload) VALUES(?,?,?,?)",
                            (run_id, time.time(), kind, encode(payload)))

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
        message = self.get("messages", run["message_id"])
        report = {
            "run": run, "message": message, "issues": self.issues(message["id"]),
            "actions": [json.loads(r[0]) for r in self.db.execute(
                "SELECT a.receipt FROM actions a JOIN issues i ON i.id=a.issue_id WHERE i.message_id=?",
                (message["id"],))],
            "events": [{"kind": r["kind"], "at": r["at"], "payload": json.loads(r["payload"])}
                       for r in self.db.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,))],
            "outgoing": [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM outgoing WHERE message_id=?", (message["id"],))],
            "reviews": [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM reviews WHERE message_id=?", (message["id"],))],
        }
        return report if cloud_context_allowed(self) else public_report(report)
