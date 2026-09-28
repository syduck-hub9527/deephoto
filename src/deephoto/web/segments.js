/* 正文分段与内嵌图片渲染:纯函数,无 DOM 依赖。
   浏览器:window.DeepphotoSegments;node(测试):module.exports。
   契约(见 md文档/deephoto_inline_images_dev.md §3):
   - answer 正文中的 [image:ID] 是块级插图锚点,决定图片位置;
   - images 数组仅提供服务端校验过的元数据,不决定排版顺序;
   - 只有 entries 里存在的 ID 才渲染图片;无效 ID 不加载任何资源、不显示裸 ID。
   状态优先级(F4):validated > streaming > broken;完成事件即终态,不等连接关闭。 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.DeepphotoSegments = api;
})(typeof self !== "undefined" ? self : globalThis, function () {
  "use strict";

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // 图号带"表"前缀时是表格编号(如 "表1.1"),否则是图号(如 "1.1")
  function figLabel(n) {
    if (!n) return "图片";
    return n.startsWith("表") ? "表 " + n.slice(1) : "图 " + n;
  }

  // ---- 消息身份(F2:图片 DOM ID 按消息隔离,跨消息重复引用不串位)----

  let msgSeq = 0;
  function newMessageId() {
    msgSeq += 1;
    return "m" + Date.now().toString(36) + "-" + msgSeq.toString(36) +
      "-" + Math.random().toString(36).slice(2, 6);
  }

  function createAssistantMessage() {
    return { role: "assistant", content: "", thinking: "", citations: [], images: [],
             streaming: true, validated: false, message_id: newMessageId() };
  }

  // 恢复历史:清掉流式态;缺失/重复的消息标识补齐(只用于页面展示,不进历史协议)
  function restoreMessages(list) {
    const seen = new Set();
    (list || []).forEach(m => {
      m.streaming = false;
      if (m.role === "assistant") {
        if (!m.message_id || seen.has(m.message_id)) m.message_id = newMessageId();
        seen.add(m.message_id);
      }
    });
    return list || [];
  }

  function figureDomId(messageId, occId) {
    const safe = s => String(s || "").replace(/[^A-Za-z0-9_-]/g, "_");
    return `fig-${safe(messageId)}-${safe(occId)}`;
  }

  // ---- 流式事件应用(F3:错误/警告独立字段,绝不拼进原始正文)----

  function applyStreamEvent(msg, ev) {
    if (ev.type === "token") msg.content += ev.text;
    else if (ev.type === "thinking") msg.thinking += ev.text;
    else if (ev.type === "done") {
      msg.content = ev.answer || msg.content;
      msg.citations = ev.citations || [];
      msg.images = ev.images || [];
      msg.validated = true;
      msg.streaming = false;      // F4: 完成事件即终态,不等连接关闭
    } else if (ev.type === "warning") msg.warning = ev.detail;
    else if (ev.type === "error") {
      msg.error = ev.detail;      // 独立字段;正文保留已流出的自然语言
      msg.broken = true;
      msg.streaming = false;
    }
    return msg;
  }

  // 流读取结束(EOF):没有 done 也没有 error 才算传输中断;已有答案不得撤销
  function finishStream(msg) {
    msg.streaming = false;
    if (!msg.validated && !msg.broken) {
      msg.broken = true;
      msg.warning = msg.warning || "回答未完成(连接中断),图片未通过校验";
    }
    return msg;
  }

  // ---- 正文分段(F1:行内代码并入文字缓冲,只有真正的图片锚点才断开文字块)----

  // 围栏代码 / 行内代码 / [image:ID] 标记统一扫描;代码区追加进文字缓冲,
  // 其中的示例标记不解释成图片
  const TOKEN_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)|\[image:([A-Za-z0-9_]+)\]/g;

  function splitAnswerSegments(content) {
    content = String(content || "");
    const segs = [];
    let buf = "";
    let last = 0;
    for (const m of content.matchAll(TOKEN_RE)) {
      buf += content.slice(last, m.index);
      if (m[1] !== undefined) buf += m[1];                    // 代码区:并入文字缓冲
      else {
        if (buf) { segs.push({ type: "text", text: buf }); buf = ""; }
        segs.push({ type: "image", id: m[2] });
      }
      last = m.index + m[0].length;
    }
    buf += content.slice(last);
    if (buf) segs.push({ type: "text", text: buf });
    return segs;
  }

  // 未校验(流式中/异常结束)时:正文末尾疑似未闭合的协议标记暂缓显示。
  // 只处理约定协议的候选前缀([image:… / [chunk:…),普通方括号文字不动;
  // 缓冲只影响展示,不修改原始正文。
  const MARKER_PREFIXES = ["[image:", "[chunk:"];

  function withholdTrailingPartial(text) {
    text = String(text);
    const i = text.lastIndexOf("[");
    if (i < 0) return text;
    const tail = text.slice(i);
    const isCandidate = MARKER_PREFIXES.some(p =>
      p.startsWith(tail) ||
      (tail.startsWith(p) && /^[A-Za-z0-9_]*$/.test(tail.slice(p.length))));
    return isCandidate ? text.slice(0, i) : text;
  }

  // ---- 文字片段渲染 ----

  const CODE_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)/g;

  // 代码区域剔出后再做 markdown/徽标替换:示例里的标记保持文字,不换徽标
  function renderTextFragment(text, citeMap, opts) {
    let html = "";
    let last = 0;
    for (const m of text.matchAll(CODE_RE)) {
      html += renderPlain(text.slice(last, m.index), citeMap, opts);
      const code = m[1];
      html += code.startsWith("```")
        ? esc(code)                                          // 围栏:原样转义展示
        : `<code>${esc(code.slice(1, -1))}</code>`;
      last = m.index + code.length;
    }
    return html + renderPlain(text.slice(last), citeMap, opts);
  }

  function renderPlain(text, citeMap, opts) {
    const html = esc(text).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    if (opts.streaming) {
      // 流式期间 chunk 标记只显示中性徽标,避免裸 ID 闪烁
      return html.replace(/\[chunk:[A-Za-z0-9_]+\]/g, '<span class="chip">📄 出处…</span>');
    }
    return html.replace(/\[chunk:([A-Za-z0-9_]+)\]/g, (_, id) =>
      citeMap.has(id) ? `<span class="chip" style="margin:0 2px">📄 出处 p.${esc(citeMap.get(id))}</span>` : "");
  }

  // ---- 图片卡片(正文插图与补充图片区共用)----

  // URL 只接受服务端校验条目里的路径;放大走事件委托(data-zoomable),不把 URL 放进内联 JS
  function imageCardHTML(im, messageId) {
    return `<figure class="img-card inline" id="${figureDomId(messageId, im.image_occurrence_id)}">` +
      `<img src="${esc(im.image_url)}" alt="${esc(im.caption || "文档配图")}" loading="lazy" data-zoomable="1">` +
      `<div class="img-err" hidden>图片加载失败,可打开来源页查看</div>` +
      `<figcaption><b>${esc(figLabel(im.figure_number))}</b> · 第 ${esc(im.page)} 页` +
      `${im.caption ? "<br>" + esc(im.caption) : ""}` +
      ` <a href="${esc(im.source_page_url)}" target="_blank" rel="noopener">查看 PDF 第 ${esc(im.page)} 页 ↗</a>` +
      `</figcaption></figure>`;
  }

  function placeholderHTML(text) {
    return `<figure class="img-card placeholder"><div class="ph-body">🖼 ${esc(text)}</div></figure>`;
  }

  /* 按正文顺序渲染文字与图片。
   * m: {content, images?, citations?, streaming?, validated?, broken?, message_id?}
   * 返回 { html, supplement } — supplement 为未被任何锚点使用的已校验图片(交给末尾补充区)。
   * 状态优先级:validated(已完成)→ 最终卡片;streaming(生成中)→ 等待占位;
   * 其余(异常/中断)→ 未完成校验提示。显式 validated=false 表示"明确未校验",
   * 与旧消息"没有该字段"(undefined,按兼容规则推断)区分开。 */
  function renderAnswerBody(m) {
    const streaming = !!m.streaming;
    const broken = !!m.broken;
    const validated = m.validated === true ||
      (m.validated === undefined && !streaming && !broken && Array.isArray(m.images));
    const entries = new Map((m.images || []).map(im => [im.image_occurrence_id, im]));
    const citeMap = new Map((m.citations || []).map(c => [c.chunk_id, c.page]));
    const used = new Set();
    const parts = [];
    const segs = splitAnswerSegments(m.content);
    segs.forEach((seg, i) => {
      if (seg.type === "text") {
        let text = seg.text;
        // 未校验时截住末尾疑似未闭合的协议标记;刷新恢复后同样生效
        if (!validated && i === segs.length - 1) text = withholdTrailingPartial(text);
        if (text) {
          parts.push(`<div class="text">${renderTextFragment(text, citeMap, { streaming: streaming && !validated })}</div>`);
        }
        return;
      }
      if (validated) {
        const im = entries.get(seg.id);
        if (!im) { parts.push(`<div class="img-missing">图片引用不可用</div>`); return; }
        if (used.has(seg.id)) {
          parts.push(`<div class="img-ref"><a class="chip fig" href="#${figureDomId(m.message_id, seg.id)}">🖼 ` +
            `${esc(figLabel(im.figure_number))} · p.${esc(im.page)}</a></div>`);
          return;
        }
        used.add(seg.id);
        parts.push(imageCardHTML(im, m.message_id));
        return;
      }
      parts.push(placeholderHTML(streaming ? "图片将在回答完成后显示" : "图片未完成校验"));
    });
    const supplement = validated ? (m.images || []).filter(im => !used.has(im.image_occurrence_id)) : [];
    return { html: parts.join(""), supplement };
  }

  return { esc, figLabel, splitAnswerSegments, withholdTrailingPartial, renderAnswerBody,
           imageCardHTML, figureDomId, newMessageId, createAssistantMessage,
           applyStreamEvent, finishStream, restoreMessages };
});
