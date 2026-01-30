import os
import random
import threading
from typing import Optional, List, Tuple

import libsql

from config import TURSO_DATABASE_URL, TURSO_AUTH_TOKEN

LOCAL_REPLICA_PATH = "local.db"  # embedded replica in container

_lock = threading.Lock()
_conn = None


def _normalize_turso_url(url: str) -> str:
    url = url.strip()
    if url.startswith("https://"):
        return "libsql://" + url[len("https://") :]
    if url.startswith("http://"):
        return "libsql://" + url[len("http://") :]
    return url


def _get_conn():
    global _conn
    if _conn is None:
        sync_url = _normalize_turso_url(TURSO_DATABASE_URL)
        _conn = libsql.connect(
            LOCAL_REPLICA_PATH,
            sync_url=sync_url,
            auth_token=TURSO_AUTH_TOKEN,
            sync_interval=60,  # background sync
        )
        # ❌ startup এ conn.sync() করবো না (hang হতে পারে)
    return _conn


def init_db():
    with _lock:
        conn = _get_conn()

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
              telegram_id INTEGER PRIMARY KEY,
              chat_id INTEGER NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS emails(
              address TEXT PRIMARY KEY,
              telegram_id INTEGER NOT NULL,
              base_name TEXT NOT NULL,
              is_active INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS name_pool(
              name TEXT PRIMARY KEY,
              used INTEGER NOT NULL DEFAULT 0,
              used_by INTEGER,
              used_email TEXT,
              used_at TEXT
            )
            """
        )

        conn.commit()


def upsert_user(telegram_id: int, chat_id: int):
    with _lock:
        conn = _get_conn()
        conn.execute(
            """
            INSERT INTO users(telegram_id, chat_id)
            VALUES(?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET chat_id=excluded.chat_id
            """,
            (telegram_id, chat_id),
        )
        conn.commit()


def seed_names_from_file(path: str = "name.txt") -> int:
    """
    name.txt থেকে নাম ঢোকাবে (INSERT OR IGNORE)
    """
    if not os.path.exists(path):
        return 0

    names: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            n = line.strip()
            if not n:
                continue
            n = n.lower()
            n = "".join(ch for ch in n if ch.isalnum() or ch == "_")
            if n:
                names.append(n)

    if not names:
        return 0

    with _lock:
        conn = _get_conn()
        for n in names:
            conn.execute("INSERT OR IGNORE INTO name_pool(name, used) VALUES(?, 0)", (n,))
        conn.commit()
        return len(names)


def create_email_for_user(telegram_id: int, domain: str) -> Optional[str]:
    """
    ✅ unused নাম random pick
    ✅ name একবারই ব্যবহার হবে (global)
    ✅ email: name + 4digit + @domain
    """
    domain = domain.strip().lower()

    with _lock:
        conn = _get_conn()

        row = conn.execute(
            "SELECT name FROM name_pool WHERE used=0 ORDER BY RANDOM() LIMIT 1"
        ).fetchone()

        if not row:
            return None

        base = row[0]

        for _ in range(30):
            suffix = random.randint(1000, 9999)
            address = f"{base}{suffix}@{domain}".lower()

            try:
                conn.execute("BEGIN")

                conn.execute(
                    """
                    INSERT INTO emails(address, telegram_id, base_name, is_active)
                    VALUES(?, ?, ?, 1)
                    """,
                    (address, telegram_id, base),
                )

                cur = conn.execute(
                    """
                    UPDATE name_pool
                    SET used=1, used_by=?, used_email=?, used_at=datetime('now')
                    WHERE name=? AND used=0
                    """,
                    (telegram_id, address, base),
                )

                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    continue

                conn.commit()
                return address

            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                continue

        return None


def deactivate_email(address: str, telegram_id: int) -> bool:
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            """
            UPDATE emails SET is_active=0
            WHERE address=? AND telegram_id=?
            """,
            (address.lower(), telegram_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_user_by_address(address: str) -> Optional[int]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            """
            SELECT telegram_id FROM emails
            WHERE address=? AND is_active=1
            LIMIT 1
            """,
            (address.lower(),),
        ).fetchone()
        return row[0] if row else None


def get_chat_id(telegram_id: int) -> Optional[int]:
    with _lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT chat_id FROM users WHERE telegram_id=? LIMIT 1",
            (telegram_id,),
        ).fetchone()
        return row[0] if row else None


def list_emails(telegram_id: int, limit: int = 20) -> List[Tuple[str, int, str]]:
    with _lock:
        conn = _get_conn()
        rows = conn.execute(
            """
            SELECT address, is_active, created_at FROM emails
            WHERE telegram_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        ).fetchall()
        return rows
