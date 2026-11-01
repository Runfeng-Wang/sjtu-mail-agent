"""3. 未回复邮件：规则排除已回复/群发，模型判断是否需要回复；以及回信草稿。"""
import re
import time
from collections import defaultdict

from ..core import store
from ..core.llm import LLMError, chat_json
from ..mail.sync import my_address
from .common import brief, fmt_email, load_prompts, make_batches, map_batches, parse_ids, today


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


async def analyze_unreplied(cfg: dict, days: int = 30, limit: int = 150) -> dict:
    me = my_address(cfg)
    cands = unreplied_candidates(days, me)[:limit]
    if not cands:
        result = {"items": [], "skipped": [], "warnings": [], "candidates": 0, "days": days}
        store.save_result("unreplied", result)
        return result
    p = load_prompts("unreplied")
    batches = make_batches(cands, cfg, max_chars=1200)
    results, warnings = await map_batches(cfg, batches, p["system"].format(me=me, today=today()), p["user"])

    judged = {}
    for res, ids in results:
        for r in (res.get("results") or []) if isinstance(res, dict) else []:
            i = (parse_ids(r.get("id")) or [None])[0]
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


async def draft_reply(cfg: dict, msg_id: int, instructions: str = "") -> dict:
    m = store.get_message(msg_id)
    if not m:
        raise LLMError("找不到这封邮件")
    prof = cfg.get("profile", {})
    sig = f"\n结尾使用这个署名：\n{prof['signature']}" if prof.get("signature") else ""
    user = fmt_email(m, cfg, max_chars=6000)
    if instructions:
        user += f"\n\n用户对回信的要求：{instructions}"
    res = await chat_json(cfg, load_prompts("draft")["system"].format(name=prof.get("name") or "（未提供）", today=today(), signature=sig), user)
    subject = res.get("subject") or ("Re: " + norm_subject(m["subject"]))
    return {"subject": subject, "body": res.get("body") or "", "to": m.get("from_addr"),
            "in_reply_to": m.get("message_id")}
