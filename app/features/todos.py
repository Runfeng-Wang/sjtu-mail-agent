"""1. 待办事项：逐批提取，再合并去重，并标出可能被遗漏的事项。"""
import json
from datetime import datetime

from ..core import store
from ..core.llm import LLMError, chat_json
from ..mail.sync import my_address
from .common import attach_sources, load_prompts, make_batches, map_batches, parse_ids, require_messages, today


async def analyze_todos(cfg: dict, days: int = 30, limit: int = 300, include_bulk: bool = True) -> dict:
    p = load_prompts("todos")
    me = my_address(cfg)
    msgs = store.recent_messages(days, include_bulk=include_bulk)[:limit]
    require_messages(msgs)
    msgs.reverse()  # 时间正序，便于模型理解前因后果
    valid = {m["id"]: m for m in msgs}
    batches = make_batches(msgs, cfg)
    results, warnings = await map_batches(cfg, batches, p["system"].format(me=me, today=today()), p["user"])

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
            red = await chat_json(cfg, p["reduce"].format(today=today()), json.dumps(payload, ensure_ascii=False))
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
