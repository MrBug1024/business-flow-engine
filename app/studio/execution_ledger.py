"""Durable idempotency ledger for model-initiated capability calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from time import time
from typing import Any

from app.studio.capability_runtime import CapabilityResult


_PROCESS_OWNER = uuid.uuid4().hex


class ToolExecutionCollision(RuntimeError):
    """A provider reused a tool call id with different input."""


class ToolExecutionInProgress(RuntimeError):
    """The same call is already executing in this process."""


class ToolExecutionUncertain(RuntimeError):
    """A non-idempotent call may have produced an external side effect."""


class ToolExecutionLedger:
    """Persist capability outcomes before graph checkpoints are committed."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._setup()

    def execute_once(
        self,
        *,
        scope: str,
        call_id: str,
        capability_name: str,
        arguments: dict[str, Any],
        retry_safe: bool,
        executor: Callable[[], CapabilityResult],
    ) -> tuple[CapabilityResult, bool]:
        """Execute once, replaying a durable success for the same call id."""

        normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
        arguments_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        now = time()

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT capability_name, arguments_hash, status, result_json, error, owner_id
                FROM tool_execution_ledger
                WHERE scope = ? AND call_id = ?
                """,
                (scope, call_id),
            ).fetchone()
            if row is not None:
                stored_name, stored_hash, status, result_json, error, owner_id = row
                if stored_name != capability_name or stored_hash != arguments_hash:
                    raise ToolExecutionCollision(
                        f"Tool call id {call_id} was reused with different capability input."
                    )
                if status == "succeeded" and result_json:
                    conn.commit()
                    return _decode_result(result_json), True
                if status == "running" and owner_id == _PROCESS_OWNER:
                    raise ToolExecutionInProgress(f"Tool call {call_id} is already running.")
                if status in {"running", "failed"} and retry_safe:
                    conn.execute(
                        """
                        UPDATE tool_execution_ledger
                        SET status = 'running', owner_id = ?, error = '', updated_at = ?, attempts = attempts + 1
                        WHERE scope = ? AND call_id = ?
                        """,
                        (_PROCESS_OWNER, now, scope, call_id),
                    )
                    conn.commit()
                else:
                    raise ToolExecutionUncertain(
                        error
                        or f"Tool call {call_id} has uncertain side effects and requires reconciliation."
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO tool_execution_ledger (
                        scope, call_id, capability_name, arguments_hash, arguments_json,
                        status, result_json, error, owner_id, attempts, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', '', '', ?, 1, ?, ?)
                    """,
                    (
                        scope,
                        call_id,
                        capability_name,
                        arguments_hash,
                        normalized,
                        _PROCESS_OWNER,
                        now,
                        now,
                    ),
                )
                conn.commit()

        try:
            result = executor()
        except Exception as exc:
            status = "failed" if retry_safe else "uncertain"
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    UPDATE tool_execution_ledger
                    SET status = ?, error = ?, updated_at = ?
                    WHERE scope = ? AND call_id = ?
                    """,
                    (status, str(exc)[:4000], time(), scope, call_id),
                )
                conn.commit()
            raise

        encoded = json.dumps(
            {
                "output": result.output,
                "summary": result.summary,
                "emitted_events": result.emitted_events,
            },
            ensure_ascii=False,
            default=str,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tool_execution_ledger
                SET status = 'succeeded', result_json = ?, error = '', updated_at = ?
                WHERE scope = ? AND call_id = ?
                """,
                (encoded, time(), scope, call_id),
            )
            conn.commit()
        return result, False

    def delete_scope(self, scope: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM tool_execution_ledger WHERE scope = ?", (scope,))
            conn.commit()

    def _setup(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_execution_ledger (
                    scope TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope, call_id)
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)


def _decode_result(value: str) -> CapabilityResult:
    payload = json.loads(value)
    return CapabilityResult(
        output=payload.get("output") or {},
        summary=str(payload.get("summary") or ""),
        emitted_events=list(payload.get("emitted_events") or []),
    )
