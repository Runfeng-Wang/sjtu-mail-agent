"""四项核心分析：待办、账单、未回复、主题搜索；以及回信草稿。

思路：先用规则在本地筛选/打分（便宜、可控），再把必要的邮件分批交给大模型，
最后在 Python 里做去重和数值汇总（不让模型做算术）。
"""
import asyncio
import json
import re
import time
from collections import defaultdict
from datetime import datetime

from . import store
from .llm import LLMError, chat_json
from .mail_sync import my_address

BATCH_CHARS = 24000


# ---------------------------------------------------------------- 公共工具

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


def _ids(v) -> list:
    out = []
    for x in v if isinstance(v, list) else [v]:
        try:
            out.append(int(str(x).strip().lstrip("#")))
        except (TypeError, ValueError):
            pass
    return out


def _attach_sources(items: list, valid: dict):
    for it in items:
        it["sources"] = [brief(valid[i]) for i in _ids(it.get("source_ids")) if i in valid]


def _require(msgs):
    if not msgs:
        raise LLMError("该时间范围内没有本地邮件，请先点击“同步邮件”")


# ---------------------------------------------------------------- 1. 待办事项

TODO_SYSTEM = """你是一名细致的邮件助理，帮助用户（交大邮箱 {me}）梳理需要他本人处理的事项。今天是 {today}。
从邮件中找出所有需要用户行动的事项：要提交/填写/回复/确认/参加/付款/准备的东西、截止日期、会议、
以及用户在自己发出的邮件里答应别人要做的事。忽略纯广告和与用户无关的信息。
输出格式：
{{"items": [{{"title": "一句话事项", "detail": "具体要求、地点、链接等关键信息",
  "deadline": "YYYY-MM-DD 或 YYYY-MM-DD HH:MM，没有则为 null",
  "priority": "high|medium|low", "requester": "谁提出的",
  "category": "学业|科研|行政|申请|财务|生活|其他",
  "evidence": "邮件中原话的简短引用", "source_ids": [邮件ID]}}]}}"""

TODO_USER = "请从下面这些邮件中提取待办事项：\n\n{emails}"

TODO_REDUCE = """你是邮件助理。今天是 {today}。下面是从多批邮件中分别提取的待办事项（可能重复）。
另外附上用户最近发出的邮件主题，用来判断哪些事可能已经做完。
请：1) 合并重复事项（合并 source_ids）；2) 判断状态 status：pending（待办）、overdue（已过期仍未见处理）、
maybe_done（从发出的邮件看可能已处理）；3) 找出“可能被遗漏”的事项：截止日临近（7天内）或已过期、
或别人追问/催促过、或你答应别人却没有后续的；4) 写一段简短总结。
输出：
{{"items": [同输入字段 + "status"], "missed": [{{"title": "...", "reason": "为什么担心遗漏", "source_ids": [..]}}],
  "summary": "3-5 句中文总结"}}"""


async def analyze_todos(cfg: dict, days: int = 30, limit: int = 300, include_bulk: bool = True) -> dict:
    me = my_address(cfg)
    msgs = store.recent_messages(days, include_bulk=include_bulk)[:limit]
    _require(msgs)
    msgs.reverse()  # 时间正序，便于模型理解前因后果
    valid = {m["id"]: m for m in msgs}
    batches = make_batches(msgs, cfg)
    results, warnings = await map_batches(cfg, batches, TODO_SYSTEM.format(me=me, today=today()), TODO_USER)

    items = []
    for res, ids in results:
        for it in (res.get("items") or []) if isinstance(res, dict) else []:
            it["source_ids"] = [i for i in _ids(it.get("source_ids")) if i in ids]
            items.append(it)

    missed, summary = [], ""
    if items:
        sent = [f"{m['date']} 致 {m.get('to_addrs')}: {m['subject']}" for m in msgs if m.get("is_sent")][-80:]
        payload = {"todos": items, "my_recent_sent": sent}
        try:
            red = await chat_json(cfg, TODO_REDUCE.format(today=today()), json.dumps(payload, ensure_ascii=False))
            items = red.get("items") or items
            missed = red.get("missed") or []
            summary = red.get("summary") or ""
        except LLMError as e:
            warnings.append(f"合并去重失败，显示原始结果：{e}")

    now = datetime.now().strftime("%Y-%m-%d")
    for it in items:
        it.setdefault("status", "pending")
        dl = (it.get("deadline") or "")[:10]
        if dl and dl < now and it["status"] == "pending":
            it["status"] = "overdue"
    order = {"overdue": 0, "pending": 1, "maybe_done": 2}
    items.sort(key=lambda x: (order.get(x.get("status"), 1), (x.get("deadline") or "9999")))
    _attach_sources(items, valid)
    _attach_sources(missed, valid)
    result = {"items": items, "missed": missed, "summary": summary, "warnings": warnings,
              "scanned": len(msgs), "days": days}
    store.save_result("todos", result)
    return result


# ---------------------------------------------------------------- 2. 账单 / 订阅

MONEY_HINT = re.compile(
    r"(¥|￥|\$|€|£|USD|CNY|RMB|HKD|EUR|元|账单|发票|收据|订阅|续费|续订|扣款|支付|付款|缴费|金额|订单|退款|"
    r"invoice|receipt|payment|paid|charged?|subscription|renew|billing|order|refund|total|amount|trial)",
    re.I,
)

BILL_SYSTEM = """你是财务整理助手。今天是 {today}。从邮件中提取与用户相关的每一笔金钱往来：
订阅/会员续费、购买订单、发票、学费/住宿费/会议注册费、付款提醒、退款等。
规则：只提取邮件中明确出现的金额；同一笔交易若同时有"即将扣款提醒"和"扣款成功收据"，分别记录但 type 不同；
营销广告里的价格不要提取；退款 amount 用负数。
输出：
{{"bills": [{{"source_id": 邮件ID, "vendor": "商家/机构", "item": "购买内容", "amount": 数字,
  "currency": "CNY|USD|EUR|HKD|...", "date": "YYYY-MM-DD 交易或账单日期",
  "type": "subscription|purchase|invoice|fee|reminder|refund",
  "billing_cycle": "monthly|yearly|weekly|once|null", "status": "paid|due|unknown",
  "due_date": "YYYY-MM-DD 或 null", "note": "补充说明，如试用期结束、自动续费"}}]}}
如果这批邮件里没有任何账单，返回 {{"bills": []}}。"""

BILL_USER = "请从下面的邮件中提取账单与费用：\n\n{emails}"

CYCLE_MONTHS = {"monthly": 1, "yearly": 12, "weekly": 12 / 52}


async def analyze_bills(cfg: dict, days: int = 180) -> dict:
    msgs = store.recent_messages(days, sent=False)
    _require(msgs)
    cands = [m for m in msgs if MONEY_HINT.search((m.get("subject") or "") + " " + (m.get("body") or "")[:5000])]
    valid = {m["id"]: m for m in cands}
    if not cands:
        result = {"bills": [], "totals": {}, "subscriptions": [], "warnings": [], "scanned": len(msgs), "candidates": 0}
        store.save_result("bills", result)
        return result
    batches = make_batches(cands, cfg, max_chars=2000)
    results, warnings = await map_batches(cfg, batches, BILL_SYSTEM.format(today=today()), BILL_USER)

    merged = {}
    for res, ids in results:
        for b in (res.get("bills") or []) if isinstance(res, dict) else []:
            try:
                b["amount"] = round(float(str(b.get("amount")).replace(",", "")), 2)
            except (TypeError, ValueError):
                continue
            sid = (_ids(b.get("source_id")) or [None])[0]
            if sid not in ids:
                continue
            b["currency"] = (b.get("currency") or "CNY").upper().replace("RMB", "CNY")
            key = ((b.get("vendor") or "").strip().lower(), b["amount"], b["currency"], b.get("date"), b.get("type"))
            if key in merged:
                merged[key]["source_ids"].append(sid)
            else:
                b["source_ids"] = [sid]
                merged[key] = b
    bills = sorted(merged.values(), key=lambda b: b.get("date") or "", reverse=True)

    totals = defaultdict(lambda: {"paid": 0.0, "due": 0.0, "refund": 0.0, "count": 0})
    subs = {}
    for b in bills:
        t = totals[b["currency"]]
        t["count"] += 1
        if b["amount"] < 0 or b.get("type") == "refund":
            t["refund"] += abs(b["amount"])
        elif b.get("type") == "reminder" or b.get("status") == "due":
            t["due"] += b["amount"]
        else:
            t["paid"] += b["amount"]
        if b.get("type") == "subscription" or b.get("billing_cycle") in CYCLE_MONTHS:
            k = (b.get("vendor") or "").strip().lower()
            if k not in subs or (b.get("date") or "") > (subs[k].get("date") or ""):
                cycle = b.get("billing_cycle")
                subs[k] = {
                    "vendor": b.get("vendor"), "item": b.get("item"), "amount": b["amount"],
                    "currency": b["currency"], "billing_cycle": cycle, "date": b.get("date"),
                    "monthly": round(b["amount"] / CYCLE_MONTHS[cycle], 2) if cycle in CYCLE_MONTHS else None,
                    "source_ids": b["source_ids"],
                }
    for t in totals.values():
        for k in ("paid", "due", "refund"):
            t[k] = round(t[k], 2)
        t["net"] = round(t["paid"] - t["refund"], 2)

    sub_list = sorted(subs.values(), key=lambda s: -(s["monthly"] or 0))
    monthly_by_cur = defaultdict(float)
    for s in sub_list:
        if s["monthly"]:
            monthly_by_cur[s["currency"]] += s["monthly"]
    _attach_sources(bills, valid)
    _attach_sources(sub_list, valid)
    result = {"bills": bills, "totals": dict(totals), "subscriptions": sub_list,
              "subscription_monthly": {k: round(v, 2) for k, v in monthly_by_cur.items()},
              "warnings": warnings, "scanned": len(msgs), "candidates": len(cands), "days": days}
    store.save_result("bills", result)
    return result


# ---------------------------------------------------------------- 3. 未回复邮件

def norm_subject(s: str) -> str:
    s = (s or "").strip().lower()
    while True:
        new = re.sub(r"^\s*(re|fw|fwd|回复|答复|转发)\s*[:：]\s*", "", s)
        if new == s:
            return s
        s = new


def unreplied_candidates(days: int, me: str) -> list:
    msgs = store.recent_messages(days)
    sent = [m for m in msgs if m.get("is_sent")]
    replied_ids = set()
    sent_threads = defaultdict(list)
    for s in sent:
        replied_ids.update(re.findall(r"<[^>]+>", (s.get("in_reply_to") or "") + " " + (s.get("refs") or "")))
        for addr in (s.get("to_addrs") or "").split(","):
            sent_threads[(addr.strip(), norm_subject(s["subject"]))].append(s["ts"])

    latest = {}
    for m in msgs:
        if m.get("is_sent") or m.get("is_bulk") or (me and m.get("from_addr") == me):
            continue
        if "\\Answered" in (m.get("flags") or ""):
            continue
        if m.get("message_id") and m["message_id"] in replied_ids:
            continue
        key = (m.get("from_addr"), norm_subject(m["subject"]))
        if any(ts > m["ts"] for ts in sent_threads.get(key, [])):
            continue
        if key not in latest or m["ts"] > latest[key]["ts"]:  # 同一线程只保留最新一封
            latest[key] = m
    return sorted(latest.values(), key=lambda m: -m["ts"])


UNREPLIED_SYSTEM = """你是邮件助理，用户邮箱是 {me}，今天是 {today}。下面这些邮件用户都还没有回复。
逐封判断是否真的需要用户本人回复（例如：对方提问、请求确认、等待答复、邀请、需要反馈）。
系统通知、群发公告、验证码、仅供知悉的抄送、对方已经说"无需回复"的，判为不需要。
输出：
{{"results": [{{"id": 邮件ID, "needs_reply": true/false, "urgency": "high|medium|low",
  "reason": "为什么需要/不需要回复（一句话）", "ask": "对方希望用户做什么/回答什么", "language": "zh|en"}}]}}
每封邮件都要给出结果。"""

UNREPLIED_USER = "请判断以下邮件是否需要回复：\n\n{emails}"


async def analyze_unreplied(cfg: dict, days: int = 30, limit: int = 150) -> dict:
    me = my_address(cfg)
    cands = unreplied_candidates(days, me)[:limit]
    if not cands:
        result = {"items": [], "skipped": [], "warnings": [], "candidates": 0, "days": days}
        store.save_result("unreplied", result)
        return result
    valid = {m["id"]: m for m in cands}
    batches = make_batches(cands, cfg, max_chars=1200)
    results, warnings = await map_batches(cfg, batches, UNREPLIED_SYSTEM.format(me=me, today=today()), UNREPLIED_USER)

    judged = {}
    for res, ids in results:
        for r in (res.get("results") or []) if isinstance(res, dict) else []:
            i = (_ids(r.get("id")) or [None])[0]
            if i in ids:
                judged[i] = r
    now = time.time()
    items, skipped = [], []
    for m in cands:
        r = judged.get(m["id"])
        if r is None:
            continue
        entry = {**brief(m), **{k: r.get(k) for k in ("urgency", "reason", "ask", "language")},
                 "waiting_days": int((now - m["ts"]) // 86400)}
        (items if r.get("needs_reply") else skipped).append(entry)
    rank = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda x: (rank.get(x.get("urgency"), 1), -x["waiting_days"]))
    result = {"items": items, "skipped": skipped, "warnings": warnings, "candidates": len(cands), "days": days}
    store.save_result("unreplied", result)
    return result


DRAFT_SYSTEM = """你是用户的邮件写作助手。用户姓名：{name}。今天是 {today}。
根据原邮件起草一封得体、简洁的回信。默认使用原邮件的语言（中文邮件用中文，英文邮件用英文）。
不确定的信息（时间、数字、承诺）用【待确认：…】标出，不要编造。{signature}
输出：{{"subject": "回信主题", "body": "回信正文"}}"""


async def draft_reply(cfg: dict, msg_id: int, instructions: str = "") -> dict:
    m = store.get_message(msg_id)
    if not m:
        raise LLMError("找不到这封邮件")
    prof = cfg.get("profile", {})
    sig = f"\n结尾使用这个署名：\n{prof['signature']}" if prof.get("signature") else ""
    user = fmt_email(m, cfg, max_chars=6000)
    if instructions:
        user += f"\n\n用户对回信的要求：{instructions}"
    res = await chat_json(cfg, DRAFT_SYSTEM.format(name=prof.get("name") or "（未提供）", today=today(), signature=sig), user)
    subject = res.get("subject") or ("Re: " + norm_subject(m["subject"]))
    return {"subject": subject, "body": res.get("body") or "", "to": m.get("from_addr"),
            "in_reply_to": m.get("message_id")}


# ---------------------------------------------------------------- 4. 主题检索 + 总结

EXPAND_SYSTEM = """你是邮件检索助手。用户想在自己的邮箱里找某一主题的所有邮件。
请把需求展开成用于关键词匹配的检索词：包括中英文同义词、常见邮件主题用语、相关机构/系统名、附件名常用词。
例如"申请材料"可展开为：申请、application、推荐信、recommendation、成绩单、transcript、CV、简历、
personal statement、SOP、语言成绩、TOEFL、IELTS、GRE、admission、offer、portal、材料、截止 等。
输出：{{"keywords": ["10-30 个检索词，最重要的放前面"], "senders": ["可能的发件人/域名关键词，可为空"],
  "intent": "一句话复述用户想找什么"}}"""

JUDGE_SYSTEM = """你是邮件检索助手。用户要找：{intent}（原话：{query}）。
判断下面每封邮件是否与该主题真正相关，并提炼要点。
输出：{{"relevant": [{{"id": 邮件ID, "relevance": 0到1的数字, "key_points": "与主题相关的关键信息（一两句）"}}]}}
只列出 relevance >= 0.5 的邮件。"""

JUDGE_USER = "候选邮件：\n\n{emails}"

SUMMARY_SYSTEM = """你是邮件助理。今天是 {today}。用户要了解："{query}"。
下面是筛选出的相关邮件。请输出：
{{"summary": "用 Markdown 写的结构化总结：整体进展、关键结论、重要日期/数字（标注来源邮件ID，如 [#12]）",
  "timeline": [{{"date": "YYYY-MM-DD", "event": "发生了什么", "source_ids": [..]}}],
  "open_issues": ["仍待处理或需要跟进的事项"]}}"""


def keyword_score(m: dict, keywords: list, senders: list) -> tuple:
    subj = (m.get("subject") or "").lower()
    frm = ((m.get("from_name") or "") + " " + (m.get("from_addr") or "")).lower()
    att = " ".join(m.get("attachments") or []).lower()
    body = (m.get("body") or "").lower()
    score, hits = 0.0, []
    for rank, kw in enumerate(keywords):
        k = kw.lower().strip()
        if not k:
            continue
        w = 1.5 if rank < 5 else 1.0
        s = 0.0
        if k in subj:
            s += 5
        if k in att:
            s += 3
        if k in frm:
            s += 3
        if k in body:
            s += 1 + min(body.count(k), 5) * 0.2
        if s:
            hits.append(kw)
            score += s * w
    for snd in senders:
        if snd and snd.lower() in frm:
            score += 4
            hits.append(snd)
    return score, hits


async def search_topic(cfg: dict, query: str, days: int = 3650, use_llm: bool = True, max_candidates: int = 80) -> dict:
    msgs = store.recent_messages(days)
    _require(msgs)
    warnings = []
    intent, senders = query, []
    keywords = [w for w in re.split(r"[\s,，、;；]+", query) if w]
    if use_llm:
        try:
            ex = await chat_json(cfg, EXPAND_SYSTEM, f"用户需求：{query}")
            keywords = list(dict.fromkeys(keywords + [str(k) for k in ex.get("keywords") or []]))
            senders = [str(s) for s in ex.get("senders") or []]
            intent = ex.get("intent") or query
        except LLMError as e:
            warnings.append(f"检索词扩展失败，仅使用原始关键词：{e}")

    scored = []
    for m in msgs:
        s, hits = keyword_score(m, keywords, senders)
        if s > 0:
            scored.append((s, hits, m))
    scored.sort(key=lambda x: -x[0])
    cands = scored[:max_candidates]
    hits_by_id = {m["id"]: hits for _, hits, m in cands}

    relevant = []
    if use_llm and cands:
        batches = make_batches([m for _, _, m in cands], cfg, max_chars=800)
        results, w = await map_batches(cfg, batches, JUDGE_SYSTEM.format(intent=intent, query=query), JUDGE_USER)
        warnings += w
        valid = {m["id"]: m for _, _, m in cands}
        for res, ids in results:
            for r in (res.get("relevant") or []) if isinstance(res, dict) else []:
                i = (_ids(r.get("id")) or [None])[0]
                try:
                    rel = float(r.get("relevance") or 0)
                except (TypeError, ValueError):
                    rel = 0
                if i in ids and rel >= 0.5:
                    relevant.append({**brief(valid[i]), "relevance": rel, "key_points": r.get("key_points"),
                                     "matched": hits_by_id.get(i, [])})
        relevant.sort(key=lambda x: (-x["relevance"], x["date"] or ""))
    else:
        relevant = [{**brief(m), "relevance": None, "key_points": None, "matched": hits} for s, hits, m in cands[:40]]

    summary = {"summary": "", "timeline": [], "open_issues": []}
    if use_llm and relevant:
        top = store.get_messages([r["id"] for r in relevant[:30]])
        top.sort(key=lambda m: m["ts"])
        text = "\n\n=====\n\n".join(fmt_email(m, cfg, max_chars=1500) for m in top)
        try:
            summary = await chat_json(cfg, SUMMARY_SYSTEM.format(today=today(), query=query), text[:60000])
        except LLMError as e:
            warnings.append(f"总结失败：{e}")

    # 统计哪些检索词最能命中相关邮件 —— 用于在网页邮箱里自己搜
    hit_count = defaultdict(int)
    for r in relevant:
        for k in r.get("matched") or []:
            hit_count[k] += 1
    best_terms = [k for k, _ in sorted(hit_count.items(), key=lambda x: -x[1])][:6]

    result = {
        "query": query, "intent": intent, "keywords": keywords, "senders": senders,
        "results": relevant, "summary": summary.get("summary") or "",
        "timeline": summary.get("timeline") or [], "open_issues": summary.get("open_issues") or [],
        "best_terms": best_terms, "warnings": warnings, "candidates": len(cands), "scanned": len(msgs),
    }
    store.save_result("search", result)
    return result
