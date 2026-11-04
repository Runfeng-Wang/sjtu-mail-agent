// ---------------------------------------------------------------- 工具
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `请求失败 (${r.status})`);
  return data;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), 2200);
}

async function copy(text) {
  await navigator.clipboard.writeText(text);
  toast("已复制");
}

// 极简 Markdown：标题、列表、粗体、代码、[#id] 邮件引用
function md(text) {
  const lines = esc(text || "").split("\n");
  let html = "", inList = false;
  for (let l of lines) {
    l = l.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
    const m = l.match(/^\s*[-*]\s+(.*)/) || l.match(/^\s*\d+[.)]\s+(.*)/);
    if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${m[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    const h = l.match(/^#{1,4}\s+(.*)/);
    if (h) html += `<h3>${h[1]}</h3>`;
    else if (l.trim()) html += `<p>${l}</p>`;
  }
  if (inList) html += "</ul>";
  return html.replace(/\[#(\d+)\]/g, (_, id) => `<a class="src" data-mid="${id}">[#${id}]</a>`);
}

function sources(list) {
  if (!list || !list.length) return "";
  return list.map((s) => `<a class="src" data-mid="${s.id}" title="${esc(s.from)} · ${esc(s.date)}">📧 ${esc(s.subject)}</a>`).join("");
}

function warnings(ws) {
  return (ws || []).map((w) => `<div class="warn">⚠️ ${esc(w)}</div>`).join("");
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
}

async function running(btn, out, fn, tip = "分析中…邮件较多时可能需要 1–3 分钟") {
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "处理中…";
  out.innerHTML = `<div class="loading">⏳ ${tip}</div>`;
  try {
    await fn();
  } catch (e) {
    out.innerHTML = `<div class="warn">❌ ${esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// ---------------------------------------------------------------- 标签页
document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + b.dataset.tab));
  })
);

// 任意位置点击 data-mid 打开原邮件
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-mid]");
  if (a) { e.preventDefault(); openMail(+a.dataset.mid); }
});

// ---------------------------------------------------------------- 原邮件弹窗
function locateText(m) {
  const lines = [
    `文件夹：${m.folder}　日期：${m.date}`,
    `发件人：${m.from_name || ""} <${m.from_addr || ""}>`,
    `主题：${m.subject}`,
  ];
  return lines.join("\n");
}

async function openMail(id) {
  const m = await api(`/api/messages/${id}`);
  const msgidQuery = m.message_id ? `msgid:"${m.message_id}"` : "";
  $("modalBody").innerHTML = `
    <h3>${esc(m.subject)}</h3>
    <div class="small muted">
      发件人：${esc(m.from_name)} &lt;${esc(m.from_addr)}&gt;<br>
      收件人：${esc(m.to_addrs)}${m.cc_addrs ? `<br>抄送：${esc(m.cc_addrs)}` : ""}<br>
      时间：${esc(m.date)}　文件夹：${esc(m.folder)}　本地 ID：#${m.id}
      ${m.attachments.length ? `<br>附件：${m.attachments.map(esc).join("、")}` : ""}
    </div>
    <div class="card" style="margin-top:12px">
      <b>如何找到这封原邮件</b>
      <ol class="small">
        <li>在网页邮箱 mail.sjtu.edu.cn 打开「${esc(m.folder)}」，搜索主题关键词：<code>${esc((m.subject || "").slice(0, 30))}</code>，按日期 ${esc(m.date)} 定位。</li>
        ${msgidQuery ? `<li>Zimbra 网页邮箱支持精确搜索：<code>${esc(msgidQuery)}</code> <button class="btn small" id="cpMsgid">复制</button></li>` : ""}
        <li>或 <a href="/api/messages/${m.id}/eml">下载原始邮件 (.eml)</a>，双击用“邮件”App 打开（含附件）。</li>
      </ol>
      <button class="btn small" id="cpLocate">复制定位信息</button>
    </div>
    <pre class="body">${esc(m.body)}</pre>`;
  $("modal").classList.remove("hidden");
  $("cpLocate").onclick = () => copy(locateText(m));
  if ($("cpMsgid")) $("cpMsgid").onclick = () => copy(msgidQuery);
}
$("modalClose").onclick = () => $("modal").classList.add("hidden");
$("modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("modal").classList.add("hidden"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("modal").classList.add("hidden"); });

// ---------------------------------------------------------------- 同步
async function refreshSync() {
  const s = await api("/api/sync/status");
  const st = s.stats;
  let text = `本地 ${st.total} 封（已发送 ${st.sent}）`;
  if (st.newest) text += ` · 最新 ${when(st.newest)}`;
  if (s.running) text = `⏳ ${s.message} ${s.done}/${s.total}`;
  else if (s.error) text = `❌ ${s.error}`;
  else if (s.message && s.message.startsWith("同步完成")) text = `✅ ${s.message} · ${text}`;
  $("syncText").textContent = text;
  $("syncBtn").disabled = s.running;
  if (s.running) setTimeout(refreshSync, 1500);
}

$("syncBtn").onclick = async () => {
  const extra = ($("extra_folders").value || "").split(/[,，]/).map((x) => x.trim()).filter(Boolean);
  try {
    await api("/api/sync", { extra_folders: extra });
    setTimeout(refreshSync, 500);
  } catch (e) { toast(e.message); }
};

// ---------------------------------------------------------------- 待办
const STATUS = { overdue: ["已过期", "red"], pending: ["待办", "amber"], maybe_done: ["可能已完成", "green"] };
const PRIO = { high: ["高", "red"], medium: ["中", "amber"], low: ["低", ""] };

function renderTodos(r) {
  if (!r || !r.items) return "";
  let html = warnings(r.warnings);
  html += `<div class="muted small">扫描了最近 ${r.days} 天的 ${r.scanned} 封邮件${r.created_at ? " · 生成于 " + when(r.created_at) : ""}</div>`;
  html += `<div class="cols"><div>`;
  if (r.summary) html += `<div class="card"><h3>总结</h3><div class="md">${md(r.summary)}</div></div>`;
  if (r.missed && r.missed.length) {
    html += `<div class="card alert"><h3>⚠️ 可能被遗漏的事项</h3>` +
      r.missed.map((m) => `<div class="item"><div class="title">${esc(m.title)}</div><div class="meta">${esc(m.reason)}</div><div>${sources(m.sources)}</div></div>`).join("") + "</div>";
  }
  html += `</div><div class="card"><h3>全部待办（${r.items.length}）</h3>`;
  if (!r.items.length) html += `<div class="empty">没有发现待办事项 🎉</div>`;
  html += r.items.map((t) => {
    const [sl, sc] = STATUS[t.status] || [t.status, ""];
    const [pl, pc] = PRIO[t.priority] || ["", ""];
    return `<div class="item">
      <div class="title"><span class="chip ${sc}">${esc(sl)}</span>${pl ? `<span class="chip ${pc}">优先级 ${pl}</span>` : ""}${esc(t.title)}</div>
      <div class="meta">${t.deadline ? `⏰ 截止 ${esc(t.deadline)}　` : ""}${t.requester ? `👤 ${esc(t.requester)}　` : ""}${t.category ? `🏷 ${esc(t.category)}` : ""}</div>
      ${t.detail ? `<div>${esc(t.detail)}</div>` : ""}
      ${t.evidence ? `<div class="meta">“${esc(t.evidence)}”</div>` : ""}
      <div>${sources(t.sources)}</div></div>`;
  }).join("");
  return html + "</div></div>";
}

$("todoRun").onclick = () => running($("todoRun"), $("todoOut"), async () => {
  const r = await api("/api/analyze/todos", { days: +$("todoDays").value, include_bulk: $("todoBulk").checked });
  $("todoOut").innerHTML = renderTodos(r);
});

// ---------------------------------------------------------------- 账单
const fmtMoney = (v, cur) => `${cur === "CNY" ? "¥" : cur === "USD" ? "$" : cur === "EUR" ? "€" : cur + " "}${Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const BILL_TYPE = { subscription: "订阅", purchase: "购买", invoice: "发票", fee: "缴费", reminder: "付款提醒", refund: "退款" };
const CYCLE = { monthly: "每月", yearly: "每年", weekly: "每周", once: "一次性" };

function renderBills(r) {
  if (!r || !r.bills) return "";
  let html = warnings(r.warnings);
  html += `<div class="muted small">扫描 ${r.scanned} 封，其中 ${r.candidates} 封可能涉及金额${r.created_at ? " · 生成于 " + when(r.created_at) : ""}。金额由程序汇总，明细由模型提取，请以原邮件为准。</div>`;
  const curs = Object.keys(r.totals || {});
  if (curs.length) {
    html += `<div class="stats">` + curs.map((c) => {
      const t = r.totals[c];
      return `<div class="stat"><div class="label">${c} 已支付（扣除退款）</div><div class="value">${fmtMoney(t.net, c)}</div>
        <div class="small muted">待付 ${fmtMoney(t.due, c)} · 退款 ${fmtMoney(t.refund, c)} · ${t.count} 笔</div></div>`;
    }).join("");
    for (const [c, v] of Object.entries(r.subscription_monthly || {})) {
      html += `<div class="stat"><div class="label">订阅月均成本（${c}）</div><div class="value">${fmtMoney(v, c)}</div><div class="small muted">≈ 每年 ${fmtMoney(v * 12, c)}</div></div>`;
    }
    html += `</div>`;
  }
  if (r.subscriptions && r.subscriptions.length) {
    html += `<div class="card"><h3>订阅 / 周期性扣费</h3><table><tr><th>服务</th><th>内容</th><th class="num">金额</th><th>周期</th><th class="num">月均</th><th>最近一次</th><th>来源</th></tr>` +
      r.subscriptions.map((s) => `<tr><td>${esc(s.vendor)}</td><td>${esc(s.item)}</td><td class="num">${fmtMoney(s.amount, s.currency)}</td>
        <td>${esc(CYCLE[s.billing_cycle] || s.billing_cycle || "")}</td><td class="num">${s.monthly != null ? fmtMoney(s.monthly, s.currency) : "-"}</td>
        <td>${esc(s.date)}</td><td>${sources(s.sources)}</td></tr>`).join("") + `</table></div>`;
  }
  html += `<div class="card"><h3>费用明细（${r.bills.length}）</h3>`;
  if (!r.bills.length) html += `<div class="empty">没有发现账单</div>`;
  else html += `<table><tr><th>日期</th><th>商家/机构</th><th>内容</th><th>类型</th><th>状态</th><th class="num">金额</th><th>来源</th></tr>` +
    r.bills.map((b) => `<tr><td>${esc(b.date)}</td><td>${esc(b.vendor)}</td><td>${esc(b.item)}${b.note ? `<div class="small muted">${esc(b.note)}</div>` : ""}</td>
      <td><span class="chip">${esc(BILL_TYPE[b.type] || b.type)}</span></td>
      <td>${b.status === "due" ? `<span class="chip amber">待付${b.due_date ? " " + esc(b.due_date) : ""}</span>` : b.status === "paid" ? `<span class="chip green">已付</span>` : ""}</td>
      <td class="num">${fmtMoney(b.amount, b.currency)}</td><td>${sources(b.sources)}</td></tr>`).join("") + `</table>`;
  return html + "</div>";
}

$("billRun").onclick = () => running($("billRun"), $("billOut"), async () => {
  $("billOut").innerHTML = renderBills(await api("/api/analyze/bills", { days: +$("billDays").value }));
});

// ---------------------------------------------------------------- 未回复
const URG = { high: ["紧急", "red"], medium: ["一般", "amber"], low: ["不急", ""] };

function renderUnreplied(r) {
  if (!r || !r.items) return "";
  let html = warnings(r.warnings);
  html += `<div class="muted small">本地规则筛出 ${r.candidates} 封未回复的非群发邮件，模型判断其中 ${r.items.length} 封需要回复${r.created_at ? " · 生成于 " + when(r.created_at) : ""}</div>`;
  html += `<div class="card"><h3>需要回复（${r.items.length}）</h3>`;
  if (!r.items.length) html += `<div class="empty">没有需要回复的邮件 🎉</div>`;
  html += r.items.map((m) => {
    const [ul, uc] = URG[m.urgency] || ["", ""];
    return `<div class="item" id="unr-${m.id}">
      <div class="title"><span class="chip ${uc}">${ul}</span><span class="chip">已等待 ${m.waiting_days} 天</span>${esc(m.subject)}</div>
      <div class="meta">👤 ${esc(m.from)} &lt;${esc(m.from_addr)}&gt; · ${esc(m.date)}</div>
      ${m.ask ? `<div>对方希望：${esc(m.ask)}</div>` : ""}
      ${m.reason ? `<div class="meta">${esc(m.reason)}</div>` : ""}
      <div class="row">
        <a class="src" data-mid="${m.id}">查看原文</a>
        <input class="grow draft-ins" placeholder="（可选）回信要点，如：同意，周五下午有空">
        <button class="btn small draft-btn" data-id="${m.id}">生成回信草稿</button>
      </div>
      <div class="draft-out"></div></div>`;
  }).join("") + "</div>";
  if (r.skipped && r.skipped.length) {
    html += `<details class="card"><summary>判断为无需回复（${r.skipped.length}）</summary>` +
      r.skipped.map((m) => `<div class="item"><a class="src" data-mid="${m.id}">${esc(m.subject)}</a><div class="meta">${esc(m.from)} · ${esc(m.reason)}</div></div>`).join("") + "</details>";
  }
  return html;
}

$("unrRun").onclick = () => running($("unrRun"), $("unrOut"), async () => {
  $("unrOut").innerHTML = renderUnreplied(await api("/api/analyze/unreplied", { days: +$("unrDays").value }));
});

$("unrOut").addEventListener("click", async (e) => {
  const btn = e.target.closest(".draft-btn");
  if (!btn) return;
  const box = btn.closest(".item");
  const out = box.querySelector(".draft-out");
  await running(btn, out, async () => {
    const d = await api("/api/draft", { id: +btn.dataset.id, instructions: box.querySelector(".draft-ins").value });
    out.innerHTML = `<div class="small muted">收件人：${esc(d.to)}　主题：${esc(d.subject)}</div>
      <textarea class="draft">${esc(d.body)}</textarea>
      <div class="row"><button class="btn small cp">复制正文</button><span class="small muted">草稿只在本地生成，不会自动发送。请复制到网页邮箱中回复。</span></div>`;
    out.querySelector(".cp").onclick = () => copy(out.querySelector("textarea").value);
  }, "正在起草回信…");
});

// ---------------------------------------------------------------- 主题检索
function renderSearch(r) {
  if (!r || !r.results) return "";
  let html = warnings(r.warnings);
  html += `<div class="muted small">“${esc(r.query)}” · 在 ${r.scanned} 封邮件中按 ${r.keywords.length} 个检索词初筛出 ${r.candidates} 封，确认相关 ${r.results.length} 封${r.created_at ? " · 生成于 " + when(r.created_at) : ""}</div>`;
  html += `<div class="cols"><div>`;
  if (r.summary) html += `<div class="card"><h3>主题总结</h3><div class="md">${md(r.summary)}</div></div>`;
  if (r.timeline && r.timeline.length) {
    html += `<div class="card"><h3>时间线</h3><div class="timeline">` +
      r.timeline.map((t) => `<div><b>${esc(t.date)}</b> ${esc(t.event)} ${(t.source_ids || []).map((i) => `<a class="src" data-mid="${i}">[#${i}]</a>`).join("")}</div>`).join("") + `</div></div>`;
  }
  if (r.open_issues && r.open_issues.length) {
    html += `<div class="card alert"><h3>仍需跟进</h3><ul>${r.open_issues.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
  }
  const terms = r.best_terms && r.best_terms.length ? r.best_terms : r.keywords.slice(0, 5);
  html += `<div class="card"><h3>🧭 如何在网页邮箱里快速找到这些邮件</h3>
    <div class="small">在 mail.sjtu.edu.cn 搜索框中依次搜索这些最有效的关键词：${terms.map((t) => `<code>${esc(t)}</code>`).join(" ")}
    <button class="btn small" id="cpTerms">复制</button></div>
    <div class="small muted" style="margin-top:6px">每封邮件点开后还会给出精确定位方式（文件夹、日期、Zimbra <code>msgid:</code> 搜索串、.eml 下载）。全部检索词：${r.keywords.map(esc).join("、")}</div></div>`;
  html += `</div><div class="card"><h3>相关邮件（${r.results.length}）</h3>`;
  if (!r.results.length) html += `<div class="empty">没有找到相关邮件，可以换个说法，或扩大同步的时间范围后再试</div>`;
  html += r.results.map((m) => `<div class="item">
      <div class="title">${m.relevance != null ? `<span class="chip blue">相关度 ${Math.round(m.relevance * 100)}%</span>` : ""}<a class="src" style="font-size:14px" data-mid="${m.id}">${esc(m.subject)}</a></div>
      <div class="meta">${m.is_sent ? "我发给 " + esc(m.to) : "👤 " + esc(m.from)} · ${esc(m.date)} · ${esc(m.folder)}${m.attachments.length ? " · 📎 " + m.attachments.map(esc).join("、") : ""}</div>
      ${m.key_points ? `<div>${esc(m.key_points)}</div>` : ""}
      <div class="meta">命中：${(m.matched || []).map(esc).join("、")}</div></div>`).join("");
  html += "</div></div>";
  setTimeout(() => { if ($("cpTerms")) $("cpTerms").onclick = () => copy(terms.join(" ")); });
  return html;
}

$("searchRun").onclick = () => running($("searchRun"), $("searchOut"), async () => {
  const r = await api("/api/search", { query: $("searchQ").value, days: +$("searchDays").value, use_llm: $("searchLLM").checked });
  $("searchOut").innerHTML = renderSearch(r);
}, "正在检索并总结…");
$("searchQ").addEventListener("keydown", (e) => { if (e.key === "Enter") $("searchRun").click(); });

// ---------------------------------------------------------------- 设置
let PROVIDERS = {};
const FIELDS = ["mail_host", "mail_port", "mail_username", "mail_password", "mail_address",
  "sync_days", "sync_max_per_folder", "llm_provider", "llm_base_url", "llm_api_key", "llm_model",
  "llm_effort", "llm_concurrency", "privacy_max_body_chars", "profile_name", "profile_signature"];

function providerChanged() {
  const p = PROVIDERS[$("llm_provider").value] || {};
  $("llm_base_url").placeholder = p.base_url || "https://…/v1";
  $("llm_model").placeholder = p.model || "填写模型名称";
  $("effortRow").style.display = p.kind === "anthropic" ? "" : "none";
  let hint = `接口类型：${p.kind === "anthropic" ? "Anthropic 官方 SDK" : "OpenAI 兼容"}`;
  if (p.env) hint += ` · Key 也可通过环境变量 <code>${p.env}</code> 提供`;
  if (p.key_url) hint += ` · <a href="${p.key_url}" target="_blank" rel="noopener">获取 Key</a>`;
  if (p.hint) hint += `<br>${esc(p.hint)}`;
  $("providerHint").innerHTML = hint;
}

async function loadSettings() {
  PROVIDERS = await api("/api/providers");
  $("llm_provider").innerHTML = Object.entries(PROVIDERS).map(([k, p]) => `<option value="${k}">${esc(p.label)}</option>`).join("");
  const c = await api("/api/config");
  for (const f of FIELDS) {
    const [sec, ...rest] = f.split("_");
    const v = c[sec]?.[rest.join("_")];
    if ($(f) && v !== undefined) $(f).value = v;
  }
  $("privacy_redact").checked = !!c.privacy.redact;
  $("extra_folders").value = localStorage.getItem("extra_folders") || "";
  providerChanged();
}

function collectSettings() {
  const c = { mail: {}, sync: {}, llm: {}, privacy: {}, profile: {} };
  for (const f of FIELDS) {
    const [sec, ...rest] = f.split("_");
    let v = $(f).value;
    if ($(f).type === "number") v = Number(v);
    c[sec][rest.join("_")] = v;
  }
  c.privacy.redact = $("privacy_redact").checked;
  try { localStorage.setItem("extra_folders", $("extra_folders").value); } catch (e) {}
  return c;
}

$("llm_provider").addEventListener("change", () => {
  $("llm_base_url").value = "";
  $("llm_model").value = "";
  providerChanged();
});

$("saveCfg").onclick = async () => {
  try {
    await api("/api/config", collectSettings());
    $("saveOut").textContent = "已保存到本地 data/config.json";
    loadSettings();
  } catch (e) { $("saveOut").textContent = e.message; }
};

$("testMail").onclick = async () => {
  await api("/api/config", collectSettings());
  await running($("testMail"), $("mailTestOut"), async () => {
    const r = await api("/api/test/mail", {});
    $("mailTestOut").innerHTML = `✅ 登录成功。已发送文件夹：<b>${esc(r.sent_folder || "未识别")}</b><br>全部文件夹：${r.folders.map(esc).join("、")}`;
  }, "正在连接…");
};

$("testLLM").onclick = async () => {
  await api("/api/config", collectSettings());
  await running($("testLLM"), $("llmTestOut"), async () => {
    const r = await api("/api/test/llm", {});
    $("llmTestOut").innerHTML = `✅ 模型回复：${esc(r.reply)}`;
  }, "正在调用模型…");
};

$("fetchModels").onclick = async () => {
  await api("/api/config", collectSettings());
  await running($("fetchModels"), $("llmTestOut"), async () => {
    const r = await api("/api/llm/models", {});
    $("modelList").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    $("llmTestOut").innerHTML = `共 ${r.models.length} 个模型，点击“模型”输入框即可选择。`;
  }, "正在获取…");
};

// ---------------------------------------------------------------- 启动
(async function init() {
  await loadSettings();
  refreshSync();
  const [t, b, u, s] = await Promise.all(["todos", "bills", "unreplied", "search"].map((k) => api(`/api/results/${k}`)));
  $("todoOut").innerHTML = renderTodos(t);
  $("billOut").innerHTML = renderBills(b);
  $("unrOut").innerHTML = renderUnreplied(u);
  $("searchOut").innerHTML = renderSearch(s);
  if (s && s.query) $("searchQ").value = s.query;
  const c = await api("/api/config");
  if (!c.mail.username) document.querySelector('[data-tab="settings"]').click();
})();
