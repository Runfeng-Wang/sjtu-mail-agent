"""统一的大模型调用层。

两类接口：
- "openai"：OpenAI 兼容的 /chat/completions。国内绝大多数厂商（DeepSeek、通义千问、Kimi、
  智谱、豆包、硅基流动）以及 OpenAI、Gemini、OpenRouter、本地 Ollama 都支持。
- "anthropic"：Claude，使用官方 anthropic SDK。
"""
import asyncio
import json
import os
import re

import anthropic
import httpx

PROVIDERS = {
    # ---- 国内
    "deepseek": {"label": "DeepSeek 深度求索", "kind": "openai", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "env": "DEEPSEEK_API_KEY", "key_url": "https://platform.deepseek.com/api_keys"},
    "qwen": {"label": "通义千问（阿里云百炼）", "kind": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model": "qwen-plus", "env": "DASHSCOPE_API_KEY", "key_url": "https://bailian.console.aliyun.com/"},
    "moonshot": {"label": "Kimi（月之暗面）", "kind": "openai", "base_url": "https://api.moonshot.cn/v1",
                 "model": "kimi-latest", "env": "MOONSHOT_API_KEY", "key_url": "https://platform.moonshot.cn/console/api-keys"},
    "zhipu": {"label": "智谱 GLM", "kind": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4",
              "model": "glm-4-flash", "env": "ZHIPUAI_API_KEY", "key_url": "https://open.bigmodel.cn/usercenter/apikeys"},
    "doubao": {"label": "豆包（火山方舟）", "kind": "openai", "base_url": "https://ark.cn-beijing.volces.com/api/v3",
               "model": "", "env": "ARK_API_KEY", "key_url": "https://console.volcengine.com/ark",
               "hint": "模型处填写方舟控制台里的模型 ID 或推理接入点 ID（ep-xxxx）"},
    "siliconflow": {"label": "硅基流动 SiliconFlow", "kind": "openai", "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3", "env": "SILICONFLOW_API_KEY", "key_url": "https://cloud.siliconflow.cn/account/ak"},
    # ---- 国外
    "anthropic": {"label": "Claude（Anthropic）", "kind": "anthropic", "base_url": "",
                  "model": "claude-opus-5-5", "env": "ANTHROPIC_API_KEY", "key_url": "https://platform.claude.com/settings/keys"},
    "openai": {"label": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1",
               "model": "gpt-4.1-mini", "env": "OPENAI_API_KEY", "key_url": "https://platform.openai.com/api-keys"},
    "gemini": {"label": "Google Gemini", "kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "env": "GEMINI_API_KEY", "key_url": "https://aistudio.google.com/apikey"},
    "openrouter": {"label": "OpenRouter（聚合多家模型）", "kind": "openai", "base_url": "https://openrouter.ai/api/v1",
                   "model": "", "env": "OPENROUTER_API_KEY", "key_url": "https://openrouter.ai/keys"},
    # ---- 本地 / 自定义
    "ollama": {"label": "本地 Ollama（不出本机，无需 Key）", "kind": "openai", "base_url": "http://localhost:11434/v1",
               "model": "qwen2.5:7b", "env": "", "key_url": "https://ollama.com/download"},
    "custom": {"label": "自定义 OpenAI 兼容接口（中转/校内服务等）", "kind": "openai", "base_url": "",
               "model": "", "env": "LLM_API_KEY", "key_url": ""},
}

# 支持服务端拒答回退（fallbacks: "default"）的 Claude 模型
CLAUDE_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


class LLMError(Exception):
    pass


def resolve(cfg: dict) -> dict:
    """合并预设和用户配置，得到最终调用参数。"""
    llm = cfg["llm"]
    preset = PROVIDERS.get(llm.get("provider"), PROVIDERS["custom"])
    api_key = llm.get("api_key") or (os.environ.get(preset["env"], "") if preset["env"] else "")
    return {
        "kind": preset["kind"],
        "base_url": (llm.get("base_url") or preset["base_url"]).rstrip("/"),
        "model": llm.get("model") or preset["model"],
        "api_key": api_key,
        "effort": llm.get("effort") or "medium",
        "provider": llm.get("provider"),
    }


async def _openai_chat(r: dict, system: str, user: str, json_mode: bool) -> str:
    if not r["base_url"]:
        raise LLMError("请填写接口地址 Base URL")
    headers = {"Content-Type": "application/json"}
    if r["api_key"]:
        headers["Authorization"] = f"Bearer {r['api_key']}"
    body = {
        "model": r["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    url = r["base_url"] + "/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20)) as client:
        for attempt in range(4):
            resp = await client.post(url, headers=headers, json=body)
            if resp.status_code == 400 and "response_format" in body:
                body.pop("response_format")  # 有些模型不支持 JSON 模式，去掉重试
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            break
    if resp.status_code != 200:
        raise LLMError(f"模型接口返回 {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError):
        raise LLMError(f"无法解析模型返回：{str(data)[:500]}")


async def _anthropic_chat(r: dict, system: str, user: str) -> str:
    kwargs = {"api_key": r["api_key"] or None}
    if r["base_url"]:
        kwargs["base_url"] = r["base_url"]
    client = anthropic.AsyncAnthropic(**kwargs)
    params = dict(
        model=r["model"],
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": r["effort"]},
    )
    try:
        if r["model"] in CLAUDE_FALLBACK_MODELS and not r["base_url"]:
            # 被安全分类器拒答时，由服务端自动换用推荐模型重试
            resp = await client.beta.messages.create(
                **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
            )
        else:
            resp = await client.messages.create(**params)
    except anthropic.AuthenticationError:
        raise LLMError("Claude API Key 无效")
    except anthropic.RateLimitError as e:
        raise LLMError(f"Claude 调用频率超限：{e}")
    except anthropic.APIStatusError as e:
        raise LLMError(f"Claude 接口返回 {e.status_code}: {e.message}")
    except anthropic.APIConnectionError as e:
        raise LLMError(f"无法连接 Claude 接口（国内网络可能需要代理）：{e}")
    if resp.stop_reason == "refusal":
        raise LLMError("模型拒绝了这次请求")
    return "".join(b.text for b in resp.content if b.type == "text")


async def chat(cfg: dict, system: str, user: str, *, json_mode: bool = False) -> str:
    r = resolve(cfg)
    if not r["model"]:
        raise LLMError("请在设置中填写模型名称")
    if r["kind"] != "anthropic" and not r["api_key"] and r["provider"] != "ollama":
        raise LLMError("请在设置中填写 API Key")
    if r["kind"] == "anthropic":
        if not r["api_key"]:
            raise LLMError("请在设置中填写 Claude API Key")
        return await _anthropic_chat(r, system, user)
    return await _openai_chat(r, system, user, json_mode)


def extract_json(text: str):
    """从模型输出中尽量提取 JSON。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"模型没有返回合法 JSON：{text[:300]}")


async def chat_json(cfg: dict, system: str, user: str):
    text = await chat(cfg, system + "\n\n只输出 JSON，不要输出其他文字。", user, json_mode=True)
    return extract_json(text)


async def list_models(cfg: dict) -> list:
    r = resolve(cfg)
    if r["kind"] == "anthropic":
        kwargs = {"api_key": r["api_key"] or None}
        if r["base_url"]:
            kwargs["base_url"] = r["base_url"]
        client = anthropic.AsyncAnthropic(**kwargs)
        return [m.id async for m in client.models.list()]
    headers = {"Authorization": f"Bearer {r['api_key']}"} if r["api_key"] else {}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(r["base_url"] + "/models", headers=headers)
    if resp.status_code != 200:
        raise LLMError(f"获取模型列表失败 {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    items = data.get("data", data.get("models", [])) if isinstance(data, dict) else data
    return sorted(str(m.get("id") or m.get("name")) for m in items if isinstance(m, dict))
