"""本地配置：保存在 data/config.json，只存在你自己的电脑上。"""
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MAIL_AGENT_DATA") or ROOT / "data")
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "mail.db"

DEFAULT_CONFIG = {
    "mail": {
        "host": "mail.sjtu.edu.cn",
        "port": 993,
        "username": "",      # jAccount 用户名（或完整邮箱地址）
        "password": "",
        "address": "",       # 你的邮箱地址，用于判断“我发出的/发给我的”
    },
    "llm": {
        "provider": "deepseek",
        "base_url": "",      # 留空则使用预设地址
        "api_key": "",       # 留空则读取预设里的环境变量
        "model": "",         # 留空则使用预设模型
        "effort": "medium",  # 仅 Claude 使用
        "concurrency": 3,
    },
    "privacy": {
        "redact": True,          # 发送前遮盖手机号/身份证/银行卡号
        "max_body_chars": 3000,  # 每封邮件最多发送给模型的正文字数
    },
    "sync": {
        "days": 90,
        "max_per_folder": 1500,
        "max_message_mb": 5,
    },
    "profile": {
        "name": "",
        "signature": "",
    },
}

SECRET_FIELDS = [("mail", "password"), ("llm", "api_key")]
MASK = "********"


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return _merge(DEFAULT_CONFIG, json.loads(CONFIG_PATH.read_text("utf-8")))
        except json.JSONDecodeError:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(new: dict) -> dict:
    """保存配置。密钥字段若为空或为掩码，则保留原值。"""
    old = load_config()
    merged = _merge(old, new)
    for section, field in SECRET_FIELDS:
        val = (new.get(section) or {}).get(field)
        if val is None or val == "" or MASK in str(val):
            merged[section][field] = old[section][field]
    DATA_DIR.mkdir(exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(CONFIG_PATH, 0o600)
    return merged


def public_config(cfg: dict) -> dict:
    """返回给前端的配置，密钥打码。"""
    out = copy.deepcopy(cfg)
    for section, field in SECRET_FIELDS:
        v = out[section].get(field) or ""
        out[section][field] = (MASK + v[-4:]) if len(v) > 8 else (MASK if v else "")
    return out
