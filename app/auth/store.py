"""SQLite persistence for accounts, verification codes, and sessions."""

from __future__ import annotations

import hmac
import math
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import time

from .models import Account


class AccountExistsError(ValueError):
    pass


class VerificationRateLimitError(ValueError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Verification code requested too frequently.")
        self.retry_after = retry_after


class VerificationCodeError(ValueError):
    pass


class AccountStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    verified_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_codes (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL COLLATE NOCASE,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    consumed_at REAL
                );
                CREATE INDEX IF NOT EXISTS ix_verification_email_purpose
                    ON verification_codes(email, purpose, created_at DESC);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_sessions_account
                    ON sessions(account_id, expires_at);
                """
            )

    def count_accounts(self) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM accounts").fetchone()
            return int(row["count"])

    def account_by_email(self, email: str) -> tuple[Account, str] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, email, password_hash, created_at, verified_at FROM accounts WHERE email = ?",
                (email,),
            ).fetchone()
            if row is None:
                return None
            return _account(row), str(row["password_hash"])

    def create_verification_code(
        self,
        email: str,
        code_hash: str,
        *,
        ttl_seconds: int,
        resend_seconds: int,
        max_per_hour: int,
    ) -> str:
        current = time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
                raise AccountExistsError(email)
            latest = connection.execute(
                """
                SELECT created_at FROM verification_codes
                WHERE email = ? AND purpose = 'register' AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (email,),
            ).fetchone()
            if latest is not None:
                retry_after = math.ceil(resend_seconds - (current - float(latest["created_at"])))
                if retry_after > 0:
                    raise VerificationRateLimitError(retry_after)
            hourly = connection.execute(
                """
                SELECT COUNT(*) AS count, MIN(created_at) AS oldest
                FROM verification_codes
                WHERE email = ? AND purpose = 'register' AND created_at >= ?
                """,
                (email, current - 3600),
            ).fetchone()
            if int(hourly["count"]) >= max_per_hour:
                retry_after = math.ceil(3600 - (current - float(hourly["oldest"])))
                raise VerificationRateLimitError(max(1, retry_after))
            code_id = f"verify_{uuid.uuid4().hex}"
            connection.execute(
                """
                UPDATE verification_codes SET consumed_at = ?
                WHERE email = ? AND purpose = 'register' AND consumed_at IS NULL
                """,
                (current, email),
            )
            connection.execute(
                """
                INSERT INTO verification_codes
                    (id, email, purpose, code_hash, created_at, expires_at)
                VALUES (?, ?, 'register', ?, ?, ?)
                """,
                (code_id, email, code_hash, current, current + ttl_seconds),
            )
            return code_id

    def invalidate_verification_code(self, code_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE verification_codes SET consumed_at = ? WHERE id = ?",
                (time(), code_id),
            )

    def register_account(
        self,
        email: str,
        password_hash: str,
        code_hash: str,
        *,
        max_attempts: int,
    ) -> tuple[Account, bool]:
        current = time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM accounts WHERE email = ?", (email,)).fetchone():
                raise AccountExistsError(email)
            is_first_account = not connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
            row = connection.execute(
                """
                SELECT id, code_hash, expires_at, attempts FROM verification_codes
                WHERE email = ? AND purpose = 'register' AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1
                """,
                (email,),
            ).fetchone()
            if row is None or float(row["expires_at"]) < current:
                raise VerificationCodeError("expired")
            attempts = int(row["attempts"])
            if attempts >= max_attempts:
                raise VerificationCodeError("attempts")
            if not hmac.compare_digest(str(row["code_hash"]), code_hash):
                connection.execute(
                    "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?",
                    (row["id"],),
                )
                connection.commit()
                raise VerificationCodeError("invalid")
            account = Account(
                id=f"user_{uuid.uuid4().hex[:20]}",
                email=email,
                created_at=current,
                verified_at=current,
            )
            connection.execute(
                """
                INSERT INTO accounts (id, email, password_hash, created_at, verified_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (account.id, account.email, password_hash, account.created_at, account.verified_at),
            )
            connection.execute(
                "UPDATE verification_codes SET consumed_at = ? WHERE id = ?",
                (current, row["id"]),
            )
            return account, is_first_account

    def create_session(self, account_id: str, token_hash: str, ttl_seconds: int) -> float:
        current = time()
        expires_at = current + ttl_seconds
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (current,))
            connection.execute(
                """
                INSERT INTO sessions (token_hash, account_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token_hash, account_id, current, expires_at),
            )
        return expires_at

    def account_for_session(self, token_hash: str) -> Account | None:
        current = time()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT accounts.id, accounts.email, accounts.created_at, accounts.verified_at,
                       sessions.expires_at
                FROM sessions
                JOIN accounts ON accounts.id = sessions.account_id
                WHERE sessions.token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if float(row["expires_at"]) <= current:
                connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return None
            return _account(row)

    def revoke_session(self, token_hash: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def _account(row: sqlite3.Row) -> Account:
    return Account(
        id=str(row["id"]),
        email=str(row["email"]),
        created_at=float(row["created_at"]),
        verified_at=float(row["verified_at"]),
    )
