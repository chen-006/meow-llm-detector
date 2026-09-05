from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import queue
import sqlite3
import threading
from typing import Any, Callable, TypeVar

from .utils import canonical_json, utc_now


SCHEMA_VERSION = 3
T = TypeVar("T")


@dataclass
class _WriteCall:
    callback: Callable[[sqlite3.Connection], Any]
    completed: threading.Event
    result: Any = None
    error: BaseException | None = None


class SQLiteStateStore:
    """SQLite WAL runtime store with one serialized writer thread."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[_WriteCall | None] = queue.Queue()
        self._closed = False
        self._lifecycle_lock = threading.Lock()
        self._writer = threading.Thread(
            target=self._writer_main,
            daemon=True,
            name=f"gpt56-sqlite-writer-{self.path.stem}",
        )
        self._writer.start()
        self._write(self._initialize)

    @staticmethod
    def _configure(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")

    @classmethod
    def _initialize(cls, connection: sqlite3.Connection) -> None:
        cls._configure(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                kind TEXT NOT NULL,
                id TEXT NOT NULL,
                body_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(kind,id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_model TEXT,
                request_model TEXT,
                safe_endpoint TEXT,
                config_json TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                official INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                stop_requested_at TEXT,
                report_json TEXT,
                report_updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                session_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                cycle INTEGER NOT NULL DEFAULT 0,
                ordinal INTEGER NOT NULL,
                manifest_json TEXT NOT NULL,
                manifest_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                final_result_id INTEGER,
                frozen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, job_id),
                UNIQUE (session_id, cycle, ordinal),
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                stage TEXT,
                category TEXT,
                retryable INTEGER,
                http_status INTEGER,
                safe_message TEXT,
                UNIQUE (session_id, job_id, attempt_no),
                FOREIGN KEY (session_id, job_id) REFERENCES jobs(session_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS results (
                result_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id, job_id) REFERENCES jobs(session_id, job_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                cycle INTEGER,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            );
            CREATE TABLE IF NOT EXISTS retained_exchanges (
                attempt_id INTEGER PRIMARY KEY REFERENCES attempts(attempt_id),
                body_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_session_cycle ON jobs(session_id, cycle, ordinal);
            CREATE INDEX IF NOT EXISTS idx_attempts_session_job ON attempts(session_id, job_id, attempt_no);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, event_id);
            CREATE INDEX IF NOT EXISTS idx_results_session ON results(session_id, result_id);
            """
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
        if "request_model" not in columns:
            connection.execute("ALTER TABLE sessions ADD COLUMN request_model TEXT")
        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()

    def _writer_main(self) -> None:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            while True:
                call = self._queue.get()
                if call is None:
                    return
                try:
                    call.result = call.callback(connection)
                except BaseException as exc:
                    call.error = exc
                finally:
                    call.completed.set()
        finally:
            connection.close()

    def _write(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        call = _WriteCall(callback=callback, completed=threading.Event())
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeError("state store is closed")
            self._queue.put(call)
        while not call.completed.wait(0.1):
            if not self._writer.is_alive():
                raise RuntimeError("state writer unavailable")
        if call.error is not None:
            raise call.error
        return call.result

    def _read(self, callback: Callable[[sqlite3.Connection], T]) -> T:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("BEGIN")
            return callback(connection)
        finally:
            connection.close()

    @staticmethod
    def _transaction(connection: sqlite3.Connection, callback: Callable[[], T]) -> T:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = callback()
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def create_session(
        self,
        *,
        session_id: str,
        kind: str,
        status: str,
        config: dict[str, Any],
        config_hash: str,
        official: bool,
        claimed_model: str | None = None,
        request_model: str | None = None,
        safe_endpoint: str | None = None,
    ) -> None:
        now = utc_now()
        config_json = canonical_json(config)

        def write(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                "SELECT config_json,config_hash,kind FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if existing:
                if existing["config_json"] != config_json or existing["config_hash"] != config_hash or existing["kind"] != kind:
                    raise ValueError("existing session configuration does not match")
                return
            connection.execute(
                "INSERT INTO sessions(session_id,kind,status,claimed_model,request_model,safe_endpoint,config_json,config_hash,official,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, kind, status, claimed_model, request_model or claimed_model, safe_endpoint, config_json, config_hash, int(official), now, now),
            )
            connection.commit()

        self._write(write)

    def session(self, session_id: str) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["request_model"] = value.get("request_model") or value.get("claimed_model")
            value["config"] = json.loads(value.pop("config_json"))
            value["official"] = bool(value["official"])
            return value

        return self._read(read)

    def interrupt_active_sessions(self) -> None:
        """Only the owning application calls this once at startup."""
        self._write(lambda connection: connection.execute(
            "UPDATE sessions SET status='paused',updated_at=?, "
            "report_json=CASE WHEN report_json IS NULL THEN NULL "
            "WHEN kind='collection' THEN json_set(report_json,'$.progress.status','paused') "
            "ELSE json_set(report_json,'$.operational_status','paused','$.progress.status','paused') END "
            "WHERE kind IN ('detection','collection') AND status IN ('prepared','running','stopping')",
            (utc_now(),),
        ))

    def update_session_status(self, session_id: str, status: str, *, clear_stop: bool = False) -> None:
        now = utc_now()
        self._write(lambda connection: connection.execute(
            "UPDATE sessions SET status=?,updated_at=?,stop_requested_at=CASE WHEN ? THEN NULL ELSE stop_requested_at END WHERE session_id=?",
            (status, now, int(clear_stop), session_id),
        ))

    def request_stop(self, session_id: str) -> str:
        now = utc_now()

        def write(connection: sqlite3.Connection) -> str:
            connection.execute(
                "UPDATE sessions SET status='stopping',stop_requested_at=COALESCE(stop_requested_at,?),updated_at=? WHERE session_id=?",
                (now, now, session_id),
            )
            connection.commit()
            row = connection.execute("SELECT stop_requested_at FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            return str(row[0])

        return self._write(write)

    def freeze_jobs(self, session_id: str, cycle: int, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [dict(job) for job in jobs]
        now = utc_now()

        def write(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            def transaction() -> list[dict[str, Any]]:
                existing = connection.execute(
                    "SELECT ordinal,manifest_json FROM jobs WHERE session_id=? AND cycle=? ORDER BY ordinal",
                    (session_id, cycle),
                ).fetchall()
                if existing:
                    frozen = [json.loads(row["manifest_json"]) for row in existing]
                    if [canonical_json(item) for item in frozen] != [canonical_json(item) for item in normalized]:
                        raise ValueError("cycle job manifest is already frozen with different jobs")
                    return frozen
                for ordinal, job in enumerate(normalized):
                    job_id = str(job.get("job_id") or "")
                    if not job_id:
                        raise ValueError("job_id is required before freezing a manifest")
                    manifest_json = canonical_json(job)
                    manifest_hash = ""  # Legacy NOT NULL column; manifest_json is the authority.
                    connection.execute(
                        "INSERT INTO jobs(session_id,job_id,cycle,ordinal,manifest_json,manifest_hash,status,frozen_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,'pending',?,?)",
                        (session_id, job_id, cycle, ordinal, manifest_json, manifest_hash, now, now),
                    )
                connection.execute(
                    "INSERT INTO events(session_id,event_type,cycle,payload_json,created_at) VALUES(?,?,?,?,?)",
                    (session_id, "cycle_manifest_frozen", cycle, canonical_json({"planned_jobs": len(normalized)}), now),
                )
                return normalized

            return self._transaction(connection, transaction)

        return self._write(write)

    def frozen_jobs(self, session_id: str, cycle: int) -> list[dict[str, Any]]:
        return self._read(lambda connection: [
            json.loads(row[0]) for row in connection.execute(
                "SELECT manifest_json FROM jobs WHERE session_id=? AND cycle=? ORDER BY ordinal",
                (session_id, cycle),
            )
        ])

    def pending_jobs(self, session_id: str, cycle: int | None = None, *, max_attempts: int | None = None) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            parameters: list[Any] = [session_id]
            clause = "j.session_id=? AND j.status='pending'"
            if cycle is not None:
                clause += " AND j.cycle=?"
                parameters.append(cycle)
            if max_attempts is not None:
                clause += " AND (SELECT COUNT(*) FROM attempts a WHERE a.session_id=j.session_id AND a.job_id=j.job_id) < ?"
                parameters.append(int(max_attempts))
            return [json.loads(row[0]) for row in connection.execute(
                f"SELECT j.manifest_json FROM jobs j WHERE {clause} ORDER BY j.cycle,j.ordinal",
                parameters,
            )]

        return self._read(read)

    def start_attempt(self, session_id: str, job_id: str, attempt_no: int, *, max_attempts: int | None = None) -> int:
        now = utc_now()

        def write(connection: sqlite3.Connection) -> int:
            if max_attempts is not None and int(attempt_no) > int(max_attempts):
                raise ValueError("HTTP attempt budget exhausted")
            job = connection.execute(
                "SELECT status FROM jobs WHERE session_id=? AND job_id=?",
                (session_id, job_id),
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["status"] != "pending":
                raise ValueError(f"job is already terminal: {job['status']}")
            cursor = connection.execute(
                "INSERT INTO attempts(session_id,job_id,attempt_no,started_at,status) VALUES(?,?,?,?,'running')",
                (session_id, job_id, attempt_no, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

        return self._write(write)

    def next_attempt_number(self, session_id: str, job_id: str) -> int:
        return self._read(lambda connection: int(connection.execute(
            "SELECT COALESCE(MAX(attempt_no),0)+1 FROM attempts WHERE session_id=? AND job_id=?",
            (session_id, job_id),
        ).fetchone()[0]))

    def reopen_parser_error(self, session_id: str, job_id: str, *, max_attempts: int) -> bool:
        """Explicit repair of a parser failure; preserve attempts and its old result."""
        def write(connection: sqlite3.Connection) -> bool:
            def transaction() -> bool:
                row = connection.execute(
                    "SELECT j.status,j.final_result_id,r.result_json FROM jobs j "
                    "LEFT JOIN results r ON r.result_id=j.final_result_id WHERE j.session_id=? AND j.job_id=?",
                    (session_id, job_id),
                ).fetchone()
                if row is None or row["status"] != "error" or not row["result_json"]:
                    return False
                result = json.loads(row["result_json"])
                if result.get("error", {}).get("code") != "invalid_stream":
                    return False
                count = connection.execute("SELECT COUNT(*) FROM attempts WHERE session_id=? AND job_id=?",
                                           (session_id, job_id)).fetchone()[0]
                if count >= max_attempts:
                    return False
                connection.execute("INSERT INTO events(session_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                    (session_id, "parser_error_reopened", canonical_json({"job_id": job_id,
                     "historical_result_id": row["final_result_id"], "attempts_already_used": count}), utc_now()))
                connection.execute("UPDATE jobs SET status='pending',final_result_id=NULL,updated_at=? WHERE session_id=? AND job_id=?",
                                   (utc_now(), session_id, job_id))
                return True
            return self._transaction(connection, transaction)
        return self._write(write)

    def finish_attempt(
        self,
        *,
        attempt_id: int,
        status: str,
        stage: str,
        category: str,
        retryable: bool,
        http_status: int | None,
        safe_message: str,
        final_result: dict[str, Any] | None = None,
        final_job_status: str | None = None,
        exchange: dict[str, Any] | None = None,
    ) -> int | None:
        now = utc_now()

        def write(connection: sqlite3.Connection) -> int | None:
            def transaction() -> int | None:
                attempt = connection.execute(
                    "SELECT a.session_id,a.job_id,a.status,j.status AS job_status "
                    "FROM attempts a JOIN jobs j ON j.session_id=a.session_id AND j.job_id=a.job_id "
                    "WHERE a.attempt_id=?",
                    (attempt_id,),
                ).fetchone()
                if attempt is None:
                    raise KeyError(f"attempt not found: {attempt_id}")
                if attempt["status"] != "running" or attempt["job_status"] != "pending":
                    return None
                connection.execute(
                    "UPDATE attempts SET completed_at=?,status=?,stage=?,category=?,retryable=?,http_status=?,safe_message=? WHERE attempt_id=?",
                    (now, status, stage, category, int(retryable), http_status, safe_message, attempt_id),
                )
                if exchange is not None:
                    connection.execute("INSERT INTO retained_exchanges(attempt_id,body_json) VALUES(?,?)",
                                       (attempt_id, canonical_json(exchange)))
                if final_result is None:
                    return None
                result_status = str(final_job_status or final_result.get("status") or "error")
                cursor = connection.execute(
                    "INSERT INTO results(session_id,job_id,status,result_json,created_at) VALUES(?,?,?,?,?)",
                    (attempt["session_id"], attempt["job_id"], result_status, canonical_json(final_result), now),
                )
                result_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE jobs SET status=?,final_result_id=?,updated_at=? WHERE session_id=? AND job_id=?",
                    (result_status, result_id, now, attempt["session_id"], attempt["job_id"]),
                )
                return result_id

            return self._transaction(connection, transaction)

        return self._write(write)

    def record_terminal_result(self, session_id: str, job_id: str, status: str, result: dict[str, Any]) -> int:
        if status not in {"error", "cancelled"}:
            raise ValueError("terminal status must be error or cancelled")
        now = utc_now()

        def write(connection: sqlite3.Connection) -> int:
            def transaction() -> int:
                job = connection.execute(
                    "SELECT status,final_result_id FROM jobs WHERE session_id=? AND job_id=?",
                    (session_id, job_id),
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                if job["status"] != "pending":
                    return int(job["final_result_id"] or 0)
                cursor = connection.execute(
                    "INSERT INTO results(session_id,job_id,status,result_json,created_at) VALUES(?,?,?,?,?)",
                    (session_id, job_id, status, canonical_json(result), now),
                )
                result_id = int(cursor.lastrowid)
                connection.execute(
                    "UPDATE jobs SET status=?,final_result_id=?,updated_at=? WHERE session_id=? AND job_id=? AND status='pending'",
                    (status, result_id, now, session_id, job_id),
                )
                return result_id

            return self._transaction(connection, transaction)

        return self._write(write)

    def reconcile_incomplete_attempts(self, session_id: str, max_attempts: int) -> dict[str, int]:
        now = utc_now()

        def write(connection: sqlite3.Connection) -> dict[str, int]:
            def transaction() -> dict[str, int]:
                running = connection.execute(
                    "SELECT attempt_id,job_id,attempt_no FROM attempts WHERE session_id=? AND status='running' ORDER BY attempt_id",
                    (session_id,),
                ).fetchall()
                for attempt in running:
                    connection.execute(
                        "UPDATE attempts SET completed_at=?,status='error',stage='runtime',category='process_interrupted',"
                        "retryable=?,safe_message=? WHERE attempt_id=?",
                        (
                            now,
                            int(int(attempt["attempt_no"]) < int(max_attempts)),
                            "检测进程在收到完整响应前中断；该次HTTP尝试已计入总预算",
                            int(attempt["attempt_id"]),
                        ),
                    )
                exhausted = 0
                pending_rows = connection.execute(
                    "SELECT j.job_id,j.manifest_json,COUNT(a.attempt_id) AS attempts "
                    "FROM jobs j LEFT JOIN attempts a ON a.session_id=j.session_id AND a.job_id=j.job_id "
                    "WHERE j.session_id=? AND j.status='pending' GROUP BY j.job_id,j.manifest_json",
                    (session_id,),
                ).fetchall()
                for row in pending_rows:
                    attempts = int(row["attempts"] or 0)
                    if attempts < int(max_attempts):
                        continue
                    manifest = json.loads(row["manifest_json"])
                    result = {
                        **manifest,
                        "job_id": row["job_id"],
                        "probe_id": manifest.get("probe_id"),
                        "request_format": manifest.get("request_format"),
                        "context_mode": manifest.get("context_mode"),
                        "effort": manifest.get("effort"),
                        "cycle": manifest.get("cycle", 0),
                        "time": now,
                        "status": "error",
                        "attempts_sent": attempts,
                        "error": {
                            "stage": "runtime",
                            "category": "attempt_budget_exhausted_after_restart",
                            "retryable": False,
                            "http_status": None,
                            "attempt": attempts,
                            "safe_message": "恢复时发现该逻辑任务已耗尽HTTP尝试预算，未再次发送",
                        },
                    }
                    cursor = connection.execute(
                        "INSERT INTO results(session_id,job_id,status,result_json,created_at) VALUES(?,?,'error',?,?)",
                        (session_id, row["job_id"], canonical_json(result), now),
                    )
                    connection.execute(
                        "UPDATE jobs SET status='error',final_result_id=?,updated_at=? WHERE session_id=? AND job_id=?",
                        (int(cursor.lastrowid), now, session_id, row["job_id"]),
                    )
                    exhausted += 1
                return {"running_reconciled": len(running), "jobs_exhausted": exhausted}

            return self._transaction(connection, transaction)

        return self._write(write)

    def latest_results(self, session_id: str) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                "SELECT j.cycle,j.ordinal,j.manifest_json,j.status,r.result_json "
                "FROM jobs j LEFT JOIN results r ON r.result_id=j.final_result_id "
                "WHERE j.session_id=? ORDER BY j.cycle,j.ordinal",
                (session_id,),
            )
            result: list[dict[str, Any]] = []
            for row in rows:
                if row["result_json"]:
                    value = json.loads(row["result_json"])
                else:
                    value = {**json.loads(row["manifest_json"]), "status": row["status"]}
                value.setdefault("cycle", int(row["cycle"]))
                result.append(value)
            return result

        return self._read(read)

    def append_event(self, session_id: str, event_type: str, *, cycle: int | None = None, payload: dict[str, Any] | None = None) -> int:
        now = utc_now()

        def write(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "INSERT INTO events(session_id,event_type,cycle,payload_json,created_at) VALUES(?,?,?,?,?)",
                (session_id, event_type, cycle, canonical_json(payload or {}), now),
            )
            connection.commit()
            return int(cursor.lastrowid)

        return self._write(write)

    def events(self, session_id: str) -> list[dict[str, Any]]:
        def read(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            values = []
            for row in connection.execute(
                "SELECT event_id,event_type,cycle,payload_json,created_at FROM events WHERE session_id=? ORDER BY event_id",
                (session_id,),
            ):
                values.append({
                    "event_id": int(row["event_id"]),
                    "event": row["event_type"],
                    "cycle": row["cycle"],
                    "time": row["created_at"],
                    **json.loads(row["payload_json"]),
                })
            return values

        return self._read(read)

    def save_report(self, session_id: str, report: dict[str, Any]) -> None:
        now = utc_now()
        self._write(lambda connection: connection.execute(
            "UPDATE sessions SET report_json=?,report_updated_at=?,updated_at=? WHERE session_id=?",
            (canonical_json(report), now, now, session_id),
        ))

    def report(self, session_id: str) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT report_json FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

        return self._read(read)

    @staticmethod
    def _progress(connection: sqlite3.Connection, session_id: str) -> dict:
        session = connection.execute(
            "SELECT status,updated_at,stop_requested_at FROM sessions WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if session is None:
            raise KeyError(session_id)
        counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status,COUNT(*) AS count FROM jobs WHERE session_id=? GROUP BY status",
                (session_id,),
            )
        }
        attempts = connection.execute(
            "SELECT COUNT(*) AS total,SUM(CASE WHEN attempt_no>1 THEN 1 ELSE 0 END) AS retries,"
            "SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running FROM attempts WHERE session_id=?",
            (session_id,),
        ).fetchone()
        total_jobs = sum(counts.values())
        completed = sum(counts.get(key, 0) for key in ("ok", "error", "cancelled"))
        valid = connection.execute(
            "SELECT COUNT(*) FROM jobs j JOIN results r ON r.result_id=j.final_result_id "
            "WHERE j.session_id=? AND j.status='ok' AND json_extract(r.result_json,'$.category') != '__INVALID_OUTPUT__'",
            (session_id,)).fetchone()[0]
        return {
            "updated_at": session["updated_at"],
            "status": session["status"],
            "planned": total_jobs,
            "logical_completed": completed,
            "successful": counts.get("ok", 0),
            "valid_samples": valid,
            "errors": counts.get("error", 0),
            "cancelled": counts.get("cancelled", 0),
            "pending": counts.get("pending", 0),
            "http_attempts": int(attempts["total"] or 0),
            "retries": int(attempts["retries"] or 0),
            "in_flight": int(attempts["running"] or 0),
            "stop_requested_at": session["stop_requested_at"],
        }

    def progress(self, session_id: str) -> dict[str, Any]:
        return self._read(lambda connection: self._progress(connection, session_id))

    def session_summaries(self, limit: int = 100) -> list[dict]:
        def read(connection):
            rows = connection.execute(
                "SELECT session_id,kind,status,claimed_model,request_model,safe_endpoint,created_at,updated_at, "
                "json_extract(config_json,'$.project.id') AS project_id "
                "FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(row), **self._progress(connection, row["session_id"])} for row in rows]
        return self._read(read)

    def attempt_details(self, session_id: str) -> list[dict[str, Any]]:
        return self._read(lambda connection: [dict(row) for row in connection.execute(
            "SELECT a.attempt_id,a.job_id,a.attempt_no,a.started_at,a.completed_at,a.status,a.stage,a.category,"
            "a.retryable,a.http_status,a.safe_message,j.manifest_json "
            "FROM attempts a JOIN jobs j ON j.session_id=a.session_id AND j.job_id=a.job_id "
            "WHERE a.session_id=? ORDER BY a.attempt_id",
            (session_id,),
        )])

    def integrity_check(self) -> str:
        return self._read(lambda connection: str(connection.execute("PRAGMA integrity_check").fetchone()[0]))

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
        self._writer.join(timeout=10)

    def put_document(self, kind: str, identity: str, value: dict[str, Any]) -> None:
        body = canonical_json(value)
        self._write(lambda connection: connection.execute(
            "INSERT INTO documents(kind,id,body_json,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET body_json=excluded.body_json,updated_at=excluded.updated_at",
            (kind, identity, body, utc_now()),
        ))

    def documents(self, kind: str) -> list[dict[str, Any]]:
        return self._read(lambda connection: [json.loads(row[0]) for row in connection.execute(
            "SELECT body_json FROM documents WHERE kind=? ORDER BY updated_at DESC,id", (kind,),
        )])

    def document_ids(self, kind: str) -> list[str]:
        return self._read(lambda connection: [row[0] for row in connection.execute(
            "SELECT id FROM documents WHERE kind=? ORDER BY updated_at DESC,id", (kind,))])

    def document(self, kind: str, identity: str) -> dict[str, Any] | None:
        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            row = connection.execute("SELECT body_json FROM documents WHERE kind=? AND id=?",
                                     (kind, identity)).fetchone()
            return json.loads(row[0]) if row else None
        return self._read(read)

    def delete_document(self, kind: str, identity: str) -> None:
        self._write(lambda connection: connection.execute(
            "DELETE FROM documents WHERE kind=? AND id=?", (kind, identity),
        ))

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._read(lambda connection: [dict(row) for row in connection.execute(
            "SELECT session_id,kind,status,claimed_model,request_model,safe_endpoint,created_at,"
            "updated_at FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,),
        )])

    def collection_sessions(self, project_id: str) -> list[dict[str, Any]]:
        return self._read(lambda connection: [dict(row) for row in connection.execute(
            "SELECT session_id,config_json,status,created_at,updated_at FROM sessions "
            "WHERE kind='collection' AND json_extract(config_json,'$.project.id')=? ORDER BY created_at", (project_id,))])

    def retained_exchanges(self, session_id: str, after: int = 0, limit: int = 10) -> dict:
        return self._read(lambda connection: {"records": [
            {key: row[key] for key in ("attempt_id", "job_id", "attempt_no", "status")} |
            {"exchange": json.loads(row["body_json"])}
            for row in connection.execute(
                "SELECT a.attempt_id,a.job_id,a.attempt_no,a.status,r.body_json FROM attempts a "
                "JOIN retained_exchanges r ON r.attempt_id=a.attempt_id "
                "WHERE a.session_id=? AND a.attempt_id>? ORDER BY a.attempt_id LIMIT ?",
                (session_id, after, limit))], "coverage": dict(connection.execute(
                "SELECT COUNT(*) AS attempts,COUNT(r.attempt_id) AS retained FROM attempts a "
                "LEFT JOIN retained_exchanges r ON r.attempt_id=a.attempt_id WHERE a.session_id=?",
                (session_id,)).fetchone())})

    def __enter__(self) -> "SQLiteStateStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


__all__ = ["SCHEMA_VERSION", "SQLiteStateStore"]
