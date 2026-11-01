"""1. 待办事项：逐批提取，再合并去重，并标出可能被遗漏的事项。"""
import json
from datetime import datetime

from ..core import store
from ..core.llm import LLMError, chat_json
from ..mail.sync import my_address
from .common import attach_sources, make_batches, map_batches, parse_ids, require_messages, today

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
    require_messages(msgs)
    msgs.reverse()  # 时间正序，便于模型理解前因后果
    valid = {m["id"]: m for m in msgs}
    batches = make_batches(msgs, cfg)
    results, warnings = await map_batches(cfg, batches, TODO_SYSTEM.format(me=me, today=today()), TODO_USER)

    items = []
    for res, ids in results:
        for it in (res.get("items") or []) if isinstance(res, dict) else []:
            it["source_ids"] = [i for i in parse_ids(it.get("source_ids")) if i in ids]
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
    attach_sources(items, valid)
    attach_sources(missed, valid)
    result = {"items": items, "missed": missed, "summary": summary, "warnings": warnings,
              "scanned": len(msgs), "days": days}
    store.save_result("todos", result)
    return result
