"""FastAPI 服务：本地网页界面 + JSON 接口。"""
import asyncio
import imaplib
import re
import threading
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import features
from .core import store
from .core.config import ROOT, load_config, public_config, save_config
from .core.llm import PROVIDERS, LLMError, chat, list_models
from .mail import sync as mail_sync

app = FastAPI(title="SJTU Mail Agent")
WEB = ROOT / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.exception_handler(LLMError)
async def _llm_error(_, exc: LLMError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


# ---------------------------------------------------------------- 设置

@app.get("/api/providers")
def providers():
    return PROVIDERS


@app.get("/api/config")
def get_config():
    return public_config(load_config())


@app.post("/api/config")
def set_config(body: dict):
    return public_config(save_config(body))


@app.post("/api/test/mail")
async def test_mail():
    try:
        return await asyncio.to_thread(mail_sync.test_connection, load_config())
    except imaplib.IMAP4.error as e:
        raise HTTPException(400, f"登录失败：{e}。请确认用户名/密码（jAccount），并在网页邮箱设置中开启 IMAP。")
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")


@app.post("/api/test/llm")
async def test_llm():
    text = await chat(load_config(), "你是一个测试助手。", "请只回复：连接成功")
    return {"ok": True, "reply": text.strip()[:200]}


@app.post("/api/llm/models")
async def models():
    return {"models": await list_models(load_config())}


# ---------------------------------------------------------------- 同步

class SyncReq(BaseModel):
    extra_folders: list[str] = []


@app.post("/api/sync")
def start_sync(req: SyncReq):
    if mail_sync.SYNC_STATUS["running"]:
        return mail_sync.SYNC_STATUS
    threading.Thread(target=mail_sync.run_sync, args=(load_config(), req.extra_folders), daemon=True).start()
    return {"running": True, "message": "已开始同步"}


@app.get("/api/sync/status")
def sync_status():
    return {**mail_sync.SYNC_STATUS, "stats": store.stats()}


# ---------------------------------------------------------------- 分析

class DaysReq(BaseModel):
    days: int = 30
    include_bulk: bool = True


class DraftReq(BaseModel):
    id: int
    instructions: str = ""


class SearchReq(BaseModel):
    query: str
    days: int = 3650
    use_llm: bool = True


@app.post("/api/analyze/todos")
async def todos(req: DaysReq):
    return await features.analyze_todos(load_config(), req.days, include_bulk=req.include_bulk)


@app.post("/api/analyze/bills")
async def bills(req: DaysReq):
    return await features.analyze_bills(load_config(), req.days)


@app.post("/api/analyze/unreplied")
async def unreplied(req: DaysReq):
    return await features.analyze_unreplied(load_config(), req.days)


@app.post("/api/draft")
async def draft(req: DraftReq):
    return await features.draft_reply(load_config(), req.id, req.instructions)


@app.post("/api/search")
async def search(req: SearchReq):
    if not req.query.strip():
        raise HTTPException(400, "请输入要查找的主题")
    return await features.search_topic(load_config(), req.query.strip(), req.days, req.use_llm)


@app.get("/api/results/{kind}")
def last_result(kind: str):
    return store.load_result(kind) or {}


# ---------------------------------------------------------------- 原邮件

@app.get("/api/messages/{msg_id}")
def message(msg_id: int):
    m = store.get_message(msg_id)
    if not m:
        raise HTTPException(404, "找不到这封邮件")
    return m


@app.get("/api/messages/{msg_id}/eml")
async def message_eml(msg_id: int):
    """从服务器取回原始邮件，下载后可用“邮件”App 打开（含附件）。"""
    m = store.get_message(msg_id)
    if not m:
        raise HTTPException(404, "找不到这封邮件")
    try:
        raw = await asyncio.to_thread(mail_sync.fetch_raw_by_uid, load_config(), m["folder"], m["uid"])
    except Exception as e:
        raise HTTPException(400, f"从服务器获取失败：{e}")
    if not raw:
        raise HTTPException(404, "服务器上已找不到这封邮件（可能被移动或删除）")
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", m["subject"] or "mail")[:60] + ".eml"
    return Response(raw, media_type="message/rfc822",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
