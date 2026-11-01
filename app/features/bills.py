"""2. 账单 / 订阅：关键词预筛，模型只提取明细，金额合计由程序计算。"""
import re
from collections import defaultdict

from ..core import store
from .common import attach_sources, load_prompts, make_batches, map_batches, parse_ids, require_messages, today

MONEY_HINT = re.compile(
    r"(¥|￥|\$|€|£|USD|CNY|RMB|HKD|EUR|元|账单|发票|收据|订阅|续费|续订|扣款|支付|付款|缴费|金额|订单|退款|"
    r"invoice|receipt|payment|paid|charged?|subscription|renew|billing|order|refund|total|amount|trial)",
    re.I,
)

CYCLE_MONTHS = {"monthly": 1, "yearly": 12, "weekly": 12 / 52}


async def analyze_bills(cfg: dict, days: int = 180) -> dict:
    msgs = store.recent_messages(days, sent=False)
    require_messages(msgs)
    cands = [m for m in msgs if MONEY_HINT.search((m.get("subject") or "") + " " + (m.get("body") or "")[:5000])]
    valid = {m["id"]: m for m in cands}
    if not cands:
        result = {"bills": [], "totals": {}, "subscriptions": [], "warnings": [], "scanned": len(msgs), "candidates": 0}
        store.save_result("bills", result)
        return result
    p = load_prompts("bills")
    batches = make_batches(cands, cfg, max_chars=2000)
    results, warnings = await map_batches(cfg, batches, p["system"].format(today=today()), p["user"])

    merged = {}
    for res, ids in results:
        for b in (res.get("bills") or []) if isinstance(res, dict) else []:
            try:
                b["amount"] = round(float(str(b.get("amount")).replace(",", "")), 2)
            except (TypeError, ValueError):
                continue
            sid = (parse_ids(b.get("source_id")) or [None])[0]
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
    attach_sources(bills, valid)
    attach_sources(sub_list, valid)
    result = {"bills": bills, "totals": dict(totals), "subscriptions": sub_list,
              "subscription_monthly": {k: round(v, 2) for k, v in monthly_by_cur.items()},
              "warnings": warnings, "scanned": len(msgs), "candidates": len(cands), "days": days}
    store.save_result("bills", result)
    return result
