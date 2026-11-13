/**
 * 用真实 jsdom + DOMPurify 验证 Web UI 的 markdown 渲染是否会执行恶意输入。
 *
 * 直接复用 aieb/web_content.py 里 INDEX_HTML 中的生产代码
 * （renderMarkdown / escapeHtml / SANITIZE_OPTIONS），不做任何重写，
 * 因此测试覆盖的是真正运行在浏览器里的那段逻辑。
 *
 * 用法: node xss_sanitizer_check.mjs <path-to-web_content.py>
 * 依赖: jsdom, dompurify, marked（由 Python 测试安装到临时目录）
 */

import fs from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { JSDOM } = require('jsdom');
const createDOMPurify = require('dompurify');
const marked = require('marked');

const PY_PATH = process.argv[2];
if (!PY_PATH) {
  console.error('usage: node xss_sanitizer_check.mjs <web_content.py>');
  process.exit(2);
}

const pySource = fs.readFileSync(PY_PATH, 'utf8');

/** 借助大括号计数，从 Python 源码字符串里截出某个 JS 声明的完整文本。 */
function extractBlock(source, startMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) throw new Error(`marker not found: ${startMarker}`);
  let depth = 0;
  let seen = false;
  for (let i = start; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '{') {
      depth += 1;
      seen = true;
    } else if (ch === '}') {
      depth -= 1;
      if (seen && depth === 0) {
        // 仅在紧邻处出现分号时才把它算进来（const X = {...}; 这种）
        let end = i;
        let j = i + 1;
        while (j < source.length && (source[j] === ' ' || source[j] === '\t')) j += 1;
        if (source[j] === ';') end = j;
        return source.slice(start, end + 1);
      }
    }
  }
  throw new Error(`unbalanced block for ${startMarker}`);
}

/** 取一行声明（`const FOO = ...;` / `let foo = ...;` 这类没有大括号的）。 */
function extractLines(source, startMarker) {
  const start = source.indexOf(startMarker);
  if (start < 0) throw new Error(`marker not found: ${startMarker}`);
  const end = source.indexOf('\n', start);
  return source.slice(start, end < 0 ? source.length : end);
}

const codeParts = [
  extractBlock(pySource, 'function escapeHtml(s) {'),
  extractBlock(pySource, 'function escapeAttr(s) {'),
  extractBlock(pySource, 'const SANITIZE_OPTIONS = {'),
  // 外部链接策略：target/rel 由 applyExternalLinkPolicy 统一改写，
  // 因此它也属于「进 DOM 前的真实生产代码」，必须一起加载。
  extractLines(pySource, 'const HTTP_URL_RE ='),
  extractLines(pySource, 'const ANY_SCHEME_RE ='),
  extractLines(pySource, 'const SAFE_LINK_REL ='),
  extractLines(pySource, 'let sanitizerHooksInstalled'),
  extractBlock(pySource, 'function applyExternalLinkPolicy(a) {'),
  extractBlock(pySource, 'function ensureSanitizerHooks() {'),
  extractBlock(pySource, 'function renderMarkdown(text) {'),
];

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  runScripts: 'outside-only',
});
const { window } = dom;
window.marked = marked;
window.DOMPurify = createDOMPurify(window);
window.eval(codeParts.join('\n'));

const renderMarkdown = window.renderMarkdown;
if (typeof renderMarkdown !== 'function') {
  console.error('renderMarkdown not exposed');
  process.exit(2);
}

const XSS_PAYLOADS = [
  '<script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<a href="javascript:alert(1)">x</a>',
  '<svg onload=alert(1)>',
  '<iframe src="javascript:alert(1)"></iframe>',
  '<div onmouseover="alert(1)">hi</div>',
  '<body onload=alert(1)>',
  '[click](javascript:alert(1))',
  '![img](javascript:alert(1))',
  '<scr<script>ipt>alert(1)</scr</script>ipt>',
];

const BENIGN = [
  ['**bold**', '<strong>bold</strong>'],
  ['[ok](https://example.com)', 'href="https://example.com"'],
  ['```\n<div>code</div>\n```', '&lt;div&gt;'],
  ['| a | b |\n|---|---|\n| 1 | 2 |', '<table>'],
  ['`inline`', '<code>inline</code>'],
];

const results = { payloads: [], benign: [], ok: true };

for (const payload of XSS_PAYLOADS) {
  const html = renderMarkdown(payload);
  const lower = html.toLowerCase();
  const problems = [];
  if (lower.includes('<script')) problems.push('script tag');
  if (/<[^>]*\son[a-z]+\s*=/.test(lower)) problems.push('event handler attribute');
  if (lower.includes('<iframe')) problems.push('iframe');
  if (lower.includes('<svg')) problems.push('svg');
  if (/<(object|embed|form)\b/.test(lower)) problems.push('dangerous tag');
  // `javascript:` 只可能在解析后的属性里造成危害，因此只看 DOM（见下方 jsLinks）。

  // 用真实 DOM 再验一遍：插入后不能产生可执行节点
  const host = window.document.createElement('div');
  host.innerHTML = html;
  if (host.querySelector('script')) problems.push('script node in DOM');
  const withHandlers = [...host.querySelectorAll('*')].filter((el) =>
    [...el.attributes].some((a) => a.name.toLowerCase().startsWith('on')),
  );
  if (withHandlers.length) problems.push('handler in DOM');
  const jsLinks = [...host.querySelectorAll('a,img')].filter((el) => {
    const target = el.getAttribute('href') || el.getAttribute('src') || '';
    return target.toLowerCase().trim().startsWith('javascript:');
  });
  if (jsLinks.length) problems.push('javascript link in DOM');

  results.payloads.push({ payload, html, problems, pass: problems.length === 0 });
  if (problems.length) results.ok = false;
}

for (const [input, expected] of BENIGN) {
  const html = renderMarkdown(input);
  const pass = html.includes(expected);
  results.benign.push({ input, html, expected, pass });
  if (!pass) results.ok = false;
}

// ── 外部链接策略：正常外链新标签页打开，危险协议不可点击 ──
const LINK_CASES = [
  ['[src](https://example.com/a)', (html) => html.includes('target="_blank"')],
  ['[src](https://example.com/a)', (html) => html.includes('rel="noopener noreferrer nofollow"')],
  ['[src](http://example.com/)', (html) => html.includes('referrerpolicy="no-referrer"')],
  ['[bad](javascript:alert(1))', (html) => !/href\s*=\s*"\s*javascript:/i.test(html)],
  ['[bad](data:text/html;base64,PHN2Zz4=)', (html) => !/href\s*=\s*"\s*data:/i.test(html)],
  ['[anchor](#section)', (html) => html.includes('href="#section"') && !html.includes('target="_blank"')],
];

for (const [input, check] of LINK_CASES) {
  const html = renderMarkdown(input);
  const pass = check(html);
  results.links = results.links || [];
  results.links.push({ input, html, pass });
  if (!pass) results.ok = false;
}

console.log(JSON.stringify(results, null, 2));
process.exit(results.ok ? 0 : 1);
