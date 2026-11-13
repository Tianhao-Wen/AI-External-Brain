"""AI 外脑 Demo 的前端应用逻辑（原生 JS，无构建步骤）。"""

APP_JS = r"""
(function(){
"use strict";

/* ── 状态 ─────────────────────────────────────────────── */
const S = {
  page: "dashboard",
  ws: null, connected: false, streaming: false,
  rpcSeq: 0, pending: new Map(),
  docs: [], docSel: null, docAnswer: null,
  meetings: [], meetingSel: null,
  todos: [], todoFilter: {status:"all", priority:"all"},
  todoSelected: new Set(),   // 待办页批量选择
  memories: [], memoryFilter: "all", memoryQuery: "",
  memSelected: new Set(),    // 记忆页批量选择
  sessions: [], sessionSel: "",
  pendingDel: "", pendingDelTimer: 0,   // 正在删除的会话（等 session_list 确认）
  emails: [],
  emailDrafts: {},        // 聊天里已渲染的邮件草稿卡片（id -> 草稿）
  mailCfg: null,
  attachments: [],          // {name, ext, data(base64), mediaType}
  chatNodes: null,
  // 连接建立时若用户不在对话页，历史重放事件先缓存在这里，
  // 等进入对话页再渲染（否则刷新/重进后会看不到历史）
  chatBuffer: [],
  // 页面内切走再切回：记住滚动位置，回来时恢复到原处而不是跳到顶部
  chatScrollTop: 0,
  // 正在等服务端把当前会话重放回来（此时先不要显示欢迎页）
  chatReplayPending: false,
  // 本轮正在流式输出的内容：期间切走再切回时用它把内容补回来
  liveText: "", liveTools: [],
  // 已渲染过的产物 id（同一产物可能被多个事件/重放重复送达，必须去重）
  renderedArtifacts: {},
  version: window.__AIEB_VERSION__ || "",
};

const ICONS = {
  dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 13h6V4H4zM14 20h6v-9h-6zM4 20h6v-4H4zM14 8h6V4h-6z"/></svg>',
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12a8 8 0 1 1-3.2-6.4L21 4l-1 4"/><path d="M8 11h8M8 15h5"/></svg>',
  knowledge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v18H6.5A2.5 2.5 0 0 1 4 18.5z"/><path d="M8 7h7M8 11h7"/></svg>',
  meeting: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M8 3v4M16 3v4M3 10h18M7 14h4"/></svg>',
  todo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 6h11M9 12h11M9 18h11"/><path d="M4 6l1.5 1.5L8 5M4 12l1.5 1.5L8 11M4 18l1.5 1.5L8 17"/></svg>',
  memory: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-1 5.8V16a3 3 0 0 0 4 2.8V20h3V4z"/><path d="M15 4a3 3 0 0 1 3 3 3 3 0 0 1 1 5.8V16a3 3 0 0 1-4 2.8"/></svg>',
  upload: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>',
  spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 3l1.8 4.6L18 9.4l-4.2 1.8L12 16l-1.8-4.8L6 9.4l4.2-1.8z"/><path d="M18 16l.9 2.1L21 19l-2.1.9L18 22l-.9-2.1L15 19l2.1-.9z"/></svg>',
  send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 12l16-8-6 8 6 8z"/></svg>',
};
const BRAND_MARK = '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" stroke-linecap="round"><circle cx="12" cy="6" r="2.2"/><circle cx="6" cy="16" r="2.2"/><circle cx="18" cy="16" r="2.2"/><path d="M12 8.2v3.2M10.3 12.9L7.4 14.4M13.7 12.9l2.9 1.5"/></svg>';

const NAV = [
  ["dashboard","工作台",ICONS.dashboard],
  ["chat","AI 对话",ICONS.chat],
  ["knowledge","知识库",ICONS.knowledge],
  ["meetings","会议中心",ICONS.meeting],
  ["todos","待办事项",ICONS.todo],
  ["memory","长期记忆",ICONS.memory],
];

/* ── 小工具 ───────────────────────────────────────────── */
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");

function fmtSize(bytes){
  const n = Number(bytes||0);
  if(n < 1024) return n + " B";
  if(n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  return (n/1024/1024).toFixed(1) + " MB";
}
function fmtTime(iso){
  if(!iso) return "—";
  const d = new Date(iso);
  if(isNaN(d.getTime())) return String(iso).slice(0,16);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = String(d.getHours()).padStart(2,"0");
  const mm = String(d.getMinutes()).padStart(2,"0");
  if(sameDay) return "今天 " + hh + ":" + mm;
  return (d.getMonth()+1) + "月" + d.getDate() + "日 " + hh + ":" + mm;
}
const STATUS_TEXT = {ready:"已解析", parsing:"解析中", failed:"解析失败", no_text:"无文字层",
                     pending:"待处理", doing:"进行中", done:"已完成",
                     draft:"草稿", sending:"发送中", sent:"已发送",
                     open:"未解决", closed:"已解决"};
const PRIORITY_TEXT = {high:"高", medium:"中", low:"低"};
const CATEGORY_TEXT = {person:"人物", time:"时间", place:"地点", topic:"主题",
                       decision:"决策", todo:"待办", note:"对话记忆", other:"其他"};

function toast(message, kind){
  const wrap = $("#toastWrap");
  const el = document.createElement("div");
  el.className = "toast " + (kind || "info");
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3600);
  setTimeout(() => el.remove(), 4000);
}

function markdown(text){
  const raw = String(text || "");
  if(window.marked && window.DOMPurify){
    try{
      const html = window.marked.parse(raw, {breaks:true, gfm:true});
      return window.DOMPurify.sanitize(html, {USE_PROFILES:{html:true}});
    }catch(e){ /* 回退到安全纯文本 */ }
  }
  return "<p>" + esc(raw).replace(/\n/g,"<br>") + "</p>";
}

/* 模态框：返回 Promise，resolve(true/false) */
function modal(opts){
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "mask";
    mask.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true">' +
        '<div class="modal-head"><h3>' + esc(opts.title || "") + '</h3>' +
          '<button class="icon-btn" data-act="close" aria-label="关闭">✕</button></div>' +
        '<div class="modal-body">' + (opts.body || "") + '</div>' +
        '<div class="modal-foot">' +
          '<button class="btn" data-act="cancel">' + esc(opts.cancelText || "取消") + '</button>' +
          '<button class="btn ' + (opts.danger ? "danger" : "primary") + '" data-act="ok">' +
            esc(opts.okText || "确定") + '</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(mask);
    const finish = (value) => { mask.remove(); resolve(value); };
    mask.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]");
      if(act){
        const a = act.dataset.act;
        if(a === "ok"){
          // onOk 可以是异步的（例如要等模型分析完）：此时保持弹窗打开并禁用按钮，
          // 任务结束后再关闭——避免用户以为"点完就完成了"。
          const busy = (state) => {
            mask.querySelectorAll("button").forEach(b => { b.disabled = state; });
          };
          let outcome;
          try { outcome = opts.onOk ? opts.onOk(mask) : true; }
          catch(err){ toast(String(err && err.message || err), "err"); return; }
          if(outcome === false) return;
          if(outcome && typeof outcome.then === "function"){
            busy(true);
            outcome.then(v => { busy(false); if(v !== false) finish(true); },
                         () => busy(false));
            return;
          }
          finish(true);
        } else { finish(false); }
        return;
      }
      if(e.target === mask) finish(false);
    });
    const focusable = mask.querySelector("input,textarea,button.primary");
    if(focusable) focusable.focus();
    mask.addEventListener("keydown", (e) => {
      if(e.key === "Escape") finish(false);
      if(e.key === "Enter" && (e.metaKey || e.ctrlKey)) finish(true);
    });
  });
}

async function confirmAction(title, text, okText, danger){
  return modal({title, body:"<p class='muted'>" + esc(text) + "</p>", okText: okText || "确认",
                danger: danger !== false});
}

/* ── WebSocket / RPC ─────────────────────────────────── */
function wsUrl(){
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return proto + "//" + location.host + "/ws";
}
function connect(){
  if(S.ws && (S.ws.readyState === 0 || S.ws.readyState === 1)) return;
  const ws = new WebSocket(wsUrl(), ["aieb.v1"]);
  S.ws = ws;
  ws.onopen = () => { S.connected = true; updateStatus(); };
  ws.onclose = () => {
    S.connected = false; S.streaming = false; updateStatus("连接已断开，正在重连…");
    setTimeout(connect, 2000);
  };
  ws.onerror = () => updateStatus("连接异常");
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch(e){ return; }
    handleEvent(msg.type, msg.data || {});
  };
  // 自动化验收用：模拟一次真实的“网络瞬断”。只调用浏览器原生 close()，
  // 走的还是上面 onclose → 2 秒后自动重连的同一条路径，不改变任何业务逻辑。
  window.__aiebDropSocket = () => {
    try { if(S.ws) S.ws.close(); } catch(e){ /* 已关闭 */ }
  };
}

function send(type, data){
  if(!S.ws || S.ws.readyState !== 1) { toast("与服务端的连接尚未建立，请稍后重试", "err"); return false; }
  S.ws.send(JSON.stringify({type, data: data || {}}));
  return true;
}

function whenConnected(){
  return new Promise((resolve, reject) => {
    if(S.ws && S.ws.readyState === 1) { resolve(); return; }
    const started = Date.now();
    const timer = setInterval(() => {
      if(S.ws && S.ws.readyState === 1){ clearInterval(timer); resolve(); }
      else if(Date.now() - started > 15000){ clearInterval(timer); reject(new Error("与服务端的连接超时，请刷新页面")); }
    }, 60);
  });
}

function rpc(op, payload, timeoutMs){
  return new Promise((resolve, reject) => {
    const id = "r" + (++S.rpcSeq);
    S.pending.set(id, {resolve, reject});
    whenConnected().then(() => {
      const ok = send("brain_request", Object.assign({op, req_id:id}, payload || {}));
      if(!ok){ S.pending.delete(id); reject(new Error("未连接")); }
    }).catch((err) => {
      S.pending.delete(id);
      reject(err);
    });
    setTimeout(() => {
      if(S.pending.has(id)){
        S.pending.delete(id);
        reject(new Error("请求超时，请重试"));
      }
    }, timeoutMs || 180000);
  });
}

function updateStatus(text){
  const dot = $("#connDot"), label = $("#connLabel");
  if(!dot || !label) return;
  dot.className = "dot " + (S.connected ? "on" : "off");
  label.textContent = text || (S.connected ? "已连接" : "连接中…");
}

function handleEvent(type, data){
  switch(type){
    case "connected":
      S.sessionSel = data.session || "";
      updateStatus();
      break;
    case "session_switched":
      // 服务端切了会话（新建 / 打开历史）：选中态必须跟着变，否则侧边栏高亮错位
      S.sessionSel = (data && data.id) || "";
      if(S.page === "chat") renderSessions();
      break;
    case "session_list":
      S.sessions = (data.sessions || []);
      if(S.page === "chat") renderSessions();
      // 删除是异步的：只有列表里真的没了才提示成功，否则会“提示删了但还在”
      if(S.pendingDel && !(S.sessions || []).some(s => s.id === S.pendingDel)){
        if(S.pendingDelTimer){ clearTimeout(S.pendingDelTimer); S.pendingDelTimer = 0; }
        S.pendingDel = "";
        toast("已删除该对话", "ok");
      }
      break;
    case "replay_done":
      if(data && data.session) S.sessionSel = data.session;
      finishChatReplay();
      break;
    case "clear":
      if(S.chatNodes) S.chatNodes.innerHTML = "";
      S.chatBuffer = [];
      // DOM 已清空，产物去重表也要重置（否则重放的卡片会被当成重复项跳过）
      S.renderedArtifacts = {};
      break;
    case "replay_user":
      if(!chatHost()){ S.chatBuffer.push({type:"replay_user", data}); break; }
      appendUserBubble(data.content, data.documents || [], true);
      break;
    case "replay_assistant":
      if(!chatHost()){ S.chatBuffer.push({type:"replay_assistant", data}); break; }
      appendReplayAssistant(data);
      break;
    case "user_message_saved":
      break;
    case "stream_text":
      S.liveText += (data.text || "");
      streamAppend(data.text || "");
      break;
    case "stream_end":
      break;
    case "tool_use":
      S.liveTools.push({name: data.toolName, running: true});
      streamTool(data.toolName, "", true);
      break;
    case "tool_result":
      S.liveTools.push({name: data.toolName, running: false, error: !!data.isError});
      streamTool(data.toolName, data.output || "", false, data.isError);
      break;
    case "turn_complete":
      break;
    case "loop_complete":
      finishStream();
      break;
    case "artifacts":
      appendArtifacts(data.files || []);
      break;
    case "email_draft":
      // 工具生成的邮件草稿：直接在聊天里出卡片，点“编辑并发送”即可真实发送
      if(!S.chatNodes){ S.chatBuffer.push({type:"email_draft", data}); break; }
      appendEmailDraftCard(data);
      break;
    case "error":
      finishStream();
      toast(prettyError(data.message), "err");
      streamErrorBubble(prettyError(data.message));
      break;
    case "permission_request":
      handlePermission(data);
      break;
    case "providers":
      S.providers = data.providers || [];
      S.activeProvider = data.active || "";
      break;
    case "brain_response": {
      const id = data.req_id;
      const entry = S.pending.get(id);
      if(!entry) break;
      S.pending.delete(id);
      if(data.ok) entry.resolve(data.data || {});
      else entry.reject(new Error(data.error || "操作失败"));
      break;
    }
    case "memory_state":
      break;
    default:
      break;
  }
}

/* 来源文案：后端给的 source_label 有时本身就带"来自…"，
   前端再拼一个"来源："就会变成"来源：来自 3 个已删除的对话"，这里统一去重。 */
function sourceText(label){
  const raw = String(label == null ? "" : label).trim();
  if(!raw) return "来源待确认";
  if(/^(来自|来源)/.test(raw)) return raw;
  return "来源：" + raw;
}

function prettyError(message){
  const raw = String(message || "");
  if(/timeout|timed out|超时/i.test(raw)) return "请求超时了，请稍后重试。";
  if(/api key|unauthorized|401|鉴权/i.test(raw)) return "模型服务未授权，请检查服务端的 API Key 配置。";
  if(/rate limit|429|限流/i.test(raw)) return "调用过于频繁，请稍等片刻再试。";
  if(/connection|network|网络/i.test(raw)) return "网络连接异常，请检查服务端网络。";
  // 去掉可能出现的绝对路径与调用栈细节
  return raw.replace(/[A-Za-z]:\\[^\s"']+/g, "<文件>").split("\n")[0].slice(0, 200) || "操作失败，请重试。";
}

/* ── 导航 ─────────────────────────────────────────────── */
function buildSidebar(){
  $("#navList").innerHTML = NAV.map(([key,label,icon]) =>
    '<button class="nav-item" data-page="' + key + '" aria-label="' + label + '">' +
      icon + '<span class="label">' + label + '</span>' +
      '<span class="nav-badge" data-badge="' + key + '" hidden></span>' +
    '</button>').join("");
  $("#navList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-page]");
    if(btn) go(btn.dataset.page);
  });
  $("#sidebarFoot").innerHTML =
    '<div class="side-meta"><span class="dot" id="connDot"></span><span id="connLabel">连接中…</span></div>' +
    '<div class="side-meta"><span>Enterprise AI Workspace</span></div>';
}

function setActiveNav(){
  $$(".nav-item").forEach(b => b.classList.toggle("active", b.dataset.page === S.page));
}

function go(page){
  // 离开对话页时记住滚动位置，回来时（重放完成后）恢复到原处
  if(S.page === "chat" && S.chatNodes) S.chatScrollTop = S.chatNodes.scrollTop;
  S.page = page;
  setActiveNav();
  const meta = {
    dashboard: ["工作台", "把企业知识与会议讨论转化为清晰、可执行的行动"],
    chat: ["AI 对话", "对话、联网检索、知识问答与汇报材料生成"],
    knowledge: ["知识库", "上传 PDF / TXT，基于原文回答并给出页码引用"],
    meetings: ["会议中心", "结构化会议纪要、风险识别与待办跟踪"],
    todos: ["待办事项", "跨会议统一跟踪负责人、截止时间与完成状态"],
    memory: ["长期记忆", "人物、时间、地点、决策与待办的跨会话记忆"],
  }[page];
  $("#pageTitle").textContent = meta[0];
  $("#pageSub").textContent = meta[1];
  $("#topActions").innerHTML = "";
  $("#content").className = "content";
  if(page === "dashboard") renderDashboard();
  else if(page === "chat") renderChat();
  else if(page === "knowledge") renderKnowledge();
  else if(page === "meetings") renderMeetings();
  else if(page === "todos") renderTodos();
  else if(page === "memory") renderMemory();
}

/* ── 工作台 ───────────────────────────────────────────── */
async function renderDashboard(){
  const root = $("#content");
  root.innerHTML = '<div class="hero"><h2>欢迎使用 AI 外脑</h2>' +
    '<p>把散落的知识文档、会议记录与讨论结论，整理成可检索、可预警、可追踪、可交付的行动项。</p></div>' +
    '<div class="quick-grid">' +
      quickCard("upload","上传知识文档","把 PDF / TXT 变成可问答的知识库","brand") +
      quickCard("meeting","分析会议记录","识别风险、生成待办并发送提醒","teal") +
      quickCard("chat","开始 AI 对话","结合知识库与联网检索开展讨论","navy") +
      quickCard("ppt","生成汇报 PPT","把结论整理成可直接汇报的 .pptx","amber") +
    '</div>' +
    '<div id="dashStats" class="stat-grid"></div>' +
    '<div class="cols"><div class="card" id="dashMeetings"></div><div class="card" id="dashSide"></div></div>';

  try{
    const data = await rpc("dashboard");
    const st = data.stats || {};
    $("#dashStats").innerHTML =
      stat("已上传知识文档", st.documents || 0, "") +
      stat("已分析会议", st.meetings || 0, "") +
      stat("待完成任务", st.todos_open || 0, "") +
      stat("未解决风险", st.risks_open || 0, (st.risks_open ? "warn" : ""));
    $("#dashMeetings").innerHTML =
      '<div class="card-head"><h3>最近会议</h3><button class="btn sm ghost" data-jump="meetings">查看全部</button></div>' +
      '<div class="card-body">' + (data.recent_meetings && data.recent_meetings.length
        ? data.recent_meetings.map(m =>
            '<div class="list-item"><div class="li-main"><div class="li-title">' + esc(m.title) + '</div>' +
            '<div class="li-sub">' + fmtTime(m.created_at) + ' · ' + (m.participants||[]).length + ' 位参与人 · ' +
            (m.risk_count||0) + ' 条风险 · ' + (m.todo_count||0) + ' 条待办</div></div>' +
            '<button class="btn sm" data-open-meeting="' + esc(m.id) + '">查看</button></div>').join("")
        : emptyState("还没有会议记录","上传一份会议文件（TXT / MD / DOCX / PDF），AI 会自动整理成结构化纪要")) + '</div>';

    const todos = data.recent_todos || [];
    const acts = data.activities || [];    $("#dashSide").innerHTML =
      '<div class="card-head"><h3>最近待办</h3><button class="btn sm ghost" data-jump="todos">全部待办</button></div>' +
      '<div class="card-body">' + (todos.length ? todos.map(t =>
        '<div class="list-item"><div class="li-main"><div class="li-title">' + esc(t.content) + '</div>' +
        '<div class="li-sub">负责人 ' + esc(t.owner || "待确认") + ' · 截止 ' + esc(t.due_date || "待确认") +
        ' · 来源 ' + esc(t.meeting_title || "手动创建") + '</div></div>' +
        '<span class="chip ' + esc(t.priority) + '">' + (PRIORITY_TEXT[t.priority] || "中") + '优先级</span></div>').join("")
        : emptyState("暂无待办","会议分析后会自动生成待办事项")) +
      '</div>' +
      '<div class="card-head" style="border-top:1px solid var(--line)"><h3>最新活动</h3>' +
        (acts.length ? '<button class="btn sm ghost" id="actClearAll">清空</button>' : '') +
      '</div>' +
      '<div class="card-body">' + (acts.length ? acts.map(a =>
        '<div class="list-item"><span class="feed-dot"></span><div class="li-main">' +
        '<div class="li-title">' + esc(a.title) + '</div>' +
        '<div class="li-sub">' + fmtTime(a.created_at) + (a.detail ? " · " + esc(a.detail) : "") + '</div></div>' +
        '<button class="icon-btn act-del" data-act-del="' + esc(a.id) + '" title="删除这条活动" aria-label="删除这条活动">✕</button>' +
        '</div>').join("")
        : '<p class="muted small">还没有任何活动记录。</p>') + '</div>';

    // 卡片内的跳转/查看按钮：同样用赋值而不是 addEventListener，
    // 否则每次回到工作台都会再挂一个监听器，点击会触发多次跳转。
    $("#content").onclick = async (e) => {
      const q = e.target.closest("[data-quick]");
      if(q){
        const k = q.dataset.quick;
        if(k === "upload") go("knowledge");
        else if(k === "meeting") go("meetings");
        else if(k === "chat") go("chat");
        else if(k === "ppt") openPPTModal();
        return;
      }
      // 单条活动记录：直接从数据库删除（不是前端隐藏）
      const actDel = e.target.closest("[data-act-del]");
      if(actDel){
        const ok = await confirmAction("删除活动记录", "这条活动记录会从数据库中彻底删除，删除后无法恢复。", "删除");
        if(!ok) return;
        try{
          await rpc("activities.delete", {id: actDel.dataset.actDel});
          toast("已删除该条活动", "ok");
          renderDashboard();
        }catch(err){ toast(err.message, "err"); }
        return;
      }
      // 批量清空：一次删掉「最新活动」里的全部记录
      if(e.target.closest("#actClearAll")){
        const ok = await confirmAction(
          "清空活动记录",
          "将删除全部 " + acts.length + " 条活动记录（不可恢复）。知识库文档、会议、待办等业务数据不受影响。",
          "清空"
        );
        if(!ok) return;
        try{
          const res = await rpc("activities.clear");
          toast("已清空 " + (res.deleted || 0) + " 条活动记录", "ok");
          renderDashboard();
        }catch(err){ toast(err.message, "err"); }
        return;
      }
      const jump = e.target.closest("[data-jump]");
      if(jump) go(jump.dataset.jump);
      const open = e.target.closest("[data-open-meeting]");
      if(open){ S.meetingSel = open.dataset.openMeeting; go("meetings"); }
    };
  }catch(err){
    $("#dashStats").innerHTML = "";
    toast(err.message, "err");
  }
}
function quickCard(kind, title, desc, tone){
  const icon = {upload:ICONS.upload, meeting:ICONS.meeting, chat:ICONS.chat, ppt:ICONS.spark}[kind];
  return '<button class="quick" data-quick="' + kind + '">' +
    '<span class="q-icon ' + tone + '">' + icon + '</span>' +
    '<span class="q-text"><strong>' + title + '</strong>' +
    '<span class="q-desc">' + desc + '</span></span></button>';
}
function stat(label, value, tone){
  return '<div class="card stat"><div class="label">' + label + '</div>' +
    '<div class="value ' + (tone||"") + '">' + value + '</div></div>';
}
function emptyState(title, sub){
  return '<div class="empty"><span class="emoji">◇</span><h4>' + esc(title) + '</h4>' +
    '<p class="small">' + esc(sub || "") + '</p></div>';
}

/* ── AI 对话 ──────────────────────────────────────────── */
function renderChat(){
  const root = $("#content");
  root.className = "content flush";
  root.innerHTML =
    '<div class="chat">' +
      '<aside class="chat-sessions"><div class="cs-head">' +
        '<button class="btn primary sm grow" id="newSession">+ 新建对话</button></div>' +
        '<div class="cs-list" id="csList"></div></aside>' +
      '<div class="chat-main">' +
        '<div class="chat-scroll" id="chatScroll"></div>' +
        '<div class="chat-composer">' +
          '<div class="quick-row">' +
            '<button class="quick-chip" data-chip="upload">上传知识</button>' +
            '<button class="quick-chip" data-chip="meeting">分析会议</button>' +
            '<button class="quick-chip" data-chip="search">联网搜索</button>' +
            '<button class="quick-chip" data-chip="ppt">制作 PPT</button>' +
          '</div>' +
          '<div id="attachTags"></div>' +
          '<div class="composer-box">' +
            '<textarea id="composer" rows="1" placeholder="描述你的问题，或让我基于知识库、会议记录与联网信息给出结论…"></textarea>' +
            '<div class="composer-actions">' +
              '<button class="icon-btn" id="attachBtn" title="上传文件" aria-label="上传文件">' + ICONS.upload + '</button>' +
              '<button class="btn primary" id="sendBtn">发送</button>' +
              '<button class="btn danger" id="stopBtn" hidden>停止</button>' +
            '</div>' +
          '</div>' +
          '<p class="hint">AI 生成内容请核对后使用；文件仅用于本次会话与知识库检索。</p>' +
        '</div>' +
      '</div>' +
    '</div>';

  S.chatNodes = $("#chatScroll");
  // 聊天里的“查看详情”/“编辑并发送”按钮：统一在这里代理
  S.chatNodes.onclick = (e) => {
    const open = e.target.closest("[data-open-meeting]");
    if(open){ S.meetingSel = open.dataset.openMeeting; go("meetings"); return; }
    const mail = e.target.closest("[data-email-open]");
    if(mail){
      const draft = S.emailDrafts[mail.dataset.emailOpen];
      if(draft) openEmailPreview(draft);
      else toast("草稿已过期，请让 AI 重新生成", "warn");
    }
  };
  S.attachments = [];
  S.chatReplayPending = true;
  renderSessions();
  // 先把连接期间缓存的历史消息放出来；没有缓存就主动向服务端要一次当前会话。
  // 这样"切到别的页面再切回来"不会退化成欢迎页，也不需要刷新浏览器。
  if(S.chatBuffer.length){
    const buffered = S.chatBuffer;
    S.chatBuffer = [];
    buffered.forEach(ev => {
      if(ev.type === "replay_user") appendUserBubble(ev.data.content, ev.data.documents || [], true);
      else if(ev.type === "email_draft") appendEmailDraftCard(ev.data);
      else appendReplayAssistant(ev.data);
    });
    finishChatReplay();
  }else if(S.connected){
    send("replay_current");
    // 兜底：服务端异常时也要结束等待状态，不能让界面一直空着
    setTimeout(() => { if(S.chatReplayPending) finishChatReplay(); }, 4000);
  }else{
    finishChatReplay();
  }

  const textarea = $("#composer");
  textarea.addEventListener("input", () => {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight, 160) + "px";
  });
  textarea.addEventListener("keydown", (e) => {
    if(e.key === "Enter" && !e.shiftKey){ e.preventDefault(); submitMessage(); }
  });
  $("#sendBtn").addEventListener("click", submitMessage);
  $("#stopBtn").addEventListener("click", () => { send("cancel"); });
  $("#newSession").addEventListener("click", async () => {
    if(S.streaming){ toast("当前回复尚未结束", "warn"); return; }
    send("new_session");
    S.chatNodes.innerHTML = "";
    S.chatBuffer = [];
    S.renderedArtifacts = {};
    S.liveText = ""; S.liveTools = [];
    S.chatScrollTop = 0;
    S.sessionSel = "";
    renderSessions();
  });
  $("#attachBtn").addEventListener("click", () => pickChatFile());
  $(".quick-row").addEventListener("click", async (e) => {
    const chip = e.target.closest("[data-chip]");
    if(!chip) return;
    const k = chip.dataset.chip;
    if(k === "upload"){ pickChatFile(); }
    else if(k === "meeting"){ go("meetings"); }
    else if(k === "search"){ textarea.value = "请联网检索并回答："; textarea.focus(); }
    else if(k === "ppt"){ openPPTModal(); }
  });
  renderAttachTags();
}

/* 对话页渲染收尾：欢迎页 / 滚动位置 / 未结束的流式内容 */
function chatHost(){
  if(S.page !== "chat" || !S.chatNodes) return null;
  // 页面重建后旧的 #chatScroll 已脱离文档，不能再往里写
  return document.body.contains(S.chatNodes) ? S.chatNodes : null;
}
function finishChatReplay(){
  S.chatReplayPending = false;
  const host = chatHost();
  if(!host) return;
  // 回到对话页时如果这一轮还在生成：把已收到的正文与工具进度补回来
  if(S.streaming && (S.liveText || S.liveTools.length)){
    ensureStreamBubble();
    const box = host.querySelector("[data-streaming='1']");
    if(box){
      const tools = box.querySelector("[data-tools]");
      if(tools && S.liveTools.length){
        tools.innerHTML = S.liveTools.map(t => t.running
          ? '<div class="tool-line"><span class="spinner"></span><span>' + esc(toolText(t.name)) + '</span></div>'
          : '<div class="tool-line"><span>' + (t.error ? "⚠️ " : "✓ ") +
            esc(toolText(t.name).replace(/…$/, "完成")) + '</span></div>').join("");
      }
      const ans = box.querySelector("[data-ans]");
      if(ans && S.liveText) ans.innerHTML = markdown(S.liveText);
    }
  }
  if(!host.childElementCount){
    host.innerHTML = CHAT_WELCOME;
  }
  host.scrollTop = S.chatScrollTop > 0 ? S.chatScrollTop : host.scrollHeight;
}

const CHAT_WELCOME = '<div class="msg" data-welcome="1"><div class="avatar ai">AI</div><div class="bubble">' +
  '<p><strong>我是你的 AI 外脑。</strong>可以直接提问，也可以让我：</p>' +
  '<ul><li>基于已上传的 PDF 回答问题（带页码引用）</li>' +
  '<li>分析会议记录并生成风险、待办与提醒邮件</li>' +
  '<li>联网检索时效性信息并给出可访问来源</li>' +
  '<li>把结论整理成可下载的 PPTX 汇报文件</li></ul></div></div>';

/* 真实消息一到，就把欢迎块拿掉（避免“欢迎语 + 历史”同时出现） */
function clearWelcome(){
  const host = chatHost();
  if(!host) return;
  host.querySelectorAll('[data-welcome="1"]').forEach(el => el.remove());
}

function renderSessions(){
  const list = $("#csList");
  if(!list) return;
  const sessions = S.sessions || [];
  list.innerHTML = sessions.length ? sessions.map(s =>
    '<div class="cs-item ' + (s.id === S.sessionSel ? "active" : "") + '" data-sid="' + esc(s.id) + '">' +
      '<div class="cs-main"><div class="t truncate" data-title="' + esc(s.id) + '">' +
        esc(s.title || "未命名对话") + '</div>' +
      '<div class="m">' + fmtTime(s.lastActive) + ' · ' + (s.messageCount||0) + ' 条</div></div>' +
      '<div class="cs-ops">' +
        '<button class="icon-btn" data-rename="' + esc(s.id) + '" title="重命名" aria-label="重命名">✎</button>' +
        '<button class="icon-btn" data-del="' + esc(s.id) + '" title="删除" aria-label="删除">🗑</button>' +
      '</div>' +
    '</div>').join("")
    : '<p class="muted small" style="padding:10px">还没有历史对话。</p>';
  if(!list.dataset.bound){
    list.dataset.bound = "1";
    list.addEventListener("click", (e) => {
      const ren = e.target.closest("[data-rename]");
      if(ren){ e.stopPropagation(); startRenameSession(ren.dataset.rename); return; }
      const del = e.target.closest("[data-del]");
      if(del){ e.stopPropagation(); confirmDeleteSession(del.dataset.del); return; }
      const item = e.target.closest("[data-sid]");
      if(!item) return;
      if(S.streaming){ toast("请等待当前回复结束", "warn"); return; }
      if(item.dataset.sid === S.sessionSel){
        // 点的是当前会话：也要把历史重放一遍（可能刚从别的页面切回来）
        S.chatReplayPending = true;
        send("replay_current");
        return;
      }
      S.sessionSel = item.dataset.sid;
      renderSessions();
      S.chatReplayPending = true;
      send("load_session", {id: S.sessionSel});
    });
  }
}

/* 重命名：直接把标题变成输入框，Enter/失焦提交，Esc 取消 */
function startRenameSession(sid){
  const list = $("#csList");
  const titleEl = list && list.querySelector('[data-title="' + sid + '"]');
  if(!titleEl || titleEl.querySelector("input")) return;
  const current = titleEl.textContent.trim() === "未命名对话" ? "" : titleEl.textContent.trim();
  const item = titleEl.closest(".cs-item");
  const input = document.createElement("input");
  input.className = "cs-input";
  input.value = current;
  input.maxLength = 50;
  titleEl.textContent = "";
  titleEl.appendChild(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (commit) => {
    if(done) return;
    done = true;
    const value = input.value.trim();
    if(commit && value && value !== current){
      send("rename_session", {id: sid, title: value});
      titleEl.textContent = value;          // 立即反馈，随后由 session_list 校正
    }else{
      titleEl.textContent = current || "未命名对话";
    }
    if(item) item.classList.remove("editing");
  };
  input.addEventListener("keydown", (e) => {
    if(e.key === "Enter"){ e.preventDefault(); finish(true); }
    else if(e.key === "Escape"){ e.preventDefault(); finish(false); }
  });
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("click", (e) => e.stopPropagation());
  if(item) item.classList.add("editing");
}

/* 删除：二次确认，并让用户决定是否同时清掉该会话产生的长期记忆 */
async function confirmDeleteSession(sid){
  if(S.streaming){ toast("请等待当前回复结束", "warn"); return; }
  const meta = (S.sessions || []).find(s => s.id === sid) || {};
  let purge = true;
  const ok = await modal({
    title: "删除这段对话？",
    danger: true,
    okText: "删除",
    cancelText: "取消",
    body:
      '<p>即将删除对话：<strong>' + esc(meta.title || "未命名对话") + '</strong></p>' +
      '<p class="muted small">只删除这段对话记录与它产生的文件产物；' +
      '知识库文档、会议记录、待办不受影响。</p>' +
      '<label class="row small" style="margin-top:10px;gap:8px">' +
        '<input type="checkbox" id="purgeMem" checked /> 同时删除这段对话产生的长期记忆' +
      '</label>',
    // 弹窗关闭后 body 已经被移除，框里的值必须在关闭前读出来
    onOk: (mask) => {
      const box = mask.querySelector("#purgeMem");
      purge = box ? !!box.checked : true;
      return true;
    },
  });
  if(!ok) return;
  send("delete_session", {id: sid, purge_memories: purge});
  // 等服务端把新列表推回来再提示成功；超时未消失就如实说没删掉
  S.pendingDel = sid;
  if(S.pendingDelTimer) clearTimeout(S.pendingDelTimer);
  S.pendingDelTimer = setTimeout(() => {
    if(S.pendingDel !== sid) return;
    S.pendingDel = "";
    S.pendingDelTimer = 0;
    toast("删除未完成：会话文件被其它进程占用（可能同一目录还开着另一个服务实例）", "err");
  }, 8000);
}

function renderAttachTags(){
  const wrap = $("#attachTags");
  if(!wrap) return;
  wrap.innerHTML = S.attachments.map((a, i) => {
    // PDF 固定进知识库；TXT/MD/DOCX 默认当会议记录，用户可改回“存知识库”
    const purpose = a.ext === "pdf"
      ? '<span class="att-kind">知识库</span>'
      : '<select class="att-sel" data-sel="' + i + '">' +
          '<option value="meeting"' + (a.purpose === "meeting" ? " selected" : "") + '>作为会议记录</option>' +
          '<option value="knowledge"' + (a.purpose === "knowledge" ? " selected" : "") + '>存入知识库</option>' +
        '</select>';
    return '<span class="attach-tag" data-att="' + i + '">' + esc(a.name) + purpose +
      '<button class="icon-btn" style="width:18px;height:18px" data-rm="' + i + '" aria-label="移除">✕</button></span>';
  }).join("");
  wrap.onclick = (e) => {
    const btn = e.target.closest("[data-rm]");
    if(!btn) return;
    S.attachments.splice(Number(btn.dataset.rm), 1);
    renderAttachTags();
  };
  wrap.onchange = (e) => {
    const sel = e.target.closest("[data-sel]");
    if(!sel) return;
    const item = S.attachments[Number(sel.dataset.sel)];
    if(item) item.purpose = sel.value;
  };
}

// 与后端 MAX_UPLOAD_BYTES (15MB) 保持一致，前端先拦一道，避免白传
const CHAT_UPLOAD_LIMIT = 15 * 1024 * 1024;
const CHAT_ALLOWED_EXT = ["pdf", "txt", "md", "docx"];

function pickChatFile(){
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".pdf,.txt,.md,.docx";
  input.onchange = async () => {
    const file = input.files && input.files[0];
    if(!file) return;
    try{
      const ext = (file.name.split(".").pop() || "").toLowerCase();
      if(CHAT_ALLOWED_EXT.indexOf(ext) < 0){
        toast("暂不支持 ." + ext + " 文件，请上传 PDF / TXT / MD / DOCX", "err");
        return;
      }
      if(!file.size){ toast("这是个空文件，换一个再试", "err"); return; }
      if(file.size > CHAT_UPLOAD_LIMIT){
        toast("文件超过 15MB，请拆分后再上传", "err");
        return;
      }
      // 注意：以前这里用 String.fromCharCode.apply(null, bytes)，文件稍大
      // （约 100KB 以上）就会 "Maximum call stack size exceeded" 直接抛错，
      // 表现为"点上传没反应"——TXT 小所以能用、PDF 就不能用。
      // 统一改用 FileReader 的 fileToBase64()，任意大小都安全。
      const b64 = await fileToBase64(file);
      if(!b64) throw new Error("读取文件内容失败");
      S.attachments.push({
        name: file.name, ext, size: file.size, data: b64,
        // PDF → 知识库（可基于页码提问）；TXT/MD/DOCX → 默认会议分析（可改）
        purpose: ext === "pdf" ? "knowledge" : "meeting",
        mediaType: ext === "pdf" ? "application/pdf" : (ext === "docx"
          ? "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          : "text/plain"),
      });
      renderAttachTags();
      toast(ext === "pdf"
        ? "已添加 PDF：发送后解析入知识库"
        : "已添加 " + ext.toUpperCase() + "：发送后默认按会议记录分析（可切换用途）", "ok");
    }catch(err){
      toast("添加文件失败：" + (err && err.message ? err.message : err), "err");
    }
  };
  input.click();
}

function ensureStreamBubble(){
  const host = chatHost();
  if(!host) return;
  clearWelcome();
  if(host.querySelector("[data-streaming='1']")) return;
  const wrap = document.createElement("div");
  wrap.className = "msg";
  wrap.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
    '<div class="tools" data-tools></div><div class="ans" data-ans></div></div>';
  wrap.dataset.streaming = "1";
  host.appendChild(wrap);
  host.scrollTop = host.scrollHeight;
}

const TOOL_TEXT = {
  SearchKnowledge:"正在检索知识库…", WebSearch:"正在查询最新信息…", WebFetch:"正在读取网页内容…",
  AnalyzeMeeting:"正在分析会议内容…", CreatePresentation:"正在制作汇报文稿…",
  ListOpenTodos:"正在整理待办事项…", DraftReminderEmail:"正在起草提醒邮件…",
  ReadFile:"正在读取文件…", WriteFile:"正在生成文件…", EditFile:"正在修改文件…",
  Glob:"正在查找文件…", Grep:"正在检索内容…", ToolSearch:"正在准备工具…",
  Bash:"正在执行操作…", MCP:"正在调用外部服务…", LoadSkill:"正在加载能力…"
};
function toolText(name){ return TOOL_TEXT[name] || "正在处理…"; }

let streamBuf = "";
function streamAppend(text){
  const host = chatHost();
  if(!host) return;
  ensureStreamBubble();
  streamBuf += text;
  const ans = host.querySelector("[data-streaming='1'] [data-ans]");
  if(ans) ans.innerHTML = markdown(streamBuf);
  host.scrollTop = host.scrollHeight;
}
function streamTool(name, output, running, isError){
  const host = chatHost();
  if(!host) return;
  ensureStreamBubble();
  const tools = host.querySelector("[data-streaming='1'] [data-tools]");
  if(!tools) return;
  if(running){
    const line = document.createElement("div");
    line.className = "tool-line";
    line.dataset.tool = name || "";
    line.innerHTML = '<span class="spinner"></span><span>' + esc(toolText(name)) + '</span>';
    tools.appendChild(line);
    host.scrollTop = host.scrollHeight;
    return;
  }
  const pending = tools.querySelector('[data-tool="' + (name||"") + '"] .spinner');
  const line = pending ? pending.closest(".tool-line") : null;
  if(line){
    line.innerHTML = '<span>' + (isError ? "⚠️ " : "✓ ") + esc(toolText(name).replace(/…$/,"完成")) + '</span>';
  }
  S.streaming = true;
  syncComposer();
}
function finishStream(){
  S.streaming = false;
  const host = chatHost();
  const box = host && host.querySelector("[data-streaming='1']");
  if(box){
    delete box.dataset.streaming;
    const ans = box.querySelector("[data-ans]");
    if(ans && !ans.innerHTML.trim()) ans.innerHTML = "<p class='muted small'>（无文本输出）</p>";
    if(ans && streamBuf) ans.innerHTML = markdown(streamBuf);
  }
  streamBuf = "";
  S.liveText = "";
  S.liveTools = [];
  syncComposer();
  if(S.page === "dashboard") { /* 保持现状 */ }
  refreshSessionList();
}
function streamErrorBubble(message){
  const host = chatHost();
  if(!host) return;
  const el = document.createElement("div");
  el.className = "msg";
  el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
    '<div class="banner err" style="margin:0">' + esc(message) + '</div></div>';
  host.appendChild(el);
}
function syncComposer(){
  const send = $("#sendBtn"), stop = $("#stopBtn");
  if(send) send.hidden = S.streaming;
  if(stop) stop.hidden = !S.streaming;
}

function appendUserBubble(content, documents, replay){
  const host = chatHost();
  if(!host) return;
  clearWelcome();
  const el = document.createElement("div");
  el.className = "msg user";
  const docTags = (documents || []).map(d => {
    // 附件类型标签按 media_type 判断，不要再把所有附件都写成「PDF 文档」
    const mt = String(d.media_type || "").toLowerCase();
    const label = mt.indexOf("pdf") >= 0 ? "PDF 文档"
      : mt.indexOf("word") >= 0 ? "Word 文档"
      : mt.indexOf("text") >= 0 ? "文本文件" : "附件";
    return '<div class="attach-card"><span class="fname">' + esc(d.name || "附件") + '</span>' +
      '<span class="muted small">' + label + '</span></div>';
  }).join("");
  el.innerHTML = '<div class="avatar me">我</div><div class="bubble">' +
    (content ? "<p>" + esc(content).replace(/\n/g,"<br>") + "</p>" : "") + docTags + '</div>';
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
}

/* 产物去重：同一个 artifact 可能被实时事件与历史重放各送一次，
   按稳定 id 去重，同一轮里不会出现两张一模一样的下载卡片。 */
function freshArtifacts(files){
  const out = [];
  (files || []).forEach(a => {
    if(!a) return;
    const key = String(a.id || a.name || "");
    if(!key || S.renderedArtifacts[key]) return;
    S.renderedArtifacts[key] = true;
    out.push(a);
  });
  return out;
}

function appendReplayAssistant(data){
  const host = chatHost();
  if(!host) return;
  clearWelcome();
  const el = document.createElement("div");
  el.className = "msg";
  const tools = (data.tools || []).map(t =>
    '<div class="tool-line"><span>✓ ' + esc(toolText(t.name).replace(/…$/,"完成")) + '</span></div>').join("");
  let artifacts = "";
  const files = freshArtifacts(data.artifacts);
  if(files.length){
    artifacts = '<div>' + files.map(a => artifactCard(a)).join("") + '</div>';
  }
  el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
    (tools ? '<div class="tools">' + tools + '</div>' : "") +
    '<div class="ans">' + markdown(data.content || "") + '</div>' + artifacts + '</div>';
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
  // 历史里的邮件草稿卡片（刷新/切页后仍要能继续发送）
  (data.email_drafts || []).forEach(d => appendEmailDraftCard(d));
}

function artifactCard(a){
  const isPpt = (a.ext || "").toLowerCase() === "pptx";
  return '<div class="attach-card"><div class="grow"><div class="fname">' + esc(a.name) + '</div>' +
    '<div class="muted small">' + (isPpt ? "演示文稿" : "文件") + ' · ' + fmtSize(a.size) + '</div></div>' +
    '<a class="btn sm primary" href="' + esc(a.url) + '" download>下载</a></div>';
}
function appendArtifacts(files){
  const host = chatHost();
  if(!files || !files.length || !host) return;
  const fresh = freshArtifacts(files);
  if(!fresh.length) return;
  ensureStreamBubble();
  const box = host.querySelector("[data-streaming='1']");
  const bubble = box ? box.querySelector(".bubble") : null;
  if(!bubble) return;
  const holder = document.createElement("div");
  holder.innerHTML = fresh.map(artifactCard).join("");
  bubble.appendChild(holder);
  host.scrollTop = host.scrollHeight;
}

/* 聊天里上传 TXT 并分析完后的结果卡片（和会议中心同一份数据） */
function appendMeetingCard(m){
  const host = chatHost();
  if(!host || !m) return;
  clearWelcome();
  const el = document.createElement("div");
  el.className = "msg";
  el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
    '<div class="attach-card" style="align-items:flex-start">' +
      '<div class="grow"><div class="fname">' + esc(m.title || "会议分析") + '</div>' +
      '<div class="muted small">风险 ' + ((m.risks || []).length) + ' 条 · 待办 ' +
        ((m.todos || []).length) + ' 条 · 来源 ' + esc(m.source_name || "—") + '</div></div>' +
      '<button class="btn sm primary" data-open-meeting="' + esc(m.id) + '">查看详情</button>' +
    '</div></div>';
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
}

async function submitMessage(){
  if(S.streaming){ toast("请等待当前回复结束", "warn"); return; }
  const ta = $("#composer");
  const text = (ta.value || "").trim();
  if(!text && !S.attachments.length) return;

  // 按用途分流：PDF（或用户指定"存知识库"的 TXT）→ 知识库解析；
  // 其余 TXT/MD → 会议分析。两条路走的是和业务页面完全相同的后端接口。
  const docs = [];
  const meetingsDone = [];
  for(const att of S.attachments){
    try{
      if(att.purpose === "meeting"){
        toast("正在分析会议：" + att.name, "info");
        const res = await rpc("meetings.analyze", {name: att.name, data: att.data}, 300000);
        meetingsDone.push(res.meeting);
        toast(res.reused ? "这份记录之前已分析过，已复用结果" : "会议分析完成", "ok");
      }else{
        const res = await rpc("documents.upload", {name: att.name, data: att.data});
        const doc = res.document || {};
        if(res.warning){ toast(res.warning, "warn"); }
        else { toast("已加入知识库：" + doc.name, "ok"); }
        if(att.ext === "pdf"){
          docs.push({media_type: att.mediaType, data: att.data, name: att.name});
        }
      }
    }catch(err){
      toast((att.purpose === "meeting" ? "会议分析失败：" : "上传失败：") + err.message, "err");
    }
  }
  S.attachments = [];
  renderAttachTags();

  appendUserBubble(text, docs, false);
  meetingsDone.forEach(m => appendMeetingCard(m));
  if(meetingsDone.length) loadMeetings();
  ta.value = ""; ta.style.height = "auto";
  S.streaming = true; streamBuf = ""; S.liveText = ""; S.liveTools = [];
  syncComposer();
  ensureStreamBubble();
  send("user_message", {content: text, documents: docs});
}
function refreshSessionList(){ setTimeout(() => send("ping"), 200); }

/* ── PPT 模态 ─────────────────────────────────────────── */
function openPPTModal(){
  modal({
    title: "生成汇报 PPT",
    okText: "开始生成",
    body:
      '<div class="stack">' +
        '<div class="field"><label>PPT 主题</label>' +
          '<input class="input" id="pptTopic" placeholder="例如：第三季度供应商风险复盘汇报" /></div>' +
        '<div class="row">' +
          '<div class="field grow"><label>页数</label>' +
            '<input class="input" id="pptSlides" type="number" min="3" max="20" value="8" /></div>' +
          '<div class="field grow"><label>汇报对象</label>' +
            '<input class="input" id="pptAudience" placeholder="例如：管理层 / 客户" /></div>' +
        '</div>' +
        '<div class="field"><label>内容重点（可选）</label>' +
          '<textarea class="textarea" id="pptFocus" placeholder="需要强调的风险、结论或数据口径"></textarea></div>' +
        '<p class="muted small">将从知识库与最近会议结论中取材，生成真正的 .pptx 文件。</p>' +
      '</div>',
    onOk: async (mask) => {
      const topic = mask.querySelector("#pptTopic").value.trim();
      if(!topic){ toast("请填写 PPT 主题", "warn"); return false; }
      const slides = Number(mask.querySelector("#pptSlides").value || 8);
      const audience = mask.querySelector("#pptAudience").value.trim();
      const focus = mask.querySelector("#pptFocus").value.trim();
      toast("正在制作汇报文稿…", "info");
      try{
        const res = await rpc("ppt.generate", {topic, slides, focus, audience});
        toast("PPT 已生成：" + res.filename, "ok");
        if(S.page === "chat" && S.chatNodes){
          const el = document.createElement("div");
          el.className = "msg";
          el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
            '<p>已生成《' + esc(topic) + '》，共 ' + res.slide_count + ' 页。</p>' +
            (res.file ? artifactCard(res.file) : "") + '</div>';
          S.chatNodes.appendChild(el);
        } else {
          go("chat");
          const el = document.createElement("div");
          el.className = "msg";
          el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
            '<p>已生成《' + esc(topic) + '》，共 ' + res.slide_count + ' 页。</p>' +
            (res.file ? artifactCard(res.file) : "") + '</div>';
          S.chatNodes.appendChild(el);
        }
      }catch(err){
        toast(err.message, "err");
      }
      return true;
    }
  });
}

/* ── 知识库 ───────────────────────────────────────────── */
async function renderKnowledge(){
  const root = $("#content");
  root.innerHTML =
    '<div class="split">' +
      '<div class="card panel-list">' +
        '<div class="card-head"><h3>知识文档</h3><span class="chip" id="docCount">0</span></div>' +
        '<div style="padding:10px 10px 0"><div class="drop" id="docDrop">' +
          '<div>' + ICONS.upload + '</div><strong>点击或拖拽上传</strong>' +
          '<div class="small">支持 PDF / TXT / MD / DOCX，单文件 ≤ 15MB</div></div></div>' +
        '<div class="scroll" id="docList"></div>' +
      '</div>' +
      '<div class="card" id="docDetail"></div>' +
    '</div>';

  bindUpload("#docDrop", async (file) => {
    if(file.size > 15*1024*1024){ toast("文件超过 15MB", "err"); return; }
    toast("正在上传并解析：" + file.name, "info");
    const b64 = await fileToBase64(file);
    try{
      const res = await rpc("documents.upload", {name:file.name, data:b64});
      if(res.warning) toast(res.warning, "warn");
      else toast("解析完成：" + res.document.name, "ok");
      await loadDocs();
      if(res.document && res.document.id) selectDoc(res.document.id);
    }catch(err){ toast(err.message, "err"); }
  }, ".pdf,.txt,.md,.docx", ["pdf", "txt", "md", "docx"]);

  await loadDocs();
  renderDocDetail();
}

function fileToBase64(file){
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });
}
function bindUpload(sel, handler, accept, exts){
  const drop = $(sel);
  if(!drop) return;
  const allow = exts || ["pdf", "txt", "md"];
  const input = document.createElement("input");
  input.type = "file"; input.accept = accept || ".pdf,.txt,.md";
  input.style.display = "none";
  document.body.appendChild(input);
  const deliver = (file) => {
    if(!file) return;
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if(allow.indexOf(ext) < 0){
      toast("不支持 ." + ext + " 文件，这里只收 " + allow.map(e => e.toUpperCase()).join(" / "), "err");
      return;
    }
    handler(file);
  };
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { deliver(input.files[0]); input.value = ""; });
  ["dragenter","dragover"].forEach(t => drop.addEventListener(t, (e) => {
    e.preventDefault(); drop.classList.add("drag");
  }));
  ["dragleave","drop"].forEach(t => drop.addEventListener(t, (e) => {
    e.preventDefault(); drop.classList.remove("drag");
  }));
  drop.addEventListener("drop", (e) => {
    deliver(e.dataTransfer.files && e.dataTransfer.files[0]);
  });
}

async function loadDocs(){
  const res = await rpc("documents.list");
  S.docs = res.documents || [];
  const list = $("#docList");
  $("#docCount").textContent = S.docs.length + " 份";
  if(!list) return;
  list.innerHTML = S.docs.length ? S.docs.map(d =>
    '<div class="doc-item ' + (d.id === S.docSel ? "active" : "") + '" data-doc="' + esc(d.id) + '">' +
      '<div class="n">' + esc(d.name) + '</div>' +
      '<div class="m">' + fmtSize(d.size) + ' · ' + (d.pages || 0) + ' 页 · ' + fmtTime(d.created_at) + '</div>' +
      '<div style="margin-top:5px"><span class="chip ' + statusChip(d.status) + '">' +
        (STATUS_TEXT[d.status] || d.status) + '</span></div>' +
    '</div>').join("")
    : emptyState("还没有知识文档", "上传 PDF 后即可基于原文提问");
  if(!list.dataset.bound){
    list.dataset.bound = "1";
    list.addEventListener("click", (e) => {
      const item = e.target.closest("[data-doc]");
      if(item) selectDoc(item.dataset.doc);
    });
  }
}
function statusChip(status){
  if(status === "ready") return "done";
  if(status === "failed") return "err";
  if(status === "no_text") return "warn";
  return "brand";
}

async function selectDoc(id){
  S.docSel = id;
  S.docAnswer = null;
  $$("[data-doc]").forEach(el => el.classList.toggle("active", el.dataset.doc === id));
  renderDocDetail();
}

function renderDocDetail(){
  const host = $("#docDetail");
  if(!host) return;
  const doc = S.docs.find(d => d.id === S.docSel);
  if(!doc){
    host.innerHTML = '<div class="card-head"><h3>文档详情与知识问答</h3></div>' +
      '<div class="card-body">' + emptyState("选择左侧文档", "选中文档后可查看解析状态、提问并核对页码引用") + '</div>';
    return;
  }
  const warn = doc.status === "no_text"
    ? '<div class="banner warn">该文件未检测到可提取的文字（可能是扫描件）。当前未启用 OCR，无法基于内容回答。</div>'
    : doc.status === "failed"
      ? '<div class="banner err">解析失败：' + esc(doc.error || "未知原因") + '</div>'
      : doc.status === "parsing"
        ? '<div class="banner info">正在解析文件内容…</div>' : "";
  host.innerHTML =
    '<div class="card-head"><div><h3>' + esc(doc.name) + '</h3>' +
      '<div class="muted small">' + fmtSize(doc.size) + ' · ' + (doc.pages||0) + ' 页 · ' +
      (doc.chars||0) + ' 字符 · ' + fmtTime(doc.created_at) + '</div></div>' +
      '<button class="btn danger sm" id="delDoc">删除</button></div>' +
    '<div class="card-body">' + warn +
      '<div class="field"><label>基于本文档提问</label>' +
        '<textarea class="textarea" id="docQ" placeholder="例如：这份文档里规定的验收标准是什么？"></textarea></div>' +
      '<div class="row" style="margin-top:10px"><button class="btn primary" id="docAsk">提问</button>' +
        '<span class="muted small">回答会附带页码引用</span></div>' +
      '<div id="docAnswer" style="margin-top:16px"></div>' +
    '</div>';

  $("#delDoc").addEventListener("click", async () => {
    const ok = await confirmAction("删除文档", "将同时删除该文档的文本分块与检索索引，无法恢复。", "删除");
    if(!ok) return;
    try{
      await rpc("documents.delete", {id: doc.id});
      toast("文档已删除", "ok");
      if(S.docSel === doc.id) S.docSel = null;
      await loadDocs();
      renderDocDetail();
    }catch(err){ toast(err.message, "err"); }
  });
  $("#docAsk").addEventListener("click", async () => {
    const q = $("#docQ").value.trim();
    if(!q){ toast("请输入问题", "warn"); return; }
    const box = $("#docAnswer");
    box.innerHTML = '<div class="tool-line"><span class="spinner"></span><span>正在检索文档内容…</span></div>';
    try{
      const res = await rpc("knowledge.query", {question:q, doc_ids:[doc.id]});
      box.innerHTML = '<div class="card" style="background:#FCFDFE"><div class="card-body">' +
        degradationBanner(res.retrieval) +
        '<div>' + markdown(res.answer) + '</div>' + citationsHtml(res.citations) + '</div></div>';
    }catch(err){
      box.innerHTML = '<div class="banner err">' + esc(err.message) + '</div>';
    }
  });
}

function degradationBanner(retrieval){
  // 后端在向量库/重排不可用时会如实标注 degraded + degraded_reason。
  // 界面必须显示出来，否则用户会以为"混合检索正常"，把残缺结果当成完整结果。
  if(!retrieval || !retrieval.degraded) return "";
  const reason = retrieval.degraded_reason || "未知原因";
  const used = (retrieval.routes_used || []).join(" + ") || "关键词";
  return '<div class="banner warn"><strong>检索已降级</strong>：' +
    esc(reason) + '<br><span class="small">本次结果仅来自 ' + esc(used) +
    '，可能不如完整混合检索全面，请核对引用原文。</span></div>';
}

function citationsHtml(citations){
  if(!citations || !citations.length) return "";
  return '<div style="margin-top:12px;border-top:1px solid var(--line);padding-top:10px">' +
    '<div class="muted small" style="margin-bottom:6px">引用来源</div>' +
    citations.map(c => '<div class="list-item"><div class="li-main">' +
      '<div class="li-title">' + esc(c.doc_name) + ' · 第' + c.page + ' 页</div>' +
      '<div class="li-sub">' + esc(c.excerpt) + '…</div></div></div>').join("") + '</div>';
}

/* ── 会议中心 ─────────────────────────────────────────── */
async function renderMeetings(){
  const root = $("#content");
  root.innerHTML =
    '<div id="wfPanel"></div>' +
    '<div class="split">' +
      '<div class="card panel-list">' +
        '<div class="card-head"><h3>会议记录</h3><span class="chip" id="mtgCount">0</span></div>' +
        '<div style="padding:10px 10px 0"><div class="drop" id="mtgDrop">' +
          '<div>' + ICONS.upload + '</div><strong>上传会议文件</strong>' +
          '<div class="small">TXT / MD / DOCX / PDF，也可直接粘贴原文</div></div>' +
          '<button class="btn sm" id="pasteMeeting" style="width:100%;margin-top:8px">粘贴会议原文</button>' +
          '<button class="btn sm primary" id="pasteWorkflow" style="width:100%;margin-top:6px">用工作流处理（含邮件审批）</button></div>' +
        '<div class="scroll" id="mtgList"></div>' +
      '</div>' +
      '<div id="mtgDetail"></div>' +
    '</div>';

  bindUpload("#mtgDrop", (file) => analyzeMeetingFile(file),
             ".txt,.md,.docx,.pdf", ["txt", "md", "docx", "pdf"]);
  $("#pasteMeeting").addEventListener("click", () => openPasteMeeting("analyze"));
  $("#pasteWorkflow").addEventListener("click", () => openPasteMeeting("workflow"));

  await loadMeetings();
  renderMeetingDetail();
  await renderWorkflowPanel();
}

/* ── LangGraph 工作流面板（状态全部来自后端 Checkpoint，不是前端动画） ── */
const WF_STATUS_TEXT = {
  pending: "排队中", running: "执行中", waiting_approval: "等待人工审批",
  completed: "已完成", cancelled: "已取消", failed: "失败"
};

async function renderWorkflowPanel(){
  const host = $("#wfPanel");
  if(!host) return;
  let workflows = [];
  try{
    const res = await rpc("workflow.list", {limit: 5});
    workflows = res.workflows || [];
  }catch(err){
    host.innerHTML = '<div class="card"><div class="card-body"><div class="banner warn">' +
      '工作流未启用：' + esc(err.message) + '</div></div></div>';
    return;
  }
  if(!workflows.length){ host.innerHTML = ""; return; }
  const current = S.workflowSel
    ? workflows.find(w => w.workflow_id === S.workflowSel) || workflows[0]
    : workflows[0];
  S.workflowSel = current.workflow_id;
  let snapshot = null;
  try{
    snapshot = (await rpc("workflow.status", {id: current.workflow_id})).workflow;
  }catch(err){ snapshot = null; }
  host.innerHTML = workflowCardHtml(current, snapshot);
  bindWorkflowCard(current, snapshot);
}

function workflowCardHtml(run, snap){
  const status = (snap && snap.execution_status) || run.status || "pending";
  const chip = status === "completed" ? "done"
    : status === "failed" ? "err"
    : status === "waiting_approval" ? "warn" : "brand";
  const label = WF_STATUS_TEXT[status] || status;
  const nodeLine = snap && snap.next_nodes && snap.next_nodes.length
    ? "下一步：" + snap.next_nodes.join(" / ")
    : (snap && snap.current_step ? "当前步骤：" + snap.current_step : "");
  let html = '<div class="card" id="wfCard"><div class="card-head"><div><h3>会议→邮件工作流</h3>' +
    '<div class="muted small">' + esc(run.workflow_id) + ' · ' +
    (run.meeting_id ? '会议 ' + esc(run.meeting_id) + ' · ' : '') +
    esc(nodeLine) + '</div></div>' +
    '<span class="chip ' + chip + '">' + esc(label) + '</span></div><div class="card-body">';

  if(snap && snap.approval){
    const a = snap.approval;
    html += '<div class="banner warn"><strong>需要你确认后才会发送邮件</strong>' +
      '（草稿 v' + a.version + '，确认前不会发出）。</div>' +
      '<div class="field"><label>收件人</label><input class="input" id="wfTo" value="' +
      esc(a.to_addr) + '" /></div>' +
      '<div class="field"><label>主题</label><input class="input" id="wfSubject" value="' +
      esc(a.subject) + '" /></div>' +
      '<div class="field"><label>正文</label><textarea class="textarea" id="wfBody" ' +
      'style="min-height:160px">' + esc(a.body) + '</textarea></div>' +
      '<div class="row" style="margin-top:10px">' +
        '<button class="btn primary" id="wfApprove">确认发送</button>' +
        '<button class="btn" id="wfEdit">保存修改（需再次确认）</button>' +
        '<button class="btn danger" id="wfCancel">取消发送</button>' +
        '<button class="btn sm" id="wfTrace">查看执行 Trace</button>' +
      '</div>' +
      '<div class="muted small" style="margin-top:8px">审批绑定到具体草稿版本：修改后版本号会 +1，旧版本确认会被拒绝。</div>';
  }else if(status === "completed"){
    html += '<div class="banner info">邮件已发送；会议、风险、待办已写入业务库。' +
      (snap && snap.meeting_id ? '会议 ID：' + esc(snap.meeting_id) : '') + '</div>' +
      '<div class="row"><button class="btn sm" id="wfTrace">查看执行 Trace</button></div>';
  }else if(status === "failed"){
    const err = (snap && snap.errors && snap.errors[0]) || {};
    html += '<div class="banner err">' + esc(err.message || run.error || "工作流执行失败") + '</div>' +
      '<div class="row"><button class="btn sm" id="wfRetry">重试发送</button>' +
      '<button class="btn sm" id="wfTrace">查看执行 Trace</button></div>';
  }else{
    html += '<div class="banner info">' + esc(label) + '…</div>' +
      '<div class="row"><button class="btn sm" id="wfTrace">查看执行 Trace</button></div>';
  }
  html += '<div id="wfTraceBox" style="margin-top:12px"></div>';
  html += '</div></div>';
  return html;
}

function bindWorkflowCard(run, snap){
  const id = run.workflow_id;
  const refresh = async () => { await renderWorkflowPanel(); };
  const on = (sel, fn) => { const el = $(sel); if(el) el.addEventListener("click", fn); };

  on("#wfApprove", async () => {
    const a = snap.approval;
    const edited = ($("#wfTo").value.trim() !== (a.to_addr || "")) ||
                   ($("#wfSubject").value.trim() !== (a.subject || "")) ||
                   ($("#wfBody").value !== (a.body || ""));
    if(edited){
      // 内容被改过就必须先升版：否则“确认”会发出未修改的旧草稿，属于静默丢修改
      toast("内容已修改：请先点「保存修改（需再次确认）」", "warn");
      return;
    }
    if(!($("#wfTo").value || "").trim()){
      // 收件人为空时不要等到 SMTP 层才失败：那是“确认了却发不出去”的体验
      toast("请先填写收件人，再保存修改后重新确认", "warn");
      return;
    }
    try{
      const res = await rpc("workflow.resume", {
        id, decision:"approve", version:a.version, draft_hash:a.draft_hash,
        to_addr:a.to_addr, subject:a.subject, body:a.body
      }, 300000);
      toast(res.workflow.execution_status === "completed" ? "邮件已发送" : "审批已提交",
            res.workflow.execution_status === "failed" ? "warn" : "ok");
      await loadMeetings();
      await refresh();
    }catch(err){ toast(err.message, "err"); }
  });

  on("#wfEdit", async () => {
    const a = snap.approval;
    try{
      const res = await rpc("workflow.resume", {
        id, decision:"edit", version:a.version, draft_hash:a.draft_hash,
        to_addr: $("#wfTo").value.trim(), subject: $("#wfSubject").value.trim(),
        body: $("#wfBody").value
      }, 300000);
      toast("已保存为 v" + (res.workflow.approval ? res.workflow.approval.version : "?") +
            "，请再次确认", "warn");
      await refresh();
    }catch(err){ toast(err.message, "err"); }
  });

  on("#wfCancel", async () => {
    const ok = await confirmAction("取消发送", "工作流会结束，草稿标记为已取消，不会发出任何邮件。", "取消发送");
    if(!ok) return;
    try{
      await rpc("workflow.cancel", {id, note:"用户取消"}, 60000);
      toast("已取消，未发送邮件", "ok");
      await refresh();
    }catch(err){ toast(err.message, "err"); }
  });

  on("#wfRetry", async () => {
    try{
      const res = await rpc("workflow.retry", {id}, 300000);
      toast(res.workflow.execution_status === "completed" ? "重试成功" : "已重试", "ok");
      await refresh();
    }catch(err){ toast(err.message, "err"); }
  });

  on("#wfTrace", async () => {
    const box = $("#wfTraceBox");
    if(!box) return;
    box.innerHTML = '<div class="tool-line"><span class="spinner"></span><span>载入 Trace…</span></div>';
    try{
      const hist = await rpc("workflow.history", {id});
      const rows = (hist.events || []).filter(e => e.event === "node");
      box.innerHTML = '<div class="muted small" style="margin-bottom:6px">节点级执行 Trace' +
        '（Token 合计 ' + (hist.tokens.input_tokens + hist.tokens.output_tokens) + '）</div>' +
        rows.map(e => '<div class="list-item"><div class="li-main">' +
          '<div class="li-title">' + esc(e.node) + ' · ' + Math.round(e.duration_ms) + 'ms' +
          (e.status === "error" ? ' · 失败' : '') + '</div>' +
          '<div class="li-sub">' + esc(e.started_at) +
          (e.input_tokens ? ' · in ' + e.input_tokens + ' / out ' + e.output_tokens : '') +
          (e.error_type ? ' · ' + esc(e.error_type) : '') + '</div></div></div>').join("") +
        '<div class="muted small" style="margin-top:8px">副作用：' +
        (hist.effects || []).map(f => esc(f.action) + "=" + esc(f.status)).join("、") + '</div>';
    }catch(err){
      box.innerHTML = '<div class="banner err">' + esc(err.message) + '</div>';
    }
  });
}

function openPasteMeeting(mode){
  const isFlow = mode === "workflow";
  modal({
    title: isFlow ? "用工作流处理会议（含邮件审批）" : "粘贴会议记录",
    okText: isFlow ? "启动工作流" : "开始分析",
    body:'<div class="stack"><div class="field"><label>会议名称</label>' +
      '<input class="input" id="mtgName" placeholder="例如：11 月供应商风险评审会" /></div>' +
      (isFlow ? '<div class="field"><label>收件人（可留空，审批时再填）</label>' +
        '<input class="input" id="mtgTo" placeholder="team@example.com" /></div>' : '') +
      '<div class="field"><label>会议原文</label>' +
      '<textarea class="textarea" id="mtgText" style="min-height:220px" placeholder="粘贴会议记录全文…"></textarea></div></div>',
    onOk: async (mask) => {
      const text = mask.querySelector("#mtgText").value.trim();
      const name = mask.querySelector("#mtgName").value.trim() || "会议记录.txt";
      if(text.length < 30){ toast("会议内容过短（少于 30 字）", "warn"); return false; }
      if(isFlow){
        const to = (mask.querySelector("#mtgTo") || {}).value || "";
        toast("工作流执行中：分析 → 校验 → 落库 → 生成草稿…", "info");
        try{
          const res = await rpc("workflow.start", {name, text, to}, 600000);
          S.workflowSel = res.workflow.workflow_id;
          toast(res.workflow.resumable ? "草稿已生成，等待你确认发送" : "工作流已结束", "ok");
          await loadMeetings();
          await renderWorkflowPanel();
        }catch(err){ toast(err.message, "err"); }
        return true;
      }
      toast("正在分析会议内容…", "info");
      try{
        const res = await rpc("meetings.analyze", {name, text});
        toast(res.reused ? "这份记录之前已分析过，已直接打开原结果" : "会议分析完成",
              res.reused ? "warn" : "ok");
        await loadMeetings();
        S.meetingSel = res.meeting.id;
        renderMeetingDetail();
      }catch(err){ toast(err.message, "err"); }
      return true;
    }
  });
}

async function analyzeMeetingFile(file){
  if(file.size > 15*1024*1024){ toast("文件超过 15MB", "err"); return; }
  // 会议文件由服务端统一解析（TXT / MD / DOCX / PDF），前端只负责传 base64：
  // 以前用 file.text() 会把 GBK 的 TXT 读成乱码，也根本读不了 docx/pdf。
  let data = "";
  try{ data = await fileToBase64(file); }
  catch(err){ toast("读取文件失败：" + (err && err.message ? err.message : err), "err"); return; }
  toast("正在分析会议内容…", "info");
  try{
    const res = await rpc("meetings.analyze", {name:file.name, data}, 300000);
    toast(res.reused ? "这份记录之前已分析过，已直接打开原结果" : "会议分析完成",
          res.reused ? "warn" : "ok");
    await loadMeetings();
    S.meetingSel = res.meeting.id;
    renderMeetingDetail();
  }catch(err){ toast(err.message, "err"); }
}

async function loadMeetings(){
  const res = await rpc("meetings.list");
  S.meetings = res.meetings || [];
  const count = $("#mtgCount");
  if(count) count.textContent = S.meetings.length + " 场";
  const list = $("#mtgList");
  if(!list) return;
  list.innerHTML = S.meetings.length ? S.meetings.map(m =>
    '<div class="doc-item ' + (m.id === S.meetingSel ? "active" : "") + '" data-mtg="' + esc(m.id) + '">' +
      '<div class="n">' + esc(m.title) + '</div>' +
      '<div class="m">' + fmtTime(m.created_at) + ' · ' + (m.risk_count||0) + ' 风险 · ' + (m.todo_count||0) + ' 待办</div>' +
    '</div>').join("")
    : emptyState("还没有会议记录", "上传 TXT 或粘贴原文开始分析");
  if(!list.dataset.bound){
    list.dataset.bound = "1";
    list.addEventListener("click", (e) => {
      const item = e.target.closest("[data-mtg]");
      if(!item) return;
      S.meetingSel = item.dataset.mtg;
      $$("[data-mtg]").forEach(el => el.classList.toggle("active", el.dataset.mtg === S.meetingSel));
      renderMeetingDetail();
    });
  }
}

async function renderMeetingDetail(){
  const host = $("#mtgDetail");
  if(!host) return;
  if(!S.meetingSel){
    host.innerHTML = '<div class="card"><div class="card-body">' +
      emptyState("选择或新建一次会议分析", "AI 会整理主题、参与人、决策、风险与待办，并可生成提醒邮件") +
      '</div></div>';
    return;
  }
  host.innerHTML = '<div class="card"><div class="card-body"><div class="tool-line">' +
    '<span class="spinner"></span><span>正在载入会议详情…</span></div></div></div>';
  let meeting;
  try{
    const res = await rpc("meetings.get", {id:S.meetingSel});
    meeting = res.meeting;
  }catch(err){
    host.innerHTML = '<div class="card"><div class="card-body"><div class="banner err">' +
      esc(err.message) + '</div></div></div>';
    return;
  }
  S.currentMeeting = meeting;
  const risks = meeting.risks || [];
  const todos = meeting.todos || [];

  host.innerHTML = '<div class="card">' +
    '<div class="card-head"><div><h3>' + esc(meeting.title) + '</h3>' +
      '<div class="muted small">来源文件：' + esc(meeting.source_name || "—") + ' · ' + fmtTime(meeting.created_at) + '</div></div>' +
      '<div class="row"><button class="btn sm" id="mtgEmail">生成提醒邮件</button>' +
      '<button class="btn sm danger" id="mtgDel">删除会议</button></div></div>' +
    '<div class="card-body">' +
      '<h4 class="small muted" style="margin-bottom:8px">会议概览</h4>' +
      '<div class="meeting-overview">' +
        ov("会议时间", meeting.meeting_time || "待确认") +
        ov("地点 / 形式", meeting.location || "待确认") +
        ov("参与人员", (meeting.participants || []).join("、") || "待确认") +
        ov("风险 / 待办", risks.length + " 条 / " + todos.length + " 条") +
      '</div>' +
      (meeting.summary ? '<div class="banner info" style="display:block">' + esc(meeting.summary) + '</div>' : "") +
      ((meeting.decisions || []).length ? '<div style="margin-bottom:16px"><h4 class="small muted">已确认决策</h4>' +
        '<ul style="margin:6px 0 0 18px">' + meeting.decisions.map(d => '<li>' + esc(d) + '</li>').join("") + '</ul></div>' : "") +
      '<h4 class="small muted" style="margin:16px 0 8px">风险分析（' + risks.length + '）</h4>' +
      (risks.length ? risks.map(riskCard).join("") : emptyState("未识别到风险", "模型未在原文中找到需要预警的内容")) +
      '<h4 class="small muted" style="margin:18px 0 8px">待办任务（' + todos.length + '）</h4>' +
      (todos.length ? todosTable(todos) : emptyState("未生成待办", "原文中没有明确的可执行事项")) +
    '</div></div>';

  $("#mtgDel").addEventListener("click", async () => {
    const ok = await confirmAction("删除会议", "将删除该会议的风险、待办与关联的记忆事实。", "删除");
    if(!ok) return;
    try{
      await rpc("meetings.delete", {id: meeting.id});
      S.meetingSel = null;
      toast("会议已删除", "ok");
      await loadMeetings();
      renderMeetingDetail();
    }catch(err){ toast(err.message, "err"); }
  });
  $("#mtgEmail").addEventListener("click", () => openEmailDraft(meeting));
}

function ov(k,v){ return '<div class="ov"><div class="k">' + k + '</div><div class="v">' + esc(v) + '</div></div>'; }

function riskCard(r){
  const level = (r.level === "high" ? "high" : r.level === "low" ? "low" : "medium");
  const label = {high:"高风险", medium:"中风险", low:"低风险"}[level];
  return '<div class="risk-card ' + level + '">' +
    '<div class="rh"><strong>' + esc(r.title) + '</strong>' +
      '<span class="chip ' + level + '">' + label + '</span>' +
      (r.status === "closed" ? '<span class="chip done">已解决</span>' : "") + '</div>' +
    '<div class="small">' + esc(r.description || "（无补充说明）") + '</div>' +
    (r.suggestion ? '<div class="small" style="margin-top:6px"><strong>建议措施：</strong>' + esc(r.suggestion) + '</div>' : "") +
    (r.evidence ? '<div class="ev"><strong>原文依据：</strong>' + esc(r.evidence) + '</div>'
                : '<div class="ev muted">原文未提供可直接引用的依据</div>') +
    '</div>';
}

function todosTable(todos){
  return '<table class="table"><thead><tr><th>待办内容</th><th>负责人</th><th>截止时间</th>' +
    '<th>优先级</th><th>状态</th></tr></thead><tbody>' +
    todos.map(t => '<tr><td>' + esc(t.content) + '</td>' +
      '<td>' + esc(t.owner || "待确认") + '</td>' +
      '<td>' + esc(t.due_date || "待确认") + '</td>' +
      '<td><span class="chip ' + esc(t.priority) + '">' + (PRIORITY_TEXT[t.priority]||"中") + '</span></td>' +
      '<td><span class="chip ' + (t.status === "done" ? "done" : t.status === "doing" ? "brand" : "") + '">' +
        (STATUS_TEXT[t.status] || t.status) + '</span></td></tr>').join("") + '</tbody></table>';
}

/* 邮件草稿 + 确认发送 */
function openEmailDraft(meeting){
  modal({
    title: "生成提醒邮件",
    okText: "生成草稿",
    body: '<div class="stack">' +
      '<div class="field"><label>收件人邮箱</label>' +
        '<input class="input" id="mailTo" placeholder="name@company.com；多个用逗号分隔" /></div>' +
      '<p class="muted small" style="margin:-4px 0 2px">可以发给任何人，不限于本企业邮箱。</p>' +
      '<div class="field"><label>额外要求（可选）</label>' +
        '<textarea class="textarea" id="mailExtra" placeholder="例如：语气正式一些，抄送项目组"></textarea></div>' +
      '<div class="banner warn" style="margin:0">生成的是草稿，必须核对后再点击发送。</div></div>',
    onOk: async (mask) => {
      const to = mask.querySelector("#mailTo").value.trim();
      const extra = mask.querySelector("#mailExtra").value.trim();
      toast("正在起草提醒邮件…", "info");
      try{
        const res = await rpc("email.draft", {meeting_id: meeting.id, to, extra});
        toast("草稿已生成，请核对后发送", "ok");
        openEmailPreview(res.email);
      }catch(err){ toast(err.message, "err"); }
      return true;
    }
  });
}

function openEmailPreview(email){
  modal({
    title: "邮件预览与发送",
    okText: "确认发送",
    body: '<div class="stack">' +
      '<div class="field"><label>收件人</label><input class="input" id="pvTo" value="' + esc(email.to_addr || "") + '" />' +
        '<p class="muted small" style="margin:4px 0 0">可填多个，用逗号分隔。</p></div>' +
      '<div class="field"><label>主题</label><input class="input" id="pvSubject" value="' + esc(email.subject) + '" /></div>' +
      '<div class="field"><label>正文</label><textarea class="textarea" id="pvBody" style="min-height:240px">' +
        esc(email.body) + '</textarea></div>' +
      (email.status === "sent" ? '<div class="banner info" style="margin:0">这封邮件已发送。</div>' : '') +
      '<p class="muted small">点击"确认发送"后会真实通过 SMTP 投递；失败原因会如实反馈。</p></div>',
    onOk: async (mask) => {
      const to = mask.querySelector("#pvTo").value.trim();
      const subject = mask.querySelector("#pvSubject").value.trim();
      const body = mask.querySelector("#pvBody").value.trim();
      if(!to || to.indexOf("@") < 0){ toast("请填写有效的收件人邮箱", "warn"); return false; }
      updateEmailDraftCard(email.id, "sending", "正在提交 SMTP…");
      try{
        const res = await rpc("email.send", {id: email.id, confirm:true, to_addr: to, subject, body});
        toast(res.message || "邮件已提交 SMTP", "ok");
        S.emails.unshift(res.email || {});
        updateEmailDraftCard(email.id, "sent", res.message || "SMTP 已接受（不代表已送达）");
      }catch(err){
        toast(err.message, "err");
        updateEmailDraftCard(email.id, "failed", err.message);
        // 发送失败要能看到草稿内容，方便修改后重试
        return false;
      }
      return true;
    }
  });
}

/* 聊天里的邮件草稿卡片：收件人 / 主题 / 正文摘要 / 关联待办 / 确认发送 */
function appendEmailDraftCard(d){
  const host = chatHost();
  if(!host || !d || !d.id) return;
  clearWelcome();
  if(host.querySelector('[data-email-card="' + d.id + '"]')) return;   // 不重复渲染
  const todos = (d.related_todos || []).map(t =>
    '<li>' + esc(t.content) + ' — ' + esc(t.owner) + ' / ' + esc(t.due_date) + '</li>').join("");
  const el = document.createElement("div");
  el.className = "msg";
  el.innerHTML = '<div class="avatar ai">AI</div><div class="bubble">' +
    '<div class="mail-card" data-email-card="' + esc(d.id) + '">' +
      '<div class="mail-head"><strong>邮件草稿</strong>' +
        '<span class="chip" data-email-state>待确认</span></div>' +
      '<div class="mail-row"><span class="muted small">收件人</span><span data-email-to>' +
        esc(d.to_addr || "待填写") + '</span></div>' +
      '<div class="mail-row"><span class="muted small">主题</span><span>' + esc(d.subject || "") + '</span></div>' +
      '<div class="mail-body">' + esc((d.body || "").slice(0, 260)) +
        ((d.body || "").length > 260 ? "…" : "") + '</div>' +
      (todos ? '<div class="mail-todos"><div class="muted small">关联待办</div><ul>' + todos + '</ul></div>' : "") +
      '<div class="mail-foot">' +
        '<button class="btn sm primary" data-email-open="' + esc(d.id) + '">编辑并发送</button>' +
        '<span class="muted small" data-email-note>必须你确认后才会真实发送</span>' +
      '</div>' +
    '</div></div>';
  host.appendChild(el);
  host.scrollTop = host.scrollHeight;
  S.emailDrafts[d.id] = d;
}

const EMAIL_STATE_TEXT = {draft:"待确认", sending:"发送中", sent:"SMTP 已接受", failed:"发送失败"};
function updateEmailDraftCard(id, state, note){
  const host = chatHost();
  if(!host || !id) return;
  const card = host.querySelector('[data-email-card="' + id + '"]');
  if(!card) return;
  const badge = card.querySelector("[data-email-state]");
  if(badge){
    badge.textContent = EMAIL_STATE_TEXT[state] || state;
    badge.className = "chip " + (state === "sent" ? "done" : state === "failed" ? "err" : state === "sending" ? "brand" : "");
  }
  const button = card.querySelector("[data-email-open]");
  if(button) button.disabled = (state === "sent" || state === "sending");
  const noteEl = card.querySelector("[data-email-note]");
  if(noteEl && note) noteEl.textContent = note;
}

/* ── 待办 ─────────────────────────────────────────────── */
async function renderTodos(){
  const root = $("#content");
  root.innerHTML =
    '<div class="card"><div class="card-head">' +
      '<div class="row"><h3>全部待办</h3>' +
        '<div class="seg" id="todoStatusSeg">' +
          '<button data-st="all" class="active">全部</button>' +
          '<button data-st="pending">待处理</button>' +
          '<button data-st="doing">进行中</button>' +
          '<button data-st="done">已完成</button></div>' +
        '<div class="seg" id="todoPriSeg">' +
          '<button data-pr="all" class="active">优先级</button>' +
          '<button data-pr="high">高</button>' +
          '<button data-pr="medium">中</button>' +
          '<button data-pr="low">低</button></div>' +
      '</div>' +
      '<div class="row">' +
        '<button class="btn sm danger" id="bulkDelTodos" hidden>删除所选</button>' +
        '<button class="btn sm" id="addTodo">+ 新建待办</button>' +
      '</div>' +
    '</div><div class="card-body" id="todoBody"></div></div>';

  $("#bulkDelTodos").addEventListener("click", async () => {
    const ids = Array.from(S.todoSelected);
    if(!ids.length){ toast("请先勾选要删除的待办", "warn"); return; }
    const ok = await confirmAction(
      "删除待办",
      "将删除选中的 " + ids.length + " 条待办，数据库记录一并删除，无法恢复。",
      "删除"
    );
    if(!ok) return;
    try{
      const res = await rpc("todos.delete", {ids});
      toast("已删除 " + (res.deleted || ids.length) + " 条待办", "ok");
      S.todoSelected = new Set();
      await loadTodos();
    }catch(err){ toast(err.message, "err"); }
  });

  $("#todoStatusSeg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-st]");
    if(!b) return;
    S.todoFilter.status = b.dataset.st;
    $$("#todoStatusSeg button").forEach(x => x.classList.toggle("active", x === b));
    loadTodos();
  });
  $("#todoPriSeg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-pr]");
    if(!b) return;
    S.todoFilter.priority = b.dataset.pr;
    $$("#todoPriSeg button").forEach(x => x.classList.toggle("active", x === b));
    loadTodos();
  });
  $("#addTodo").addEventListener("click", openAddTodo);
  await loadTodos();
}

async function loadTodos(){
  const body = $("#todoBody");
  if(!body) return;
  body.innerHTML = '<div class="tool-line"><span class="spinner"></span><span>正在载入待办…</span></div>';
  try{
    const res = await rpc("todos.list", S.todoFilter);
    S.todos = res.todos || [];
  }catch(err){
    body.innerHTML = '<div class="banner err">' + esc(err.message) + '</div>';
    return;
  }
  if(!S.todos.length){
    S.todoSelected = new Set();
    syncTodoBulk();
    body.innerHTML = emptyState("没有符合条件的待办", "换一个筛选条件，或先在会议中心分析一份会议记录");
    return;
  }
  S.todoSelected = new Set();
  body.innerHTML = '<table class="table"><thead><tr>' +
    '<th style="width:34px"><input type="checkbox" id="todoAll" title="全选本页待办" /></th>' +
    '<th>待办</th><th>来源会议</th><th>负责人</th>' +
    '<th>截止</th><th>优先级</th><th>状态</th><th>操作</th></tr></thead><tbody>' +
    S.todos.map(t => '<tr data-todo="' + esc(t.id) + '">' +
      '<td><input type="checkbox" data-sel-todo="' + esc(t.id) + '" title="选择这条待办" /></td>' +
      '<td><div>' + esc(t.content) + '</div>' +
        (t.risk_title ? '<div class="muted small">关联风险：' + esc(t.risk_title) + '</div>' : '') + '</td>' +
      '<td class="small">' + esc(t.meeting_title || "手动创建") + '</td>' +
      '<td class="small" style="white-space:nowrap">' + esc(t.owner || "待确认") + '</td>' +
      '<td class="small" style="white-space:nowrap">' + esc(t.due_date || "待确认") + '</td>' +
      '<td><span class="chip ' + esc(t.priority) + '">' + (PRIORITY_TEXT[t.priority]||"中") + '</span></td>' +
      '<td><span class="chip ' + (t.status === "done" ? "done" : t.status === "doing" ? "brand" : "") + '">' +
        (STATUS_TEXT[t.status]||t.status) + '</span></td>' +
      '<td><select class="select" style="width:auto;padding:4px 8px" data-set-status="' + esc(t.id) + '">' +
        '<option value="pending"' + (t.status === "pending" ? " selected" : "") + '>待处理</option>' +
        '<option value="doing"' + (t.status === "doing" ? " selected" : "") + '>进行中</option>' +
        '<option value="done"' + (t.status === "done" ? " selected" : "") + '>已完成</option>' +
      '</select></td></tr>').join("") + '</tbody></table>';

  // 表头全选：只作用于当前筛选结果（即页面上可见的行）
  const allBox = $("#todoAll");
  if(allBox){
    allBox.addEventListener("change", () => {
      S.todoSelected = new Set();
      $$("[data-sel-todo]").forEach(c => {
        c.checked = allBox.checked;
        if(allBox.checked) S.todoSelected.add(c.dataset.selTodo);
      });
      syncTodoBulk();
    });
  }
  syncTodoBulk();

  body.onchange = async (e) => {
    const chk = e.target.closest("[data-sel-todo]");
    if(chk){
      if(chk.checked) S.todoSelected.add(chk.dataset.selTodo);
      else S.todoSelected.delete(chk.dataset.selTodo);
      const boxes = $$("[data-sel-todo]");
      const all = $("#todoAll");
      if(all) all.checked = boxes.length > 0 && boxes.every(b => b.checked);
      syncTodoBulk();
      return;
    }
    const sel = e.target.closest("[data-set-status]");
    if(!sel) return;
    try{
      await rpc("todos.update", {id: sel.dataset.setStatus, status: sel.value});
      toast("待办状态已更新", "ok");
      loadTodos();
    }catch(err){ toast(err.message, "err"); }
  };
}

/* 批量删除按钮：只在有选择时出现，并显示已选条数 */
function syncTodoBulk(){
  const btn = $("#bulkDelTodos");
  if(!btn) return;
  const n = S.todoSelected.size;
  btn.hidden = n === 0;
  btn.textContent = n ? ("删除所选 (" + n + ")") : "删除所选";
}

function openAddTodo(){
  modal({
    title:"新建待办", okText:"创建",
    body:'<div class="stack"><div class="field"><label>待办内容</label>' +
      '<input class="input" id="tdContent" /></div>' +
      '<div class="row"><div class="field grow"><label>负责人</label><input class="input" id="tdOwner" /></div>' +
      '<div class="field grow"><label>截止时间</label><input class="input" id="tdDue" placeholder="如 11 月 20 日" /></div>' +
      '<div class="field" style="width:120px"><label>优先级</label><select class="select" id="tdPri">' +
        '<option value="high">高</option><option value="medium" selected>中</option><option value="low">低</option>' +
      '</select></div></div></div>',
    onOk: async (mask) => {
      const content = mask.querySelector("#tdContent").value.trim();
      if(!content){ toast("请填写待办内容", "warn"); return false; }
      try{
        await rpc("todos.create", {
          content,
          owner: mask.querySelector("#tdOwner").value.trim(),
          due_date: mask.querySelector("#tdDue").value.trim(),
          priority: mask.querySelector("#tdPri").value,
        });
        toast("待办已创建", "ok");
        loadTodos();
      }catch(err){ toast(err.message, "err"); }
      return true;
    }
  });
}

/* ── 长期记忆 ─────────────────────────────────────────── */
async function renderMemory(){
  const root = $("#content");
  root.innerHTML =
    '<div class="card"><div class="card-head">' +
      '<div class="row"><h3>记忆条目</h3>' +
        '<div class="seg" id="memSeg">' +
          '<button data-cat="all" class="active">全部</button>' +
          '<button data-cat="person">人物</button>' +
          '<button data-cat="time">时间</button>' +
          '<button data-cat="place">地点</button>' +
          '<button data-cat="topic">主题</button>' +
          '<button data-cat="decision">决策</button>' +
          '<button data-cat="todo">待办</button>' +
          '<button data-cat="note">对话记忆</button>' +
        '</div></div>' +
      '<div class="row">' +
        '<label class="chk-all"><input type="checkbox" id="memAll" /> 全选</label>' +
        '<button class="btn sm danger" id="bulkDelMem" hidden>删除所选</button>' +
        '<input class="input" id="memSearch" placeholder="搜索记忆…" style="max-width:220px" />' +
      '</div>' +
    '</div><div class="card-body" id="memBody"></div></div>';

  $("#bulkDelMem").addEventListener("click", async () => {
    const ids = Array.from(S.memSelected);
    if(!ids.length){ toast("请先勾选要删除的记忆", "warn"); return; }
    const ok = await confirmAction(
      "删除记忆",
      "将删除选中的 " + ids.length + " 条记忆，记忆文件与检索索引一并删除，无法恢复。",
      "删除"
    );
    if(!ok) return;
    try{
      const res = await rpc("memory.delete", {ids});
      toast("已删除 " + (res.deleted || ids.length) + " 条记忆", "ok");
      S.memSelected = new Set();
      await loadMemories();
    }catch(err){ toast(err.message, "err"); }
  });

  $("#memSeg").addEventListener("click", (e) => {
    const b = e.target.closest("[data-cat]");
    if(!b) return;
    S.memoryFilter = b.dataset.cat;
    $$("#memSeg button").forEach(x => x.classList.toggle("active", x === b));
    renderMemoryList();
  });
  $("#memSearch").addEventListener("input", (e) => {
    S.memoryQuery = e.target.value.trim().toLowerCase();
    renderMemoryList();
  });
  await loadMemories();
}

async function loadMemories(){
  const body = $("#memBody");
  body.innerHTML = '<div class="tool-line"><span class="spinner"></span><span>正在载入记忆…</span></div>';
  try{
    const res = await rpc("memory.list");
    S.memories = res.memories || [];
  }catch(err){
    body.innerHTML = '<div class="banner err">' + esc(err.message) + '</div>';
    return;
  }
  renderMemoryList();
}

/* 搜索归一化：去掉空白与常见中英文标点，让"决策11"能匹配"决策：11月20日…" */
function normText(value){
  return String(value == null ? "" : value)
    .toLowerCase()
    .replace(/[\s\u3000]+/g, "")
    .replace(/[，,。.、；;：:！!？?（）()【】\[\]\u201c\u201d"'\u2018\u2019\-—_/\\]+/g, "");
}

/* 把查询拆成关键词：空格 / 全角空格 / 逗号 / 顿号 / 分号 都算分隔符 */
function queryTerms(query){
  return String(query || "")
    .split(/[\s\u3000,，、;；]+/)
    .map(t => normText(t))
    .filter(Boolean);
}

function memoryHaystack(m){
  return normText([
    m.content || "",
    m.source_label || "",
    CATEGORY_TEXT[m.category] || m.category || "",
  ].join(" "));
}

function renderMemoryList(){
  const body = $("#memBody");
  S.memSelected = new Set();
  syncMemBulk();
  let items = S.memories.slice();
  if(S.memoryFilter !== "all") items = items.filter(m => m.category === S.memoryFilter);

  // 搜索：多个关键词可以任意顺序、可以只记得几个字，不再要求整句连续匹配。
  // 先用「全部关键词都命中」；一个都没有时退回「命中任一关键词」，并在顶部说明。
  let fallbackNote = "";
  const terms = queryTerms(S.memoryQuery);
  if(terms.length){
    const all = items.filter(m => {
      const hay = memoryHaystack(m);
      return terms.every(t => hay.indexOf(t) >= 0);
    });
    if(all.length){
      items = all;
    }else{
      const any = items.filter(m => {
        const hay = memoryHaystack(m);
        return terms.some(t => hay.indexOf(t) >= 0);
      });
      if(any.length){
        items = any;
        fallbackNote = '<div class="banner info" style="margin-bottom:10px">没有全部命中的记忆，下面是含任一关键词的 ' +
          any.length + ' 条。</div>';
      }else{
        items = [];
      }
    }
  }

  if(!items.length){
    body.innerHTML = emptyState(
      S.memoryQuery ? "没有找到相关记忆" : "没有符合条件的记忆",
      S.memoryQuery ? "换个词试试，多个关键词用空格分开"
                   : "完成一次会议分析或对话后，关键事实会自动沉淀到这里");
    return;
  }
  // 结构化事实（人物/时间/地点/主题/决策/待办，来自会议与知识）与
  // 对话记忆（AI 从对话中提炼的偏好与规则）性质不同，混排会让人以为
  // 格式乱，这里分组展示。
  const facts = items.filter(m => m.kind !== "note");
  const notes = items.filter(m => m.kind === "note");
  const section = (title, hint, rows) => rows.length
    ? '<div class="mem-sec"><span>' + title + '</span>' +
      '<span class="muted small">' + hint + ' · ' + rows.length + ' 条</span></div>' +
      rows.map(memoryRow).join("")
    : "";
  body.innerHTML = fallbackNote
    + section("结构化事实", "来自会议分析与知识库", facts)
    + section("对话记忆", "AI 从对话中提炼的偏好与规则", notes);

  // 全选：只作用于当前渲染（筛选 + 搜索后）的条目
  const allBox = $("#memAll");
  if(allBox){
    allBox.checked = false;
    allBox.onchange = () => {
      S.memSelected = new Set();
      $$("[data-sel-mem]").forEach(c => {
        c.checked = allBox.checked;
        if(allBox.checked) S.memSelected.add(c.dataset.selMem);
      });
      syncMemBulk();
    };
  }
  body.onchange = (e) => {
    const chk = e.target.closest("[data-sel-mem]");
    if(!chk) return;
    if(chk.checked) S.memSelected.add(chk.dataset.selMem);
    else S.memSelected.delete(chk.dataset.selMem);
    const boxes = $$("[data-sel-mem]");
    if(allBox) allBox.checked = boxes.length > 0 && boxes.every(b => b.checked);
    syncMemBulk();
  };

  body.onclick = async (e) => {
    const btn = e.target.closest("[data-del-mem]");
    if(!btn) return;
    const ok = await confirmAction("删除记忆", "记忆文件与检索索引会一并删除，无法恢复。", "删除");
    if(!ok) return;
    try{
      await rpc("memory.delete", {id: btn.dataset.delMem});
      toast("记忆已删除", "ok");
      await loadMemories();
    }catch(err){ toast(err.message, "err"); }
  };
}

/* 批量删除按钮：只在有选择时出现，并显示已选条数 */
function syncMemBulk(){
  const btn = $("#bulkDelMem");
  if(!btn) return;
  const n = S.memSelected.size;
  btn.hidden = n === 0;
  btn.textContent = n ? ("删除所选 (" + n + ")") : "删除所选";
}

/* 单条记忆的渲染（事实与对话记忆共用同一套排版） */
function memoryRow(m){
  return '<div class="list-item">' +
    '<label class="mem-chk"><input type="checkbox" data-sel-mem="' + esc(m.id) + '" title="选择这条记忆" /></label>' +
    '<div class="li-main">' +
    '<div class="mem-row"><span class="chip brand">' +
      esc(CATEGORY_TEXT[m.category] || m.category || "记忆") + '</span>' +
      '<span class="li-title mem-title" title="' + esc(m.content) + '">' +
        esc(m.content) + '</span></div>' +
    '<div class="li-sub">' + esc(sourceText(m.source_label)) +
      (m.created_at ? " · " + fmtTime(m.created_at) : "") + '</div></div>' +
    '<button class="btn sm danger" data-del-mem="' + esc(m.id) + '">删除</button></div>';
}

/* ── 权限请求（普通用户视角，不展示工具名） ───────────── */
async function handlePermission(data){
  const ok = await confirmAction(
    "需要你的确认",
    (data.action || "执行一个操作") + "：" + (data.target || "") + "\n" + (data.why || ""),
    "允许执行",
    false
  );
  send("permission_response", {id: data.id, approved: ok});
}

/* ── 启动 ─────────────────────────────────────────────── */
function boot(){
  buildSidebar();
  connect();
  go("dashboard");
  window.addEventListener("keydown", (e) => {
    if((e.ctrlKey || e.metaKey) && e.key === "k"){
      e.preventDefault(); go("chat");
      const ta = $("#composer"); if(ta) ta.focus();
    }
  });
}
document.addEventListener("DOMContentLoaded", boot);
})();
"""
