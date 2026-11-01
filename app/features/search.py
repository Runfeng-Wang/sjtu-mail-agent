"""4. 主题检索 + 总结：扩展检索词，本地打分，模型判断相关性并总结。"""
import re
from collections import defaultdict

from ..core import store
from ..core.llm import LLMError, chat_json
from .common import brief, fmt_email, load_prompts, make_batches, map_batches, parse_ids, require_messages, today


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
    p = load_prompts("search")
    msgs = store.recent_messages(days)
    require_messages(msgs)
    warnings = []
    intent, senders = query, []
    keywords = [w for w in re.split(r"[\s,，、;；]+", query) if w]
    if use_llm:
        try:
            ex = await chat_json(cfg, p["expand"].format(), f"用户需求：{query}")
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
        results, w = await map_batches(cfg, batches, p["judge"].format(intent=intent, query=query), p["judge_user"])
        warnings += w
        valid = {m["id"]: m for _, _, m in cands}
        for res, ids in results:
            for r in (res.get("relevant") or []) if isinstance(res, dict) else []:
                i = (parse_ids(r.get("id")) or [None])[0]
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
            summary = await chat_json(cfg, p["summary"].format(today=today(), query=query), text[:60000])
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
