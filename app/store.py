"""本地 SQLite 邮件缓存。"""
import json
import sqlite3
import time
from contextlib import contextmanager

from .config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    message_id TEXT,
    in_reply_to TEXT,
    refs TEXT,
    subject TEXT,
    from_name TEXT,
    from_addr TEXT,
    to_addrs TEXT,
    cc_addrs TEXT,
    date TEXT,
    ts INTEGER,
    flags TEXT,
    is_sent INTEGER DEFAULT 0,
    is_bulk INTEGER DEFAULT 0,
    body TEXT,
    attachments TEXT,
    size INTEGER,
    UNIQUE(folder, uidvalidity, uid)
);
CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_msgid ON messages(message_id);
CREATE TABLE IF NOT EXISTS results (
    kind TEXT PRIMARY KEY,
    payload TEXT,
    created_at INTEGER
);
"""

COLUMNS = ["folder", "uidvalidity", "uid", "message_id", "in_reply_to", "refs", "subject",
           "from_name", "from_addr", "to_addrs", "cc_addrs", "date", "ts", "flags",
           "is_sent", "is_bulk", "body", "attachments", "size"]


@contextmanager
def connect():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row) -> dict:
    d = dict(row)
    d["attachments"] = json.loads(d.get("attachments") or "[]")
    return d


def existing_uids(conn, folder: str, uidvalidity: int) -> set:
    rows = conn.execute("SELECT uid FROM messages WHERE folder=? AND uidvalidity=?",
                        (folder, uidvalidity)).fetchall()
    return {r["uid"] for r in rows}


def drop_stale_folder(conn, folder: str, uidvalidity: int):
    """UIDVALIDITY 变化说明服务器重建了文件夹，旧缓存作废。"""
    conn.execute("DELETE FROM messages WHERE folder=? AND uidvalidity<>?", (folder, uidvalidity))


def insert_message(conn, m: dict):
    vals = [m.get(c) for c in COLUMNS]
    vals[COLUMNS.index("attachments")] = json.dumps(m.get("attachments") or [], ensure_ascii=False)
    conn.execute(
        f"INSERT OR REPLACE INTO messages ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})",
        vals,
    )


def update_flags(conn, folder: str, uidvalidity: int, uid: int, flags: str):
    conn.execute("UPDATE messages SET flags=? WHERE folder=? AND uidvalidity=? AND uid=?",
                 (flags, folder, uidvalidity, uid))


def get_message(msg_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        return row_to_dict(row) if row else None


def get_messages(ids) -> list:
    ids = [int(i) for i in ids]
    if not ids:
        return []
    with connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def recent_messages(days: int, *, sent=None, include_bulk=True) -> list:
    since = int(time.time()) - days * 86400
    sql = "SELECT * FROM messages WHERE ts>=?"
    args = [since]
    if sent is not None:
        sql += " AND is_sent=?"
        args.append(1 if sent else 0)
    if not include_bulk:
        sql += " AND is_bulk=0"
    sql += " ORDER BY ts DESC"
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


def stats() -> dict:
    with connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) n, SUM(is_sent) sent, MIN(ts) oldest, MAX(ts) newest FROM messages"
        ).fetchone()
        folders = conn.execute(
            "SELECT folder, COUNT(*) n FROM messages GROUP BY folder ORDER BY n DESC"
        ).fetchall()
        return {
            "total": r["n"] or 0,
            "sent": r["sent"] or 0,
            "oldest": r["oldest"],
            "newest": r["newest"],
            "folders": [dict(f) for f in folders],
        }


def save_result(kind: str, payload: dict):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO results VALUES (?,?,?)",
                     (kind, json.dumps(payload, ensure_ascii=False), int(time.time())))


def load_result(kind: str):
    with connect() as conn:
        row = conn.execute("SELECT payload, created_at FROM results WHERE kind=?", (kind,)).fetchone()
        if not row:
            return None
        return {"created_at": row["created_at"], **json.loads(row["payload"])}
