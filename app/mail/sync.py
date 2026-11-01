"""通过 IMAP 只读地同步邮件到本地。

交大邮箱（Zimbra）IMAP：mail.sjtu.edu.cn:993 SSL。
所有文件夹都用 EXAMINE（只读）打开，正文用 BODY.PEEK 获取，不会把邮件标为已读。
"""
import base64
import email
import email.utils
import imaplib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from html.parser import HTMLParser

from ..core import store

imaplib.Commands.setdefault("ID", ("NONAUTH", "AUTH", "SELECTED"))
imaplib._MAXLINE = 10_000_000

SENT_NAMES = ["sent", "sent items", "sent messages", "已发送", "已发送邮件", "发件箱"]
BULK_SENDER = re.compile(r"(no-?reply|do-?not-?reply|notification|newsletter|mailer-daemon|postmaster|bounce)", re.I)
BODY_LIMIT = 20000


# ---------------------------------------------------------------- 工具函数

def imap_utf7_decode(s: str) -> str:
    """IMAP 修改版 UTF-7（RFC 3501）解码，用于中文文件夹名。"""
    out, i = [], 0
    while i < len(s):
        if s[i] == "&":
            j = s.index("-", i)
            if j == i + 1:
                out.append("&")
            else:
                b64 = s[i + 1:j].replace(",", "/")
                b64 += "=" * (-len(b64) % 4)
                out.append(base64.b64decode(b64).decode("utf-16-be"))
            i = j + 1
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


class _HTMLText(HTMLParser):
    SKIP = {"script", "style", "head"}
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    try:
        p.feed(html)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return "".join(p.parts)


def _clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _decode_bytes(payload: bytes, charset) -> str:
    charset = (charset or "utf-8").lower()
    if charset in ("gb2312", "gbk", "gb_2312-80", "x-gbk"):
        charset = "gb18030"
    for cs in (charset, "utf-8", "gb18030"):
        try:
            return payload.decode(cs)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def _header(msg, name: str) -> str:
    try:
        v = msg.get(name)
        return str(v) if v is not None else ""
    except Exception:
        raw = msg.get_all(name, failobj=[""])[0] if hasattr(msg, "get_all") else ""
        try:
            return str(make_header(decode_header(str(raw))))
        except Exception:
            return str(raw)


def _addresses(value: str) -> list:
    return [(n, a.lower()) for n, a in email.utils.getaddresses([value]) if a]


def parse_message(raw: bytes) -> dict:
    """把原始邮件解析成结构化字段。"""
    msg = email.message_from_bytes(raw, policy=policy.default)
    subject = _header(msg, "Subject")
    from_list = _addresses(_header(msg, "From"))
    from_name, from_addr = from_list[0] if from_list else ("", "")
    to_addrs = ", ".join(a for _, a in _addresses(_header(msg, "To")))
    cc_addrs = ", ".join(a for _, a in _addresses(_header(msg, "Cc")))

    try:
        dt = email.utils.parsedate_to_datetime(_header(msg, "Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    except Exception:
        dt = datetime.now(timezone.utc)

    plain, html, attachments = [], [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if filename or part.get_content_disposition() == "attachment":
            attachments.append(str(filename or "未命名附件"))
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:
            continue
        text = _decode_bytes(payload, part.get_content_charset())
        (plain if ctype == "text/plain" else html).append(text)

    body = "\n".join(plain) if plain else html_to_text("\n".join(html))
    body = _clean_text(body)[:BODY_LIMIT]

    precedence = _header(msg, "Precedence").lower()
    auto = _header(msg, "Auto-Submitted").lower()
    is_bulk = bool(
        msg.get("List-Unsubscribe") or msg.get("List-Id")
        or precedence in ("bulk", "list", "junk")
        or (auto and auto != "no")
        or BULK_SENDER.search(from_addr or "")
    )

    return {
        "message_id": _header(msg, "Message-ID").strip(),
        "in_reply_to": _header(msg, "In-Reply-To").strip(),
        "refs": _header(msg, "References").strip(),
        "subject": subject.strip() or "(无主题)",
        "from_name": from_name,
        "from_addr": from_addr,
        "to_addrs": to_addrs,
        "cc_addrs": cc_addrs,
        "date": dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M"),
        "ts": int(dt.timestamp()),
        "is_bulk": 1 if is_bulk else 0,
        "body": body,
        "attachments": attachments,
    }


def my_address(cfg: dict) -> str:
    m = cfg["mail"]
    addr = (m.get("address") or m.get("username") or "").strip().lower()
    if addr and "@" not in addr:
        addr += "@sjtu.edu.cn"
    return addr


# ---------------------------------------------------------------- IMAP

def connect(cfg: dict) -> imaplib.IMAP4_SSL:
    m = cfg["mail"]
    if not m.get("username") or not m.get("password"):
        raise ValueError("请先在设置中填写邮箱用户名和密码")
    conn = imaplib.IMAP4_SSL(m["host"], int(m["port"]), timeout=60)
    conn.login(m["username"], m["password"])
    try:  # 部分服务器要求客户端先发送 ID
        conn._simple_command("ID", '("name" "sjtu-mail-agent" "version" "0.1")')
    except Exception:
        pass
    return conn


LIST_RE = re.compile(r'\((?P<flags>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)')


def list_folders(conn) -> list:
    typ, data = conn.list()
    folders = []
    for line in data or []:
        if isinstance(line, tuple):
            line = line[0] + b" " + line[1]
        mo = LIST_RE.match(line.decode("utf-8", "replace"))
        if not mo:
            continue
        name = mo.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        flags = mo.group("flags")
        if "\\Noselect" in flags:
            continue
        folders.append({"raw": name, "name": imap_utf7_decode(name), "flags": flags})
    return folders


def find_sent_folder(folders: list):
    for f in folders:
        if "\\Sent" in f["flags"]:
            return f
    for f in folders:
        if f["name"].lower() in SENT_NAMES:
            return f
    return None


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _fetch_flags(conn, uids: list) -> dict:
    """返回 {uid: (flags, size)}"""
    out = {}
    for i in range(0, len(uids), 500):
        chunk = ",".join(str(u) for u in uids[i:i + 500])
        typ, data = conn.uid("FETCH", chunk, "(UID FLAGS RFC822.SIZE)")
        for item in data or []:
            if isinstance(item, tuple):
                item = item[0]
            if not isinstance(item, bytes):
                continue
            mu = re.search(rb"UID (\d+)", item)
            mf = re.search(rb"FLAGS \(([^)]*)\)", item)
            ms = re.search(rb"RFC822\.SIZE (\d+)", item)
            if mu:
                out[int(mu.group(1))] = (
                    mf.group(1).decode() if mf else "",
                    int(ms.group(1)) if ms else 0,
                )
    return out


def _fetch_raw(conn, uids: list, part: str) -> dict:
    """返回 {uid: raw_bytes}"""
    out = {}
    typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), f"(UID {part})")
    for item in data or []:
        if isinstance(item, tuple) and len(item) == 2:
            mu = re.search(rb"UID (\d+)", item[0])
            if mu:
                out[int(mu.group(1))] = item[1]
    return out


def fetch_raw_by_uid(cfg: dict, folder: str, uid: int) -> bytes:
    """按 UID 从服务器取回原始邮件（用于下载 .eml）。"""
    conn = connect(cfg)
    try:
        conn.select(_quote(folder), readonly=True)
        return _fetch_raw(conn, [uid], "BODY.PEEK[]").get(uid, b"")
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 同步任务

SYNC_STATUS = {"running": False, "message": "尚未同步", "done": 0, "total": 0, "error": None}
_lock = threading.Lock()


def _set(**kw):
    with _lock:
        SYNC_STATUS.update(kw)


def sync_folder(conn, db, folder: dict, *, is_sent_folder: bool, days: int, limit: int,
                max_bytes: int, me: str):
    raw_name = folder["raw"]
    typ, _ = conn.select(_quote(raw_name), readonly=True)
    if typ != "OK":
        return 0
    uidvalidity = int((conn.response("UIDVALIDITY")[1] or [b"0"])[0] or 0)
    store.drop_stale_folder(db, raw_name, uidvalidity)

    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
    typ, data = conn.uid("SEARCH", None, "SINCE", since)
    uids = [int(x) for x in (data[0] or b"").split()][-limit:]
    if not uids:
        return 0

    meta = _fetch_flags(conn, uids)
    have = store.existing_uids(db, raw_name, uidvalidity)
    for uid in have & set(uids):
        store.update_flags(db, raw_name, uidvalidity, uid, meta.get(uid, ("", 0))[0])

    new = [u for u in uids if u not in have]
    _set(total=SYNC_STATUS["total"] + len(new))
    small = [u for u in new if meta.get(u, ("", 0))[1] <= max_bytes]
    big = [u for u in new if u not in set(small)]

    def save(uid, raw, truncated=False):
        try:
            m = parse_message(raw)
        except Exception as e:  # 单封解析失败不影响整体
            m = {"subject": f"(解析失败: {e})", "body": "", "ts": int(time.time()), "attachments": []}
        if truncated:
            m["body"] = f"（邮件较大，约 {meta[uid][1] // 1024 // 1024} MB，仅同步了邮件头，正文请在网页邮箱查看）"
        m.update({
            "folder": raw_name, "uidvalidity": uidvalidity, "uid": uid,
            "flags": meta.get(uid, ("", 0))[0], "size": meta.get(uid, ("", 0))[1],
            "is_sent": 1 if (is_sent_folder or (me and m.get("from_addr") == me)) else 0,
        })
        store.insert_message(db, m)

    for i in range(0, len(small), 25):
        chunk = small[i:i + 25]
        for uid, raw in _fetch_raw(conn, chunk, "BODY.PEEK[]").items():
            save(uid, raw)
        db.commit()
        _set(done=SYNC_STATUS["done"] + len(chunk), message=f"正在同步 {folder['name']} …")
    for i in range(0, len(big), 25):
        chunk = big[i:i + 25]
        for uid, raw in _fetch_raw(conn, chunk, "BODY.PEEK[HEADER]").items():
            save(uid, raw, truncated=True)
        db.commit()
        _set(done=SYNC_STATUS["done"] + len(chunk))
    return len(new)


def run_sync(cfg: dict, extra_folders=None):
    if SYNC_STATUS["running"]:
        return
    _set(running=True, done=0, total=0, error=None, message="正在连接邮箱 …")
    try:
        conn = connect(cfg)
        try:
            folders = list_folders(conn)
            targets = []
            inbox = next((f for f in folders if f["raw"].upper() == "INBOX"), {"raw": "INBOX", "name": "INBOX", "flags": ""})
            targets.append((inbox, False))
            sent = find_sent_folder(folders)
            if sent:
                targets.append((sent, True))
            for name in extra_folders or []:
                f = next((f for f in folders if f["name"] == name or f["raw"] == name), None)
                if f and f not in [t[0] for t in targets]:
                    targets.append((f, False))

            s = cfg["sync"]
            me = my_address(cfg)
            added = 0
            with store.connect() as db:
                for f, is_sent in targets:
                    added += sync_folder(
                        conn, db, f, is_sent_folder=is_sent, days=int(s["days"]),
                        limit=int(s["max_per_folder"]),
                        max_bytes=int(float(s["max_message_mb"]) * 1024 * 1024), me=me,
                    )
            names = "、".join(f["name"] for f, _ in targets)
            _set(message=f"同步完成：{names}，新增 {added} 封")
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        _set(error=f"IMAP 错误：{e}（请检查用户名/密码，或在网页邮箱设置中确认已开启 IMAP）", message="同步失败")
    except Exception as e:
        _set(error=f"{type(e).__name__}: {e}", message="同步失败")
    finally:
        _set(running=False)


def test_connection(cfg: dict) -> dict:
    conn = connect(cfg)
    try:
        folders = list_folders(conn)
        sent = find_sent_folder(folders)
        return {"ok": True, "folders": [f["name"] for f in folders], "sent_folder": sent["name"] if sent else None}
    finally:
        conn.logout()
