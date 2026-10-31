# 交大邮箱智能助手（SJTU Mail Agent）

一个跑在你自己电脑上的邮箱助手。它通过 IMAP **只读**同步交大邮箱，再调用你选择的大模型（国产或国外），帮你：

1. **待办事项**：整理需要你处理的事项和截止日期，并标出可能被遗漏的（快到期、已过期、被催过、你答应了却没后续的）
2. **账单汇总**：提取订阅、订单、发票、缴费、退款，按币种汇总总额，并估算订阅的月均成本
3. **未回复提醒**：找出你还没回、而且确实需要你回复的邮件，可一键起草回信
4. **主题检索**：例如“所有关于我申请材料的邮件”，自动扩展中英文检索词，找出相关邮件并总结成进展、时间线和待跟进事项，同时告诉你怎么在网页邮箱里找到原邮件

## 快速开始

```bash
cd "Sjtu Mail Agent"
python3 -m venv .venv            # 已有 .venv 可跳过
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py
```

浏览器会自动打开 http://127.0.0.1:8765，然后：

1. **设置 → 邮箱**：填写 jAccount 用户名、密码和你的邮箱地址，点“测试邮箱连接”
2. **设置 → 大模型**：选择服务商，填 API Key，点“测试模型”
3. 点右上角 **同步邮件**（默认同步最近 90 天的收件箱和已发送）
4. 在各个页面点“开始分析”

## 交大邮箱连接说明

| 项目 | 值 |
|---|---|
| IMAP 服务器 | `mail.sjtu.edu.cn` |
| 端口 | `993`（SSL） |
| 用户名 | jAccount 用户名（不行的话试完整邮箱地址） |
| 密码 | jAccount 密码 |

- 交大邮箱是 Zimbra 系统，已确认 IMAP 端口对外开放。如果登录失败，到网页邮箱的「设置 → 邮件 / 帐户」里看看 IMAP 是否开启，以及是否要求使用单独的客户端密码。
- 同步是**只读**的：文件夹用 EXAMINE 打开，正文用 `BODY.PEEK` 读取，**不会把邮件标为已读，也不会移动或删除邮件**。
- 超过 5 MB 的邮件（一般是带大附件的）只同步邮件头。

## 大模型怎么接

除 Claude 外，所有服务商都走 **OpenAI 兼容接口**（`/chat/completions`），所以列表里没有的服务商，一般选“自定义”并填 Base URL 就能用。

| 服务商 | Base URL | 默认模型 | 环境变量 |
|---|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| 通义千问（百炼） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | `DASHSCOPE_API_KEY` |
| Kimi | `https://api.moonshot.cn/v1` | `kimi-latest` | `MOONSHOT_API_KEY` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-flash`（免费） | `ZHIPUAI_API_KEY` |
| 豆包（火山方舟） | `https://ark.cn-beijing.volces.com/api/v3` | 填方舟里的模型 ID / `ep-xxx` | `ARK_API_KEY` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V3` | `SILICONFLOW_API_KEY` |
| Claude | 官方 SDK | `claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` | `OPENAI_API_KEY` |
| Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| OpenRouter | `https://openrouter.ai/api/v1` | 自选 | `OPENROUTER_API_KEY` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b` | 无需 Key |

除了在界面里直接填 Key，还可以这样接：

- **环境变量**：界面里的 Key 留空，启动前设置环境变量，Key 就不会写进配置文件。例如：
  ```bash
  DEEPSEEK_API_KEY=sk-xxx .venv/bin/python run.py
  ```
- **本地模型（Ollama）**：邮件内容完全不出本机。安装 Ollama 后先运行 `ollama pull qwen2.5:7b`，再在界面中选“本地 Ollama”。
- **中转 / 校内 / 企业网关**：只要是 OpenAI 兼容接口，选“自定义”，填 Base URL 和 Key 即可。
- **获取模型列表**：模型名会更新，点“获取模型列表”可以拉取服务商当前可用的模型。
- Claude、OpenAI、Gemini 在国内网络下一般需要代理。可以在启动前设置 `HTTPS_PROXY`。

## 隐私

- 所有数据都存放在本机的 `data/` 目录：`config.json`（配置，权限 600）和 `mail.db`（邮件缓存）。服务只监听 `127.0.0.1`。
- 只有在你点击“分析”时，相关邮件才会发送给你选择的模型服务商。默认会遮盖手机号、身份证号和银行卡号，去掉引用的历史邮件，并且每封最多发送 3000 字（可在设置里调整）。
- 回信草稿只在本地生成，**不会自动发送**。
- 想清除全部数据，删除 `data/` 目录即可。

## 原理

```
IMAP 只读同步 ──▶ SQLite 本地缓存 ──▶ 本地规则预筛 ──▶ 分批交给大模型（JSON 输出） ──▶ Python 去重 / 汇总 ──▶ 网页
```

- **待办**：所有邮件（包括你发出的）按时间顺序分批提取，再做一轮合并去重；结合你发出的邮件判断哪些“可能已完成”，并标出可能被遗漏的事项。
- **账单**：先用关键词（¥、$、元、发票、订阅、receipt、invoice 等）筛出候选邮件，模型只负责提取明细，**金额合计由程序计算**。
- **未回复**：先用规则排除：带 `\Answered` 标记的、已发送邮件的 `In-Reply-To` / `References` 指向它的、你之后给同一个人回过同主题邮件的，以及群发和通知类邮件；剩下的再交给模型判断是否真的需要回复。
- **主题检索**：模型把你的需求扩展成中英文检索词，本地给邮件打分（主题权重最高，其次是附件名和发件人，再次是正文），然后由模型判断相关性并总结。每封邮件都给出定位方式：文件夹 + 日期 + 主题关键词、Zimbra 精确搜索串 `msgid:"<…>"`，或者直接下载 `.eml` 原件。

## 目录结构

```
app/config.py      配置读写（密钥打码）
app/mail_sync.py   IMAP 同步、邮件解析（中文编码、HTML 转文本、群发识别）
app/store.py       SQLite 存储
app/llm.py         大模型适配层（OpenAI 兼容 + Claude SDK），服务商预设
app/analyzers.py   四项分析 + 回信草稿（提示词都在这里，可以按需修改）
app/server.py      FastAPI 接口
web/               前端页面（原生 HTML/JS，无需构建）
```

## 可以继续扩展的方向

- 定时自动同步，每天早上推送待办和未回复提醒
- 把回信草稿通过 IMAP `APPEND` 存进网页邮箱的「草稿箱」
- 同步更多文件夹（设置里已支持“额外同步的文件夹”）
- 导出账单为 CSV / Excel
