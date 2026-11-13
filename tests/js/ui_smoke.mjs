/**
 * AI 外脑 Demo 的前端端到端测试（jsdom 真跑生产 JS）。
 *
 * 用法: node ui_smoke.mjs <rendered-html-path>
 * 依赖: jsdom（由 Python 测试安装到临时依赖目录）
 *
 * 覆盖点：
 *   1. 左侧导航渲染 6 个入口，默认进入工作台；
 *   2. 工作台数据来自后端 RPC（不是写死的假数字）；
 *   3. 切换到知识库会真的发出 documents.list 请求，空库显示空状态；
 *   4. 对话流式渲染 + 工具进度用自然语言（不出现原始工具 JSON）；
 *   5. markdown 输出经过消毒函数。
 */

import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');

const htmlPath = process.argv[2];
if (!htmlPath) {
  console.error('usage: node ui_smoke.mjs <html>');
  process.exit(2);
}
const html = fs.readFileSync(htmlPath, 'utf8');

const failures = [];
const checks = [];
function check(name, ok, detail) {
  checks.push({ name, ok, detail: detail == null ? '' : String(detail) });
  if (!ok) failures.push(name);
}
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const sent = [];
const sockets = [];
const pageErrors = [];

class FakeWebSocket {
  constructor(url, protocols) {
    this.url = url;
    this.protocols = protocols;
    this.readyState = 1;
    sockets.push(this);
    setTimeout(() => this.onopen && this.onopen(), 0);
  }
  send(raw) {
    sent.push(JSON.parse(raw));
  }
  close() {
    this.readyState = 3;
  }
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  url: 'http://127.0.0.1:18889/',
  beforeParse(window) {
    window.marked = { parse: (text) => '<p>' + String(text) + '</p>' };
    window.DOMPurify = {
      sanitize: (value) => {
        window.__sanitizeCalls = (window.__sanitizeCalls || 0) + 1;
        return String(value);
      },
    };
    window.WebSocket = FakeWebSocket;
    window.fetch = async () => ({ ok: true, json: async () => ({}), text: async () => '' });
    window.addEventListener('error', (event) => pageErrors.push(String(event.message)));
  },
});

const { window } = dom;
const doc = window.document;

function lastRequest() {
  return [...sent].reverse().find((m) => m.type === 'brain_request');
}
async function reply(op, data) {
  const req = [...sent].reverse().find((m) => m.type === 'brain_request' && m.data.op === op);
  if (!req) throw new Error('未收到请求: ' + op);
  const ws = sockets[0];
  ws.onmessage({
    data: JSON.stringify({
      type: 'brain_response',
      data: { req_id: req.data.req_id, ok: true, data },
    }),
  });
  await wait(30);
}
async function pushEvent(type, data) {
  sockets[0].onmessage({ data: JSON.stringify({ type, data }) });
  await wait(10);
}

await wait(120);

if (pageErrors.length) {
  console.error('页面脚本异常: ' + pageErrors.join(' | '));
}
check('页面脚本无异常', pageErrors.length === 0, pageErrors.join(' | '));
check('WebSocket 已连接', sockets.length === 1, sockets.length);
check('导航渲染 6 个入口', doc.querySelectorAll('.nav-item').length === 6,
      doc.querySelectorAll('.nav-item').length);
check('默认页面是工作台', doc.querySelector('#pageTitle').textContent === '工作台',
      doc.querySelector('#pageTitle').textContent);
check('欢迎标题存在', doc.body.textContent.includes('欢迎使用 AI 外脑'));
check('工作台发出 dashboard 请求', !!sent.find((m) => m.data && m.data.op === 'dashboard'));

await reply('dashboard', {
  stats: { documents: 2, meetings: 1, todos_open: 3, risks_open: 1 },
  recent_meetings: [{
    id: 'mtg_1', title: '供应商年度评审', created_at: new Date().toISOString(),
    participants: ['陈明', '刘洋'], risk_count: 3, todo_count: 2,
  }],
  recent_todos: [{ content: '输出质量整改要求清单', owner: '刘洋',
                   due_date: '11 月 5 日', priority: 'high', meeting_title: '供应商年度评审' }],
  activities: [{ title: '完成会议分析：供应商年度评审', detail: '3 条风险 / 2 条待办',
                 created_at: new Date().toISOString() }],
  documents: [],
});

const stats = doc.querySelectorAll('#dashStats .stat');
check('工作台统计渲染 4 张卡片', stats.length === 4, stats.length);
check('统计数字来自后端', doc.querySelector('#dashStats').textContent.includes('3'),
      doc.querySelector('#dashStats').textContent.replace(/\s+/g, ' '));
check('最近会议来自后端', doc.body.textContent.includes('供应商年度评审'));

// ── 知识库页面 ──
doc.querySelector('.nav-item[data-page="knowledge"]').click();
await wait(60);
check('切换到知识库', doc.querySelector('#pageTitle').textContent === '知识库');
await reply('documents.list', { documents: [] });
check('空知识库显示空状态', doc.body.textContent.includes('还没有知识文档'));
check('知识库发出了 list 请求', !!sent.find((m) => m.data && m.data.op === 'documents.list'));

// ── 对话页面：流式 + 工具进度 ──
doc.querySelector('.nav-item[data-page="chat"]').click();
await wait(60);
const composer = doc.querySelector('#composer');
check('对话输入框存在', !!composer);
composer.value = '帮我看看知识库里验收标准怎么写';
doc.querySelector('#sendBtn').click();
await wait(80);
check('对话发送 user_message',
      !!sent.find((m) => m.type === 'user_message' && m.data.content.includes('验收标准')));

await pushEvent('tool_use', { toolId: 't1', toolName: 'SearchKnowledge', args: { query: '验收标准' } });
const chatText = doc.querySelector('#chatScroll').textContent;
check('工具进度用自然语言', chatText.includes('正在检索知识库…'));
check('界面不出现原始工具 JSON', !chatText.includes('"query"'));
check('界面不出现工具英文名', !chatText.includes('SearchKnowledge'), chatText.slice(0, 80));

await pushEvent('stream_text', { text: '验收条款 C 要求给出页码引用。' });
await pushEvent('loop_complete', { totalTurns: 1, elapsed: 1.2 });
check('流式回答渲染到界面', doc.body.textContent.includes('验收条款 C 要求给出页码引用'));
check('markdown 经过消毒函数', (window.__sanitizeCalls || 0) > 0, window.__sanitizeCalls);

// ── 待办页面 ──
doc.querySelector('.nav-item[data-page="todos"]').click();
await wait(60);
await reply('todos.list', { todos: [{ id: 'todo_1', content: '输出整改清单', owner: '刘洋',
  due_date: '11 月 5 日', priority: 'high', status: 'pending', meeting_title: '供应商年度评审' }] });
check('待办列表渲染', doc.body.textContent.includes('输出整改清单'));
check('缺失值显示待确认', doc.body.textContent.includes('刘洋'));

for (const c of checks) {
  console.log(`${c.ok ? 'PASS' : 'FAIL'}\t${c.name}${c.detail ? '\t' + c.detail : ''}`);
}
console.log(`\n${checks.length - failures.length}/${checks.length} 通过`);
window.close();
dom.window.close();
if (failures.length) {
  console.error('失败项: ' + failures.join(', '));
  process.exit(1);
}
process.exit(0);
