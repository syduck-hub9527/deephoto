// M0 技术基座验证页脚本(临时,M1 落地后删除)。
// 验证:import map 解析、preact/htm/markdown-it 的导出形态、hooks 可用性、
// 以及 markdown-it(html:false)的安全行为与 §5.3 实测表一致。
import { h, render } from "preact";
import { useState } from "preact/hooks";
import htm from "htm";
import MarkdownIt from "markdown-it";

const html = htm.bind(h);
const checks = [];
function check(name, ok) { checks.push({ name, ok: !!ok }); }

// 1. 模块导出形态(§5.1 中"未在浏览器验证过"的写法)
check("htm 默认导出可 bind", typeof htm.bind === "function");
check("markdown-it 默认导出可构造", typeof MarkdownIt === "function");
check("preact 命名导出 h/render", typeof h === "function" && typeof render === "function");

// 2. 带状态组件(hooks.mjs 内含裸导入 "preact",依赖 import map)
function Counter() {
  const [n, setN] = useState(0);
  return html`<button type="button" onClick=${() => setN(n + 1)}>计数 ${n}(点击应递增)</button>`;
}

// 3. markdown-it 渲染(html:false 是安全底线,与 §5.3 配置一致)
const md = new MarkdownIt({ html: false, linkify: false, breaks: false });
const SAMPLE = [
  "嵌套列表:",
  "- a",
  "  - b",
  "  - c",
  "- d",
  "",
  "| 列一 | 列二 |",
  "| --- | --- |",
  "| 1 | 2 |",
  "",
  "删除线 ~~gone~~、*斜体*、`行内代码`。",
  "",
  "> 引用块",
  "",
  "正常链接 [官网](https://example.com);危险链接 [点我](javascript:alert(1))。",
  "",
  "原始 HTML 应被转义:<img src=x onerror=alert(1)>",
  "",
  "```python",
  "def hello():",
  "    return 42",
  "```",
  "",
  "---",
  "",
  "协议标记透传:[chunk:chunk_abc123]",
  "",
  "[image:occ_9f]",
].join("\n");
const rendered = md.render(SAMPLE);

check("嵌套列表保留层级(两个 <ul>)", (rendered.match(/<ul>/g) || []).length >= 2);
check("GFM 表格", rendered.includes("<table>"));
check("围栏代码块带语言 class", rendered.includes('class="language-python"'));
check("删除线 <s>", rendered.includes("<s>"));
check("引用块 <blockquote>", rendered.includes("<blockquote>"));
check("水平线 <hr", rendered.includes("<hr"));
check("正常 https 链接生成", rendered.includes('href="https://example.com"'));
check("原始 HTML 被转义", !rendered.includes("<img src=x") && rendered.includes("&lt;img"));
check("javascript: 链接被拒绝(保持字面文本,不生成 <a>)",
  !rendered.includes('href="javascript:') && rendered.includes("[点我](javascript:alert(1))"));
check("[chunk:]/[image:] 标记原样保留",
  rendered.includes("[chunk:chunk_abc123]") && rendered.includes("[image:occ_9f]"));

function App() {
  return html`
    <h2>1. Preact 组件(hooks)</h2>
    <${Counter} />
    <h2>2. markdown-it 渲染结果</h2>
    <div class="md" dangerouslySetInnerHTML=${{ __html: rendered }}></div>
    <h2>3. 自检</h2>
    <ul>
      ${checks.map((c) => html`<li class=${c.ok ? "ok" : "bad"}>${c.ok ? "PASS" : "FAIL"} — ${c.name}</li>`)}
    </ul>
  `;
}

const root = document.getElementById("app");
render(html`<${App} />`, root);
root.dataset.rendered = "1";
window.__PROBE_OK = checks.every((c) => c.ok);
