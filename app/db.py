"""SQLite storage for audits, their tasks, and the saved shelf photos.

One small file database (data/shelfsense.db) plus photos in data/images/. Audits survive
page refreshes and server restarts because they live on disk, not in memory.
"""

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.schemas import ATTENTION_BELOW, Task

log = logging.getLogger("shelfsense.db")

JSON_COLUMNS = {"planogram_json", "detected_json", "working_json", "result_json", "warnings_json", "usage_json"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audits (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'queued',
    category        TEXT NOT NULL,
    category_label  TEXT NOT NULL DEFAULT '',
    category_name   TEXT NOT NULL DEFAULT '',
    store           TEXT NOT NULL DEFAULT '',
    shelf           TEXT NOT NULL DEFAULT '',
    image_path      TEXT,
    image_mime      TEXT,
    image_w         INTEGER,
    image_h         INTEGER,
    planogram_json  TEXT NOT NULL,
    detected_json   TEXT,
    working_json    TEXT,
    result_json     TEXT,
    score           REAL,
    detected_units  INTEGER NOT NULL DEFAULT 0,
    missing_units   INTEGER NOT NULL DEFAULT 0,
    misplaced_units INTEGER NOT NULL DEFAULT 0,
    unexpected_units INTEGER NOT NULL DEFAULT 0,
    uncertain_units INTEGER NOT NULL DEFAULT 0,
    confidence      REAL,
    model           TEXT,
    error           TEXT,
    warnings_json   TEXT,
    confirmed_empty INTEGER NOT NULL DEFAULT 0,
    corrected       INTEGER NOT NULL DEFAULT 0,
    confirmed_at    TEXT,
    usage_json      TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audits_created ON audits(created_at);
CREATE INDEX IF NOT EXISTS idx_audits_status ON audits(status);

CREATE TABLE IF NOT EXISTS tasks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id  TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,
    priority  TEXT NOT NULL,
    text      TEXT NOT NULL,
    done      INTEGER NOT NULL DEFAULT 0,
    done_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_audit ON tasks(audit_id);

-- How many AI runs happened each day (for the optional daily limit).
CREATE TABLE IF NOT EXISTS runs (
    day   TEXT PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
"""

# The "needs attention" rule, shared by the stats, the filter and the dashboard.
_ATTENTION_SQL = (
    f"(status IN ('needs_review', 'failed') OR (status = 'completed' AND score < {ATTENTION_BELOW}))"
)

_LIST_COLUMNS = """
    a.id, a.created_at, a.status, a.stage, a.category, a.category_label, a.category_name,
    a.store, a.shelf, a.score, a.detected_units, a.missing_units, a.misplaced_units,
    a.unexpected_units, a.uncertain_units, a.confidence, a.error,
    (SELECT COUNT(*) FROM tasks t WHERE t.audit_id = a.id) AS task_total,
    (SELECT COUNT(*) FROM tasks t WHERE t.audit_id = a.id AND t.done = 1) AS task_done
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Database:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory)
        self.images_dir = self.directory / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "shelfsense.db"
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database created by an older version up to date without losing audits."""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audits)")}
        added = {
            "confirmed_at": "TEXT",
            "usage_json": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in added.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE audits ADD COLUMN {name} {definition}")
                log.info("database upgraded: added audits.%s", name)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---- audits ----------------------------------------------------------------

    def create_audit(
        self,
        *,
        category: str,
        category_label: str,
        category_name: str,
        planogram: dict,
        image: bytes,
        mime: str,
        width: int,
        height: int,
        warnings: list[str],
    ) -> str:
        audit_id = f"SS-{uuid.uuid4().hex[:8].upper()}"
        image_path = self.images_dir / f"{audit_id}.jpg"
        image_path.write_bytes(image)
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO audits (id, created_at, updated_at, status, stage, category,
                       category_label, category_name, store, shelf, image_path, image_mime,
                       image_w, image_h, planogram_json, warnings_json)
                   VALUES (?, ?, ?, 'processing', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    audit_id, now, now, category, category_label, category_name,
                    str(planogram.get("store", "")), str(planogram.get("shelf", "")),
                    str(image_path), mime, width, height,
                    json.dumps(planogram), json.dumps(warnings),
                ),
            )
        log.info("created audit %s (%s)", audit_id, category)
        return audit_id

    def update_audit(self, audit_id: str, **fields: Any) -> None:
        if not fields:
            return
        values = {
            k: (json.dumps(v) if k in JSON_COLUMNS and not isinstance(v, str) else v)
            for k, v in fields.items()
        }
        values["updated_at"] = _now()
        assignments = ", ".join(f"{k} = ?" for k in values)
        with self._conn() as conn:
            conn.execute(f"UPDATE audits SET {assignments} WHERE id = ?", (*values.values(), audit_id))

    def get_audit(self, audit_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM audits WHERE id = ?", (audit_id,)).fetchone()
        if row is None:
            return None
        audit = dict(row)
        for column in JSON_COLUMNS:
            raw = audit.get(column)
            audit[column] = json.loads(raw) if raw else None
        audit["confirmed_empty"] = bool(audit["confirmed_empty"])
        audit["corrected"] = bool(audit["corrected"])
        return audit

    def set_confirmed(self, audit_id: str, confirmed: bool) -> str | None:
        """Mark a result as confirmed (or not) by the person who reviewed it. Returns the time."""
        stamp = _now() if confirmed else None
        self.update_audit(audit_id, confirmed_at=stamp)
        return stamp

    def read_image(self, audit_id: str) -> bytes | None:
        path = self.images_dir / f"{audit_id}.jpg"
        return path.read_bytes() if path.exists() else None

    def image_path(self, audit_id: str) -> Path | None:
        path = self.images_dir / f"{audit_id}.jpg"
        return path if path.exists() else None

    def list_audits(
        self,
        *,
        q: str = "",
        status: str = "",
        category: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 15,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        where, params = [], []
        if q.strip():
            like = f"%{q.strip()}%"
            where.append(
                "(a.id LIKE ? OR a.store LIKE ? OR a.shelf LIKE ? OR a.category LIKE ? "
                "OR a.category_label LIKE ? OR a.category_name LIKE ?)"
            )
            params += [like] * 6
        if status == "attention":
            where.append(_ATTENTION_SQL.replace("status", "a.status").replace("score", "a.score"))
        elif status:
            where.append("a.status = ?")
            params.append(status)
        if category:
            where.append("a.category = ?")
            params.append(category)
        if date_from:
            where.append("substr(a.created_at, 1, 10) >= ?")
            params.append(date_from)
        if date_to:
            where.append("substr(a.created_at, 1, 10) <= ?")
            params.append(date_to)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        with self._conn() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM audits a {clause}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM audits a {clause} "
                "ORDER BY a.created_at DESC, a.rowid DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], total

    def stats(self) -> dict[str, int]:
        with self._conn() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS total,
                           COALESCE(SUM(status = 'completed'), 0) AS completed,
                           COALESCE(SUM(status = 'processing'), 0) AS processing,
                           COALESCE(SUM({_ATTENTION_SQL}), 0) AS attention
                    FROM audits"""
            ).fetchone()
        return dict(row)

    def recent_scores(self, limit: int = 12) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, created_at, score FROM audits WHERE status = 'completed' "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def category_averages(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT category, MAX(category_label) AS label, AVG(score) AS avg_score,
                          COUNT(*) AS audits
                   FROM audits WHERE status = 'completed'
                   GROUP BY category ORDER BY audits DESC, label"""
            ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM audits GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def delete_audit(self, audit_id: str) -> bool:
        """Remove an audit, its tasks and its saved photo. Returns False if there was no such audit."""
        with self._conn() as conn:
            deleted = conn.execute("DELETE FROM audits WHERE id = ?", (audit_id,)).rowcount
        photo = self.images_dir / f"{audit_id}.jpg"
        if photo.exists():
            photo.unlink()
        if deleted:
            log.info("deleted audit %s", audit_id)
        return bool(deleted)

    def runs_today(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT count FROM runs WHERE day = ?", (_today(),)).fetchone()
        return row["count"] if row else 0

    def bump_runs(self) -> int:
        """Count one more AI run today and return today's total."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO runs (day, count) VALUES (?, 1) ON CONFLICT(day) DO UPDATE SET count = count + 1",
                (_today(),),
            )
            return conn.execute("SELECT count FROM runs WHERE day = ?", (_today(),)).fetchone()["count"]

    def recover_interrupted(
        self, max_age_minutes: int = 30, max_attempts: int = 3
    ) -> tuple[list[str], int]:
        """Deal with audits a stopped server left in 'processing'.

        The photo, planogram and settings are all saved, so a recent audit is simply started again.
        Two guards stop a problem audit from running forever: one older than `max_age_minutes`
        is given up on, and so is one that has already been started `max_attempts` times (for
        example because it keeps crashing the server).

        Returns (ids to run again, number given up on).
        """
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_minutes * 60
        resume, failed = [], 0
        with self._conn() as conn:
            rows = conn.execute("SELECT id, created_at, attempts FROM audits WHERE status = 'processing'").fetchall()
            for row in rows:
                too_old = datetime.fromisoformat(row["created_at"]).timestamp() < cutoff
                if too_old or row["attempts"] >= max_attempts:
                    why = (
                        "This audit was interrupted by a restart and is too old to resume. Please run it again."
                        if too_old
                        else "This audit kept being interrupted by restarts and was stopped. Please run it again."
                    )
                    conn.execute(
                        "UPDATE audits SET status = 'failed', stage = 'done', updated_at = ?, error = ? WHERE id = ?",
                        (_now(), why, row["id"]),
                    )
                    failed += 1
                else:
                    conn.execute(
                        "UPDATE audits SET stage = 'queued', updated_at = ? WHERE id = ?", (_now(), row["id"])
                    )
                    resume.append(row["id"])
        return resume, failed

    # ---- tasks -----------------------------------------------------------------

    def replace_tasks(self, audit_id: str, tasks: list[Task]) -> None:
        """Store the audit's tasks.

        A task whose text is unchanged keeps its row (same id) and its done state; tasks that
        no longer apply are removed and new ones are added. So an open browser tab stays valid
        after the comparison is re-run.
        """
        with self._conn() as conn:
            existing: dict[str, list[int]] = {}
            for r in conn.execute("SELECT id, text FROM tasks WHERE audit_id = ? ORDER BY id", (audit_id,)):
                existing.setdefault(r["text"], []).append(r["id"])

            keep: set[int] = set()
            for t in tasks:
                ids = existing.get(t.text)
                if ids:
                    task_id = ids.pop(0)
                    keep.add(task_id)
                    conn.execute("UPDATE tasks SET kind = ?, priority = ? WHERE id = ?", (t.kind, t.priority, task_id))
                else:
                    cur = conn.execute(
                        "INSERT INTO tasks (audit_id, kind, priority, text) VALUES (?, ?, ?, ?)",
                        (audit_id, t.kind, t.priority, t.text),
                    )
                    keep.add(cur.lastrowid)
            for leftover in (i for ids in existing.values() for i in ids):
                conn.execute("DELETE FROM tasks WHERE id = ?", (leftover,))

    def get_tasks(self, audit_id: str) -> list[dict]:
        """Tasks, most urgent first."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT id, kind, priority, text, done FROM tasks WHERE audit_id = ?
                   ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id""",
                (audit_id,),
            ).fetchall()
        return [{**dict(r), "done": bool(r["done"])} for r in rows]

    def set_task_done(self, audit_id: str, task_id: int, done: bool) -> dict | None:
        """Tick or untick a task. Returns the new progress, or None if there is no such task."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE tasks SET done = ?, done_at = ? WHERE id = ? AND audit_id = ?",
                (1 if done else 0, _now() if done else None, task_id, audit_id),
            )
            if cur.rowcount == 0:
                return None
            total, finished = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(done), 0) FROM tasks WHERE audit_id = ?", (audit_id,)
            ).fetchone()
        return {"total": total, "done": finished}


# ---- shared instance ------------------------------------------------------------

_db: Database | None = None


def configure(directory: Path | str | None = None) -> Database:
    """(Re)open the database. Tests point this at a temporary folder."""
    global _db
    if directory is None:
        from app.config import get_settings

        directory = get_settings().data_path
    _db = Database(directory)
    return _db


def get_db() -> Database:
    return _db if _db is not None else configure()
