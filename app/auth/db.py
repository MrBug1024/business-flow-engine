"""系统数据的 SQLite 存储（用户 / OAuth 身份）。

数据文件落在 `system/app.db`，随项目走、gitignore，不依赖任何外部数据库服务。
业务场景仍是文件存储；这里只承载用户体系等系统数据，轻量、易迁移。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from app.core.config import settings

_LOCK = threading.RLock()
_DB_PATH: Optional[Path] = None


def _db_file() -> Path:
    return settings.system_path / "app.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_file(), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    """建表（幂等）。应用启动时调用一次。"""
    global _DB_PATH
    _DB_PATH = _db_file()
    with _LOCK, _connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT DEFAULT '',
                name          TEXT DEFAULT '',
                avatar        TEXT DEFAULT '',
                provider      TEXT DEFAULT 'password',
                created_at    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_identities (
                provider     TEXT NOT NULL,
                provider_uid TEXT NOT NULL,
                user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                PRIMARY KEY (provider, provider_uid)
            );
            """
        )
        con.commit()


def _new_user_id() -> str:
    return f"u_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------- users
def create_user(email: str, password_hash: str = "", name: str = "",
                avatar: str = "", provider: str = "password") -> dict:
    uid = _new_user_id()
    with _LOCK, _connect() as con:
        con.execute(
            "INSERT INTO users(id,email,password_hash,name,avatar,provider,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (uid, email.strip().lower(), password_hash, name or email.split("@")[0],
             avatar, provider, time.time()),
        )
        con.commit()
    return get_user(uid)


def get_user(user_id: str) -> Optional[dict]:
    with _LOCK, _connect() as con:
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> Optional[dict]:
    with _LOCK, _connect() as con:
        row = con.execute("SELECT * FROM users WHERE email=?", (email.strip().lower(),)).fetchone()
    return dict(row) if row else None


def count_users() -> int:
    with _LOCK, _connect() as con:
        return con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


# ----------------------------------------------------------- oauth identities
def get_user_by_oauth(provider: str, provider_uid: str) -> Optional[dict]:
    with _LOCK, _connect() as con:
        row = con.execute(
            "SELECT u.* FROM users u JOIN oauth_identities o ON o.user_id=u.id"
            " WHERE o.provider=? AND o.provider_uid=?",
            (provider, str(provider_uid)),
        ).fetchone()
    return dict(row) if row else None


def link_oauth(provider: str, provider_uid: str, user_id: str) -> None:
    with _LOCK, _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO oauth_identities(provider,provider_uid,user_id) VALUES(?,?,?)",
            (provider, str(provider_uid), user_id),
        )
        con.commit()
