"""Small, versioned SQLite schema, independent of PostgreSQL migrations."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, finished_at TEXT, steps_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE events (
    run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL, PRIMARY KEY (run_id, sequence)
);
CREATE TABLE audit (
    action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, event_json TEXT NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
CREATE TABLE traces (action_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, trace_json TEXT NOT NULL);
CREATE TABLE oauth_metadata (
    provider TEXT PRIMARY KEY, expires_at TEXT, scope TEXT
);
CREATE TABLE payments (
    remote_id TEXT PRIMARY KEY, kind TEXT NOT NULL, data_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE TABLE intents (
    operation_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, action_id TEXT NOT NULL,
    target_system TEXT NOT NULL, action TEXT NOT NULL, request_hash TEXT NOT NULL,
    state TEXT NOT NULL, remote_id TEXT, created_at TEXT NOT NULL, result_json TEXT
);
CREATE INDEX intents_state ON intents(state);
CREATE INDEX audit_run ON audit(run_id);
"""


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("AgentGate's private directory must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class LocalDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> LocalDatabase:
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError("AgentGate's database must not be a symlink")
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        self.path.chmod(0o600)
        connection = sqlite3.connect(self.path, timeout=5)
        self.connection = connection
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError("Local database is newer than this AgentGate installation")
            if version == 0:
                connection.executescript(
                    f"BEGIN IMMEDIATE;\n{_SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args: Any) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Local database is not open")
        return self.connection
