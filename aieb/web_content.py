
# Remote 模式的 Web 前端 HTML。
# 精心重制的高级视觉版本：玻璃拟态 + 渐变品牌色 + 流畅微动效。
# 与 aieb/remote.py 的 WebSocket 协议完全兼容（消息类型未改变）。

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="aieb-version" content="__AIEB_VERSION__">
<title>AI 外脑 Demo · Enterprise AI Workspace</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
  --bg: #f6f6fb;
  --bg-2: #ffffff;
  --surface: rgba(23,24,54,0.04);
  --surface-hover: rgba(23,24,54,0.065);
  --surface-solid: #ffffff;
  --border: rgba(23,24,54,0.09);
  --border-strong: rgba(23,24,54,0.15);
  --text: #23243a;
  --text-dim: #6d6f8a;
  --text-faint: #9598b3;
  --text-bright: #0f1022;
  --accent-1: #7c5cfa;
  --accent-2: #4f8ef7;
  --accent-grad: linear-gradient(135deg, var(--accent-1), var(--accent-2));
  --accent-glow: rgba(124,92,250,0.28);
  --green: #1fa971;
  --red: #e1476b;
  --yellow: #c9860f;
  --code-bg: #16172a;
  --code-text: #dcdcec;
  --code-text-dim: #9b9dbe;
  --code-border: rgba(255,255,255,0.09);
  --code-surface: rgba(255,255,255,0.07);
  --shadow-sm: rgba(30,34,80,0.08);
  --shadow-md: rgba(30,34,80,0.12);
  --shadow-lg: rgba(30,34,80,0.16);
  --radius-lg: 18px;
  --radius-md: 12px;
  --radius-sm: 8px;
}

html, body {
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 14.5px;
  -webkit-font-smoothing: antialiased;
}
code, pre, .mono { font-family: 'JetBrains Mono', 'SF Mono', Consolas, monospace; }

body {
  background:
    radial-gradient(900px 520px at 12% -8%, rgba(124,92,250,0.10), transparent 60%),
    radial-gradient(700px 520px at 100% 8%, rgba(79,142,247,0.09), transparent 55%),
    radial-gradient(600px 500px at 50% 110%, rgba(124,92,250,0.06), transparent 60%),
    var(--bg);
}

#app { display: flex; height: 100vh; width: 100%; position: relative; }

/* ── 侧边栏 ── */
#sidebar {
  width: 272px; flex-shrink: 0; height: 100vh; overflow: hidden;
  background: var(--surface-solid); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; transition: width 0.2s ease;
}
#app.sidebar-collapsed #sidebar { width: 0; border-right: none; }
.sidebar-header { display: flex; align-items: center; gap: 9px; padding: 16px 16px 12px; flex-shrink: 0; }
.brand-mark-sm {
  width: 26px; height: 26px; border-radius: 8px; display: flex; align-items: center; justify-content: center;
  background: var(--accent-grad); box-shadow: 0 0 14px var(--accent-glow); flex-shrink: 0;
}
.sidebar-brand-title {
  font-weight: 800; font-size: 14.5px; letter-spacing: -0.01em; white-space: nowrap;
  background: var(--accent-grad); -webkit-background-clip: text; background-clip: text; color: transparent;
}
#new-chat-btn {
  margin: 4px 12px 14px; display: flex; align-items: center; gap: 8px; justify-content: center;
  padding: 9px 14px; border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface-solid);
  color: var(--text); font-family: inherit; font-weight: 600; font-size: 13px; cursor: pointer; flex-shrink: 0;
  box-shadow: 0 2px 8px var(--shadow-sm); transition: all 0.15s ease;
}
#new-chat-btn:hover { background: var(--surface-hover); transform: translateY(-1px); box-shadow: 0 4px 14px var(--shadow-sm); }
#new-chat-btn:active { transform: translateY(0); }

/* ── 侧边栏底部：记忆管理入口 ── */
.sidebar-footer { padding: 8px 12px 12px; flex-shrink: 0; }
#memory-btn {
  width: 100%; display: flex; align-items: center; gap: 8px; padding: 9px 12px;
  border-radius: 10px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-dim); font-family: inherit; font-size: 12.5px; cursor: pointer;
  transition: all 0.15s ease;
}
#memory-btn:hover { background: var(--surface-hover); color: var(--text); }

/* ── 显示模式切换（普通 / 开发者） ── */
.display-mode { margin-bottom: 8px; }
.display-mode-label {
  display: block; font-size: 11px; color: var(--text-faint);
  padding: 0 2px 5px; letter-spacing: 0.02em;
}
.mode-seg {
  display: flex; gap: 2px; padding: 3px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface);
}
.mode-btn {
  flex: 1; padding: 6px 8px; border: 0; border-radius: 8px; background: transparent;
  color: var(--text-dim); font-family: inherit; font-size: 12px; cursor: pointer;
  transition: all 0.15s ease; white-space: nowrap;
}
.mode-btn:hover { color: var(--text); }
.mode-btn.active {
  background: var(--accent-1); color: #fff; font-weight: 600;
  box-shadow: 0 2px 8px var(--shadow-sm);
}

/* ── 执行状态容器（一轮一个） ──
   普通模式：只有一行状态；开发者模式：可展开的执行详情。
   两种模式共用同一个容器元素，切换时重建内部结构而不是靠 display:none 藏旧节点。 */
.thinking-block.exec-panel { margin: 6px 0 12px 36px; }
.exec-panel.mode-normal .thinking-header { cursor: default; }
.exec-panel.mode-normal .thinking-header .chev { display: none; }
.exec-panel.mode-normal .exec-count { color: var(--text-faint); }
.exec-panel .exec-status-running .exec-glyph { animation: pulse-dot 1.4s ease-in-out infinite; }
/* 执行详情行数可能较多：展开后给更宽松的高度上限，内部行自己滚动 */
.exec-panel .thinking-body.show { max-height: 460px; }
.exec-panel.failed { border-color: rgba(225,71,107,0.45); }
.exec-panel.failed .exec-label { color: var(--red); }
.exec-rows { display: flex; flex-direction: column; gap: 6px; }
details.exec-row {
  border: 1px solid var(--border); border-radius: 8px; background: var(--surface);
  overflow: hidden;
}
details.exec-row > summary {
  list-style: none; cursor: pointer; padding: 7px 10px;
  display: flex; align-items: center; gap: 8px;
  font-size: 12px; color: var(--text-dim);
}
details.exec-row > summary::-webkit-details-marker { display: none; }
details.exec-row > summary:hover { background: var(--surface-hover); }
details.exec-row .exec-name {
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px; color: var(--text-bright);
}
details.exec-row .exec-badge {
  margin-left: auto; font-size: 11px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--border);
}
details.exec-row .exec-badge.ok { color: var(--green); border-color: rgba(74,222,128,0.4); }
details.exec-row .exec-badge.failed { color: var(--red); border-color: rgba(225,71,107,0.4); }
details.exec-row .exec-badge.running { color: var(--accent-2); border-color: rgba(79,142,247,0.4); }
details.exec-row .exec-badge.cancelled,
details.exec-row .exec-badge.unknown { color: var(--text-faint); }
details.exec-row .exec-time { font-size: 11px; color: var(--text-faint); }
.exec-row-body {
  border-top: 1px solid var(--border); padding: 8px 10px;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px; line-height: 1.5; color: var(--text-faint);
  white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto;
}
.exec-row-body .exec-section-label {
  display: block; color: var(--accent-2); font-weight: 600; margin: 0 0 3px;
  font-family: inherit;
}
.exec-row-body .exec-section + .exec-section { margin-top: 8px; }
.exec-thought {
  margin: 0 0 10px; padding: 0 2px; white-space: pre-wrap;
  font-style: italic; font-size: 12px; color: var(--text-faint);
}

/* ── 记忆管理面板 ── */
#memory-modal {
  position: fixed; inset: 0; z-index: 60; display: none;
  align-items: center; justify-content: center; padding: 18px;
  background: rgba(16,16,32,0.42); backdrop-filter: blur(4px);
}
#memory-modal.show { display: flex; }
.memory-card {
  width: min(560px, 94vw); max-height: min(78vh, 720px); display: flex; flex-direction: column;
  background: var(--surface-solid); border: 1px solid var(--border-strong);
  border-radius: var(--radius-lg); box-shadow: 0 18px 48px var(--shadow-md); overflow: hidden;
}
.memory-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 18px 10px; font-size: 15px; font-weight: 700; color: var(--text-bright);
}
#memory-close {
  border: none; background: transparent; color: var(--text-faint); cursor: pointer;
  font-size: 15px; padding: 4px 8px; border-radius: 8px;
}
#memory-close:hover { background: var(--surface-hover); color: var(--text); }
.memory-intro { padding: 0 18px 12px; font-size: 12.5px; color: var(--text-faint); line-height: 1.6; }
.memory-switch {
  display: flex; align-items: center; gap: 10px; margin: 0 18px 10px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px;
  font-size: 13px; color: var(--text); cursor: pointer; background: var(--surface);
}
.memory-switch input { width: 16px; height: 16px; accent-color: var(--accent-1); cursor: pointer; }
.memory-hint { padding: 0 18px 8px; font-size: 11.5px; color: var(--text-faint); }
.memory-list { flex: 1; overflow-y: auto; padding: 4px 18px 8px; display: flex; flex-direction: column; gap: 8px; }
.memory-item {
  border: 1px solid var(--border); border-radius: 10px; background: var(--surface);
  padding: 10px 12px; display: flex; gap: 10px; align-items: flex-start;
}
.memory-item-main { flex: 1; min-width: 0; }
.memory-item-title { font-size: 13px; font-weight: 600; color: var(--text); word-break: break-word; }
.memory-item-preview {
  margin-top: 4px; font-size: 12px; color: var(--text-dim); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.memory-item-meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; }
.memory-tag {
  font-size: 10.5px; padding: 2px 7px; border-radius: 100px;
  background: var(--surface-hover); color: var(--text-faint); white-space: nowrap;
}
.memory-item-del {
  border: 1px solid var(--border); background: transparent; color: var(--text-faint);
  border-radius: 8px; padding: 4px 9px; font-size: 11.5px; cursor: pointer; flex-shrink: 0;
}
.memory-item-del:hover { color: var(--red); border-color: rgba(225,71,107,0.5); background: rgba(225,71,107,0.08); }
.memory-empty { padding: 20px 18px; text-align: center; font-size: 12.5px; color: var(--text-faint); }
.memory-actions {
  display: flex; gap: 8px; justify-content: flex-end; padding: 12px 18px 16px;
  border-top: 1px solid var(--border); flex-shrink: 0;
}
.memory-actions button {
  padding: 7px 12px; border-radius: 8px; font-family: inherit; font-size: 12px; font-weight: 600; cursor: pointer;
}
#memory-clear-project { border: 1px solid var(--border); background: transparent; color: var(--text-dim); }
#memory-clear-project:hover { background: var(--surface-hover); color: var(--text); }
#memory-clear-all { border: 1px solid rgba(225,71,107,0.45); background: transparent; color: var(--red); }
#memory-clear-all:hover { background: rgba(225,71,107,0.1); }
.memory-actions button:disabled { opacity: 0.45; cursor: not-allowed; }
@media (max-width: 640px) {
  .memory-card { width: 96vw; max-height: 82vh; }
  .memory-actions { flex-direction: column; }
  .memory-actions button { width: 100%; }
}
.sidebar-section-label {
  padding: 10px 16px 4px; font-size: 10.8px; font-weight: 700; color: var(--text-faint);
  text-transform: uppercase; letter-spacing: 0.06em;
}
#session-list { flex: 1; overflow-y: auto; padding: 0 8px 14px; }
#session-list::-webkit-scrollbar { width: 6px; }
#session-list::-webkit-scrollbar-track { background: transparent; }
#session-list::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 3px; }
.session-empty-hint { padding: 10px 12px; font-size: 12px; color: var(--text-faint); }
.session-item {
  display: flex; align-items: center; gap: 4px; padding: 8px 8px 8px 10px; margin: 1px 0; border-radius: 8px;
  cursor: pointer; font-size: 12.8px; color: var(--text-dim); transition: background 0.12s ease, color 0.12s ease;
}
.session-item:hover { background: var(--surface-hover); color: var(--text); }
.session-item.active { background: rgba(124,92,250,0.11); color: var(--text-bright); font-weight: 600; }
.session-item .session-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.session-delete, .session-rename {
  opacity: 0; flex-shrink: 0; width: 22px; height: 22px; border-radius: 6px; border: none; background: transparent;
  color: var(--text-faint); cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.12s ease;
}
.session-item:hover .session-delete, .session-item.active .session-delete,
.session-item:hover .session-rename, .session-item.active .session-rename { opacity: 1; }
.session-delete:hover { background: rgba(225,71,107,0.14); color: var(--red); }
.session-rename:hover { background: var(--surface-hover); color: var(--text); }
.session-title-input {
  flex: 1; min-width: 0; background: var(--bg-2); color: var(--text-bright); border: 1px solid var(--accent-1);
  border-radius: 6px; padding: 2px 6px; font-family: inherit; font-size: 12.8px; font-weight: 600;
}
#session-search-wrap { padding: 0 12px 8px; }
#session-search {
  width: 100%; box-sizing: border-box; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 10px; font-family: inherit; font-size: 12.5px; color: var(--text);
}
#session-search::placeholder { color: var(--text-faint); }
#session-search:focus { outline: none; border-color: var(--accent-1); }

#sidebar-backdrop { display: none; }

/* ── 主区域 ── */
#main { flex: 1; display: flex; flex-direction: column; height: 100vh; min-width: 0; }

/* ── 顶部状态栏 ── */
#status-bar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 12px 20px; flex-shrink: 0; position: sticky; top: 0; z-index: 20;
  background: rgba(246,246,251,0.78); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
}
.status-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
#sidebar-toggle-btn {
  width: 32px; height: 32px; border-radius: 8px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-dim); cursor: pointer; display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; transition: all 0.15s ease;
}
#sidebar-toggle-btn:hover { background: var(--surface-hover); color: var(--text); }
.brand-sub { font-size: 11.5px; color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; display: none !important; }
.status-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.pill {
  display: flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 100px;
  font-size: 11.5px; color: var(--text-dim); background: var(--surface); border: 1px solid var(--border);
  white-space: nowrap;
}
#provider-select {
  padding: 5px 10px; border-radius: 100px; font-size: 11.5px; font-family: inherit;
  color: var(--text-dim); background: var(--surface); border: 1px solid var(--border); cursor: pointer;
}
#provider-select:focus { outline: none; border-color: var(--accent-1); }
#tts-rate {
  padding: 5px 8px; border-radius: 100px; font-size: 11.5px; font-family: inherit;
  color: var(--text-dim); background: var(--surface); border: 1px solid var(--border); cursor: pointer;
}
#tts-rate:focus { outline: none; border-color: var(--accent-1); }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }
.dot.connected { background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse-dot 2s ease-in-out infinite; }
.dot.disconnected { background: var(--red); }
@keyframes pulse-dot { 0%,100% { opacity: 1; } 50% { opacity: 0.45; } }

/* ── 消息区域 ── */
#messages-wrap { flex: 1; overflow: hidden; position: relative; }
#messages {
  height: 100%; overflow-y: auto; scroll-behavior: smooth;
}
#messages::-webkit-scrollbar { width: 8px; }
#messages::-webkit-scrollbar-track { background: transparent; }
#messages::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
#messages::-webkit-scrollbar-thumb:hover { background: rgba(23,24,54,0.28); }
#messages-col { max-width: 820px; margin: 0 auto; min-height: 100%; box-sizing: border-box; padding: 24px 24px 12px; }

@keyframes fadeInUp { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

/* ── 空状态欢迎页 ── */
#empty-state {
  height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center; padding: 20px; gap: 6px; animation: fadeInUp 0.4s ease;
}
#empty-state .hero-mark {
  width: 64px; height: 64px; border-radius: 20px; display: flex; align-items: center; justify-content: center;
  background: var(--accent-grad); box-shadow: 0 0 40px var(--accent-glow); margin-bottom: 18px;
}
#empty-state h1 {
  font-size: 24px; font-weight: 800; letter-spacing: -0.02em;
  background: var(--accent-grad); -webkit-background-clip: text; background-clip: text; color: transparent;
  margin-bottom: 6px;
}
#empty-state p { color: var(--text-dim); font-size: 13.5px; max-width: 380px; margin-bottom: 22px; }
.hero-chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; max-width: 480px; }
.hero-chip {
  padding: 8px 14px; border-radius: 100px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-dim); font-size: 12.5px; cursor: pointer; transition: all 0.15s ease;
}
.hero-chip:hover { background: var(--surface-hover); border-color: var(--border-strong); color: var(--text); transform: translateY(-1px); }

/* ── 消息行（用户 / 助手） ── */
.msg-row { display: flex; gap: 10px; margin-bottom: 22px; animation: fadeInUp 0.25s ease; }
.avatar {
  width: 26px; height: 26px; border-radius: 8px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; margin-top: 2px;
}
.avatar.assistant { background: var(--accent-grad); box-shadow: 0 0 12px var(--accent-glow); }
.avatar.user { background: var(--surface-solid); border: 1px solid var(--border-strong); color: var(--text-dim); font-size: 11px; font-weight: 700; }

.msg-row.user { flex-direction: row-reverse; }
.msg-row.user .col { align-items: flex-end; display: flex; flex-direction: column; max-width: 76%; }
.msg-row.assistant .col { flex: 1; min-width: 0; }

.bubble-user {
  background: var(--accent-grad); color: #0c0c16; font-weight: 500;
  padding: 10px 14px; border-radius: 16px 16px 4px 16px; white-space: pre-wrap; line-height: 1.55;
}
.msg-user-actions { display: flex; align-items: center; gap: 6px; margin-top: 4px; align-self: flex-end; }
.msg-edit-btn {
  opacity: 0; display: flex; align-items: center; gap: 4px;
  background: transparent; border: none; color: var(--text-faint); cursor: pointer; font-size: 11px;
  padding: 2px 4px; transition: opacity 0.12s ease, color 0.12s ease;
}
.msg-row.user:hover .msg-edit-btn { opacity: 1; }
.msg-edit-btn:hover { color: var(--text); }
.turn-version-nav { display: none; align-items: center; gap: 1px; font-size: 11px; color: var(--text-faint); }
.turn-version-nav.show { display: flex; }
.turn-version-nav button {
  background: transparent; border: none; color: var(--text-faint); cursor: pointer; padding: 2px 3px;
  border-radius: 4px; font-size: 12px; line-height: 1; display: flex; align-items: center;
}
.turn-version-nav button:hover:not(:disabled) { background: var(--surface); color: var(--text); }
.turn-version-nav button:disabled { opacity: 0.3; cursor: default; }
.turn-version-nav span { padding: 0 2px; white-space: nowrap; }
.msg-edit-area {
  width: 100%; max-width: 420px; box-sizing: border-box; background: var(--bg-2); color: var(--text-bright);
  border: 1px solid var(--accent-1); border-radius: var(--radius-md); padding: 10px 12px;
  font-family: inherit; font-size: 14px; line-height: 1.55; resize: vertical; min-height: 60px;
}
.msg-edit-actions { display: flex; gap: 6px; justify-content: flex-end; margin-top: 6px; }
.msg-edit-actions button {
  padding: 5px 12px; border-radius: 8px; border: 1px solid var(--border); cursor: pointer;
  font-family: inherit; font-size: 12px; font-weight: 600; background: var(--surface); color: var(--text-dim);
}
.msg-edit-actions .btn-save { background: var(--accent-grad); color: #0c0c16; border: none; }
.msg-edit-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
/* 编辑历史消息时保留/移除原附件 */
.msg-edit-attach { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; max-width: 420px; justify-content: flex-end; }
.msg-edit-attach .attach-chip {
  display: inline-flex; align-items: center; gap: 6px; padding: 4px 8px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-dim); font-size: 11.5px;
  max-width: 200px;
}
.msg-edit-attach .attach-chip .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.msg-edit-attach .attach-chip button {
  background: transparent; border: none; color: var(--text-faint); cursor: pointer;
  font-size: 13px; line-height: 1; padding: 0 2px;
}
.msg-edit-attach .attach-chip button:hover { color: var(--red); }
.msg-edit-attach img.thumb { width: 40px; height: 40px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border); }
.msg-edit-hint { font-size: 11px; color: var(--text-faint); margin-top: 6px; text-align: right; max-width: 420px; }
/* 触屏没有 hover：编辑入口常驻可点 */
@media (hover: none) {
  .msg-edit-btn { opacity: 0.75; }
  .msg-user-actions { margin-top: 6px; }
}
/* 外部链接：默认在下划线可识别；被安全策略拦下的链接显示为不可点 */
.content a { color: var(--accent-1); text-decoration: underline; text-underline-offset: 2px; }
.content a.link-blocked { color: var(--text-faint); text-decoration: line-through; cursor: not-allowed; }
.msg-retry-btn {
  display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; background: transparent;
  border: none; color: var(--text-faint); cursor: pointer; font-size: 11.5px; padding: 2px 6px;
  border-radius: 6px; transition: all 0.12s ease; opacity: 0.7;
}
.msg-row.assistant:hover .msg-retry-btn { opacity: 1; }
/* 每条回答下方的操作行（重新生成 + ‹ 1/2 ›）*/
.msg-assistant-actions { display: flex; align-items: center; gap: 4px; margin-top: 4px; flex-wrap: wrap; }
.msg-assistant-actions .msg-retry-btn { margin-left: 0; }
.turn-version-nav-assistant { margin-left: 2px; }
@media (hover: none) {
  .msg-retry-btn { opacity: 1; }
}
.msg-retry-btn:hover { background: var(--surface); color: var(--text); }
.msg-speak-btn {
  display: inline-flex; align-items: center; gap: 4px; margin-left: 8px; background: transparent;
  border: none; color: var(--text-faint); cursor: pointer; font-size: 11.5px; padding: 2px 6px;
  border-radius: 6px; transition: all 0.12s ease;
}
.msg-speak-btn:hover { background: var(--surface); color: var(--text); }
.msg-speak-btn.speaking { color: var(--accent-1); animation: pulse-dot 1.4s ease-in-out infinite; }
/* ── 本轮产物（图片预览 + 按需下载）── */
.artifacts { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; max-width: 760px; }
.artifact {
  display: flex; align-items: center; gap: 12px;
  border: 1px solid var(--border); border-radius: 12px;
  background: var(--surface-solid, var(--surface)); padding: 10px 14px;
}
.artifact-thumb {
  width: 44px; height: 44px; object-fit: cover; border-radius: 8px;
  cursor: zoom-in; flex-shrink: 0; border: 1px solid var(--border);
}
.artifact-glyph {
  width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;
  font-size: 20px; border-radius: 8px; background: var(--surface); flex-shrink: 0;
}
.artifact-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1; }
.artifact-name {
  font-size: 13px; font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.artifact-sub { font-size: 11.5px; color: var(--text-faint); }
.artifact-dl {
  margin-left: auto; color: var(--accent-1); text-decoration: none; font-weight: 600;
  font-size: 12px; white-space: nowrap; padding: 4px 10px; border-radius: 8px;
}
.artifact-dl:hover { background: var(--surface-hover); }
.artifact-gone { margin-left: auto; font-size: 11.5px; color: var(--red); white-space: nowrap; }

.content { color: var(--text); line-height: 1.7; }
.content > *:first-child { margin-top: 0 !important; }
.content p { margin-bottom: 10px; }
.content code {
  background: var(--code-bg); padding: 2px 6px; border-radius: 4px;
  font-size: 12.8px; border: 1px solid var(--code-border); color: #d7c9ff;
}
.content pre {
  background: var(--code-bg); border-radius: var(--radius-sm);
  margin: 10px 0; border: 1px solid var(--code-border); overflow: hidden;
  box-shadow: 0 6px 20px var(--shadow-sm);
}
.content pre code { background: none; border: none; padding: 14px; display: block; overflow-x: auto; font-size: 12.8px; line-height: 1.6; color: var(--code-text); }
.code-bar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 12px; border-bottom: 1px solid var(--code-border); background: rgba(255,255,255,0.03);
}
.code-lang { font-size: 11px; color: var(--code-text-dim); text-transform: uppercase; letter-spacing: 0.04em; }
.code-copy {
  font-size: 11px; color: var(--code-text-dim); background: var(--code-surface); border: 1px solid var(--code-border);
  border-radius: 6px; padding: 3px 9px; cursor: pointer; font-family: inherit; transition: all 0.15s ease;
}
.code-copy:hover { background: rgba(255,255,255,0.14); color: var(--code-text); }

.content ul, .content ol { padding-left: 22px; margin: 8px 0; }
.content li { margin: 4px 0; }
.content h1, .content h2, .content h3 { color: var(--text-bright); margin: 16px 0 8px; font-weight: 700; letter-spacing: -0.01em; }
.content h1 { font-size: 19px; } .content h2 { font-size: 16.5px; } .content h3 { font-size: 15px; }
.content a { color: var(--accent-2); text-decoration: none; border-bottom: 1px solid rgba(79,142,247,0.4); }
.content a:hover { border-bottom-color: var(--accent-2); }
.content blockquote {
  border-left: 3px solid var(--accent-1); padding: 4px 14px; color: var(--text-dim); margin: 10px 0;
  background: var(--surface); border-radius: 0 8px 8px 0;
}
.content hr { border: none; border-top: 1px solid var(--border); margin: 14px 0; }
.content table { border-collapse: collapse; margin: 10px 0; width: 100%; font-size: 13px; }
.content th, .content td { border: 1px solid var(--border); padding: 7px 12px; text-align: left; }
.content th { background: var(--surface); color: var(--text-bright); font-weight: 600; }
.content tr:nth-child(even) td { background: rgba(23,24,54,0.02); }

/* hljs 简化配色，贴合品牌色（代码块始终为深色，与主题无关） */
.hljs-keyword, .hljs-tag, .hljs-selector-tag { color: #8b9dfc; }
.hljs-string, .hljs-attr, .hljs-symbol, .hljs-addition { color: #a3e2a3; }
.hljs-comment, .hljs-quote { color: var(--code-text-dim); font-style: italic; }
.hljs-number, .hljs-literal { color: #f0b859; }
.hljs-title, .hljs-function, .hljs-name, .hljs-section { color: #7bc9f0; }
.hljs-built_in, .hljs-type, .hljs-class { color: #d7c9ff; }
.hljs-variable, .hljs-params, .hljs-attribute { color: #dcdcec; }
.hljs-deletion { color: var(--red); }

.msg-system { color: var(--text-faint); font-size: 12.5px; white-space: pre-wrap; margin: 6px 0 18px; padding-left: 36px; }
.msg-error {
  color: var(--red); font-size: 13px; margin: 6px 0 18px; padding: 10px 14px;
  background: rgba(225,71,107,0.07); border: 1px solid rgba(225,71,107,0.22); border-radius: var(--radius-sm);
  display: flex; align-items: center; gap: 8px;
}

/* ── 工具调用卡片 ── */
.tool-block {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-md);
  margin: 6px 0 18px 36px; overflow: hidden; transition: border-color 0.15s ease;
}
.tool-block:hover { border-color: var(--border-strong); }
.tool-header {
  display: flex; align-items: center; gap: 9px;
  padding: 9px 13px; cursor: pointer; user-select: none; font-size: 12.8px;
}
.tool-header:hover { background: var(--surface-hover); }
.tool-header .chev { color: var(--text-faint); transition: transform 0.2s ease; display: flex; }
.tool-header .chev.expanded { transform: rotate(90deg); }
.tool-icon {
  width: 20px; height: 20px; border-radius: 6px; display: flex; align-items: center; justify-content: center;
  background: rgba(79,142,247,0.12); color: var(--accent-2); flex-shrink: 0;
}
.tool-header .name { color: var(--text-bright); font-weight: 600; }
.tool-header .preview {
  color: var(--text-faint); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1; min-width: 0;
}
.tool-header .status { margin-left: auto; font-size: 11.5px; flex-shrink: 0; display: flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 100px; }
.tool-header .status.ok { color: var(--green); background: rgba(31,169,113,0.1); }
.tool-header .status.err { color: var(--red); background: rgba(225,71,107,0.1); }
.tool-header .status.loading { color: var(--yellow); background: rgba(201,134,15,0.1); }
.spin { animation: spin 0.9s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
.tool-body {
  max-height: 0; overflow: hidden; transition: max-height 0.25s ease;
  border-top: 0 solid var(--border); font-size: 12px; color: var(--text-dim);
}
.tool-body.show { max-height: 320px; overflow-y: auto; border-top: 1px solid var(--border); }
.tool-body .inner { padding: 10px 13px; white-space: pre-wrap; word-break: break-word; }
.tool-body .args-label { color: var(--accent-2); font-weight: 600; margin-bottom: 4px; display: block; }

/* ── 思考过程 ── */
.thinking-block {
  background: transparent; border: 1px dashed var(--border); border-radius: var(--radius-md);
  margin: 6px 0 18px 36px; overflow: hidden;
}
.thinking-header {
  display: flex; align-items: center; gap: 8px; padding: 8px 13px; cursor: pointer; user-select: none;
  font-size: 12.5px; color: var(--text-faint);
}
.thinking-header:hover { background: var(--surface); }
.thinking-header .chev { transition: transform 0.2s ease; display: flex; }
.thinking-header .chev.expanded { transform: rotate(90deg); }
.think-glyph { animation: pulse-dot 1.4s ease-in-out infinite; }
.thinking-body {
  max-height: 0; overflow: hidden; transition: max-height 0.25s ease; border-top: 0 solid var(--border);
}
.thinking-body.show { max-height: 280px; overflow-y: auto; border-top: 1px solid var(--border); }
.thinking-body .inner { padding: 10px 13px; white-space: pre-wrap; color: var(--text-faint); font-style: italic; font-size: 12px; }
/* 同一个 Thought 容器内部：思考段落 + 工具调用段落 */
.thinking-body .inner .thought-text { white-space: pre-wrap; margin: 0 0 8px; }
.thinking-body .inner .tool-seg {
  white-space: normal; font-style: normal; margin: 10px 0;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface);
}
.thinking-body .inner .tool-title { font-size: 12px; color: var(--text-dim, var(--text-faint)); font-weight: 600; }
.thinking-body .inner .tool-glyph { margin-right: 4px; }
.thinking-body .inner .tool-body {
  margin: 6px 0 0; padding: 0; font-style: normal; white-space: pre-wrap; word-break: break-word;
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11.5px; line-height: 1.5; color: var(--text-faint);
  max-height: 200px; overflow: auto;
}

/* ── 弹窗（权限 / 提问） ── */
.dialog-card {
  background: var(--surface-solid); border: 1px solid var(--border-strong); border-left: 3px solid var(--yellow);
  border-radius: var(--radius-md); padding: 16px; margin: 6px 0 18px 36px;
  box-shadow: 0 8px 24px var(--shadow-md); animation: fadeInUp 0.2s ease;
}
.dialog-card.answered { border-left-color: var(--border); }
.dialog-title { display: flex; align-items: center; gap: 8px; color: var(--text-bright); font-weight: 700; font-size: 13.5px; margin-bottom: 10px; }
.dialog-desc { color: var(--text-dim); margin-bottom: 14px; font-size: 13px; white-space: pre-wrap; line-height: 1.6; }
.dialog-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.dialog-actions button {
  padding: 7px 16px; border-radius: 8px; border: 1px solid var(--border);
  cursor: pointer; font-family: inherit; font-size: 12.8px; font-weight: 600; transition: all 0.15s ease;
}
.dialog-actions button:hover { transform: translateY(-1px); }
.btn-allow { background: var(--green); color: #062; border-color: var(--green); }
.btn-deny { background: transparent; color: var(--red); border-color: rgba(225,71,107,0.4); }
.btn-always { background: transparent; color: var(--accent-2); border-color: rgba(79,142,247,0.4); }
.q-block { margin-bottom: 14px; }
.q-text { margin-bottom: 8px; color: var(--text-bright); font-size: 13px; font-weight: 500; }
.q-option {
  display: flex; align-items: baseline; gap: 8px; margin: 5px 0; cursor: pointer; padding: 6px 10px;
  border-radius: 8px; border: 1px solid transparent; transition: all 0.15s ease; font-size: 12.8px;
}
.q-option:hover { background: var(--surface); border-color: var(--border); }
.q-option input[type="radio"] { accent-color: var(--accent-1); }
.q-option .opt-label { color: var(--accent-2); font-weight: 600; }
.q-option .opt-desc { color: var(--text-faint); font-size: 12px; }
.q-other-input {
  background: var(--bg-2); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 9px; font-family: inherit; font-size: 12.5px; width: 260px; margin-left: 4px;
}

/* ── 完成指示 ── */
.done-indicator { color: var(--text-faint); font-size: 11.5px; margin: 4px 0 20px 36px; display: flex; align-items: center; gap: 6px; }

/* ── 流式光标 ── */
.cursor { display: inline-block; width: 2px; height: 15px; background: var(--accent-2); margin-left: 2px; vertical-align: -2px; animation: blink 0.9s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* ── 回到底部悬浮按钮 ── */
.scroll-fab {
  position: absolute; bottom: 14px; left: 50%; transform: translateX(-50%);
  background: var(--surface-solid); border: 1px solid var(--border-strong); color: var(--text);
  padding: 7px 16px; border-radius: 100px; font-size: 12px; cursor: pointer; z-index: 15;
  box-shadow: 0 6px 20px var(--shadow-md); display: none; align-items: center; gap: 6px;
  font-family: inherit; transition: all 0.15s ease;
}
.scroll-fab:hover { background: var(--surface-hover); transform: translateX(-50%) translateY(-2px); }

/* ── 斜杠命令菜单 ── */
#slash-menu {
  display: none; position: absolute; bottom: 100%; left: 0; right: 0;
  background: var(--surface-solid); border: 1px solid var(--border-strong);
  border-radius: var(--radius-md); margin-bottom: 8px; max-height: 260px;
  overflow-y: auto; box-shadow: 0 -8px 28px var(--shadow-lg); padding: 6px;
}
#slash-menu.show { display: block; }
.slash-item {
  padding: 8px 10px; cursor: pointer; display: flex; gap: 10px; align-items: baseline;
  border-radius: 8px; border-left: 2px solid transparent;
}
.slash-item:hover, .slash-item.active { background: var(--surface-hover); border-left-color: var(--accent-1); }
.slash-item .cmd-name { color: var(--accent-2); font-weight: 700; white-space: nowrap; font-size: 12.8px; }
.slash-item .cmd-desc { color: var(--text-faint); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── 输入区域 ── */
#input-outer { flex-shrink: 0; padding: 12px 20px 18px; }
#input-inner { max-width: 820px; margin: 0 auto; position: relative; }
#input-area {
  display: flex; gap: 8px; align-items: flex-end; background: var(--surface-solid);
  border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 8px 8px 8px 16px;
  box-shadow: 0 10px 30px var(--shadow-md); transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
#input-area:focus-within { border-color: var(--accent-1); box-shadow: 0 10px 30px var(--shadow-md), 0 0 0 3px rgba(124,92,250,0.14); }
#input-area textarea {
  flex: 1; background: transparent; color: var(--text-bright); border: none; outline: none;
  font-family: inherit; font-size: 14px; resize: none; min-height: 26px; max-height: 200px;
  line-height: 1.55; padding: 8px 0;
}
#input-area textarea::placeholder { color: var(--text-faint); }
#send-btn {
  background: var(--accent-grad); color: #0c0c16; border: none;
  border-radius: 12px; width: 40px; height: 40px; cursor: pointer; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; transition: all 0.15s ease;
  box-shadow: 0 4px 14px var(--accent-glow);
}
#send-btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
#send-btn:disabled { opacity: 0.35; cursor: not-allowed; box-shadow: none; transform: none; }
#send-btn.stop-mode { background: var(--surface); color: var(--red); box-shadow: none; border: 1px solid rgba(225,71,107,0.4); }
#send-btn.stop-mode:disabled { opacity: 1; cursor: pointer; }
#upload-btn {
  background: var(--surface); color: var(--text-dim); border: 1px solid var(--border);
  border-radius: 12px; width: 40px; height: 40px; cursor: pointer; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; transition: all 0.15s ease;
}
#upload-btn:hover { background: var(--surface-hover); color: var(--text); }
#mic-btn {
  background: var(--surface); color: var(--text-dim); border: 1px solid var(--border);
  border-radius: 12px; width: 40px; height: 40px; cursor: pointer; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center; transition: all 0.15s ease;
}
#mic-btn:hover { background: var(--surface-hover); color: var(--text); }
#mic-btn.recording { color: #fff; background: var(--red); border-color: var(--red); animation: pulse-dot 1.1s ease-in-out infinite; }
#mic-btn:disabled { opacity: 0.45; cursor: not-allowed; }
#image-preview-row {
  display: none; flex-wrap: wrap; gap: 8px; padding: 0 4px 10px;
}
#image-preview-row.show { display: flex; }
.image-preview-item { position: relative; width: 56px; height: 56px; flex-shrink: 0; }
.image-preview-item img {
  width: 100%; height: 100%; object-fit: cover; border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong);
}
.image-preview-remove {
  position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%;
  background: var(--red); color: #fff; border: none; cursor: pointer; font-size: 11px;
  line-height: 1; display: flex; align-items: center; justify-content: center;
}
.file-chip {
  display: flex; align-items: center; gap: 6px; padding: 6px 10px 6px 8px;
  border-radius: var(--radius-sm); background: var(--surface); border: 1px solid var(--border-strong);
  font-size: 12px; color: var(--text-dim); max-width: 220px;
}
.file-chip .file-chip-icon { flex-shrink: 0; font-size: 14px; }
.file-chip .file-chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-chip .file-chip-remove {
  flex-shrink: 0; width: 16px; height: 16px; border-radius: 50%; background: var(--border-strong);
  color: var(--text); border: none; cursor: pointer; font-size: 10px; line-height: 1;
  display: flex; align-items: center; justify-content: center; margin-left: 2px;
}
.file-chip .file-chip-remove:hover { background: var(--red); color: #fff; }
.bubble-user-files { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; justify-content: flex-end; }
.bubble-user-files .file-chip { background: var(--surface-solid); }
.bubble-user-images { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; justify-content: flex-end; }
.bubble-user-images img {
  max-width: 200px; max-height: 200px; border-radius: var(--radius-md);
  border: 1px solid var(--border-strong); display: block; cursor: zoom-in;
}
#image-lightbox {
  display: none; position: fixed; inset: 0; z-index: 100; background: rgba(10,10,18,0.82);
  align-items: center; justify-content: center; padding: 40px; cursor: zoom-out;
}
#image-lightbox.show { display: flex; }
#image-lightbox img { max-width: 100%; max-height: 100%; border-radius: var(--radius-md); box-shadow: 0 20px 60px rgba(0,0,0,0.5); }
#lightbox-close {
  position: absolute; top: 18px; right: 22px; width: 36px; height: 36px; border-radius: 50%;
  background: rgba(255,255,255,0.12); border: none; color: #fff; cursor: pointer; font-size: 18px;
  display: flex; align-items: center; justify-content: center;
}
#lightbox-close:hover { background: rgba(255,255,255,0.22); }
#confirm-modal {
  display: none; position: fixed; inset: 0; z-index: 110; background: rgba(10,10,18,0.55);
  backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
  align-items: center; justify-content: center; padding: 20px;
}
#confirm-modal.show { display: flex; }
.confirm-card {
  background: var(--surface-solid); border: 1px solid var(--border-strong); border-radius: var(--radius-lg);
  padding: 22px 24px; max-width: 380px; width: 100%; box-shadow: 0 24px 64px rgba(0,0,0,0.35);
  animation: fadeInUp 0.18s ease;
}
.confirm-title { font-size: 15.5px; font-weight: 700; color: var(--text-bright); margin-bottom: 8px; }
.confirm-desc { font-size: 13px; color: var(--text-dim); line-height: 1.55; margin-bottom: 20px; white-space: pre-line; }
.confirm-option {
  display: flex; align-items: flex-start; gap: 8px;
  font-size: 13px; color: var(--text); line-height: 1.5;
  padding: 10px 12px; margin: -6px 0 18px;
  border: 1px solid var(--border); border-radius: var(--radius-md);
  background: var(--surface); cursor: pointer;
}
.confirm-option:hover { background: var(--surface-hover); }
.confirm-option input {
  margin: 2px 0 0; width: 15px; height: 15px; flex-shrink: 0;
  accent-color: var(--accent-1); cursor: pointer;
}
.confirm-option .confirm-option-hint {
  display: block; margin-top: 3px; font-size: 11.5px; color: var(--text-faint);
}
.confirm-actions { display: flex; justify-content: flex-end; gap: 8px; }
.confirm-actions button {
  padding: 8px 16px; border-radius: 9px; border: 1px solid var(--border); cursor: pointer;
  font-family: inherit; font-size: 13px; font-weight: 600; background: var(--surface); color: var(--text);
  transition: all 0.12s ease;
}
.confirm-actions .btn-cancel:hover { background: var(--surface-hover); }
.confirm-actions .btn-danger { background: var(--red); border-color: var(--red); color: #fff; }
.confirm-actions .btn-danger:hover { filter: brightness(1.1); }
#drop-overlay {
  display: none; position: fixed; inset: 0; z-index: 90; background: rgba(124,92,250,0.10);
  backdrop-filter: blur(2px); align-items: center; justify-content: center;
}
#drop-overlay.show { display: flex; }
#drop-overlay .drop-card {
  border: 2px dashed var(--accent-1); border-radius: var(--radius-lg); padding: 40px 56px;
  background: var(--surface-solid); color: var(--text-bright); font-weight: 700; font-size: 15px;
  display: flex; flex-direction: column; align-items: center; gap: 10px; pointer-events: none;
}
#input-hint { text-align: center; font-size: 10.8px; color: var(--text-faint); margin-top: 8px; }
#input-hint kbd {
  background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 1px 5px;
  font-family: inherit; font-size: 10px; color: var(--text-dim);
}

/* ── 功能栏（输入框下方的能力选择）── */
/* 芯片区可横向滚动，但「更多」必须固定在最右侧：否则窄屏下菜单会被裁到屏外，
   而 overflow 又会把弹层一起剪掉（overflow-x:auto + overflow-y:hidden）。 */
#cap-bar { display: flex; align-items: center; gap: 4px; margin-top: 8px; padding: 0 2px; }
#cap-bar-scroll {
  display: flex; align-items: center; gap: 4px; flex: 1 1 auto; min-width: 0;
  overflow-x: auto; scrollbar-width: none; -ms-overflow-style: none;
}
#cap-bar-scroll::-webkit-scrollbar { display: none; }
.cap-chip {
  display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0;
  background: transparent; color: var(--text-dim); border: 1px solid transparent;
  border-radius: 100px; padding: 6px 11px; font-size: 12.5px; font-family: inherit;
  cursor: pointer; white-space: nowrap; transition: background .15s ease, color .15s ease, border-color .15s ease;
}
.cap-chip:hover { background: var(--surface); color: var(--text); }
.cap-chip.active {
  background: rgba(124,92,250,0.16); border-color: rgba(124,92,250,0.45);
  color: var(--accent-2); font-weight: 600;
}
.cap-chip:disabled { opacity: .38; cursor: not-allowed; }
.cap-chip:disabled:hover { background: transparent; color: var(--text-dim); }
.cap-chip.active:disabled { opacity: .55; }
.cap-chip svg { flex-shrink: 0; }
.cap-more { position: relative; flex: 0 0 auto; }
.cap-model-tag {
  flex: 0 0 auto; font-size: 11px; color: var(--text-faint);
  border: 1px dashed var(--border-strong); border-radius: 100px; padding: 3px 8px;
  white-space: nowrap; margin-right: 4px;
}
.cap-menu {
  display: none; position: absolute; bottom: calc(100% + 10px); right: 0; min-width: 240px;
  background: var(--surface-solid); border: 1px solid var(--border-strong);
  border-radius: var(--radius-md); box-shadow: 0 -8px 28px var(--shadow-lg);
  padding: 6px; z-index: 40;
}
.cap-menu.show { display: block; }
.cap-menu-item {
  display: flex; align-items: flex-start; gap: 8px; width: 100%; text-align: left;
  background: transparent; border: none; color: var(--text); font-family: inherit;
  font-size: 12.8px; padding: 8px 10px; border-radius: 8px; cursor: pointer;
}
.cap-menu-item svg { margin-top: 1px; flex-shrink: 0; }
.cap-menu-item:hover:not(:disabled) { background: var(--surface-hover); }
.cap-menu-item:disabled { opacity: .5; cursor: not-allowed; }
.cap-menu-item.active { color: var(--accent-2); }
.cap-menu-label { display: block; }
.cap-menu-reason { display: block; font-size: 11px; color: var(--text-faint); margin-top: 2px; white-space: normal; line-height: 1.4; }

/* ── 生成任务卡片 ── */
/* 进度原则：只展示真实阶段（提交/排队/生成/下载）+ 真实已用时间，
   绝不用假百分比；顶部是一条"不确定进度"的流动条，只表示"还在动"。 */
.cap-task {
  position: relative; overflow: hidden;
  border: 1px solid var(--border-strong); background: var(--surface);
  border-radius: var(--radius-md); padding: 13px 14px 12px; margin: 10px 0 18px 36px; max-width: 760px;
  transition: border-color .2s ease;
}
.cap-progress-bar {
  position: absolute; left: 0; right: 0; top: 0; height: 2px;
  background: rgba(124,92,250,0.10); overflow: hidden;
}
.cap-progress-bar > i {
  position: absolute; top: 0; bottom: 0; width: 38%; border-radius: 2px;
  background: var(--accent-grad); animation: cap-indeterminate 1.5s ease-in-out infinite;
}
@keyframes cap-indeterminate {
  0% { left: -40%; } 60% { left: 100%; } 100% { left: 100%; }
}
.cap-task.is-done .cap-progress-bar > i { animation: none; left: 0; width: 100%; background: var(--accent-1); }
.cap-task.is-failed .cap-progress-bar > i { animation: none; left: 0; width: 100%; background: var(--red); }
/* 已取消：进度条必须停下来（否则看起来像还在生成） */
.cap-task.is-cancelled .cap-progress-bar > i {
  animation: none; left: 0; width: 100%; background: var(--border-strong);
}
.cap-task-head { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.cap-task-ico {
  width: 22px; height: 22px; border-radius: 7px; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: center;
  background: rgba(124,92,250,0.14); color: var(--accent-2);
}
.cap-task-title { font-weight: 600; color: var(--text-bright); }
.cap-task-status { color: var(--text-dim); font-size: 12px; }
.cap-spinner {
  width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0;
  border: 2px solid rgba(124,92,250,0.25); border-top-color: var(--accent-2);
  animation: cap-spin .8s linear infinite;
}
@keyframes cap-spin { to { transform: rotate(360deg); } }
.cap-task-timer {
  margin-left: auto; font-size: 11.5px; color: var(--text-faint);
  font-variant-numeric: tabular-nums; letter-spacing: .02em;
}
.cap-task.is-failed { border-color: rgba(225,71,107,0.45); }
.cap-task.is-failed .cap-task-title { color: var(--red); }
.cap-task.is-failed .cap-task-ico { background: rgba(225,71,107,0.12); color: var(--red); }

/* 阶段步骤条 */
.cap-steps { display: flex; align-items: center; flex-wrap: wrap; row-gap: 6px; margin-top: 11px; }
.cap-step {
  display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px;
  color: var(--text-faint); white-space: nowrap; transition: color .2s ease;
}
.cap-step .dot {
  width: 14px; height: 14px; border-radius: 50%; flex: 0 0 auto;
  border: 1.5px solid currentColor; display: flex; align-items: center; justify-content: center;
  font-size: 9px; line-height: 1; transition: all .2s ease;
}
.cap-step.active { color: var(--accent-2); font-weight: 600; }
.cap-step.active .dot { border-color: var(--accent-2); animation: cap-pulse 1.4s ease-in-out infinite; }
.cap-step.done { color: var(--text-dim); }
.cap-step.done .dot { background: var(--accent-1); border-color: var(--accent-1); color: #fff; font-weight: 700; }
.cap-step.done .label { color: var(--text-dim); }
.cap-step.fail { color: var(--red); font-weight: 600; }
.cap-step.fail .dot { border-color: var(--red); }
.cap-step-line { flex: 1 1 14px; min-width: 10px; height: 1.5px; background: var(--border-strong); margin: 0 8px; transition: background .2s ease; }
.cap-step-line.done { background: var(--accent-1); }
@keyframes cap-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(124,92,250,0.28); }
  50% { box-shadow: 0 0 0 4px rgba(124,92,250,0.10); }
}

/* 生成中的占位骨架（真实结果一到就替换） */
.cap-skeleton-wrap { position: relative; }
.cap-skeleton {
  position: relative; overflow: hidden; border-radius: var(--radius-sm);
  border: 1px dashed var(--border-strong);
  background: linear-gradient(100deg, rgba(124,92,250,0.05) 30%, rgba(124,92,250,0.13) 50%, rgba(124,92,250,0.05) 70%);
  background-size: 220% 100%; animation: cap-shimmer 1.5s linear infinite;
}
@keyframes cap-shimmer { from { background-position: 160% 0; } to { background-position: -60% 0; } }
.cap-skeleton.k-image { width: 100%; max-width: 300px; aspect-ratio: 4 / 3; }
.cap-skeleton.k-video { width: 100%; max-width: 480px; aspect-ratio: 16 / 9; }
.cap-skeleton.k-audio { width: 100%; max-width: 380px; height: 62px; padding-bottom: 16px; animation: none; }
.cap-skeleton-label {
  position: absolute; left: 0; right: 0; bottom: 8px; text-align: center;
  font-size: 11px; color: var(--text-faint); pointer-events: none;
}
.cap-wave { display: flex; align-items: center; justify-content: center; gap: 4px; height: 100%; padding: 0 14px; }
.cap-wave > i {
  flex: 1 1 auto; max-width: 8px; min-height: 4px; border-radius: 3px;
  background: var(--accent-2); opacity: .45; animation: cap-wave 1.05s ease-in-out infinite;
}
@keyframes cap-wave { 0%, 100% { height: 18%; } 50% { height: 88%; } }

.cap-task-prompt {
  color: var(--text-dim); font-size: 12.5px; margin-top: 8px;
  white-space: pre-wrap; word-break: break-word;
}
.cap-task-body { margin-top: 10px; }
.cap-task-body:empty { display: none; }
.cap-media-grid { display: flex; flex-wrap: wrap; gap: 10px; }
.cap-media-grid img {
  display: block; max-width: 260px; max-height: 260px; border-radius: var(--radius-sm);
  border: 1px solid var(--border-strong); cursor: zoom-in;
}
.cap-task video { display: block; width: 100%; max-width: 560px; border-radius: var(--radius-sm); background: #000; }
.cap-task audio { display: block; width: 100%; max-width: 480px; }
.cap-media-meta { display: flex; align-items: center; gap: 10px; margin-top: 8px; font-size: 12px; color: var(--text-faint); }
.cap-task-dl { color: var(--accent-2); text-decoration: none; }
.cap-task-dl:hover { text-decoration: underline; }
.cap-task-error { margin-top: 8px; color: var(--red); font-size: 12.5px; line-height: 1.5; }
.cap-task-tip { margin-top: 6px; color: var(--text-dim); font-size: 12px; line-height: 1.5; }
.cap-task-actions { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; }
.cap-task-actions:empty { display: none; }
.cap-task-btn {
  background: var(--surface-solid); border: 1px solid var(--border-strong); color: var(--text-dim);
  border-radius: 8px; padding: 4px 10px; font-size: 12px; font-family: inherit; cursor: pointer;
}
.cap-task-btn:hover { color: var(--text); border-color: var(--accent-1); }
.cap-task-btn:disabled { opacity: .5; cursor: not-allowed; }
.cap-task-btn.danger { color: var(--red); border-color: rgba(225,71,107,0.35); }
.cap-task-btn.danger:hover { color: var(--red); border-color: var(--red); }
.cap-task.is-cancelled { border-color: var(--border-strong); }
.cap-task.is-cancelled .cap-task-title { color: var(--text-dim); }
.cap-task.is-cancelled .cap-task-ico { background: var(--surface); color: var(--text-faint); }
.cap-task-note { margin-top: 8px; color: var(--text-faint); font-size: 12px; line-height: 1.5; }
.cap-dev {
  display: none; margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border);
  font-size: 11.5px; color: var(--text-faint); white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
html[data-mode='developer'] .cap-dev { display: block; }

/* 系统设置了"减少动效"就别晃 */
@media (prefers-reduced-motion: reduce) {
  .cap-progress-bar > i, .cap-skeleton, .cap-wave > i, .cap-spinner,
  .cap-step.active .dot { animation: none !important; }
  .cap-progress-bar > i { left: 0; width: 100%; opacity: .5; }
}

@media (max-width: 860px) {
  #sidebar {
    position: fixed; top: 0; left: 0; width: 272px; height: 100%; z-index: 60;
    transform: translateX(0); transition: transform 0.25s ease; box-shadow: 0 0 40px var(--shadow-lg);
  }
  #app.sidebar-collapsed #sidebar { width: 272px; transform: translateX(-100%); border-right: 1px solid var(--border); }
  #sidebar-backdrop {
    display: none; position: fixed; inset: 0; background: rgba(15,16,34,0.35); z-index: 55;
  }
  #app:not(.sidebar-collapsed) #sidebar-backdrop { display: block; }
}

@media (max-width: 640px) {
  #status-bar, #input-outer { padding-left: 12px; padding-right: 12px; }
  #messages-col { padding-left: 12px; padding-right: 12px; }
  .msg-row.user .col { max-width: 88%; }
  #input-hint { display: none; }
  .brand-sub { display: none; }
  /* 窄屏：执行详情与权限卡片贴边，避免左侧留白吃掉宽度 */
  .thinking-block.exec-panel { margin-left: 0; margin-right: 0; }
  .dialog-card { margin-left: 0; margin-right: 0; }
  .thinking-header { font-size: 12px; padding: 7px 10px; }
  details.exec-row > summary { flex-wrap: wrap; gap: 4px 6px; }
  details.exec-row .exec-badge { margin-left: 0; }
  .exec-row-body { font-size: 11px; max-height: 180px; }
  .mode-btn { padding: 7px 6px; font-size: 11.5px; }
  #cap-bar { margin-top: 6px; }
  .cap-chip { padding: 6px 9px; font-size: 12px; }
  .cap-menu { min-width: 210px; }
  .cap-task { margin-left: 0; margin-right: 0; padding: 12px 11px 11px; }
  /* 窄屏阶段条会挤：只留圆点，阶段名靠 title 提示 */
  .cap-step .label { display: none; }
  .cap-step-line { min-width: 6px; margin: 0 5px; }
  .cap-skeleton.k-image { max-width: 100%; }
  .cap-skeleton.k-video { max-width: 100%; }
}
</style>
</head>
<body>
<div id="app">
  <aside id="sidebar">
    <div class="sidebar-header">
      <div class="brand-mark-sm">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path d="M12 4c-.86 0-1.62.35-2.18.95L6.8 2.3l.55 4.3C5.1 7.85 3.6 10.35 3.6 13.2 3.6 18 7.35 21.7 12 21.7s8.4-3.7 8.4-8.5c0-2.85-1.5-5.35-3.75-6.6l.55-4.3-3.02 2.65C13.62 4.35 12.86 4 12 4z" fill="#fff"/>
          <circle cx="9.1" cy="13.6" r="1.15" fill="#161320"/>
          <circle cx="14.9" cy="13.6" r="1.15" fill="#161320"/>
        </svg>
      </div>
      <span class="sidebar-brand-title">AI 外脑 Demo</span>
    </div>
    <button id="new-chat-btn">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>
      新对话
    </button>
    <div id="session-search-wrap"><input type="text" id="session-search" placeholder="搜索会话..."></div>
    <div id="session-list"><div class="session-empty-hint">暂无会话</div></div>
    <div class="sidebar-footer">
      <div class="display-mode">
        <span class="display-mode-label">显示模式</span>
        <div class="mode-seg" role="group" aria-label="显示模式">
          <button type="button" class="mode-btn" data-mode="normal" aria-pressed="true">普通</button>
          <button type="button" class="mode-btn" data-mode="developer" aria-pressed="false">开发者</button>
        </div>
      </div>
      <button id="memory-btn" type="button" title="查看和管理 AI 外脑记住的内容">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none"><path d="M12 3a4 4 0 00-4 4v1a3 3 0 00-1 5.83V16a3 3 0 003 3h1v2h2v-2h1a3 3 0 003-3v-2.17A3 3 0 0017 8V7a4 4 0 00-4-4z" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>
        记忆
      </button>
    </div>
  </aside>
  <div id="sidebar-backdrop"></div>
  <div id="image-lightbox">
    <button id="lightbox-close" title="Close">✕</button>
    <img id="lightbox-img" src="" alt="">
  </div>
  <div id="drop-overlay"><div class="drop-card">📎 Drop files here to attach</div></div>
  <div id="confirm-modal">
    <div class="confirm-card">
      <div class="confirm-title" id="confirm-title"></div>
      <div class="confirm-desc" id="confirm-desc"></div>
      <label class="confirm-option" id="confirm-option-wrap">
        <input type="checkbox" id="confirm-option">
        <span id="confirm-option-label"></span>
      </label>
      <div class="confirm-actions">
        <button class="btn-cancel" id="confirm-cancel-btn" type="button">Cancel</button>
        <button class="btn-danger" id="confirm-ok-btn" type="button">Delete</button>
      </div>
    </div>
  </div>

  <div id="main">
    <div id="status-bar">
      <div class="status-left">
        <button id="sidebar-toggle-btn" title="Toggle sidebar">
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="16" rx="3" stroke="currentColor" stroke-width="1.8"/><line x1="9" y1="4" x2="9" y2="20" stroke="currentColor" stroke-width="1.8"/></svg>
        </button>
        <span class="brand-sub" id="cwd-info" aria-hidden="true"></span>
      </div>
      <div class="status-right">
        <select id="tts-rate" title="朗读语速">
          <option value="0.75">0.75x</option>
          <option value="1">1.0x</option>
          <option value="1.25">1.25x</option>
          <option value="1.5">1.5x</option>
          <option value="2">2.0x</option>
        </select>
        <select id="provider-select" title="Model"></select>
        <span class="pill" id="token-info" style="display:none;"></span>
        <span class="pill" id="conn-status"><span class="dot disconnected"></span>连接中…</span>
      </div>
    </div>

    <div id="messages-wrap">
      <div id="messages"><div id="messages-col"></div></div>
      <button class="scroll-fab" id="scroll-fab">↓ New messages</button>
    </div>

    <div id="input-outer">
      <div id="input-inner">
        <div id="image-preview-row"></div>
        <div id="input-area">
          <div id="slash-menu"></div>
          <input type="file" id="file-input" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf,.txt,.md,.py,.js,.ts,.jsx,.tsx,.json,.yaml,.yml,.csv,.html,.css,.java,.c,.cpp,.h,.go,.rs,.rb,.php,.sh,.sql,.xml,.log,.toml,.ini" multiple style="display:none;">
          <button id="upload-btn" title="Upload file">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </button>
          <button id="mic-btn" title="语音输入（点击开始/停止）">
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M12 15a3.5 3.5 0 003.5-3.5V5.5a3.5 3.5 0 10-7 0v6A3.5 3.5 0 0012 15z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M5.5 11.5a6.5 6.5 0 0013 0M12 18v3.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>
          </button>
          <textarea id="input" placeholder="输入消息给 AI 外脑…" rows="1"></textarea>
          <button id="send-btn" title="Send">
            <svg id="send-icon" width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M4 12l16-8-6 8 6 8-16-8z" fill="currentColor"/></svg>
          </button>
        </div>
        <!-- 功能栏：具体有哪些能力完全由服务端下发，前端不预设"能用" -->
        <div id="cap-bar" role="toolbar" aria-label="功能选择"><div id="cap-bar-scroll"></div></div>
        <div id="input-hint">Enter to send &nbsp;·&nbsp; <kbd>Shift</kbd>+<kbd>Enter</kbd> newline &nbsp;·&nbsp; <kbd>/</kbd> commands</div>
      </div>
    </div>
  </div>
</div>

<div id="memory-modal">
  <div class="memory-card" role="dialog" aria-modal="true" aria-label="记忆管理">
    <div class="memory-head">
      <span>记忆管理</span>
      <button id="memory-close" type="button" title="关闭">✕</button>
    </div>
    <div class="memory-intro">
      AI 外脑可以记住你分享的偏好，让以后的回答更符合你的需要。你可以随时查看或删除这些记忆。
    </div>
    <label class="memory-switch">
      <input type="checkbox" id="memory-enabled">
      <span>允许 AI 外脑记住我的偏好</span>
    </label>
    <div class="memory-hint" id="memory-hint"></div>
    <div class="memory-list" id="memory-list"></div>
    <div class="memory-empty" id="memory-empty" style="display:none;">还没有保存任何记忆。</div>
    <div class="memory-actions">
      <button id="memory-clear-project" type="button">清空项目记忆</button>
      <button id="memory-clear-all" type="button">清空全部记忆</button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<!-- XSS 防线：marked 不是 sanitizer，模型输出必须先过 DOMPurify 再进 innerHTML -->
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js"></script>
<script>
const appEl = document.getElementById('app');
const scrollWrap = document.getElementById('messages');
const messagesEl = document.getElementById('messages-col');
// 链接守卫（最后一道保险）：即使某条渲染路径把 target 属性弄丢了，点击前也补上，
// 然后**不阻止默认行为**——交给浏览器按 target="_blank" 打开新标签页。
// 不用 window.open：那会走弹窗拦截，被拦掉时用户会以为"点了没反应"。
messagesEl.addEventListener('click', (e) => {
  const target = e.target;
  const anchor = target && target.closest ? target.closest('a') : null;
  if (!anchor) return;
  const href = (anchor.getAttribute('href') || '').trim();
  if (!HTTP_URL_RE.test(href)) return;    // 站内锚点/相对路径/邮件链接交给浏览器
  if (anchor.getAttribute('target') !== '_blank') anchor.setAttribute('target', '_blank');
  if (!anchor.getAttribute('rel')) anchor.setAttribute('rel', SAFE_LINK_REL);
  if (!anchor.getAttribute('referrerpolicy')) anchor.setAttribute('referrerpolicy', 'no-referrer');
});
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const sendIcon = document.getElementById('send-icon');
const connStatus = document.getElementById('conn-status');
const tokenInfo = document.getElementById('token-info');
const cwdInfo = document.getElementById('cwd-info');
const slashMenu = document.getElementById('slash-menu');
const uploadBtn = document.getElementById('upload-btn');
const fileInput = document.getElementById('file-input');
const imagePreviewRow = document.getElementById('image-preview-row');
const imageLightbox = document.getElementById('image-lightbox');
const lightboxImg = document.getElementById('lightbox-img');
const lightboxClose = document.getElementById('lightbox-close');
const dropOverlay = document.getElementById('drop-overlay');
const providerSelect = document.getElementById('provider-select');
const confirmModal = document.getElementById('confirm-modal');
const confirmTitleEl = document.getElementById('confirm-title');
const confirmDescEl = document.getElementById('confirm-desc');
const confirmCancelBtn = document.getElementById('confirm-cancel-btn');
const confirmOkBtn = document.getElementById('confirm-ok-btn');
const confirmOptionWrap = document.getElementById('confirm-option-wrap');
const confirmOptionEl = document.getElementById('confirm-option');
const confirmOptionLabelEl = document.getElementById('confirm-option-label');
const scrollFab = document.getElementById('scroll-fab');
const sidebarToggleBtn = document.getElementById('sidebar-toggle-btn');
const sidebarBackdrop = document.getElementById('sidebar-backdrop');
// ── 回答播报：每条回答一个按钮（浏览器原生 speechSynthesis，无外部依赖 / 无 API key）──
let ttsVoice = null;
let ttsPending = 0;
let activeSpeakBtn = null;
let ttsRate = 1;
try { ttsRate = parseFloat(localStorage.getItem('aieb_tts_rate') || '1') || 1; } catch (e) { ttsRate = 1; }

const ttsRateSelect = document.getElementById('tts-rate');
if (ttsRateSelect) {
  ttsRateSelect.value = String(ttsRate);
  if (ttsRateSelect.value !== String(ttsRate)) ttsRateSelect.value = '1';
  ttsRateSelect.addEventListener('change', () => {
    ttsRate = parseFloat(ttsRateSelect.value) || 1;
    try { localStorage.setItem('aieb_tts_rate', String(ttsRate)); } catch (e) { /* 隐私模式忽略 */ }
  });
}

function ttsSupported() { return typeof window !== 'undefined' && 'speechSynthesis' in window; }

function pickTtsVoice() {
  if (!ttsSupported()) return;
  const voices = window.speechSynthesis.getVoices() || [];
  ttsVoice =
    voices.find(v => /^zh[-_]/i.test(v.lang) && /xiaoxiao|yunxi|xiaoyi|huihui|yaoyao|tingting|kangkang|natural/i.test(v.name)) ||
    voices.find(v => /^zh[-_]CN/i.test(v.lang)) ||
    voices.find(v => /^zh/i.test(v.lang)) ||
    null;
}

function resetSpeakBtn() {
  if (activeSpeakBtn) {
    activeSpeakBtn.classList.remove('speaking');
    activeSpeakBtn.innerHTML = '🔊 朗读';
    activeSpeakBtn.title = '朗读这条回答';
  }
  activeSpeakBtn = null;
}

function stopSpeaking() {
  if (ttsSupported()) {
    ttsPending = 0;
    window.speechSynthesis.cancel();
  }
  resetSpeakBtn();
}

// Markdown -> 可朗读的纯文本：代码块保留内容（用占位符保护，避免被去符号逻辑吃掉）
function ttsClean(markdown) {
  let t = (markdown || '').replace(/\r/g, '');

  // 先把围栏代码块抽出来，保证代码里的 * _ ~ | # 等符号不被后续清洗破坏
  const codeBlocks = [];
  t = t.replace(/```[a-zA-Z0-9+#._-]*[ \t]*\n?([\s\S]*?)```/g, (m, code) => {
    codeBlocks.push(code.replace(/\s+$/, ''));
    return '\n@@CODE' + (codeBlocks.length - 1) + '@@\n';
  });

  t = t.replace(/`([^`]*)`/g, '$1');
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, '');
  t = t.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');
  t = t.replace(/^\s{0,3}#{1,6}\s*/gm, '');
  t = t.replace(/^\s{0,3}>\s?/gm, '');
  t = t.replace(/\*\*([^*]+)\*\*/g, '$1');
  t = t.replace(/[*_~]/g, '');
  t = t.replace(/^\s*[-*+]\s+/gm, '');
  t = t.replace(/\|/g, ' ');
  t = t.replace(/https?:\/\/\S+/g, '链接');
  t = t.replace(/[ \t]+/g, ' ');
  t = t.replace(/\n{2,}/g, '\n').trim();

  // 把代码块放回去：前面加"代码"提示，便于听出上下文切换
  t = t.replace(/@@CODE(\d+)@@/g, (m, i) => {
    const code = codeBlocks[Number(i)];
    return code ? '\n代码：\n' + code + '\n' : '';
  });
  return t.replace(/\n{2,}/g, '\n').trim();
}

// 按句子切块，避免长文本被引擎提前截断
function splitForTts(text, size) {
  const parts = [];
  let buf = '';
  const sentences = text.match(/[^。！？；.!?\n]+[。！？；.!?\n]?/g) || [text];
  sentences.forEach(s => {
    if (buf && (buf + s).length > size) { parts.push(buf); buf = s; }
    else buf += s;
  });
  if (buf.trim()) parts.push(buf);
  return parts;
}

function speakText(markdown, btn) {
  if (!ttsSupported()) { addSystem('当前浏览器不支持语音朗读。'); return; }
  const clean = ttsClean(markdown);
  if (!clean) return;
  // 朗读与录音互斥：否则麦克风会把扬声器声音当成输入（回声/自激）
  stopMic();
  stopSpeaking();
  const chunks = splitForTts(clean, 160);
  if (!chunks.length) return;
  activeSpeakBtn = btn || null;
  if (btn) {
    btn.classList.add('speaking');
    btn.innerHTML = '⏹ 停止';
    btn.title = '停止朗读';
  }
  ttsPending = chunks.length;
  const done = () => {
    ttsPending = Math.max(0, ttsPending - 1);
    if (ttsPending === 0) resetSpeakBtn();
  };
  chunks.forEach(chunk => {
    const u = new SpeechSynthesisUtterance(chunk);
    u.lang = 'zh-CN';
    if (ttsVoice) u.voice = ttsVoice;
    u.rate = ttsRate;
    u.pitch = 1.0;
    u.onend = done;
    u.onerror = done;
    window.speechSynthesis.speak(u);
  });
}

// 在某条助手回答下面挂一个「朗读」按钮
function attachSpeakButton(row, text) {
  if (!ttsSupported() || !row || !text) return;
  const col = row.querySelector('.col') || row;
  if (col.querySelector('.msg-speak-btn')) return;
  const btn = document.createElement('button');
  btn.className = 'msg-speak-btn';
  btn.type = 'button';
  btn.title = '朗读这条回答';
  btn.innerHTML = '🔊 朗读';
  btn.addEventListener('click', () => {
    if (btn.classList.contains('speaking')) { stopSpeaking(); return; }
    speakText(text, btn);
  });
  col.appendChild(btn);
}

if (ttsSupported()) {
  pickTtsVoice();
  window.speechSynthesis.onvoiceschanged = pickTtsVoice;
}
const newChatBtn = document.getElementById('new-chat-btn');
const sessionSearchEl = document.getElementById('session-search');
const sessionListEl = document.getElementById('session-list');

let currentSessionId = '';
let sessions = [];

let ws = null;
let streaming = false;
let allCommands = [];
let slashCursor = 0;
let slashFiltered = [];
let currentAssistantEl = null;
let currentStreamText = '';
// 本轮唯一的执行状态容器：思考文字 + 工具调用都归到它下面，
// 由 displayMode 决定渲染成「一行进度」还是「可展开的执行详情」。
let currentExecEl = null;
let currentExecTools = [];
let currentExecThought = '';

function resetExecState() {
  currentExecEl = null;
  currentExecTools = [];
  currentExecThought = '';
}

// ── 显示模式：普通（默认）/ 开发者 ──
// 纯 UI 展示偏好：不改变 Provider、工具注册、权限等级或 System Prompt。
const DISPLAY_MODE_KEY = 'aieb_display_mode';
const DISPLAY_MODES = ['normal', 'developer'];
let displayMode = 'normal';

function loadDisplayMode() {
  try {
    const saved = localStorage.getItem(DISPLAY_MODE_KEY);
    if (saved && DISPLAY_MODES.includes(saved)) return saved;
  } catch (e) { /* localStorage 不可用时回退默认值 */ }
  return 'normal';
}

function readExecData(el) {
  if (!el || !el.__wdExec) return null;
  return el.__wdExec;
}

// 给执行容器挂上数据（渲染时只读这份数据，切换模式靠重建 DOM 而不是 CSS 隐藏）
function bindExecData(el, data) {
  el.__wdExec = data;
  return el;
}
let autoScroll = true;
let pingTimer = null;
let connectedOnce = false;
let pendingImages = [];
let pendingDocuments = [];   // PDF：{media_type, data, name}
let pendingTextFiles = [];   // 纯文本/代码文件：{name, content}
let visionEnabled = false;
let lastUserRow = null;        // 最近一条用户消息行（服务端回传 message_id 时挂上去）

const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const MAX_DOCUMENT_BYTES = 20 * 1024 * 1024;
const MAX_TEXT_FILE_BYTES = 300 * 1024;
const MAX_ATTACHMENTS_PER_MESSAGE = 5;
const ACCEPTED_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
const TEXT_FILE_EXTENSIONS = [
  'txt', 'md', 'py', 'js', 'ts', 'jsx', 'tsx', 'json', 'yaml', 'yml', 'csv',
  'html', 'css', 'java', 'c', 'cpp', 'h', 'go', 'rs', 'rb', 'php', 'sh',
  'sql', 'xml', 'log', 'toml', 'ini',
];

function attachmentCount() {
  return pendingImages.length + pendingDocuments.length + pendingTextFiles.length;
}

function classifyFile(file) {
  if (ACCEPTED_IMAGE_TYPES.includes(file.type)) return 'image';
  if (file.type === 'application/pdf') return 'pdf';
  const ext = (file.name.split('.').pop() || '').toLowerCase();
  if (file.type.startsWith('text/') || TEXT_FILE_EXTENSIONS.includes(ext)) return 'text';
  return 'unsupported';
}

if (typeof marked !== 'undefined') {
  marked.setOptions({ breaks: true, gfm: true });
}

const ICON_STOP = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none"><rect x="5" y="5" width="14" height="14" rx="3" fill="currentColor"/></svg>';
const ICON_SEND = '<svg width="17" height="17" viewBox="0 0 24 24" fill="none"><path d="M4 12l16-8-6 8 6 8-16-8z" fill="currentColor"/></svg>';

const EMPTY_STATE_HTML =
  '<div id="empty-state">' +
    '<div class="hero-mark"><svg width="30" height="30" viewBox="0 0 24 24" fill="none">' +
      '<path d="M12 4c-.86 0-1.62.35-2.18.95L6.8 2.3l.55 4.3C5.1 7.85 3.6 10.35 3.6 13.2 3.6 18 7.35 21.7 12 21.7s8.4-3.7 8.4-8.5c0-2.85-1.5-5.35-3.75-6.6l.55-4.3-3.02 2.65C13.62 4.35 12.86 4 12 4z" fill="#fff"/>' +
      '<circle cx="9.1" cy="13.6" r="1.3" fill="#0a0a12"/><circle cx="14.9" cy="13.6" r="1.3" fill="#0a0a12"/>' +
    '</svg></div>' +
    '<h1>AI 外脑 Demo</h1>' +
    '<p>你的智能助手，已在浏览器中就绪。你可以提问、总结内容、写作润色、规划任务，或让它协助你完成各种工作。</p>' +
    '<div class="hero-chips">' +
      '<span class="hero-chip" data-fill="总结这份资料的重点">总结资料</span>' +
      '<span class="hero-chip" data-fill="帮我润色这段文案">润色文案</span>' +
      '<span class="hero-chip" data-fill="帮我制定一个学习计划">学习计划</span>' +
      '<span class="hero-chip" data-fill="/help">/help</span>' +
    '</div>' +
  '</div>';

function resetEmptyState() {
  messagesEl.innerHTML = EMPTY_STATE_HTML;
  messagesEl.querySelectorAll('.hero-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      inputEl.value = chip.dataset.fill;
      inputEl.focus();
      inputEl.dispatchEvent(new Event('input'));
    });
  });
}

function hideEmptyState() {
  const es = document.getElementById('empty-state');
  if (es) es.remove();
}

function renderMarkdown(text) {
  if (typeof marked === 'undefined') return escapeHtml(text);

  let html = '';
  try {
    // 第一步：解析 markdown；同时把原始 HTML token 转义，去掉“未消毒 HTML”路径。
    const renderer = new marked.Renderer();
    renderer.html = (token) => escapeHtml(typeof token === 'string' ? token : (token && token.text) || '');
    html = marked.parse(text, { renderer: renderer });
  } catch (e) {
    return escapeHtml(text);
  }

  // 第二步：消毒。DOMPurify 不存在时（CDN 不可达/离线）直接典到纯文本，
  // 绝不把未消毒 HTML 写进 DOM。
  if (typeof DOMPurify === 'undefined') return escapeHtml(text);
  if (typeof ensureSanitizerHooks === 'function') ensureSanitizerHooks();
  try {
    return DOMPurify.sanitize(html, SANITIZE_OPTIONS);
  } catch (e) {
    return escapeHtml(text);
  }
}

// ── 外部链接策略 ──────────────────────────────────────────────
// 目标：点新闻来源/官网链接时在新标签页打开，当前会话（消息、输入框、流式
// 状态）保持不动；同时不能因为加 target 而削弱 XSS 防护。
//
// 三层覆盖，因为回答来自三条不同的路径：
//   1) DOMPurify hook —— 实时流式回答 + 历史回放都经过 sanitize，统一在这里
//      给 <a> 补 target/rel，并摘掉危险协议；
//   2) hardenLinks() —— innerHTML 写进 DOM 之后再走一遍，兜住不走 sanitize
//      的渲染路径，也覆盖旧会话里已经存在的内容；
//   3) 事件委托 —— 万一 target 属性被别的逻辑清掉，点击时仍然强制新标签页。
const HTTP_URL_RE = /^\s*https?:\/\//i;
const ANY_SCHEME_RE = /^\s*([a-z][a-z0-9+.\-]*):/i;
const SAFE_LINK_REL = 'noopener noreferrer nofollow';

let sanitizerHooksInstalled = false;
function ensureSanitizerHooks() {
  if (sanitizerHooksInstalled || typeof DOMPurify === 'undefined') return;
  sanitizerHooksInstalled = true;
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node && node.tagName === 'A') applyExternalLinkPolicy(node);
  });
}

// 把一条 <a> 调整成「安全 + 新标签页」；危险协议直接摘掉 href。
function applyExternalLinkPolicy(a) {
  if (!a || !a.getAttribute) return;
  const raw = (a.getAttribute('href') || '').trim();
  if (!raw) { a.removeAttribute('target'); a.removeAttribute('rel'); return; }

  // 页内锚点属于「内部导航」，保持默认行为
  if (raw.startsWith('#')) {
    a.removeAttribute('target');
    a.removeAttribute('rel');
    return;
  }

  if (HTTP_URL_RE.test(raw)) {
    a.setAttribute('target', '_blank');
    // noopener：新页面拿不到 window.opener，不能反向操纵 AI 外脑页面；
    // noreferrer：连 Referer 都不带，避免把当前地址/参数泄露给第三方。
    a.setAttribute('rel', SAFE_LINK_REL);
    a.setAttribute('referrerpolicy', 'no-referrer');
    a.classList && a.classList.remove('link-blocked');
    return;
  }

  const scheme = ANY_SCHEME_RE.exec(raw);
  if (!scheme) {
    // 相对路径：当作站内导航，不加 target
    a.removeAttribute('target');
    a.removeAttribute('rel');
    return;
  }
  const proto = scheme[1].toLowerCase();
  if (proto === 'mailto' || proto === 'tel') {
    a.removeAttribute('target');
    a.removeAttribute('rel');
    return;
  }
  // javascript: / data: / vbscript: / file: / blob: 一律不可点击
  a.removeAttribute('href');
  a.removeAttribute('target');
  a.removeAttribute('rel');
  a.classList && a.classList.add('link-blocked');
}

// innerHTML 之后再过一遍：覆盖历史恢复、以及任何绕过 sanitize hook 的路径。
function hardenLinks(container) {
  if (!container || !container.querySelectorAll) return;
  const anchors = container.querySelectorAll('a');
  for (let i = 0; i < anchors.length; i++) applyExternalLinkPolicy(anchors[i]);
}

// 只保留 Markdown 实际会用到的标签；危险标签/属性与 javascript: 类 URL 一并禁止。
// target/rel/referrerpolicy 允许存在，但它们的取值由 applyExternalLinkPolicy()
// 统一改写（模型自己写的 target 不会绕过策略）。
const SANITIZE_OPTIONS = {
  ALLOWED_TAGS: [
    'p','br','hr','strong','b','em','i','del','s','code','pre','blockquote',
    'ul','ol','li','h1','h2','h3','h4','h5','h6','a','img','table','thead',
    'tbody','tr','th','td','span','div','sup','sub'
  ],
  ALLOWED_ATTR: ['href','title','alt','src','class','align','colspan','rowspan','start','target','rel','referrerpolicy'],
  ALLOW_DATA_ATTR: false,
  FORBID_TAGS: ['script','style','iframe','object','embed','form','input','button','link','meta','base','svg','math'],
  FORBID_ATTR: ['onerror','onload','onclick','onmouseover','onfocus','onbegin','style','srcdoc','formaction'],
  ALLOW_UNKNOWN_PROTOCOLS: false
};

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// 属性值转义（用于 data-id / value 这类属性拼接）
function escapeAttr(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function enhanceCodeBlocks(container) {
  container.querySelectorAll('pre > code').forEach((codeEl) => {
    const pre = codeEl.parentElement;
    if (pre.dataset.enhanced) return;
    pre.dataset.enhanced = '1';
    const m = (codeEl.className || '').match(/language-(\w+)/);
    const lang = m ? m[1] : 'text';
    const bar = document.createElement('div');
    bar.className = 'code-bar';
    const langSpan = document.createElement('span');
    langSpan.className = 'code-lang';
    langSpan.textContent = lang;
    const copyBtn = document.createElement('button');
    copyBtn.className = 'code-copy';
    copyBtn.type = 'button';
    copyBtn.textContent = 'Copy';
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(codeEl.textContent).then(() => {
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1400);
      }).catch(() => {});
    });
    bar.appendChild(langSpan);
    bar.appendChild(copyBtn);
    pre.insertBefore(bar, codeEl);
    if (typeof hljs !== 'undefined') {
      try { hljs.highlightElement(codeEl); } catch(e) {}
    }
  });
}

// Remote 认证 token：从 URL fragment 取（#token=...），fragment 不会发到服务端，
// 取到后立即从地址栏抹掉，并保存在 sessionStorage。WS 用手势般的 subprotocol 携带，
// 避免 token 进入 URL query / 浏览器历史 / 服务端访问日志。
function readRemoteToken() {
  let token = '';
  try {
    const hash = location.hash || '';
    const m = hash.match(/[#&]token=([^&]+)/);
    if (m) token = decodeURIComponent(m[1]);
    if (token) {
      sessionStorage.setItem('aieb_remote_token', token);
      history.replaceState(null, '', location.pathname + location.search);
    } else {
      token = sessionStorage.getItem('aieb_remote_token') || '';
    }
  } catch (e) {
    token = '';
  }
  return token;
}

const REMOTE_TOKEN = readRemoteToken();

function connect() {
  if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
  if (ws) { try { ws.onclose = null; ws.close(); } catch(e) {} ws = null; }
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const protocols = ['aieb.v1'];
  if (REMOTE_TOKEN) protocols.push('aieb-token.' + REMOTE_TOKEN);
  ws = new WebSocket(proto + '//' + location.host + '/ws', protocols);

  ws.onopen = () => {
    connStatus.innerHTML = '<span class="dot connected"></span>已连接';
    pingTimer = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping', data: {} }));
      }
    }, 10000);
  };

  ws.onclose = () => {
    connStatus.innerHTML = '<span class="dot disconnected"></span>重连中…';
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    setTimeout(connect, 3000);
  };

  ws.onerror = () => {};

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    handleMessage(msg);
  };
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'connected':
      currentSessionId = msg.data.session;
      cwdInfo.textContent = msg.data.cwd;
      cwdInfo.title = 'Session: ' + msg.data.session + '\nCWD: ' + msg.data.cwd;
      connectedOnce = true;
      visionEnabled = !!msg.data.visionEnabled;
      // 上传按钮始终可见：纯文本/代码文件对任何模型都可用，只有图片/PDF 依赖 visionEnabled（在 handleFiles 里单独拦截）。
      if (msg.data.activeProvider) providerSelect.value = msg.data.activeProvider;
      renderSessionList();
      break;

    case 'commands':
      allCommands = msg.data || [];
      break;

    case 'providers':
      renderProviderSelect((msg.data && msg.data.providers) || [], msg.data && msg.data.active);
      break;

    case 'session_list':
      sessions = (msg.data && msg.data.sessions) || [];
      renderSessionList();
      break;

    case 'session_switched':
      currentSessionId = msg.data.id;
      cwdInfo.textContent = msg.data.cwd;
      cwdInfo.title = 'Session: ' + msg.data.id + (msg.data.cwd ? '\nCWD: ' + msg.data.cwd : '');
      tokenInfo.style.display = 'none';
      tokenInfo.textContent = '';
      if (msg.data.visionEnabled !== undefined) visionEnabled = !!msg.data.visionEnabled;
      if (msg.data.activeProvider) providerSelect.value = msg.data.activeProvider;
      // 切换会话：上一轮的执行状态不属于新会话
      resetExecState();
      currentAssistantEl = null;
      currentStreamText = '';
      renderSessionList();
      break;

    case 'system':
      addSystem(msg.data.message);
      break;

    case 'clear':
      resetEmptyState();
      resetExecState();
      currentAssistantEl = null;
      currentStreamText = '';
      closeActiveEditor();
      lastUserRow = null;
      // 清屏 = 换会话/重放：旧卡片已经从 DOM 里没了，索引也要一起清
      capTasks = {};
      capUnbound = [];
      break;

    case 'capabilities':
      applyCapabilities(msg.data || {});
      break;

    case 'capability_started':
      handleCapabilityEvent(msg.data || {}, 'capability_started');
      break;

    case 'capability_progress':
      handleCapabilityEvent(msg.data || {}, 'capability_progress');
      break;

    case 'capability_result':
      handleCapabilityEvent(msg.data || {}, 'capability_result');
      break;

    case 'capability_ack': {
      var ack = msg.data || {};
      if (ack.cancelled) {
        // 停止请求的回执：真正的状态由随后的 capability_result(cancelled) 更新
        if (!ack.ok && ack.error_message) addSystem(ack.error_message);
        break;
      }
      if (ack.ok && ack.task_id) {
        var bound = findCapTaskEl(ack);
        if (!bound) {
          // started 没收到也不要紧：卡片就在这里建出来
          bound = createCapTaskCard({
            capability: ack.capability,
            model: ack.model,
            prompt: ack.prompt,
            status: ack.status || 'running'
          });
        }
        capTasks[ack.task_id] = bound;
      } else if (ack.deduplicated) {
        addSystem('这个生成请求已经提交过了，不会重复计费。');
      } else if (ack.error_message) {
        // 服务端拒绝（模型不支持/参数不合法）：把本地卡片直接标成失败
        var target = null;
        for (var i = 0; i < capUnbound.length; i += 1) {
          if ((capUnbound[i].__cap || {}).request_id === ack.request_id) { target = capUnbound[i]; break; }
        }
        if (!target && capUnbound.length) target = capUnbound[capUnbound.length - 1];
        if (target) {
          capUnbound = capUnbound.filter(function (item) { return item !== target; });
          stopCapTimer(target);
          updateCapTask(target, {
            status: 'failed',
            error_code: ack.error_code || 'invalid_argument',
            error_message: ack.error_message
          });
        }
        addError(ack.error_message);
      }
      break;
    }

    case 'command_done':
      streaming = false;
      updateUI();
      break;

    case 'replay_user':
      resetExecState();
      currentAssistantEl = null;
      currentStreamText = '';
      addUser(msg.data.content, msg.data.images || [], msg.data.documents || [], {
        message_id: msg.data.message_id || '',
        version: msg.data.version || null,
      });
      break;

    case 'user_message_saved':
      // 刚发出的消息在服务端落盘后的真实 ID / 版本信息：补到最新那条用户消息上，
      // 这样它可以立即被编辑、也能显示 ‹ 1/2 ›。
      if (lastUserRow && lastUserRow.__wdMsg) {
        lastUserRow.__wdMsg.id = (msg.data && msg.data.message_id) || lastUserRow.__wdMsg.id;
        const navEl = lastUserRow.querySelector('.turn-version-nav');
        const version = msg.data && msg.data.version;
        if (version) lastUserRow.__wdMsg.version = version;
        if (navEl && version) {
          renderTurnVersionNav(navEl, version.index, version.total, lastUserRow.__wdMsg.id);
        }
      }
      break;

    case 'replay_assistant': {
      // 历史工具记录走结构化字段（服务端持久化的真实数据），不拼进回答正文。
      // 旧会话没有保存结果/耗时的，状态显示为「无记录」，不编造。
      if (msg.data.tools && msg.data.tools.length) {
        addExecPanelFromHistory(msg.data.tools);
      }
      const hasText = String(msg.data.content || '').trim();
      const hasArtifacts = msg.data.artifacts && msg.data.artifacts.length;
      if (!hasText && !hasArtifacts) break;  // 纯工具轮次：不留空气泡
      const el = addAssistant();
      if (hasText) {
        updateAssistant(el, msg.data.content, false);
        attachRetryButton(el, { userMessageId: msg.data.user_message_id || '' });
        attachAssistantVersionNav(el, msg.data.user_message_id || '', msg.data.version);
        attachSpeakButton(el, msg.data.content);
      }
      // 历史会话里的产物卡片（服务端按轮次随回答一起下发）
      if (hasArtifacts) renderArtifacts(msg.data.artifacts);
      break;
    }

    case 'stream_text':
      // 纯空白分片不新建气泡：否则会出现一条只含空白的助手消息。
      if (!currentStreamText && !String(msg.data.text || '').trim()) break;
      if (!currentAssistantEl) { currentAssistantEl = addAssistant(); currentStreamText = ''; }
      currentStreamText += msg.data.text;
      updateAssistant(currentAssistantEl, currentStreamText, true);
      break;

    case 'stream_end':
      // 只结束“这一段”流式文本，不结束这一轮回答。
      // 一轮里经常是「说一句 → 调工具 → 再说一句」，它们属于同一条助手回复；
      // 以前在这里把 currentAssistantEl 置空，于是每段文字都变成一条新气泡，
      // 并且每段都挂了一个「朗读」按钮。
      if (currentAssistantEl && currentStreamText) {
        updateAssistant(currentAssistantEl, currentStreamText, false);
      }
      break;

    case 'thinking_text':
      appendThoughtText(msg.data.text);
      break;

    case 'tool_use':
      // 工具事件只更新本轮的执行容器，不新建气泡、不新建思考块。
      addToolUse(msg.data);
      break;

    case 'tool_result':
      updateToolResult(msg.data);
      break;

    case 'permission_request':
      addPermissionDialog(msg.data);
      break;

    case 'ask_user':
      addAskUserDialog(msg.data);
      break;

    case 'turn_complete':
      break;

    case 'loop_complete': {
      if (currentAssistantEl) {
        if (String(currentStreamText || '').trim()) {
          updateAssistant(currentAssistantEl, currentStreamText, false);
          // 消息级操作（Retry / 朗读）只出现在这条真正的最终回答上，
          // 不挂在任何中间步骤或空白气泡上。
          attachRetryButton(currentAssistantEl, {
            userMessageId: (lastUserRow && lastUserRow.__wdMsg) ? lastUserRow.__wdMsg.id : '',
          });
          // 重新生成后这一轮就有多个版本了：回答下方立刻给出 ‹ 1/2 ›
          if (lastUserRow && lastUserRow.__wdMsg && lastUserRow.__wdMsg.version) {
            attachAssistantVersionNav(
              currentAssistantEl,
              lastUserRow.__wdMsg.id,
              lastUserRow.__wdMsg.version,
            );
          }
          if (!msg.data.cancelled) attachSpeakButton(currentAssistantEl, currentStreamText);
        } else {
          // 这一轮只调了工具、没有正文：不要留一个空气泡。
          currentAssistantEl.remove();
        }
        currentAssistantEl = null;
        currentStreamText = '';
      }
      // 收尾本轮执行容器：取消的轮次要标明「已停止」，不能显示成成功
      if (msg.data.cancelled) markRunningToolsCancelled();
      else finalizeExecPanel();
      // 保留容器数据（el.__wdExec），切回开发者模式时随时可以重新展开
      currentExecEl = null;
      currentExecTools = [];
      currentExecThought = '';
      const el = document.createElement('div');
      el.className = 'done-indicator';
      if (msg.data.cancelled) {
        el.innerHTML = '■ Stopped after ' + msg.data.elapsed.toFixed(1) + 's';
      } else {
        el.innerHTML = '✻ Done in ' + msg.data.elapsed.toFixed(1) + 's';
      }
      messagesEl.appendChild(el);
      streaming = false;
      updateUI();
      scrollToBottom();
      break;
    }

    case 'usage':
      tokenInfo.style.display = 'flex';
      tokenInfo.textContent = '↑' + formatTokens(msg.data.inputTokens) + '  ↓' + formatTokens(msg.data.outputTokens);
      break;

    case 'error':
      addError(msg.data.message);
      streaming = false;
      updateUI();
      break;

    case 'artifacts':
      renderArtifacts(msg.data.files || []);
      break;

    case 'memory_state':
      renderMemoryState(msg.data || {});
      break;

    case 'memory_action':
      if (msg.data && msg.data.message) {
        if (msg.data.ok) addSystem(msg.data.message);
        else addError(msg.data.message);
      }
      break;

    case 'compact':
      addSystem('⟳ ' + msg.data.message);
      break;

    case 'retry':
      addSystem('↻ Retrying: ' + msg.data.reason);
      break;
  }
}

function formatTokens(n) {
  if (n > 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n > 1000) return (n/1000).toFixed(1) + 'K';
  return '' + n;
}

// 纯文本文件的内容直接拼进发给模型的 content 里（用这个格式包一层），
// 展示气泡时再用同一套规则把它抠掉，只留用户自己敲的文字 + 一个文件名 chip。
function buildFileBlock(name, content) {
  return '\n\n<file name="' + name.replace(/"/g, '&quot;') + '">\n' + content + '\n</file>';
}

const FILE_BLOCK_RE = /\n*<file name="([^"]*)">\n[\s\S]*?\n<\/file>/g;

function stripFileBlocks(text) {
  const fileNames = [];
  const blocks = [];
  const display = text.replace(FILE_BLOCK_RE, (match, name) => {
    fileNames.push(name.replace(/&quot;/g, '"'));
    blocks.push(match.replace(/^\n*/, ''));
    return '';
  }).trim();
  return { display, fileNames, blocks };
}

// 每条用户消息都带「编辑」入口（不再只保留最新一条）。
// meta = { message_id, version: {index, total} }
function addUser(text, images, documents, meta) {
  hideEmptyState();
  const { display, fileNames, blocks } = stripFileBlocks(text || '');
  const row = document.createElement('div');
  row.className = 'msg-row user';
  row.innerHTML =
    '<div class="avatar user">' + initials() + '</div>' +
    '<div class="col"></div>';
  const col = row.querySelector('.col');

  // 把这条消息的原始数据挂在 DOM 上：编辑时要用它回填文字、附件和文件块
  row.__wdMsg = {
    id: (meta && meta.message_id) || '',
    wire: text || '',
    display: display,
    images: (images || []).slice(),
    documents: (documents || []).slice(),
    fileBlocks: blocks,
    fileNames: fileNames,
    version: (meta && meta.version) || null,
  };
  // 生成任务的提示词卡片：{ capability, prompt, taskEl }
  // 放在 row 上而不是闭包里，外面才能后补 taskEl（先插气泡、后建卡片）
  row.__wdGen = (meta && meta.genTask) || null;

  const attachRow = document.createElement('div');
  attachRow.className = 'bubble-user-images';
  if (images && images.length) {
    images.forEach(img => {
      const el = document.createElement('img');
      el.src = 'data:' + img.media_type + ';base64,' + img.data;
      attachRow.appendChild(el);
    });
    col.appendChild(attachRow);
  }

  const fileChips = (documents || []).map(d => ({ icon: '📕', name: d.name || 'document.pdf' }))
    .concat(fileNames.map(n => ({ icon: '📄', name: n })));
  if (fileChips.length) {
    const fileRow = document.createElement('div');
    fileRow.className = 'bubble-user-files';
    fileChips.forEach(f => fileRow.appendChild(makeFileChip(f.icon, f.name, null)));
    col.appendChild(fileRow);
  }

  const bubble = document.createElement('div');
  bubble.className = 'bubble-user';
  bubble.textContent = display || (fileChips.length || (images && images.length) ? '（附件）' : '');
  col.appendChild(bubble);

  const actionsRow = document.createElement('div');
  actionsRow.className = 'msg-user-actions';
  if (meta && meta.no_actions) {
    // 生成类提示词不进对话历史，自然也没有"编辑/版本"可言，不渲染操作栏
    actionsRow.style.display = 'none';
  } else {
    const editBtn = document.createElement('button');
    editBtn.className = 'msg-edit-btn';
    editBtn.type = 'button';
    editBtn.title = meta && meta.genTask ? '修改描述并重新生成' : '编辑这条消息';
    editBtn.innerHTML = ICON_PENCIL + ' 编辑';
    if (meta && meta.genTask) {
      // 生成任务：改文字 = 停掉旧任务 + 按新描述重新生成
      editBtn.addEventListener('click', () => {
        startEditGeneratedPrompt(row, col, bubble, editBtn, actionsRow, row.__wdGen);
      });
    } else {
      editBtn.addEventListener('click', () => startEditMessage(row, col, bubble, editBtn, actionsRow));
    }
    actionsRow.appendChild(editBtn);
    const navEl = document.createElement('div');
    navEl.className = 'turn-version-nav';
    actionsRow.appendChild(navEl);
    const version = meta && meta.version;
    if (version) renderTurnVersionNav(navEl, version.index, version.total, row.__wdMsg.id);
  }
  col.appendChild(actionsRow);

  messagesEl.appendChild(row);
  lastUserRow = row;
  scrollToBottom();
  return row;
}

// 附录：编辑时把保留下来的附件重新拼回发给模型的正文
function composeEditedWire(msg, text, keepFileBlocks) {
  let wire = text;
  keepFileBlocks.forEach(block => { wire += '\n\n' + block; });
  return wire;
}

let activeEditor = null;

function closeActiveEditor() {
  if (activeEditor && activeEditor.cancel) activeEditor.cancel();
  activeEditor = null;
}

function startEditMessage(row, col, bubble, editBtn, actionsRow) {
  if (streaming) {
    addSystem('正在生成回答，暂时不能编辑。等这一轮结束，或先点停止。');
    return;
  }
  if (activeEditor) {
    // 同一时间只允许一个编辑框，避免两条编辑请求互相覆盖
    if (activeEditor.row === row) return;
    closeActiveEditor();
  }

  const msg = row.__wdMsg || { id: '', display: '', images: [], documents: [], fileBlocks: [], fileNames: [] };
  actionsRow.style.display = 'none';

  const textarea = document.createElement('textarea');
  textarea.className = 'msg-edit-area';
  textarea.rows = Math.min(12, Math.max(2, (msg.display || '').split('\n').length + 1));
  textarea.value = msg.display || '';

  // 附件：默认全部保留，用户可以逐个移除（不允许静默丢弃）
  const keptImages = msg.images.slice();
  const keptDocuments = msg.documents.slice();
  const keptFiles = msg.fileBlocks.slice();
  const attachBox = document.createElement('div');
  attachBox.className = 'msg-edit-attach';

  function rebuildAttachBox() {
    attachBox.innerHTML = '';
    keptImages.forEach((img, i) => {
      const chip = document.createElement('div');
      chip.className = 'attach-chip';
      const thumb = document.createElement('img');
      thumb.className = 'thumb';
      thumb.src = 'data:' + img.media_type + ';base64,' + img.data;
      chip.appendChild(thumb);
      const label = document.createElement('span');
      label.className = 'name';
      label.textContent = '图片 ' + (i + 1);
      chip.appendChild(label);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.title = '移除这张图片';
      rm.textContent = '×';
      rm.addEventListener('click', () => { keptImages.splice(i, 1); rebuildAttachBox(); });
      chip.appendChild(rm);
      attachBox.appendChild(chip);
    });
    keptDocuments.forEach((doc, i) => {
      const chip = document.createElement('div');
      chip.className = 'attach-chip';
      const label = document.createElement('span');
      label.className = 'name';
      label.textContent = doc.name || ('文档 ' + (i + 1));
      chip.appendChild(label);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.title = '移除这个文档';
      rm.textContent = '×';
      rm.addEventListener('click', () => { keptDocuments.splice(i, 1); rebuildAttachBox(); });
      chip.appendChild(rm);
      attachBox.appendChild(chip);
    });
    keptFiles.forEach((_block, i) => {
      const chip = document.createElement('div');
      chip.className = 'attach-chip';
      const label = document.createElement('span');
      label.className = 'name';
      label.textContent = msg.fileNames[i] || ('文件 ' + (i + 1));
      chip.appendChild(label);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.title = '移除这个文件';
      rm.textContent = '×';
      rm.addEventListener('click', () => { keptFiles.splice(i, 1); rebuildAttachBox(); });
      chip.appendChild(rm);
      attachBox.appendChild(chip);
    });
    attachBox.style.display = attachBox.children.length ? 'flex' : 'none';
  }
  rebuildAttachBox();

  const actions = document.createElement('div');
  actions.className = 'msg-edit-actions';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn-save';
  saveBtn.textContent = '保存并发送';
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);

  const hint = document.createElement('div');
  hint.className = 'msg-edit-hint';
  hint.textContent = '保存会新建一条分支，原对话仍可切回。';

  bubble.style.display = 'none';
  col.insertBefore(textarea, actionsRow);
  col.insertBefore(attachBox, actionsRow);
  if (attachBox.children.length || keptFiles.length) col.insertBefore(hint, actionsRow);
  col.insertBefore(actions, actionsRow);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  function cancel() {
    textarea.remove();
    attachBox.remove();
    hint.remove();
    actions.remove();
    bubble.style.display = '';
    actionsRow.style.display = '';
    if (activeEditor && activeEditor.row === row) activeEditor = null;
  }

  function save() {
    if (streaming || !ws || ws.readyState !== WebSocket.OPEN) {
      addSystem('连接不可用，稍后再试。');
      return;
    }
    const newText = textarea.value.trim();
    if (!newText && !keptImages.length && !keptDocuments.length && !keptFiles.length) return;
    const wire = composeEditedWire(msg, newText, keptFiles);
    ws.send(JSON.stringify({
      type: 'edit_message',
      data: {
        message_id: msg.id || '',
        content: wire,
        images: keptImages.map(i => ({ media_type: i.media_type, data: i.data })),
        documents: keptDocuments.map(d => ({ media_type: d.media_type, data: d.data, name: d.name })),
      },
    }));
    activeEditor = null;
    streaming = true;
    updateUI();
  }

  cancelBtn.addEventListener('click', cancel);
  saveBtn.addEventListener('click', save);
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  activeEditor = { row: row, cancel: cancel };
}

// 生成任务的提示词也要能就地改：保存 = 停掉旧任务 + 按新描述重新生成
// （生成任务不进对话历史，所以没有"分支/版本"，只有"改文字再生成一次"）
function startEditGeneratedPrompt(row, col, bubble, editBtn, actionsRow, gen) {
  if (activeEditor) {
    if (activeEditor.row === row) return;
    closeActiveEditor();
  }
  actionsRow.style.display = 'none';

  const textarea = document.createElement('textarea');
  textarea.className = 'msg-edit-area';
  textarea.rows = Math.min(10, Math.max(2, (gen.prompt || '').split('\n').length + 1));
  textarea.value = gen.prompt || '';

  const actions = document.createElement('div');
  actions.className = 'msg-edit-actions';
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  cancelBtn.textContent = '取消';
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.className = 'btn-save';
  saveBtn.textContent = '保存并重新生成';
  actions.appendChild(cancelBtn);
  actions.appendChild(saveBtn);

  const hint = document.createElement('div');
  hint.className = 'msg-edit-hint';
  hint.textContent = '保存会停掉这张卡片原来的任务，并按新描述重新生成。';

  bubble.style.display = 'none';
  col.insertBefore(textarea, actionsRow);
  col.insertBefore(hint, actionsRow);
  col.insertBefore(actions, actionsRow);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);

  function cancel() {
    textarea.remove();
    hint.remove();
    actions.remove();
    bubble.style.display = '';
    actionsRow.style.display = '';
    if (activeEditor && activeEditor.row === row) activeEditor = null;
  }

  function save() {
    const next = textarea.value.trim();
    if (!next) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      addSystem('连接不可用，稍后再试。');
      return;
    }
    // 旧任务如果还在跑，先停掉，别让用户白等一个作废的结果
    const oldTask = gen.taskEl && gen.taskEl.__cap ? gen.taskEl.__cap : {};
    if (capIsRunning(oldTask.status) && oldTask.task_id) {
      ws.send(JSON.stringify({ type: 'capability_cancel', data: { task_id: oldTask.task_id } }));
    }
    gen.prompt = next;
    row.__wdMsg.display = next;
    row.__wdMsg.wire = next;
    bubble.textContent = next;
    cancel();
    dispatchCapability(gen.capability, next, '');
  }

  cancelBtn.addEventListener('click', cancel);
  saveBtn.addEventListener('click', save);
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); save(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
  });
  activeEditor = { row: row, cancel: cancel };
}

function initials() { return 'U'; }
// 回答下方的操作行（重新生成 + 版本导航），所有回答共用同一套
function assistantActionsBar(row) {
  var col = row.querySelector('.col');
  if (!col) return null;
  var bar = col.querySelector('.msg-assistant-actions');
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'msg-assistant-actions';
    col.appendChild(bar);
  }
  return bar;
}

// 每条助手回答都带「重新生成」+ 版本导航（不再只允许最新一条）
function attachRetryButton(row, opts) {
  var options = opts || {};
  var bar = assistantActionsBar(row);
  if (!bar) return;
  var existing = bar.querySelector('.msg-retry-btn');
  if (existing) existing.remove();
  const btn = document.createElement('button');
  btn.className = 'msg-retry-btn';
  btn.type = 'button';
  btn.title = '用同一个提问重新生成这条回答（会保留旧版本，可切回）';
  btn.innerHTML = '↻ 重新生成';
  btn.addEventListener('click', () => {
    if (streaming || !ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({
      type: 'regenerate',
      data: { message_id: options.userMessageId || '' },
    }));
    streaming = true;
    updateUI();
  });
  bar.appendChild(btn);
}

// 回答下方的 ‹ 1/2 ›：切换的是「同一个提问的不同回答」，纯读不重跑模型
function attachAssistantVersionNav(row, messageId, version) {
  if (!row || !version) return;
  var bar = assistantActionsBar(row);
  if (!bar) return;
  var nav = bar.querySelector('.turn-version-nav');
  if (!nav) {
    nav = document.createElement('div');
    nav.className = 'turn-version-nav turn-version-nav-assistant';
    bar.appendChild(nav);
  }
  renderTurnVersionNav(nav, version.index, version.total, messageId || '');
}

function switchTurnVersion(messageId, newIndex) {
  if (streaming || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    type: 'switch_turn_version',
    data: { message_id: messageId || '', index: newIndex },
  }));
}

// ‹ 1/2 ›：只在同一条消息有多个版本时出现，切换不会重新请求模型。
function renderTurnVersionNav(navEl, index, total, messageId) {
  if (!navEl) return;
  if (total <= 1) { navEl.classList.remove('show'); navEl.innerHTML = ''; return; }
  navEl.classList.add('show');
  navEl.innerHTML =
    '<button type="button" data-dir="prev" title="上一个版本">‹</button>' +
    '<span>' + (index + 1) + '/' + total + '</span>' +
    '<button type="button" data-dir="next" title="下一个版本">›</button>';
  const prevBtn = navEl.querySelector('[data-dir="prev"]');
  const nextBtn = navEl.querySelector('[data-dir="next"]');
  prevBtn.disabled = index <= 0;
  nextBtn.disabled = index >= total - 1;
  prevBtn.addEventListener('click', () => switchTurnVersion(messageId, index - 1));
  nextBtn.addEventListener('click', () => switchTurnVersion(messageId, index + 1));
}

function addAssistant() {
  hideEmptyState();
  const row = document.createElement('div');
  row.className = 'msg-row assistant';
  row.innerHTML =
    '<div class="avatar assistant">' + brandGlyph() + '</div>' +
    '<div class="col"><div class="content"></div></div>';
  messagesEl.appendChild(row);
  scrollToBottom();
  return row;
}

function brandGlyph() {
  return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 4c-.86 0-1.62.35-2.18.95L6.8 2.3l.55 4.3C5.1 7.85 3.6 10.35 3.6 13.2 3.6 18 7.35 21.7 12 21.7s8.4-3.7 8.4-8.5c0-2.85-1.5-5.35-3.75-6.6l.55-4.3-3.02 2.65C13.62 4.35 12.86 4 12 4z" fill="#fff"/><circle cx="9.1" cy="13.6" r="1.15" fill="#161320"/><circle cx="14.9" cy="13.6" r="1.15" fill="#161320"/></svg>';
}

function updateAssistant(row, text, isStreaming) {
  const contentEl = row.querySelector('.content');
  const thinkMatch = text.match(/^<think>([\s\S]*?)<\/think>\s*([\s\S]*)$/);
  let html = '';
  if (thinkMatch) {
    const thinkText = thinkMatch[1].trim();
    const bodyText = thinkMatch[2].trim();
    if (thinkText) html += thinkingHtml(thinkText, false);
    if (bodyText) html += renderMarkdown(bodyText);
    if (isStreaming) html += '<span class="cursor"></span>';
  } else if (text.startsWith('<think>') && isStreaming) {
    html += thinkingHtml(text.replace(/^<think>\s*/, ''), true);
    html += '<span class="cursor"></span>';
  } else {
    html = renderMarkdown(text);
    if (isStreaming) html += '<span class="cursor"></span>';
  }
  contentEl.innerHTML = html;
  // 链接策略在写进 DOM 之后再走一遍：实时回答、历史回放、旧会话内容共用这条路径
  hardenLinks(contentEl);
  enhanceCodeBlocks(contentEl);
  scrollToBottom();
}

function thinkingHtml(text, active) {
  return '<div class="thinking-block">' +
    '<div class="thinking-header" onclick="toggleThinking(this)">' +
      '<span class="chev">▶</span><span class="think-glyph">💭</span><span>' + (active ? 'Thinking…' : 'Thought') + '</span>' +
    '</div>' +
    '<div class="thinking-body"><div class="inner">' + escapeHtml(text) + '</div></div>' +
  '</div>';
}

// ── 执行状态容器 ──
// 一轮对话 = 一个容器。容器自己保存数据（el.__wdExec），渲染完全由 displayMode 决定：
//   普通模式：一行进度（正在…/已完成），不渲染工具名、参数、路径、结果、思考内容；
//   开发者模式：可展开的「执行详情 · N 次工具调用」，逐条展示工具名/状态/耗时/参数/结果。
// 切换模式时调用 renderExecPanel 重建内部结构（不是 display:none 藏起来），
// 因此历史记录、已完成的轮次都会跟着切换。
function ensureExecPanel() {
  if (currentExecEl && currentExecEl.isConnected) return currentExecEl;
  hideEmptyState();
  const el = document.createElement('div');
  el.className = 'thinking-block exec-panel';
  bindExecData(el, { tools: currentExecTools, thought: '', running: true, cancelled: false });
  messagesEl.appendChild(el);
  currentExecEl = el;
  return el;
}

function execToolStatus(tool) {
  if (!tool) return 'unknown';
  if (tool.status) return tool.status;
  return 'running';
}

// 预算是“软失败”：搜索/读取配额用尽是 Agent 应该停止检索的信号，
// 不是用户需要看到红字的故障。普通模式不算作未完成，开发者模式仍然逐条保留。
const BUDGET_DENIAL_RE = /已达上限|配额已用尽|不要再调用|本轮.*上限|budget|quota/i;
function execIsBudgetDenial(tool) {
  if (execToolStatus(tool) !== 'failed') return false;
  return BUDGET_DENIAL_RE.test(String(tool.output || ''));
}

function execFailedTools(el) {
  const d = readExecData(el);
  if (!d) return [];
  return (d.tools || []).filter(t => execToolStatus(t) === 'failed');
}

function execRealFailures(el) {
  return execFailedTools(el).filter(t => !execIsBudgetDenial(t));
}

function execSummary(el) {
  const d = readExecData(el);
  if (!d) return { state: 'idle', label: '', count: 0 };
  const tools = d.tools || [];
  const count = tools.length;
  if (d.running) {
    const running = tools.find(t => execToolStatus(t) === 'running');
    const label = running
      ? actionLabelForTool(running.name) + '…'
      : (count ? '正在处理…' : '正在思考…');
    return { state: 'running', label, count };
  }
  const failedAll = tools.filter(t => execToolStatus(t) === 'failed');
  const realFailed = failedAll.filter(t => !execIsBudgetDenial(t));
  const softCount = failedAll.length - realFailed.length;
  if (d.cancelled) return { state: 'cancelled', label: '已停止', count };
  if (realFailed.length) {
    return { state: 'failed', label: realFailed.length + ' 个步骤未完成', count, softCount };
  }
  if (!count) return { state: 'done', label: '', count: 0 };
  return { state: 'done', label: '已完成', count, softCount };
}

// 普通模式下，工具失败要把可读的错误显示出来（不能静默吞掉）
function execFailureText(el) {
  const d = readExecData(el);
  if (!d) return '';
  const all = (d.tools || []).filter(t => execToolStatus(t) === 'failed');
  const real = all.filter(t => !execIsBudgetDenial(t));
  if (!real.length) {
    // 只剩预算类拒绝 → 给一句人会说的话，而不是英文错误
    if (all.length) return '检索次数已到本轮上限，已基于已有信息作答';
    return '';
  }
  const first = real[0];
  const raw = (first.output || '').trim();
  // 权限被拒是最常见的"失败"：给普通用户一句人话，而不是原始英文错误
  if (/permission denied/i.test(raw)) {
    return '你拒绝了这一步，可以重新发起并选择允许。';
  }
  const line = raw.split('\n').find(l => l.trim()) || '';
  const hint = line.replace(/^(Error|错误)[:：]?\s*/i, '').slice(0, 160);
  return hint || '这一步没有完成，可以稍后重试。';
}

function renderExecPanel(el) {
  if (!el) return;
  const d = readExecData(el);
  if (!d) return;
  const summary = execSummary(el);
  el.className = 'thinking-block exec-panel mode-' + displayMode +
    (summary.state === 'failed' ? ' failed' : '');

  // 重绘前先清空旧结构：否则每次工具事件 / 每次切换模式都会把新的标题和行
  // 叠加在旧节点上，切回普通模式时开发者内容仍然留在 DOM 里。
  while (el.firstChild) el.removeChild(el.firstChild);

  // 没有任何可展示的东西（无工具、无思考）→ 普通模式下一个节点都不留
  if (!summary.count && !(d.thought || '').trim()) {
    el.style.display = 'none';
    return;
  }
  // 普通模式只关心「任务进度」：没有工具调用的纯推理不展示，避免出现空状态条
  if (displayMode !== 'developer' && !summary.count) {
    el.style.display = 'none';
    return;
  }
  el.style.display = '';

  const head = document.createElement('div');
  head.className = 'thinking-header exec-status-' + summary.state;

  if (displayMode === 'developer') {
    // 开发者模式：标题 + 折叠开关（展开状态记在数据里，重绘后不丢）
    head.addEventListener('click', () => {
      const state = readExecData(el);
      if (!state) return;
      state.expanded = !state.expanded;
      renderExecPanel(el);
    });
    const chev = document.createElement('span');
    chev.className = 'chev' + (d.expanded ? ' expanded' : '');
    chev.textContent = '▶';
    head.appendChild(chev);
    const glyph = document.createElement('span');
    glyph.className = 'think-glyph exec-glyph';
    glyph.textContent = '⚙';
    head.appendChild(glyph);
    const label = document.createElement('span');
    label.className = 'think-label';
    label.textContent = summary.count
      ? '执行详情 · ' + summary.count + ' 次工具调用'
      : '执行详情';
    head.appendChild(label);
    if (summary.state !== 'done') {
      const state = document.createElement('span');
      state.className = 'exec-count';
      state.textContent = summary.label;
      head.appendChild(state);
    }
  } else {
    // 普通模式：一行人话状态，不出现工具名
    const glyph = document.createElement('span');
    glyph.className = 'think-glyph exec-glyph';
    glyph.textContent = summary.state === 'failed' ? '⚠' : (summary.state === 'done' ? '✓' : '⚙');
    head.appendChild(glyph);
    const label = document.createElement('span');
    label.className = 'exec-label';
    label.textContent = summary.label;
    head.appendChild(label);
    if (summary.count > 1 && summary.state === 'done') {
      const count = document.createElement('span');
      count.className = 'exec-count';
      count.textContent = summary.count + ' 步';
      head.appendChild(count);
    }
    if (summary.state === 'failed') {
      const hint = document.createElement('span');
      hint.className = 'exec-count';
      hint.textContent = execFailureText(el);
      head.appendChild(hint);
    }
  }
  el.appendChild(head);

  if (displayMode !== 'developer') return;  // 普通模式到此为止，内容根本不进 DOM

  const body = document.createElement('div');
  body.className = 'thinking-body' + (d.expanded ? ' show' : '');  // 默认折叠，用户点开头展开
  const inner = document.createElement('div');
  inner.className = 'inner';
  inner.style.fontStyle = 'normal';

  if ((d.thought || '').trim()) {
    const thought = document.createElement('div');
    thought.className = 'exec-thought';
    thought.textContent = d.thought;
    inner.appendChild(thought);
  }

  const rows = document.createElement('div');
  rows.className = 'exec-rows';
  (d.tools || []).forEach(tool => rows.appendChild(renderExecRow(tool)));
  inner.appendChild(rows);
  body.appendChild(inner);
  el.appendChild(body);
}

function renderExecRow(tool) {
  const status = execToolStatus(tool);
  const row = document.createElement('details');
  row.className = 'exec-row';

  const summary = document.createElement('summary');
  const name = document.createElement('span');
  name.className = 'exec-name';
  // 工具名只出现在开发者模式的详情里，且用 textContent（不可注入）
  name.textContent = tool.name || '(unknown)';
  summary.appendChild(name);

  const badge = document.createElement('span');
  const badgeClass = status === 'ok' ? 'ok'
    : status === 'failed' ? 'failed'
    : status === 'running' ? 'running'
    : status === 'cancelled' ? 'cancelled' : 'unknown';
  badge.className = 'exec-badge ' + badgeClass;
  badge.textContent = status === 'ok' ? '成功'
    : status === 'failed' ? '失败'
    : status === 'running' ? '进行中'
    : status === 'cancelled' ? '已取消' : '无记录';
  summary.appendChild(badge);

  if (typeof tool.elapsed === 'number' && isFinite(tool.elapsed)) {
    const time = document.createElement('span');
    time.className = 'exec-time';
    time.textContent = tool.elapsed < 1
      ? Math.round(tool.elapsed * 1000) + 'ms'
      : tool.elapsed.toFixed(2) + 's';
    summary.appendChild(time);
  }
  row.appendChild(summary);

  const body = document.createElement('div');
  body.className = 'exec-row-body';

  const argsText = tool.args && Object.keys(tool.args).length
    ? JSON.stringify(tool.args, null, 2)
    : '';
  if (argsText) {
    const section = document.createElement('div');
    section.className = 'exec-section';
    const label = document.createElement('span');
    label.className = 'exec-section-label';
    label.textContent = '参数';
    section.appendChild(label);
    section.appendChild(document.createTextNode(argsText));
    body.appendChild(section);
  }

  if ((tool.output || '').trim()) {
    const section = document.createElement('div');
    section.className = 'exec-section';
    const label = document.createElement('span');
    label.className = 'exec-section-label';
    label.textContent = '结果';
    section.appendChild(label);
    section.appendChild(document.createTextNode(tool.output));
    body.appendChild(section);
  }

  if (body.childNodes.length) row.appendChild(body);
  return row;
}

function refreshAllExecPanels() {
  messagesEl.querySelectorAll('.thinking-block.exec-panel').forEach(el => {
    if (readExecData(el)) renderExecPanel(el);
  });
}

function applyDisplayMode(next, opts) {
  const options = opts || {};
  const mode = DISPLAY_MODES.includes(next) ? next : 'normal';
  displayMode = mode;
  // 普通/开发者模式也影到 <html> 上，功能栏卡片里的内部记录靠 CSS 控制显隐
  document.documentElement.dataset.mode = mode;
  if (options.persist !== false) {
    try { localStorage.setItem(DISPLAY_MODE_KEY, mode); } catch (e) { /* 忽略 */ }
  }
  document.querySelectorAll('.mode-btn').forEach(btn => {
    const active = btn.dataset.mode === mode;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
  // 只重绘展示，不触碰会话、不发任何请求：模式切换绝不重跑任务
  refreshAllExecPanels();
  refreshPermissionDialogs();
}

function refreshPermissionDialogs() {
  document.querySelectorAll('.dialog-card[data-perm]').forEach(card => {
    const showTool = displayMode === 'developer';
    const toolEl = card.querySelector('.perm-tool');
    const detailEl = card.querySelector('.perm-detail');
    if (toolEl) toolEl.style.display = showTool ? '' : 'none';
    if (detailEl) detailEl.style.display = showTool ? '' : 'none';
  });
}

// 历史回放：把服务端持久化的工具元数据渲染成同一套容器
function addExecPanelFromHistory(tools) {
  if (!tools || !tools.length) return null;
  hideEmptyState();
  const el = document.createElement('div');
  el.className = 'thinking-block exec-panel';
  bindExecData(el, { tools: tools.map(t => Object.assign({}, t)), thought: '', running: false });
  messagesEl.appendChild(el);
  renderExecPanel(el);
  return el;
}

function appendThoughtText(text) {
  ensureExecPanel();
  currentExecThought += text;
  const data = readExecData(currentExecEl);
  if (data) data.thought = currentExecThought;
  // 普通模式不展示模型推理，也不需要为此重绘
  if (displayMode === 'developer') renderExecPanel(currentExecEl);
}

function finalizeExecPanel() {
  if (!currentExecEl) return;
  const data = readExecData(currentExecEl);
  if (data) {
    data.running = false;
    data.thought = currentExecThought;
  }
  renderExecPanel(currentExecEl);
}

function finalizeThinking(el) {
  if (!el) return;
  const data = readExecData(el);
  if (data) data.running = false;
  renderExecPanel(el);
}

function toggleThinking(header) {
  const chev = header.querySelector('.chev');
  const body = header.nextElementSibling;
  if (chev) chev.classList.toggle('expanded');
  if (body) body.classList.toggle('show');
}

function addSystem(text) {
  hideEmptyState();
  const div = document.createElement('div');
  div.className = 'msg-system';
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

// ── 本轮产物：图片预览 + 按需下载（不自动落盘到用户机器）──
function formatBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function artifactGlyph(f) {
  if (f.kind === 'image') return '🖼';
  if (f.type_label === '表格') return '📊';
  if (f.type_label === 'PDF') return '📕';
  if (f.type_label === '演示文稿') return '📽';
  if (f.type_label === '压缩包') return '🗜';
  return '📄';
}

// files 由服务端下发：{ id, name, size, kind, type_label, url, preview_url, available }
function renderArtifacts(files) {
  if (!files || !files.length) return;
  const allRows = messagesEl.querySelectorAll('.msg-row.assistant');
  const host = allRows.length
    ? (allRows[allRows.length - 1].querySelector('.col') || allRows[allRows.length - 1])
    : messagesEl;
  const box = document.createElement('div');
  box.className = 'artifacts';

  files.forEach(f => {
    const item = document.createElement('div');
    item.className = 'artifact';

    const available = f.available !== false && !!f.url;
    const canPreview = available && f.preview_url && f.kind === 'image';

    if (canPreview) {
      const img = document.createElement('img');
      img.className = 'artifact-thumb';
      img.src = f.preview_url;
      img.alt = f.name || 'image';
      img.loading = 'lazy';
      img.title = '点击放大预览';
      img.addEventListener('click', () => {
        if (typeof openLightbox === 'function') openLightbox(img.src);
        else window.open(img.src, '_blank');
      });
      // 预览失败（文件被清理）→ 退化成普通卡片，不影响下载区
      img.addEventListener('error', () => img.remove());
      item.appendChild(img);
    } else {
      const glyph = document.createElement('div');
      glyph.className = 'artifact-glyph';
      glyph.textContent = artifactGlyph(f);
      item.appendChild(glyph);
    }

    const meta = document.createElement('div');
    meta.className = 'artifact-meta';

    const name = document.createElement('span');
    name.className = 'artifact-name';
    name.textContent = f.name || '未命名文件';
    name.title = f.name || '';

    const sub = document.createElement('span');
    sub.className = 'artifact-sub';
    sub.textContent = (f.type_label || '文件') + ' · ' + formatBytes(f.size);

    meta.appendChild(name);
    meta.appendChild(sub);

    if (available) {
      const dl = document.createElement('a');
      dl.className = 'artifact-dl';
      dl.href = f.url;
      dl.setAttribute('download', f.name || '');
      dl.textContent = '下载';
      meta.appendChild(dl);
    } else {
      const gone = document.createElement('span');
      gone.className = 'artifact-gone';
      gone.textContent = '文件已不可用';
      meta.appendChild(gone);
    }

    item.appendChild(meta);
    box.appendChild(item);
  });

  host.appendChild(box);
  scrollToBottom();
}

function addError(text) {
  hideEmptyState();
  const div = document.createElement('div');
  div.className = 'msg-error';
  div.innerHTML = '✖ ' + escapeHtml(text);
  messagesEl.appendChild(div);
  scrollToBottom();
}

// 用户视角的动作文案：普通用户只需要知道“在做哪一类事”，
// 原始工具名/路径/Session ID 放在可折叠的执行详情里给开发者看。
const TOOL_ACTION_LABELS = {
  SaveMemory: '正在保存偏好',
  WriteFile: '正在写入文件',
  EditFile: '正在修改文件',
  ReadFile: '正在读取文件',
  Grep: '正在查找内容',
  Glob: '正在查找文件',
  Bash: '正在执行命令',
  WebSearch: '正在搜索网络',
  WebFetch: '正在读取网页',
  Agent: '正在处理子任务',
  LoadSkill: '正在加载技能',
  AskUserQuestion: '正在确认需求',
};

function actionLabelForTool(name) {
  if (!name) return '正在处理';
  if (TOOL_ACTION_LABELS[name]) return TOOL_ACTION_LABELS[name];
  for (const key in TOOL_ACTION_LABELS) {
    if (name.indexOf(key) !== -1) return TOOL_ACTION_LABELS[key];
  }
  return '正在处理';
}

// ── 工具事件：只更新本轮容器的数据，绝不新建聊天气泡 ──
function addToolUse(data) {
  ensureExecPanel();
  const id = data.toolId || ('t' + currentExecTools.length);
  const existing = currentExecTools.find(t => t.id === id);
  if (existing) {
    existing.name = data.toolName || existing.name;
    existing.args = data.args || {};
  } else {
    currentExecTools.push({
      id,
      name: data.toolName || '',
      args: data.args || {},
      output: '',
      status: 'running',
      elapsed: null,
      isError: null,
    });
  }
  const payload = readExecData(currentExecEl);
  if (payload) payload.tools = currentExecTools;
  renderExecPanel(currentExecEl);
  scrollToBottom();
}

function updateToolResult(data) {
  ensureExecPanel();
  const id = data.toolId || '';
  let tool = currentExecTools.find(t => t.id === id);
  if (!tool) {
    // 结果先于调用到达（并发/重连）：仍然归到本轮容器，用 toolId 建一条
    tool = {
      id,
      name: data.toolName || '',
      args: {},
      output: '',
      status: 'running',
      elapsed: null,
      isError: null,
    };
    currentExecTools.push(tool);
  }
  if (data.toolName) tool.name = data.toolName;
  tool.output = (data.output || '').trim();
  tool.isError = !!data.isError;
  tool.status = data.isError ? 'failed' : 'ok';
  if (typeof data.elapsed === 'number') tool.elapsed = data.elapsed;
  const payload = readExecData(currentExecEl);
  if (payload) payload.tools = currentExecTools;
  renderExecPanel(currentExecEl);
  scrollToBottom();
}

// 用户中断：把仍在运行的工具标成已取消，而不是假装成功
function markRunningToolsCancelled() {
  if (!currentExecEl) return;
  const data = readExecData(currentExecEl);
  if (!data) return;
  (data.tools || []).forEach(t => {
    if (execToolStatus(t) === 'running') t.status = 'cancelled';
  });
  data.running = false;
  data.cancelled = true;
  renderExecPanel(currentExecEl);
}

// ── 权限弹窗 ──
// 显示模式与权限模式彻底分开：这里只决定「怎么描述」，从不决定「是否放行」。
// 授权与否完全由后端 PermissionChecker 决定，弹窗只是把决定告诉用户。
function addPermissionDialog(data) {
  hideEmptyState();
  const div = document.createElement('div');
  div.className = 'dialog-card';
  div.id = 'perm-' + data.id;
  div.dataset.perm = '1';

  const title = document.createElement('div');
  title.className = 'dialog-title';
  title.textContent = '🔒 需要你的同意';
  div.appendChild(title);

  const desc = document.createElement('div');
  desc.className = 'dialog-desc';
  const action = data.action || '执行一个操作';
  const target = data.target || (data.description || '');
  desc.textContent = action + (target ? '：' + target : '') +
    '\n' + (data.why || '只有你同意后，AI 外脑才会继续执行这一步。');
  div.appendChild(desc);

  // 工具名与原始描述只在开发者模式显示
  const tool = document.createElement('div');
  tool.className = 'perm-tool';
  tool.style.cssText = 'font-size:11.5px;color:var(--text-faint);margin:-6px 0 12px;';
  tool.textContent = '工具：' + (data.toolName || '未知');
  div.appendChild(tool);

  const detail = document.createElement('div');
  detail.className = 'perm-detail';
  detail.style.cssText = 'font-size:11.5px;color:var(--text-faint);margin:-6px 0 12px;white-space:pre-wrap;word-break:break-all;';
  detail.textContent = '请求内容：' + (data.description || '');
  div.appendChild(detail);

  const actions = document.createElement('div');
  actions.className = 'dialog-actions';
  const mk = (cls, text, response) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = cls;
    btn.textContent = text;
    btn.addEventListener('click', () => respondPerm(data.id, response));
    return btn;
  };
  actions.appendChild(mk('btn-allow', '允许', 'allow'));
  actions.appendChild(mk('btn-always', '本会话一直允许', 'allowAlways'));
  actions.appendChild(mk('btn-deny', '拒绝', 'deny'));
  div.appendChild(actions);

  messagesEl.appendChild(div);
  refreshPermissionDialogs();
  scrollToBottom();
}

function respondPerm(id, response) {
  ws.send(JSON.stringify({ type: 'permission_response', data: { id, response } }));
  const el = document.getElementById('perm-' + id);
  if (el) {
    el.classList.add('answered');
    const label = response === 'deny' ? '已拒绝' : '已允许';
    el.textContent = '';
    const note = document.createElement('div');
    note.style.cssText = 'color:var(--text-faint);font-size:12.5px;';
    note.textContent = '🔒 ' + label;
    el.appendChild(note);
  }
}

// ── Ask User 弹窗 ──
function addAskUserDialog(data) {
  hideEmptyState();
  const div = document.createElement('div');
  div.className = 'dialog-card';
  div.id = 'ask-' + data.id;
  div.style.borderLeftColor = 'var(--accent-2)';

  let html = '<div class="dialog-title">❓ Question</div>';
  const questions = data.questions || [];
  questions.forEach((q, qi) => {
    html += '<div class="q-block">';
    html += '<div class="q-text">' + escapeHtml(q.question || q.Text || '') + '</div>';
    const options = q.options || q.Options || [];
    options.forEach((opt) => {
      const label = opt.label || opt.Label || '';
      const desc = opt.description || opt.Description || '';
      html += '<label class="q-option">' +
        '<input type="radio" name="ask_' + data.id + '_' + qi + '" value="' + escapeHtml(label) + '"> ' +
        '<span class="opt-label">' + escapeHtml(label) + '</span>' +
        (desc ? '<span class="opt-desc">— ' + escapeHtml(desc) + '</span>' : '') +
        '</label>';
    });
    html += '<label class="q-option">' +
      '<input type="radio" name="ask_' + data.id + '_' + qi + '" value="__other__"> ' +
      '<span class="opt-desc">Other:</span>' +
      '<input type="text" class="q-other-input" id="ask_other_' + data.id + '_' + qi + '">' +
      '</label>';
    html += '</div>';
  });
  html += '<div class="dialog-actions"><button class="btn-allow" onclick="respondAsk(\'' + data.id + '\',' + questions.length + ')">Submit</button></div>';

  div.innerHTML = html;
  messagesEl.appendChild(div);
  scrollToBottom();
}

function respondAsk(id, qCount) {
  const answers = {};
  for (let i = 0; i < qCount; i++) {
    const radios = document.querySelectorAll('input[name="ask_' + id + '_' + i + '"]');
    let val = '';
    radios.forEach(r => {
      if (r.checked) {
        val = r.value === '__other__' ? document.getElementById('ask_other_' + id + '_' + i).value : r.value;
      }
    });
    answers['question_' + i] = val;
  }
  ws.send(JSON.stringify({ type: 'ask_user_response', data: { id, answers } }));
  const el = document.getElementById('ask-' + id);
  if (el) {
    el.classList.add('answered');
    el.style.borderLeftColor = 'var(--border)';
    el.innerHTML = '<div style="color:var(--text-faint);font-size:12.5px;">✓ Answered</div>';
  }
}

// ── 滚动控制 ──
scrollWrap.addEventListener('scroll', () => {
  const { scrollTop, scrollHeight, clientHeight } = scrollWrap;
  autoScroll = scrollHeight - scrollTop - clientHeight < 60;
  scrollFab.style.display = autoScroll ? 'none' : 'flex';
});
scrollFab.addEventListener('click', () => {
  autoScroll = true;
  scrollWrap.scrollTop = scrollWrap.scrollHeight;
  scrollFab.style.display = 'none';
});

function scrollToBottom() {
  if (autoScroll) {
    requestAnimationFrame(() => { scrollWrap.scrollTop = scrollWrap.scrollHeight; });
  }
}

// ── 侧边栏：折叠/展开 ──
function setSidebarCollapsed(collapsed) {
  appEl.classList.toggle('sidebar-collapsed', collapsed);
  try { localStorage.setItem('aieb_sidebar_collapsed', collapsed ? '1' : '0'); } catch(e) {}
}

(function initSidebarState() {
  let collapsed = window.innerWidth <= 860;
  try {
    const saved = localStorage.getItem('aieb_sidebar_collapsed');
    if (saved !== null && window.innerWidth > 860) collapsed = saved === '1';
  } catch(e) {}
  setSidebarCollapsed(collapsed);
})();

sidebarToggleBtn.addEventListener('click', () => {
  setSidebarCollapsed(!appEl.classList.contains('sidebar-collapsed'));
});
sidebarBackdrop.addEventListener('click', () => setSidebarCollapsed(true));

// ── 侧边栏：会话列表 ──
const ICON_TRASH = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M4 7h16M9 7V4h6v3M6 7l1 13a2 2 0 002 2h6a2 2 0 002-2l1-13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_PENCIL = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4L16.5 3.5z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

newChatBtn.addEventListener('click', () => {
  if (streaming || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'new_session', data: {} }));
  if (window.innerWidth <= 860) setSidebarCollapsed(true);
});

function groupSessionsByDate(list) {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const oneDay = 86400000;
  const buckets = { today: [], yesterday: [], week: [], month: [], older: [] };
  list.forEach(s => {
    const t = new Date(s.lastActive).getTime();
    if (t >= startOfToday) buckets.today.push(s);
    else if (t >= startOfToday - oneDay) buckets.yesterday.push(s);
    else if (t >= startOfToday - 7 * oneDay) buckets.week.push(s);
    else if (t >= startOfToday - 30 * oneDay) buckets.month.push(s);
    else buckets.older.push(s);
  });
  return [
    { label: '今天', items: buckets.today },
    { label: '昨天', items: buckets.yesterday },
    { label: '近 7 天', items: buckets.week },
    { label: '近 30 天', items: buckets.month },
    { label: '更早', items: buckets.older },
  ];
}

function renderSessionList() {
  if (!sessions.length) {
    sessionListEl.innerHTML = '<div class="session-empty-hint">暂无会话</div>';
    return;
  }
  const query = sessionSearchEl.value.trim().toLowerCase();
  const filtered = query
    ? sessions.filter(s => (s.title || '新对话').toLowerCase().includes(query))
    : sessions;
  if (!filtered.length) {
    sessionListEl.innerHTML = '<div class="session-empty-hint">没有匹配的会话</div>';
    return;
  }
  const groups = groupSessionsByDate(filtered);
  let html = '';
  groups.forEach(g => {
    if (!g.items.length) return;
    html += '<div class="sidebar-section-label">' + g.label + '</div>';
    g.items.forEach(s => {
      const title = escapeHtml(s.title || '新对话');
      const safeId = escapeAttr(s.id);
      const active = s.id === currentSessionId ? ' active' : '';
      html += '<div class="session-item' + active + '" data-id="' + safeId + '">' +
        '<span class="session-title">' + title + '</span>' +
        '<button class="session-rename" data-id="' + safeId + '" title="Rename">' + ICON_PENCIL + '</button>' +
        '<button class="session-delete" data-id="' + safeId + '" title="Delete">' + ICON_TRASH + '</button>' +
      '</div>';
    });
  });
  sessionListEl.innerHTML = html;

  sessionListEl.querySelectorAll('.session-item').forEach(el => {
    el.addEventListener('click', (e) => {
      if (e.target.closest('.session-delete') || e.target.closest('.session-rename')) return;
      const id = el.dataset.id;
      if (id === currentSessionId || streaming || !ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ type: 'load_session', data: { id } }));
      if (window.innerWidth <= 860) setSidebarCollapsed(true);
    });
  });
  sessionListEl.querySelectorAll('.session-delete').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      const res = await showConfirm(
        '删除这个对话？',
        '将永久删除这项对话的消息记录，以及它生成的文件（下载链接会立即失效）。\n' +
        '这段对话产生的记忆按下面的选项处理；被其他对话共同使用的记忆不会被删掉。',
        '删除',
        {
          checkboxLabel: '同时删除这段对话产生的长期记忆',
          checkboxChecked: true,
        }
      );
      if (!res) return;
      ws.send(JSON.stringify({
        type: 'delete_session',
        data: { id, purge_memories: res.checked !== false },
      }));
    });
  });
  sessionListEl.querySelectorAll('.session-rename').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      startRenameSession(btn.closest('.session-item'), btn.dataset.id);
    });
  });
}

function startRenameSession(itemEl, id) {
  const titleEl = itemEl.querySelector('.session-title');
  const currentTitle = titleEl.textContent;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-title-input';
  input.value = currentTitle;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const commit = () => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== currentTitle) {
      ws.send(JSON.stringify({ type: 'rename_session', data: { id, title: newTitle } }));
    } else {
      renderSessionList();
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
    else if (e.key === 'Escape') { done = true; renderSessionList(); }
  });
  input.addEventListener('blur', commit);
}

sessionSearchEl.addEventListener('input', renderSessionList);

// ── 模型/provider 切换 ──
function renderProviderSelect(providers, active) {
  // 用 DOM 构建 option（不用 innerHTML），display_name 来自配置，优先展示
  providerSelect.textContent = '';
  (providers || []).forEach(p => {
    const opt = document.createElement('option');
    opt.value = p.name;
    opt.textContent = p.display_name || (p.name + ' (' + p.model + ')');
    providerSelect.appendChild(opt);
  });
  if (active) providerSelect.value = active;
}
providerSelect.addEventListener('change', () => {
  if (streaming || !ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({ type: 'switch_provider', data: { name: providerSelect.value } }));
});

// ── 图片放大预览 ──
function openLightbox(src) {
  lightboxImg.src = src;
  imageLightbox.classList.add('show');
}
function closeLightbox() {
  imageLightbox.classList.remove('show');
  lightboxImg.src = '';
}
messagesEl.addEventListener('click', (e) => {
  const img = e.target.closest('.bubble-user-images img');
  if (img) openLightbox(img.src);
});
lightboxClose.addEventListener('click', closeLightbox);
imageLightbox.addEventListener('click', (e) => {
  if (e.target === imageLightbox) closeLightbox();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && imageLightbox.classList.contains('show')) closeLightbox();
});

// ── 自定义确认弹窗（替代原生 confirm()） ──
// 支持一个可选勾选项（例如「同时删除这段对话产生的长期记忆」）。
// 解析结果统一为 { ok: true, checked: boolean } 或 false。
let confirmResolve = null;
function showConfirm(title, desc, confirmLabel, options) {
  const opts = options || {};
  confirmTitleEl.textContent = title;
  confirmDescEl.textContent = desc;
  confirmOkBtn.textContent = confirmLabel || 'Confirm';

  const hasOption = !!opts.checkboxLabel;
  confirmOptionWrap.style.display = hasOption ? 'flex' : 'none';
  if (hasOption) {
    confirmOptionLabelEl.textContent = opts.checkboxLabel;
    // 默认勾选：与产品要求一致（默认连同记忆一起删）
    confirmOptionEl.checked = opts.checkboxChecked !== false;
  } else {
    confirmOptionEl.checked = false;
  }

  confirmModal.classList.add('show');
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function closeConfirm(result) {
  confirmModal.classList.remove('show');
  if (confirmResolve) {
    confirmResolve(result ? { ok: true, checked: !!confirmOptionEl.checked } : false);
    confirmResolve = null;
  }
}
confirmOkBtn.addEventListener('click', () => closeConfirm(true));
confirmCancelBtn.addEventListener('click', () => closeConfirm(false));
confirmModal.addEventListener('click', (e) => {
  if (e.target === confirmModal) closeConfirm(false);
});

// ── 显示模式：普通 / 开发者（仅前端展示偏好，不发送任何请求）──
document.querySelectorAll('.mode-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (btn.dataset.mode === displayMode) return;
    // 只重绘已有内容；不会重新执行任务、不会重发 user_message
    applyDisplayMode(btn.dataset.mode);
  });
});
applyDisplayMode(loadDisplayMode(), { persist: false });

// ── 记忆管理面板（面向普通用户，不暴露路径 / Session ID）──
const memoryBtn = document.getElementById('memory-btn');
const memoryModal = document.getElementById('memory-modal');
const memoryCloseBtn = document.getElementById('memory-close');
const memoryListEl = document.getElementById('memory-list');
const memoryEmptyEl = document.getElementById('memory-empty');
const memoryEnabledEl = document.getElementById('memory-enabled');
const memoryHintEl = document.getElementById('memory-hint');
const memoryClearProjectBtn = document.getElementById('memory-clear-project');
const memoryClearAllBtn = document.getElementById('memory-clear-all');
let memoryItems = [];

function sendMemoryRequest(type, data) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    addError('连接已断开，请稍后重试');
    return false;
  }
  ws.send(JSON.stringify({ type, data: data || {} }));
  return true;
}

function openMemoryPanel() {
  memoryModal.classList.add('show');
  memoryHintEl.textContent = '正在读取…';
  sendMemoryRequest('memory_list');
}

function closeMemoryPanel() {
  memoryModal.classList.remove('show');
}

// 全部用 textContent 写入，记忆内容里的 HTML 不会被执行
function renderMemoryState(data) {
  const enabled = data.enabled !== false;
  memoryEnabledEl.checked = enabled;
  memoryHintEl.textContent = enabled
    ? '开启后 AI 外脑会把值得记住的偏好保存下来；关闭后不再保存，也不再使用已有记忆。'
    : '记忆功能已关闭：不会再保存新记忆，也不会使用已有记忆（已保存的记忆仍保留，可随时删除）。';

  memoryItems = Array.isArray(data.items) ? data.items : [];
  memoryListEl.innerHTML = '';
  memoryEmptyEl.style.display = memoryItems.length ? 'none' : 'block';

  memoryItems.forEach(item => {
    const row = document.createElement('div');
    row.className = 'memory-item';

    const main = document.createElement('div');
    main.className = 'memory-item-main';

    const title = document.createElement('div');
    title.className = 'memory-item-title';
    title.textContent = item.title || '未命名记忆';
    main.appendChild(title);

    const preview = document.createElement('div');
    preview.className = 'memory-item-preview';
    preview.textContent = item.preview || item.description || '（暂无内容预览）';
    main.appendChild(preview);

    const meta = document.createElement('div');
    meta.className = 'memory-item-meta';
    [item.scope_label, item.source_label].forEach(text => {
      if (!text) return;
      const tag = document.createElement('span');
      tag.className = 'memory-tag';
      tag.textContent = text;
      meta.appendChild(tag);
    });
    main.appendChild(meta);
    row.appendChild(main);

    const del = document.createElement('button');
    del.className = 'memory-item-del';
    del.type = 'button';
    del.textContent = '删除';
    del.addEventListener('click', async () => {
      del.disabled = true;
      const ok = await showConfirm(
        '删除这条记忆？',
        '这条记忆会从 AI 外脑中移除，不会影响你的聊天记录。',
        '删除'
      );
      del.disabled = false;
      if (!ok) return;
      sendMemoryRequest('memory_delete', { id: item.id });
    });
    row.appendChild(del);

    memoryListEl.appendChild(row);
  });

  memoryClearProjectBtn.disabled = !memoryItems.some(item => item.scope === 'project');
  memoryClearAllBtn.disabled = memoryItems.length === 0;
  if (data.error) addError(data.error);
}

if (memoryBtn) {
  memoryBtn.addEventListener('click', () => {
    if (memoryModal.classList.contains('show')) closeMemoryPanel();
    else openMemoryPanel();
  });
  memoryCloseBtn.addEventListener('click', closeMemoryPanel);
  memoryModal.addEventListener('click', (e) => {
    if (e.target === memoryModal) closeMemoryPanel();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && memoryModal.classList.contains('show')) closeMemoryPanel();
  });

  memoryEnabledEl.addEventListener('change', () => {
    sendMemoryRequest('memory_set_enabled', { enabled: memoryEnabledEl.checked });
  });

  memoryClearProjectBtn.addEventListener('click', async () => {
    const ok = await showConfirm(
      '清空项目记忆？',
      '将删除当前项目里保存的长期记忆，不会删除聊天记录。',
      '清空'
    );
    if (!ok) return;
    sendMemoryRequest('memory_clear', { scope: 'project' });
  });

  memoryClearAllBtn.addEventListener('click', async () => {
    const ok = await showConfirm(
      '清空全部记忆？',
      '将删除所有保存的长期记忆（包括跨项目的偏好），不会删除聊天记录。',
      '清空全部'
    );
    if (!ok) return;
    sendMemoryRequest('memory_clear', { scope: 'all' });
  });
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && confirmModal.classList.contains('show')) closeConfirm(false);
});

// ── 图片/文件上传 ──
function makeFileChip(icon, name, onRemove) {
  const chip = document.createElement('div');
  chip.className = 'file-chip';
  const iconEl = document.createElement('span');
  iconEl.className = 'file-chip-icon';
  iconEl.textContent = icon;
  const nameEl = document.createElement('span');
  nameEl.className = 'file-chip-name';
  nameEl.textContent = name;
  chip.appendChild(iconEl);
  chip.appendChild(nameEl);
  if (onRemove) {
    const removeBtn = document.createElement('button');
    removeBtn.className = 'file-chip-remove';
    removeBtn.type = 'button';
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', onRemove);
    chip.appendChild(removeBtn);
  }
  return chip;
}

function renderAttachmentPreview() {
  imagePreviewRow.innerHTML = '';
  pendingImages.forEach((img, idx) => {
    const item = document.createElement('div');
    item.className = 'image-preview-item';
    const thumb = document.createElement('img');
    thumb.src = 'data:' + img.media_type + ';base64,' + img.data;
    const removeBtn = document.createElement('button');
    removeBtn.className = 'image-preview-remove';
    removeBtn.type = 'button';
    removeBtn.textContent = '×';
    removeBtn.addEventListener('click', () => {
      pendingImages.splice(idx, 1);
      renderAttachmentPreview();
    });
    item.appendChild(thumb);
    item.appendChild(removeBtn);
    imagePreviewRow.appendChild(item);
  });
  pendingDocuments.forEach((doc, idx) => {
    imagePreviewRow.appendChild(makeFileChip('📕', doc.name, () => {
      pendingDocuments.splice(idx, 1);
      renderAttachmentPreview();
    }));
  });
  pendingTextFiles.forEach((tf, idx) => {
    imagePreviewRow.appendChild(makeFileChip('📄', tf.name, () => {
      pendingTextFiles.splice(idx, 1);
      renderAttachmentPreview();
    }));
  });
  imagePreviewRow.classList.toggle('show', attachmentCount() > 0);
}

function handleFiles(files) {
  for (const file of files) {
    if (attachmentCount() >= MAX_ATTACHMENTS_PER_MESSAGE) {
      addSystem('最多同时上传 ' + MAX_ATTACHMENTS_PER_MESSAGE + ' 个文件/图片');
      break;
    }
    const kind = classifyFile(file);

    if (kind === 'image') {
      if (!visionEnabled) { addSystem('当前模型不支持识图，图片已忽略：' + file.name); continue; }
      if (file.size > MAX_IMAGE_BYTES) { addSystem('图片过大（超过 5MB）：' + file.name); continue; }
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result;
        const commaIdx = result.indexOf(',');
        const meta = result.substring(5, result.indexOf(';'));
        pendingImages.push({ media_type: meta, data: result.substring(commaIdx + 1) });
        renderAttachmentPreview();
      };
      reader.readAsDataURL(file);

    } else if (kind === 'pdf') {
      if (!visionEnabled) { addSystem('当前模型不支持解析 PDF，已忽略：' + file.name); continue; }
      if (file.size > MAX_DOCUMENT_BYTES) { addSystem('文件过大（超过 20MB）：' + file.name); continue; }
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result;
        const commaIdx = result.indexOf(',');
        const meta = result.substring(5, result.indexOf(';'));
        pendingDocuments.push({ media_type: meta, data: result.substring(commaIdx + 1), name: file.name });
        renderAttachmentPreview();
      };
      reader.readAsDataURL(file);

    } else if (kind === 'text') {
      if (file.size > MAX_TEXT_FILE_BYTES) { addSystem('文件过大（超过 300KB）：' + file.name); continue; }
      const reader = new FileReader();
      reader.onload = () => {
        pendingTextFiles.push({ name: file.name, content: reader.result });
        renderAttachmentPreview();
      };
      reader.readAsText(file);

    } else {
      addSystem('不支持的文件类型：' + (file.type || file.name));
    }
  }
}

uploadBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  handleFiles(fileInput.files);
  fileInput.value = '';
});
inputEl.addEventListener('paste', (e) => {
  if (!visionEnabled || !e.clipboardData) return;
  const items = Array.from(e.clipboardData.items).filter(i => i.type && i.type.startsWith('image/'));
  if (!items.length) return;
  e.preventDefault();
  handleFiles(items.map(i => i.getAsFile()).filter(Boolean));
});

// ── 拖拽上传 ──
let dragCounter = 0;
function isFileDrag(e) {
  return e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
}
document.addEventListener('dragenter', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dragCounter++;
  dropOverlay.classList.add('show');
});
document.addEventListener('dragover', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
});
document.addEventListener('dragleave', (e) => {
  if (!isFileDrag(e)) return;
  dragCounter = Math.max(0, dragCounter - 1);
  if (dragCounter === 0) dropOverlay.classList.remove('show');
});
document.addEventListener('drop', (e) => {
  if (!isFileDrag(e)) return;
  e.preventDefault();
  dragCounter = 0;
  dropOverlay.classList.remove('show');
  if (e.dataTransfer.files && e.dataTransfer.files.length) {
    handleFiles(e.dataTransfer.files);
  }
});

// ── 功能栏：能力选择 + 生成任务卡片 ──
// 原则：
//  1. 哪些功能"能用"完全以服务端下发的 capabilities 为准，前端不自行点亮；
//  2. 切换功能只改输入框提示与发送路径，绝不偷偷发起模型调用；
//  3. 任务卡片只显示真实状态（提交中/排队中/生成中 + 真实已等待秒数），不编造百分比。
var CAP_AGENT_SIDE = ['chat', 'writing', 'web_search', 'web_fetch', 'vision_understanding'];
var CAP_PRIMARY = ['chat', 'image_generate', 'video_generate', 'writing', 'audio_transcribe'];
var CAP_MORE_ORDER = ['audio_speech', 'web_search', 'web_fetch', 'realtime_voice', 'vision_understanding', 'image_search', 'text_image_search', 'code_interpreter'];
var CAP_RUNNING_STATES = ['submitting', 'queued', 'running', 'downloading'];

var CAP_ICONS = {
  chat: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M21 11.5a8.5 8.5 0 01-12.3 7.6L4 20.5l1.4-4.6A8.5 8.5 0 1121 11.5z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  image_generate: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="16" rx="3" stroke="currentColor" stroke-width="1.8"/><circle cx="8.5" cy="9.5" r="1.6" fill="currentColor"/><path d="M4 17l5-4 4 3 3-2 4 3" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  video_generate: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="13" height="14" rx="3" stroke="currentColor" stroke-width="1.8"/><path d="M16 10.5l5-3v9l-5-3" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  writing: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 20h16M6 16l9.5-9.5a2 2 0 012.8 2.8L8.8 18.8 5 20l1-3.9z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>',
  audio_transcribe: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 14.5a2.6 2.6 0 002.6-2.6V6a2.6 2.6 0 10-5.2 0v5.9A2.6 2.6 0 0012 14.5z" stroke="currentColor" stroke-width="1.8"/><path d="M6 11.6a6 6 0 0012 0M12 18v3" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  audio_speech: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  web_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><circle cx="11" cy="11" r="6.5" stroke="currentColor" stroke-width="1.8"/><path d="M16 16l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  web_fetch: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="8.5" stroke="currentColor" stroke-width="1.8"/><path d="M3.5 12h17M12 3.5c2.5 2.4 2.5 14.6 0 17M12 3.5c-2.5 2.4-2.5 14.6 0 17" stroke="currentColor" stroke-width="1.5"/></svg>',
  realtime_voice: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M12 3v18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M7 8v8M17 8v8M3 11v2M21 11v2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  vision_understanding: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2.6" stroke="currentColor" stroke-width="1.8"/></svg>',
  image_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><rect x="4" y="4" width="16" height="16" rx="3" stroke="currentColor" stroke-width="1.8"/><circle cx="10" cy="10" r="2" stroke="currentColor" stroke-width="1.6"/><path d="M15 15l4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  text_image_search: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M4 7h16M4 12h10M4 17h7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><circle cx="17.5" cy="15.5" r="3" stroke="currentColor" stroke-width="1.6"/><path d="M19.8 17.8l2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  code_interpreter: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none"><path d="M9 8l-4 4 4 4M15 8l4 4-4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
};
var CAP_CARET = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
var CAP_FALLBACK_LABELS = {
  chat: '对话', writing: '帮我写作', image_generate: '图片生成', video_generate: '视频生成',
  audio_transcribe: '录音转写', audio_speech: '语音生成', realtime_voice: '实时语音',
  vision_understanding: '看图理解', web_search: '联网搜索', web_fetch: '网页读取',
  image_search: '以图搜图', text_image_search: '文搜图', code_interpreter: '代码解释器'
};
var CAP_DEFAULT_PLACEHOLDER = '输入消息，AI 外脑会帮你完成任务…';

var capState = { active: 'chat', items: {}, loaded: false, provider: null };
var capTasks = {};    // task_id -> DOM
var capUnbound = [];  // 已本地渲染、尚未拿到 task_id 的卡片

function capInfo(cap) {
  return capState.items[cap] || null;
}

function capLabel(cap) {
  var info = capInfo(cap);
  return (info && info.label) || CAP_FALLBACK_LABELS[cap] || cap;
}

function capEnabled(cap) {
  if (cap === 'chat') return true;
  var info = capInfo(cap);
  // 服务端还没下发状态时，一律按不可用处理：宁可不点，也不假装能用
  return !!(info && info.enabled);
}

function capReason(cap) {
  var info = capInfo(cap);
  if (capEnabled(cap)) return '';
  return (info && info.reason) || '正在检查这个功能是否可用…';
}

function capPlaceholder(cap) {
  var info = capInfo(cap);
  if (info && info.placeholder) return info.placeholder;
  return CAP_DEFAULT_PLACEHOLDER;
}

function makeCapChip(cap, inMenu) {
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = inMenu ? 'cap-menu-item' : 'cap-chip';
  btn.dataset.cap = cap;
  btn.innerHTML = (CAP_ICONS[cap] || '') + '<span class="cap-menu-label">' + escapeHtml(capLabel(cap)) + '</span>';
  btn.addEventListener('click', function () { setActiveCapability(cap); });
  return btn;
}

function renderCapabilityBar() {
  var bar = document.getElementById('cap-bar');
  var scroller = document.getElementById('cap-bar-scroll');
  if (!bar || !scroller) return;
  scroller.innerHTML = '';
  CAP_PRIMARY.forEach(function (cap) { scroller.appendChild(makeCapChip(cap, false)); });

  var more = document.createElement('div');
  more.className = 'cap-more';
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cap-chip';
  btn.id = 'cap-more-btn';
  btn.setAttribute('aria-haspopup', 'true');
  btn.setAttribute('aria-expanded', 'false');
  btn.innerHTML = '<span>更多</span>' + CAP_CARET;
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    toggleCapMenu();
  });
  more.appendChild(btn);

  var menu = document.createElement('div');
  menu.className = 'cap-menu';
  menu.id = 'cap-menu';
  CAP_MORE_ORDER.forEach(function (cap) {
    var item = makeCapChip(cap, true);
    var reason = document.createElement('span');
    reason.className = 'cap-menu-reason';
    reason.dataset.capReason = cap;
    item.appendChild(reason);
    menu.appendChild(item);
  });
  more.appendChild(menu);
  // 当前模型提示（例如"DeepSeek · 仅对话"）
  var tag = document.createElement('span');
  tag.className = 'cap-model-tag';
  tag.id = 'cap-model-tag';
  tag.style.display = 'none';
  bar.appendChild(tag);
  // 重新渲染时先摘掉旧的「更多」容器，避免出现两个
  var stale = bar.querySelector('.cap-more');
  if (stale) stale.remove();
  bar.appendChild(more);
  syncCapabilityBar();
}

function syncCapabilityBar() {
  var active = capState.active;
  document.querySelectorAll('#cap-bar .cap-chip, #cap-menu .cap-menu-item').forEach(function (el) {
    var cap = el.dataset.cap;
    if (!cap) return;
    var enabled = capEnabled(cap);
    el.disabled = !enabled;
    el.classList.toggle('active', cap === active);
    el.setAttribute('aria-pressed', cap === active ? 'true' : 'false');
    var reason = capReason(cap);
    el.title = reason || '';
    var reasonEl = el.querySelector('[data-cap-reason]');
    if (reasonEl) {
      reasonEl.textContent = enabled ? '' : reason;
      reasonEl.style.display = enabled ? 'none' : 'block';
    }
  });
  inputEl.placeholder = capPlaceholder(active);
  // 当前模型不具备生成能力时，明确告诉用户"只能对话"
  var tag = document.getElementById('cap-model-tag');
  if (tag) {
    var prov = capState.provider || {};
    if (prov.media_capable === false) {
      tag.textContent = (prov.label || '当前模型') + ' · 仅对话';
      tag.title = prov.hint || '当前模型不具备生成图片/视频/语音的能力';
      tag.style.display = '';
    } else {
      tag.textContent = '';
      tag.style.display = 'none';
    }
  }
}

function toggleCapMenu() {
  var menu = document.getElementById('cap-menu');
  if (!menu) return;
  var show = !menu.classList.contains('show');
  menu.classList.toggle('show', show);
  var btn = document.getElementById('cap-more-btn');
  if (btn) btn.setAttribute('aria-expanded', show ? 'true' : 'false');
}

function closeCapMenu() {
  var menu = document.getElementById('cap-menu');
  if (menu) menu.classList.remove('show');
  var btn = document.getElementById('cap-more-btn');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

document.addEventListener('click', function (e) {
  if (!e.target.closest || !e.target.closest('.cap-more')) closeCapMenu();
});

// 切换功能：只改变"待发送请求走哪条路"，不发起任何模型调用，也不清空输入
function setActiveCapability(cap) {
  if (!cap || cap === capState.active) { closeCapMenu(); return; }
  if (!capEnabled(cap)) {
    addSystem('「' + capLabel(cap) + '」暂时不可用：' + capReason(cap));
    closeCapMenu();
    return;
  }
  capState.active = cap;
  closeCapMenu();
  syncCapabilityBar();
  inputEl.focus();
}

function applyCapabilities(data) {
  var items = (data && data.capabilities) || [];
  capState.items = {};
  items.forEach(function (item) {
    if (item && item.capability) capState.items[item.capability] = item;
  });
  capState.provider = (data && data.active_provider) || null;
  capState.loaded = items.length > 0;
  // 当前选中的能力如果变不可用了（例如套餐变化），退回对话
  if (!capEnabled(capState.active)) capState.active = 'chat';
  renderCapabilityBar();
}

// ── 任务卡片 ──
// 阶段模型：每个生成能力有自己真实的阶段序列，进度条只表达"还在动"，
// 不编百分比（云端接口本来就不给）。
var CAP_STEPS = {
  image_generate: [
    { key: 'submitting', label: '提交' },
    { key: 'running', label: '生成' },
    { key: 'downloading', label: '下载' }
  ],
  audio_speech: [
    { key: 'submitting', label: '提交' },
    { key: 'running', label: '合成' },
    { key: 'downloading', label: '下载' }
  ],
  video_generate: [
    { key: 'submitting', label: '提交' },
    { key: 'queued', label: '排队' },
    { key: 'running', label: '生成' },
    { key: 'downloading', label: '下载' }
  ]
};
var CAP_ETA = {
  image_generate: '同类任务通常需要 20–60 秒',
  audio_speech: '通常几秒内完成',
  video_generate: '同类任务通常需要 2–5 分钟'
};

function capStepsFor(capability) {
  return CAP_STEPS[capability] || CAP_STEPS.image_generate;
}

function capStatusText(status, cap) {
  switch (status) {
    case 'submitting': return '正在提交…';
    case 'queued': return '云端排队中…';
    case 'running': return '正在生成…';
    case 'downloading': return '正在下载结果…';
    case 'success': return '已完成';
    case 'failed': return '生成失败';
    case 'timeout': return '等待超时，云端可能仍在生成';
    case 'cancelled':
      return (cap && cap.cancel_mode === 'salvaged') ? '已停止 · 结果已保留' : '已取消';
    default: return '处理中…';
  }
}

function capHumanSeconds(secs) {
  if (secs < 60) return secs + ' 秒';
  var m = Math.floor(secs / 60);
  var s = secs % 60;
  return s ? m + ' 分 ' + s + ' 秒' : m + ' 分';
}

// 计时用 mm:ss：等宽数字，秒数跳动时宽度不变
function capClock(secs) {
  var total = Math.max(0, Math.round(secs || 0));
  var m = Math.floor(total / 60);
  var s = total % 60;
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

function capIsRunning(status) {
  return CAP_RUNNING_STATES.indexOf(status) >= 0;
}

function renderCapSteps(el, cap) {
  var box = el.querySelector('.cap-steps');
  if (!box) return;
  var steps = capStepsFor(cap.capability);
  var status = cap.status || 'submitting';
  // 停止但结果已被接住 = 阶段其实走完了，不该打红叉
  var salvaged = status === 'cancelled' && cap.cancel_mode === 'salvaged';
  var finished = status === 'success' || salvaged;
  var bad = status === 'failed' || status === 'timeout'
    || (status === 'cancelled' && !salvaged);
  // 当前索引：下载/生成这些状态可能被跳过，取"最后到达的阶段"
  var current = steps.findIndex(function (st) { return st.key === status; });
  if (current < 0) current = 0;
  if (finished) current = steps.length;
  else if (bad) current = Math.max(0, cap.__lastStep || 0);
  else cap.__lastStep = current;

  var html = '';
  steps.forEach(function (st, i) {
    var cls = 'cap-step';
    if (finished || i < current) cls += ' done';
    else if (i === current) cls += bad ? ' fail' : ' active';
    html += '<span class="' + cls + '" title="' + escapeHtml(st.label) + '">' +
      '<span class="dot">' + (finished || i < current ? '✓' : (bad && i === current ? '✕' : '')) + '</span>' +
      '<span class="label">' + escapeHtml(st.label) + '</span></span>';
    if (i < steps.length - 1) {
      html += '<span class="cap-step-line' + (finished || i < current ? ' done' : '') + '"></span>';
    }
  });
  box.innerHTML = html;
}

function renderCapSkeleton(el, cap) {
  var body = el.querySelector('.cap-task-body');
  if (!body || !capIsRunning(cap.status)) return;
  if (body.querySelector('.cap-skeleton-wrap')) return;   // 已在，不重建（避免闪烁）
  var kind = cap.capability === 'video_generate' ? 'k-video'
    : cap.capability === 'audio_speech' ? 'k-audio' : 'k-image';
  var wrap = document.createElement('div');
  wrap.className = 'cap-skeleton-wrap';
  var box = document.createElement('div');
  box.className = 'cap-skeleton ' + kind;
  if (kind === 'k-audio') {
    var wave = document.createElement('div');
    wave.className = 'cap-wave';
    for (var i = 0; i < 9; i += 1) {
      var bar = document.createElement('i');
      bar.style.animationDelay = (i * 0.09).toFixed(2) + 's';
      wave.appendChild(bar);
    }
    box.appendChild(wave);
  }
  var label = document.createElement('div');
  label.className = 'cap-skeleton-label';
  label.textContent = CAP_ETA[cap.capability] || '';
  box.appendChild(label);
  wrap.appendChild(box);
  body.appendChild(wrap);
}

function createCapTaskCard(info) {
  hideEmptyState();
  var el = document.createElement('div');
  el.className = 'cap-task';
  el.innerHTML =
    '<div class="cap-progress-bar"><i></i></div>' +
    '<div class="cap-task-head">' +
      '<span class="cap-task-ico"></span>' +
      '<span class="cap-spinner"></span>' +
      '<span class="cap-task-title"></span>' +
      '<span class="cap-task-status"></span>' +
      '<span class="cap-task-timer"></span>' +
    '</div>' +
    '<div class="cap-steps"></div>' +
    '<div class="cap-task-prompt"></div>' +
    '<div class="cap-task-body"></div>' +
    '<div class="cap-task-actions"></div>' +
    '<div class="cap-dev"></div>';
  el.querySelector('.cap-task-title').textContent = capLabel(info.capability);
  el.querySelector('.cap-task-ico').innerHTML = CAP_ICONS[info.capability] || '';
  el.querySelector('.cap-task-prompt').textContent = info.prompt || '';
  el.__cap = {
    capability: info.capability,
    prompt: info.prompt || '',
    model: info.model || '',
    status: info.status || 'submitting',
    request_id: info.request_id || ''
  };
  messagesEl.appendChild(el);
  if (info.request_id) el.dataset.requestId = info.request_id;
  // 计时从"用户点发送"这一刻开始：只靠服务端事件计时会在两次事件之间停住
  el.__startedAt = Date.now();
  updateCapTask(el, el.__cap);
  scrollToBottom();
  return el;
}

function capElapsed(el) {
  // 本地墙钟优先：服务端的 elapsed 只是快照，两次进度事件之间不会自己走
  if (el.__startedAt) return (Date.now() - el.__startedAt) / 1000;
  var cap = el.__cap || {};
  if (typeof cap.elapsed === 'number') return cap.elapsed;
  return null;
}

function startCapTimer(el) {
  if (el.__timer) return;
  if (!el.__startedAt) {
    var cap = el.__cap || {};
    el.__startedAt = cap.created_at ? cap.created_at * 1000 : Date.now();
  }
  el.__timer = setInterval(function () {
    var statusEl = el.querySelector('.cap-task-status');
    var timerEl = el.querySelector('.cap-task-timer');
    var cap = el.__cap || {};
    var secs = capElapsed(el);
    if (statusEl) statusEl.textContent = capStatusText(cap.status, cap);
    if (timerEl && capIsRunning(cap.status)) timerEl.textContent = capClock(secs);
  }, 1000);
}

function stopCapTimer(el) {
  if (el.__timer) {
    clearInterval(el.__timer);
    el.__timer = null;
  }
}

function updateCapTask(el, data) {
  var cap = Object.assign(el.__cap || {}, data || {});
  el.__cap = cap;
  if (data && data.created_at && !el.__startedAt) el.__startedAt = data.created_at * 1000;
  if (cap.request_id) el.dataset.requestId = cap.request_id;
  if (cap.task_id) el.dataset.taskId = cap.task_id;

  var status = cap.status || 'submitting';
  var running = capIsRunning(status);
  var finished = status === 'success';
  el.classList.toggle('is-failed', status === 'failed' || status === 'timeout');
  el.classList.toggle('is-done', finished);
  el.dataset.state = status;

  var spinner = el.querySelector('.cap-spinner');
  if (spinner) spinner.style.display = running ? '' : 'none';
  var statusEl = el.querySelector('.cap-task-status');
  if (statusEl) statusEl.textContent = capStatusText(status, cap);
  var timerEl = el.querySelector('.cap-task-timer');
  if (timerEl) {
    if (running) {
      timerEl.textContent = capClock(capElapsed(el));
    } else if (finished && typeof cap.elapsed === 'number') {
      timerEl.textContent = '耗时 ' + capClock(cap.elapsed);
    } else {
      timerEl.textContent = '';
    }
  }

  renderCapSteps(el, cap);
  if (running) {
    renderCapSkeleton(el, cap);
    startCapTimer(el);
  } else {
    stopCapTimer(el);
  }

  el.classList.toggle('is-cancelled', status === 'cancelled');
  if (status === 'success') renderCapTaskResult(el, cap);
  else if (status === 'failed') renderCapTaskError(el, cap);
  else if (status === 'timeout') renderCapTaskTimeout(el, cap);
  else if (status === 'cancelled') renderCapTaskCancelled(el, cap);
  renderCapActions(el, cap);

  renderCapDev(el, cap);
  scrollToBottom();
}

function renderCapDev(el, cap) {
  var dev = el.querySelector('.cap-dev');
  if (!dev) return;
  var lines = [];
  if (cap.task_id) lines.push('任务: ' + cap.task_id);
  if (cap.provider_task_id) lines.push('云端任务: ' + cap.provider_task_id);
  if (cap.model) lines.push('模型: ' + cap.model);
  if (cap.capability) lines.push('能力: ' + cap.capability);
  if (cap.status) lines.push('状态: ' + cap.status);
  if (cap.cancel_mode) lines.push('停止方式: ' + cap.cancel_mode);
  if (typeof cap.elapsed === 'number') lines.push('耗时: ' + cap.elapsed + 's');
  if (cap.error_code) lines.push('错误码: ' + cap.error_code);
  if (cap.detail && Object.keys(cap.detail).length) {
    try { lines.push('详情: ' + JSON.stringify(cap.detail, null, 2)); } catch (e) { /* 忽略 */ }
  }
  dev.textContent = lines.join('\n');
}

function clearCapBody(el) {
  var body = el.querySelector('.cap-task-body');
  if (body) body.innerHTML = '';
  return body;
}

function renderCapTaskResult(el, cap) {
  var body = clearCapBody(el);
  if (!body) return;
  var files = cap.artifacts || [];
  if (!files.length) {
    var empty = document.createElement('div');
    empty.className = 'cap-task-error';
    empty.textContent = '任务完成了，但没有拿到可用的文件';
    body.appendChild(empty);
    return;
  }
  var grid = document.createElement('div');
  grid.className = 'cap-media-grid';
  var images = 0;
  files.forEach(function (f) {
    if (f.kind === 'image' && f.url) {
      var img = document.createElement('img');
      img.src = f.preview_url || f.url;
      img.alt = f.name || '生成的图片';
      img.loading = 'lazy';
      img.title = '点击放大';
      img.addEventListener('click', function () { openLightbox(img.src); });
      grid.appendChild(img);
      images += 1;
    }
  });
  if (images) body.appendChild(grid);
  files.forEach(function (f) {
    if (f.kind === 'video' && f.url) {
      var v = document.createElement('video');
      v.controls = true;
      v.preload = 'metadata';
      v.src = f.url;
      body.appendChild(v);
    } else if (f.kind === 'audio' && f.url) {
      var a = document.createElement('audio');
      a.controls = true;
      a.preload = 'metadata';
      a.src = f.url;
      body.appendChild(a);
    }
    var meta = document.createElement('div');
    meta.className = 'cap-media-meta';
    var name = document.createElement('span');
    name.textContent = f.name || '未命名文件';
    var size = document.createElement('span');
    size.textContent = formatBytes(f.size || 0);
    meta.appendChild(name);
    meta.appendChild(size);
    if (f.url) {
      var dl = document.createElement('a');
      dl.className = 'cap-task-dl';
      dl.href = f.url;
      dl.setAttribute('download', f.name || '');
      dl.textContent = '下载';
      meta.appendChild(dl);
    } else {
      var gone = document.createElement('span');
      gone.textContent = '文件已不可用';
      meta.appendChild(gone);
    }
    body.appendChild(meta);
  });
  if (cap.prompt) {
    // 结果已出，提示语冗余，保留在 dev 面板即可
  }
}

function renderCapTaskError(el, cap) {
  var body = clearCapBody(el);
  if (!body) return;
  var box = document.createElement('div');
  box.className = 'cap-task-error';
  box.textContent = '✖ ' + (cap.error_message || '生成失败，请稍后重试');
  body.appendChild(box);
  // 被内容审核拦截：重试没用，要告诉用户改什么
  if (cap.error_code === 'content_blocked') {
    var tip = document.createElement('div');
    tip.className = 'cap-task-tip';
    tip.textContent = '提示：避开具体的影视/动漫角色、明星或品牌名，用外形描述代替'
      + '（例如把角色名换成「一只蓝色的小狗」），通常就能正常生成。';
    body.appendChild(tip);
  }
}

function renderCapTaskTimeout(el, cap) {
  var body = clearCapBody(el);
  if (!body) return;
  var box = document.createElement('div');
  box.className = 'cap-task-error';
  box.textContent = '⏱ ' + (cap.error_message || '还在云端生成，等待超时了');
  body.appendChild(box);
}

function renderCapTaskCancelled(el, cap) {
  var files = cap.artifacts || [];
  if (files.length) {
    // 停止时云端其实已经算完了：这份钱已经花掉，结果必须给用户留着
    renderCapTaskResult(el, cap);
    var body = el.querySelector('.cap-task-body');
    if (!body) return;
    var kept = document.createElement('div');
    kept.className = 'cap-task-note';
    kept.textContent = '⏹ ' + (cap.error_message || '已停止，结果已保留。');
    body.appendChild(kept);
    return;
  }
  var cleared = clearCapBody(el);
  if (!cleared) return;
  var note = document.createElement('div');
  note.className = 'cap-task-note';
  note.textContent = '⏹ ' + (cap.error_message || '已停止。');
  cleared.appendChild(note);
}

// 卡片操作区：运行中给「停止」，结束后给「重新生成 / 编辑提示词」
function renderCapActions(el, cap) {
  var box = el.querySelector('.cap-task-actions');
  if (!box) return;
  box.innerHTML = '';
  var status = cap.status || 'submitting';
  var running = capIsRunning(status);

  if (running) {
    var stop = document.createElement('button');
    stop.type = 'button';
    stop.className = 'cap-task-btn danger';
    stop.textContent = '停止';
    stop.title = '停止生成：排队中的云端任务会被撤销；已经开始生成的无法撤销';
    if (!cap.task_id) stop.disabled = true;   // 还没拿到任务号，点了也没意义
    stop.addEventListener('click', function () {
      if (!cap.task_id) return;
      stop.disabled = true;
      stop.textContent = '正在停止…';
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'capability_cancel', data: { task_id: cap.task_id } }));
      }
    });
    box.appendChild(stop);
    return;
  }

  if (status === 'timeout' && cap.provider_task_id) {
    var resume = document.createElement('button');
    resume.type = 'button';
    resume.className = 'cap-task-btn';
    resume.textContent = '继续查询';
    resume.addEventListener('click', function () {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      resume.disabled = true;
      resume.textContent = '正在查询…';
      ws.send(JSON.stringify({ type: 'capability_resume', data: { task_id: cap.task_id } }));
    });
    box.appendChild(resume);
  }

  var edit = document.createElement('button');
  edit.type = 'button';
  edit.className = 'cap-task-btn';
  edit.textContent = '编辑提示词';
  edit.title = '把描述填回输入框，直接发送即是重新生成，改一改再发也可以';
  edit.addEventListener('click', function () { editCapTaskPrompt(el); });
  box.appendChild(edit);
}

// 把这张卡片的描述填回输入框（运行中的先停掉，避免白等）
function editCapTaskPrompt(el) {
  var cap = el.__cap || {};
  if (!cap.prompt) return;
  var stopped = false;
  if (capIsRunning(cap.status) && cap.task_id && ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'capability_cancel', data: { task_id: cap.task_id } }));
    stopped = true;
  }
  if (cap.capability && capEnabled(cap.capability)) setActiveCapability(cap.capability);
  inputEl.value = cap.prompt;
  inputEl.style.height = 'auto';
  inputEl.dispatchEvent(new Event('input'));
  inputEl.focus();
  // 别让用户以为"编辑一下"不花钱：原任务的取消结果会显示在卡片上
  addSystem(
    '已把提示词填回输入框，改完直接发送即可重新生成。'
    + (stopped ? '（原任务已请求停止，若已提交给模型服务，可能仍会计费）' : '')
  );
}

function findCapTaskEl(data) {
  if (data && data.task_id && capTasks[data.task_id]) return capTasks[data.task_id];
  // 服务端先广播 started 再回 ack：用 request_id / capability + prompt 把本地卡片绑到真实 task_id 上
  var match = null;
  var i;
  if (data && data.request_id) {
    for (i = 0; i < capUnbound.length; i += 1) {
      if ((capUnbound[i].__cap || {}).request_id === data.request_id) { match = capUnbound[i]; break; }
    }
  }
  if (!match) {
    for (i = 0; i < capUnbound.length; i += 1) {
      var cap = capUnbound[i].__cap || {};
      if (cap.capability === data.capability && (cap.prompt || '') === (data.prompt || '')) { match = capUnbound[i]; break; }
    }
  }
  if (!match) return null;
  capUnbound = capUnbound.filter(function (item) { return item !== match; });
  if (data.task_id) capTasks[data.task_id] = match;
  return match;
}

function handleCapabilityEvent(data, eventName) {
  if (!data || typeof data !== 'object') return;
  var el = findCapTaskEl(data);
  if (!el) {
    if (eventName === 'capability_progress') return;  // 进度事件不新建卡片
    el = createCapTaskCard({
      capability: data.capability,
      model: data.model,
      prompt: data.prompt,
      status: data.status || 'submitting',
      created_at: data.created_at,
      request_id: data.request_id
    });
  }
  updateCapTask(el, data);
}

function capSubmitInFlight(capability, prompt) {
  for (var id in capTasks) {
    var cap = capTasks[id].__cap || {};
    if (cap.capability === capability && (cap.prompt || '') === prompt && capIsRunning(cap.status)) return true;
  }
  for (var i = 0; i < capUnbound.length; i += 1) {
    var pending = capUnbound[i].__cap || {};
    if (pending.capability === capability && (pending.prompt || '') === prompt) return true;
  }
  return false;
}

// 真正提交一个生成任务（发送与「重新生成」共用同一条路径）
function dispatchCapability(capability, prompt, model, beforeCreate) {
  var info = capInfo(capability);
  if (!info || !info.enabled) {
    addSystem('「' + capLabel(capability) + '」暂时不可用：' + capReason(capability));
    return null;
  }
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    addError('连接已断开，无法提交生成任务');
    return null;
  }
  if (capSubmitInFlight(capability, prompt)) {
    addSystem('这个生成任务已经在进行中了，请等它完成再提交。');
    return null;
  }
  if (typeof beforeCreate === 'function') beforeCreate();
  var requestId = 'req_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
  var card = createCapTaskCard({
    capability: capability,
    model: info.default_model,
    prompt: prompt,
    status: 'submitting',
    request_id: requestId
  });
  capUnbound.push(card);
  ws.send(JSON.stringify({
    type: 'capability_request',
    data: {
      request_id: requestId,
      capability: capability,
      prompt: prompt,
      model: model || '',
      size: '',
      voice: ''
    }
  }));
  return card;
}

// 生成类功能：走 capability_request，由服务端校验 + 后台执行
function submitCapabilityRequest() {
  var cap = capState.active;
  var info = capInfo(cap);
  if (!info || !info.enabled) {
    addSystem('「' + capLabel(cap) + '」暂时不可用：' + capReason(cap));
    return;
  }
  var text = inputEl.value.trim();
  if (!text) return;
  if ((pendingImages.length || pendingDocuments.length) && cap === 'image_generate') {
    addSystem('提示：本地附件暂不会作为参考图使用（需要公网图片链接），本次只按文字描述生成。');
  }
  // 气泡与卡片互相引用：气泡上的"编辑"要能停掉这张卡片的旧任务
  var genTask = { capability: cap, prompt: text, taskEl: null };
  var card = dispatchCapability(cap, text, '', function () {
    addUser(text, pendingImages, pendingDocuments, { genTask: genTask });
  });
  if (!card) return;
  genTask.taskEl = card;
  inputEl.value = '';
  inputEl.style.height = 'auto';
  pendingImages = [];
  pendingDocuments = [];
  pendingTextFiles = [];
  renderAttachmentPreview();
  stopSpeaking();
  stopMic();
}

// ── 输入处理 ──
function sendMessage() {
  if (slashMenu.classList.contains('show')) hideSlashMenu();
  // 生成类功能（图片/视频/语音）走独立的能力任务，不进对话历史
  var activeCap = capState.active || 'chat';
  if (CAP_AGENT_SIDE.indexOf(activeCap) < 0) {
    submitCapabilityRequest();
    return;
  }
  const text = inputEl.value.trim();
  if ((!text && !attachmentCount()) || streaming) return;

  let wireText = text;
  pendingTextFiles.forEach(tf => { wireText += buildFileBlock(tf.name, tf.content); });

  addUser(wireText, pendingImages, pendingDocuments);
  stopSpeaking();
  stopMic();
  ws.send(JSON.stringify({
    type: 'user_message',
    data: {
      content: wireText,
      // 功能栏选中的对话类能力随消息发出（写作 / 联网搜索 / 网页读取 / 看图理解）
      capability: activeCap,
      images: pendingImages.map(p => ({ media_type: p.media_type, data: p.data })),
      documents: pendingDocuments.map(d => ({ media_type: d.media_type, data: d.data, name: d.name })),
    },
  }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
  pendingImages = [];
  pendingDocuments = [];
  pendingTextFiles = [];
  renderAttachmentPreview();
  streaming = true;
  // 新一轮开始：本轮使用自己的执行容器，不污染上一轮的记录。
  resetExecState();
  updateUI();
}

function cancelStreaming() {
  stopSpeaking();
  stopMic();
  if (ws) ws.send(JSON.stringify({ type: 'cancel', data: {} }));
}

function updateUI() {
  if (streaming) {
    sendBtn.classList.add('stop-mode');
    sendBtn.innerHTML = ICON_STOP;
    sendBtn.disabled = false;
    sendBtn.title = 'Stop';
    inputEl.disabled = true;
  } else {
    sendBtn.classList.remove('stop-mode');
    sendBtn.innerHTML = ICON_SEND;
    sendBtn.disabled = false;
    sendBtn.title = 'Send';
    inputEl.disabled = false;
    inputEl.focus();
  }
}

sendBtn.addEventListener('click', () => {
  if (streaming) cancelStreaming(); else sendMessage();
});

// ── 斜杠命令菜单 ──
function showSlashMenu(filter) {
  const prefix = filter.toLowerCase();
  slashFiltered = allCommands.filter(c => c.name.startsWith(prefix));
  if (slashFiltered.length === 0) { hideSlashMenu(); return; }
  slashCursor = 0;
  renderSlashMenu();
  slashMenu.classList.add('show');
}

function hideSlashMenu() {
  slashMenu.classList.remove('show');
  slashFiltered = [];
  slashCursor = 0;
}

function renderSlashMenu() {
  slashMenu.innerHTML = slashFiltered.map((c, i) =>
    '<div class="slash-item' + (i === slashCursor ? ' active' : '') + '" data-idx="' + i + '">' +
      '<span class="cmd-name">/' + escapeHtml(c.name) + '</span>' +
      '<span class="cmd-desc">' + escapeHtml(c.description) + '</span>' +
    '</div>'
  ).join('');
  slashMenu.querySelectorAll('.slash-item').forEach(el => {
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      selectSlashItem(parseInt(el.dataset.idx));
    });
  });
  const activeEl = slashMenu.querySelector('.active');
  if (activeEl) activeEl.scrollIntoView({ block: 'nearest' });
}

function selectSlashItem(idx) {
  const cmd = slashFiltered[idx];
  if (!cmd) return;
  inputEl.value = '/' + cmd.name + ' ';
  hideSlashMenu();
  inputEl.focus();
}

inputEl.addEventListener('keydown', (e) => {
  if (slashMenu.classList.contains('show')) {
    if (e.key === 'ArrowDown') { e.preventDefault(); slashCursor = Math.min(slashCursor + 1, slashFiltered.length - 1); renderSlashMenu(); return; }
    if (e.key === 'ArrowUp') { e.preventDefault(); slashCursor = Math.max(slashCursor - 1, 0); renderSlashMenu(); return; }
    if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) { e.preventDefault(); selectSlashItem(slashCursor); return; }
    if (e.key === 'Escape') { e.preventDefault(); hideSlashMenu(); return; }
  }
  if (e.key === 'Escape' && streaming) { e.preventDefault(); cancelStreaming(); return; }
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
});

inputEl.addEventListener('input', () => {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + 'px';

  const val = inputEl.value;
  if (val.startsWith('/') && !val.includes('\n')) {
    const prefix = val.substring(1).split(' ')[0];
    if (!val.includes(' ')) showSlashMenu(prefix); else hideSlashMenu();
  } else {
    hideSlashMenu();
  }
});

// ── 语音输入（浏览器原生 SpeechRecognition，中文识别，无外部依赖 / 无 API key）──
const micBtn = document.getElementById('mic-btn');
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
let micRecognition = null;
let micRecording = false;
let micBaseText = '';

function micSupported() { return !!SpeechRec; }

function setMicState(recording) {
  micRecording = recording;
  if (!micBtn) return;
  micBtn.classList.toggle('recording', recording);
  micBtn.title = recording ? '正在聆听…（点击停止）' : '语音输入（点击开始/停止）';
}

function applyMicText(text) {
  inputEl.value = text;
  // 复用已有的 input 监听：自动撑高行高
  inputEl.dispatchEvent(new Event('input'));
}

function stopMic() {
  if (micRecognition && micRecording) {
    try { micRecognition.stop(); } catch (e) { /* 已停止 */ }
  }
  setMicState(false);
}

function startMic() {
  if (!micSupported()) {
    addSystem('当前浏览器不支持语音输入（推荐使用 Edge / Chrome）。');
    return;
  }
  if (micRecording) { stopMic(); return; }
  // 开始录音前先关掉正在播报的回答，否则扬声器声音会被麦克风录进去
  stopSpeaking();
  try {
    if (!micRecognition) {
      const r = new SpeechRec();
      r.lang = 'zh-CN';
      r.continuous = true;
      r.interimResults = true;
      r.onstart = () => setMicState(true);
      r.onresult = (e) => {
        let interim = '';
        let finalText = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) finalText += t;
          else interim += t;
        }
        if (finalText) micBaseText = (micBaseText + finalText).replace(/\s+/g, ' ');
        applyMicText(micBaseText + (interim || ''));
      };
      r.onerror = (e) => {
        if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
          addSystem('麦克风权限被拒绝：请在浏览器地址栏允许麦克风权限后重试。');
        } else if (e.error !== 'aborted' && e.error !== 'no-speech') {
          addSystem('语音识别出错：' + e.error);
        }
        setMicState(false);
      };
      r.onend = () => setMicState(false);
      micRecognition = r;
    }
    micBaseText = inputEl.value ? inputEl.value.replace(/\s*$/, ' ') : '';
    micRecognition.start();
    setMicState(true);
  } catch (err) {
    // start() 在已在录音时会抛 InvalidStateError，忽略即可
    setMicState(false);
  }
}

if (micBtn) {
  if (!micSupported()) {
    micBtn.disabled = true;
    micBtn.title = '当前浏览器不支持语音输入';
  }
  micBtn.addEventListener('click', startMic);
}

// ── 启动 ──
// 功能栏先按"全部未就绪"渲染：服务端 capabilities 到达前，除对话外的功能一律不可点，
// 避免出现"按钮亮着但点了必然报错"的假可用状态。
renderCapabilityBar();
resetEmptyState();
connect();
inputEl.focus();
</script>
</body>
</html>"""
