"""SQLite persistence for task de-duplication, last-known pathways, and polling cursors."""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import CaseManagerTask, Pathway

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    dedup_key TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_patient ON tasks(patient_id);
CREATE TABLE IF NOT EXISTS patient_pathways (
    patient_id TEXT PRIMARY KEY,
    pathway TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cursors (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()

    def claim_task(self, task: CaseManagerTask) -> bool:
        """Record a task; returns False if a task with the same dedup key was already claimed."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO tasks (dedup_key, patient_id, rule_id, payload, created_at) VALUES (?,?,?,?,?)",
                (task.dedup_key, task.patient_id, task.rule_id, task.model_dump_json(), _now()),
            )
            return cur.rowcount == 1

    def release_task(self, dedup_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE dedup_key = ?", (dedup_key,))

    def list_tasks(self, patient_id: str | None = None, limit: int = 100) -> list[CaseManagerTask]:
        sql = "SELECT payload FROM tasks"
        args: tuple = ()
        if patient_id:
            sql += " WHERE patient_id = ?"
            args = (patient_id,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*args, limit)).fetchall()
        return [CaseManagerTask.model_validate_json(r[0]) for r in rows]

    def swap_pathway(self, patient_id: str, pathway: Pathway) -> Pathway | None:
        """Store the patient's current pathway and return the previous one (None if first seen)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT pathway FROM patient_pathways WHERE patient_id = ?", (patient_id,)
            ).fetchone()
            self._conn.execute(
                "INSERT INTO patient_pathways (patient_id, pathway, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(patient_id) DO UPDATE SET pathway = excluded.pathway, updated_at = excluded.updated_at",
                (patient_id, pathway.value, _now()),
            )
        return Pathway(row[0]) if row else None

    def get_cursor(self, name: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM cursors WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, name: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO cursors (name, value) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value),
            )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
