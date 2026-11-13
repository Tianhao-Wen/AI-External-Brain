"""AI 外脑 Demo 的单页应用外壳（HTML）。

页面结构与导航：左侧功能导航 + 顶部标题/操作区 + 中央主内容区。
所有内容由前端 JS 渲染，数据全部来自后端的真实接口。
"""

from __future__ import annotations

from aieb.brain.ui.app_js import APP_JS
from aieb.brain.ui.css import STYLE

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AI 外脑 Demo · Enterprise AI Workspace</title>
<meta name="description" content="面向企业知识管理、会议分析、风险预警与任务追踪的 AI 外脑助手" />
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%231E3A5F'/%3E%3Cg fill='none' stroke='%23fff' stroke-width='1.5' stroke-linecap='round'%3E%3Ccircle cx='12' cy='7' r='2'/%3E%3Ccircle cx='7' cy='16' r='2'/%3E%3Ccircle cx='17' cy='16' r='2'/%3E%3Cpath d='M12 9v3M10.5 13.5l-2 1.5M13.5 13.5l2 1.5'/%3E%3C/g%3E%3C/svg%3E" />
<style>__STYLE__</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-mark">__BRAND_MARK__</span>
      <span>
        <span class="brand-name">AI 外脑 Demo</span><br />
        <span class="brand-sub">Enterprise AI Workspace</span>
      </span>
    </div>
    <nav class="nav" id="navList" aria-label="主导航"></nav>
    <div class="sidebar-foot" id="sidebarFoot"></div>
  </aside>

  <main class="main">
    <header class="topbar">
      <div>
        <h1 id="pageTitle">工作台</h1>
        <p class="sub" id="pageSub"></p>
      </div>
      <div class="top-actions" id="topActions"></div>
    </header>
    <div class="content" id="content"></div>
  </main>
</div>

<div class="toast-wrap" id="toastWrap" aria-live="polite"></div>

<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"></script>
<script>window.__AIEB_VERSION__ = "__AIEB_VER__";</script>
<script>__APP_JS__</script>
</body>
</html>
"""


def render_index(version: str = "") -> str:
    """返回带版本号的完整页面。"""
    return (
        INDEX_HTML
        .replace("__STYLE__", STYLE)
        .replace("__BRAND_MARK__",
                 '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="1.6" '
                 'stroke-linecap="round"><circle cx="12" cy="6" r="2.2"/>'
                 '<circle cx="6" cy="16" r="2.2"/><circle cx="18" cy="16" r="2.2"/>'
                 '<path d="M12 8.2v3.2M10.3 12.9L7.4 14.4M13.7 12.9l2.9 1.5"/></svg>')
        .replace("__APP_JS__", APP_JS)
        .replace("__AIEB_VER__", version or "0.1.0")
    )
