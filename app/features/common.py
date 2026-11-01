"""各项分析共用的工具：日期、隐私遮盖、邮件格式化、分批调用模型。

思路：先用规则在本地筛选/打分（便宜、可控），再把必要的邮件分批交给大模型，
最后在 Python 里做去重和数值汇总（不让模型做算术）。
"""
import asyncio
import re
from datetime import datetime

from ..core.llm import LLMError, chat_json

BATCH_CHARS = 24000


def today() -> str:
    wd = "一二三四五六日"[datetime.now().weekday()]
    return datetime.now().strftime("%Y-%m-%d") + f"（星期{wd}）"


PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
CARD = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
QUOTE_MARKERS = re.compile(
    r"^(-{2,}\s*(Original Message|原始邮件|Forwarded message|转发邮件).*|On .+wrote:|在 .+写道[:：]|发件人[:：].+|From: .+)$",
    re.M | re.I,
)


def redact(text: str) -> str:
    text = IDCARD.sub("[身份证号已隐藏]", text)
    text = CARD.sub("[卡号已隐藏]", text)
    return PHONE.sub("[手机号已隐藏]", text)


def strip_quoted(text: str) -> str:
    """去掉回复里引用的历史邮件，节省 token。"""
    mo = QUOTE_MARKERS.search(text)
    if mo and mo.start() > 30:
        text = text[:mo.start()] + "\n[……以下为引用的历史邮件，已省略]"
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def fmt_email(m: dict, cfg: dict, max_chars=None) -> str:
    max_chars = max_chars or int(cfg["privacy"]["max_body_chars"])
    body = strip_quoted(m.get("body") or "")
    if cfg["privacy"]["redact"]:
        body = redact(body)
    if len(body) > max_chars:
        body = body[:max_chars] + " ……（已截断）"
    lines = [
        f"[邮件ID {m['id']}] {'我发出的' if m.get('is_sent') else '收到的'}{'（群发/通知）' if m.get('is_bulk') else ''}",
        f"日期: {m.get('date')}",
        f"发件人: {m.get('from_name') or ''} <{m.get('from_addr') or ''}>",
        f"收件人: {m.get('to_addrs') or ''}",
    ]
    if m.get("cc_addrs"):
        lines.append(f"抄送: {m['cc_addrs']}")
    lines.append(f"主题: {m.get('subject')}")
    if m.get("attachments"):
        lines.append("附件: " + "、".join(m["attachments"]))
    lines.append("正文:\n" + body)
    return "\n".join(lines)


def make_batches(msgs: list, cfg: dict, max_chars=None) -> list:
    """按字符预算把邮件切成多批，返回 [(text, ids)]。"""
    out, cur, ids, size = [], [], [], 0
    for m in msgs:
        t = fmt_email(m, cfg, max_chars)
        if cur and size + len(t) > BATCH_CHARS:
            out.append(("\n\n=====\n\n".join(cur), ids))
            cur, ids, size = [], [], 0
        cur.append(t)
        ids.append(m["id"])
        size += len(t)
    if cur:
        out.append(("\n\n=====\n\n".join(cur), ids))
    return out


async def map_batches(cfg: dict, batches: list, system: str, user_tpl: str):
    """并发地对每一批调用模型，返回 (结果列表, 警告列表)。"""
    sem = asyncio.Semaphore(max(1, int(cfg["llm"].get("concurrency") or 3)))
    warnings = []

    async def one(i, text, ids):
        async with sem:
            try:
                res = await chat_json(cfg, system, user_tpl.format(emails=text))
                return res, set(ids)
            except LLMError as e:
                warnings.append(f"第 {i + 1} 批失败：{e}")
                return None, set(ids)

    results = await asyncio.gather(*(one(i, t, ids) for i, (t, ids) in enumerate(batches)))
    return [r for r in results if r[0] is not None], warnings


def brief(m: dict) -> dict:
    """返回给前端的邮件摘要信息 + 定位方式。"""
    return {
        "id": m["id"],
        "subject": m.get("subject"),
        "from": m.get("from_name") or m.get("from_addr"),
        "from_addr": m.get("from_addr"),
        "to": m.get("to_addrs"),
        "date": m.get("date"),
        "folder": m.get("folder"),
        "is_sent": m.get("is_sent"),
        "attachments": m.get("attachments") or [],
        "message_id": m.get("message_id"),
    }


def parse_ids(v) -> list:
    out = []
    for x in v if isinstance(v, list) else [v]:
        try:
            out.append(int(str(x).strip().lstrip("#")))
        except (TypeError, ValueError):
            pass
    return out


def attach_sources(items: list, valid: dict):
    for it in items:
        it["sources"] = [brief(valid[i]) for i in parse_ids(it.get("source_ids")) if i in valid]


def require_messages(msgs):
    if not msgs:
        raise LLMError("该时间范围内没有本地邮件，请先点击“同步邮件”")
