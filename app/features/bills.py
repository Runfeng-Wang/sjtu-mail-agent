"""2. 账单 / 订阅：关键词预筛，模型只提取明细，金额合计由程序计算。"""
import re
from collections import defaultdict

from ..core import store
from .common import attach_sources, make_batches, map_batches, parse_ids, require_messages, today

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
    require_messages(msgs)
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
