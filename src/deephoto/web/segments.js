/* 正文分段与内嵌图片渲染:纯函数,无 DOM 依赖。
   浏览器:window.DeepphotoSegments;node(测试):module.exports。
   契约(见 md文档/deephoto_inline_images_dev.md §3):
   - answer 正文中的 [image:ID] 是块级插图锚点,决定图片位置;
   - images 数组仅提供服务端校验过的元数据,不决定排版顺序;
   - 只有 entries 里存在的 ID 才渲染图片;无效 ID 不加载任何资源、不显示裸 ID。 */
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

  // 围栏代码 / 行内代码 / [image:ID] 标记统一扫描,先匹配到哪个算哪个;
  // 代码区原样保留为文本片段,其中的示例标记不解释成图片
  const TOKEN_RE = /(```[\s\S]*?(?:```|$)|`[^`\n]*`)|\[image:([A-Za-z0-9_]+)\]/g;

  function splitAnswerSegments(content) {
    content = String(content || "");
    const segs = [];
    let last = 0;
    for (const m of content.matchAll(TOKEN_RE)) {
      if (m.index > last) segs.push({ type: "text", text: content.slice(last, m.index) });
      if (m[1] !== undefined) segs.push({ type: "text", text: m[1] });   // 代码区
      else segs.push({ type: "image", id: m[2] });
      last = m.index + m[0].length;
    }
    if (last < content.length) segs.push({ type: "text", text: content.slice(last) });
    return segs.filter(s => s.type !== "text" || s.text);
  }

  // 流式期间:正文末尾疑似未闭合的 [image:… / [chunk:… 标记暂缓显示。
  // 缓冲只影响展示,不修改原始正文;下一批 token 到达后标记完整即可正常识别。
  const PARTIAL_RE = /\[[A-Za-z]*(?::[A-Za-z0-9_]*)?$/;

  function withholdTrailingPartial(text) {
    const m = String(text).match(PARTIAL_RE);
    return m ? String(text).slice(0, m.index) : String(text);
  }

  // 文字片段:转义 -> 极简 markdown -> chunk 引用徽标(validated 后才有真实页码)
  function renderTextFragment(text, citeMap, opts) {
    let html = esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    if (opts.streaming) {
      // 流式期间 chunk 标记只显示中性徽标,避免裸 ID 闪烁
      return html.replace(/\[chunk:[A-Za-z0-9_]+\]/g, '<span class="chip">📄 出处…</span>');
    }
    return html.replace(/\[chunk:([A-Za-z0-9_]+)\]/g, (_, id) =>
      citeMap.has(id) ? `<span class="chip" style="margin:0 2px">📄 出处 p.${esc(citeMap.get(id))}</span>` : "");
  }

  // 图片卡片:正文插图与补充图片区共用。URL 只接受服务端校验条目里的路径;
  // 放大走事件委托(data-zoomable),不把 URL 放进内联 JS 字符串
  function imageCardHTML(im) {
    return `<figure class="img-card inline" id="fig-${esc(im.image_occurrence_id)}">` +
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
   * m: {content, images?, citations?, streaming?, validated?, broken?}
   * 返回 { html, supplement } — supplement 为未被任何锚点使用的已校验图片(交给末尾补充区)。
   * 状态机:streaming → 占位"完成后显示";!validated 或 broken → "未完成校验";
   * 无条目 → "图片引用不可用"(不发请求、不露 ID);重复锚点 → 引用徽标。 */
  function renderAnswerBody(m) {
    const streaming = !!m.streaming;
    const broken = !!m.broken;
    // 旧消息没有 validated 字段:完成态且带 images 数组即视为已校验(兼容刷新恢复)
    const validated = !!m.validated || (!streaming && !broken && Array.isArray(m.images));
    const entries = new Map((m.images || []).map(im => [im.image_occurrence_id, im]));
    const citeMap = new Map((m.citations || []).map(c => [c.chunk_id, c.page]));
    const used = new Set();
    const parts = [];
    const segs = splitAnswerSegments(m.content);
    segs.forEach((seg, i) => {
      if (seg.type === "text") {
        let text = seg.text;
        if (streaming && i === segs.length - 1) text = withholdTrailingPartial(text);
        if (text) parts.push(`<div class="text">${renderTextFragment(text, citeMap, { streaming })}</div>`);
        return;
      }
      if (streaming) { parts.push(placeholderHTML("图片将在回答完成后显示")); return; }
      if (!validated) { parts.push(placeholderHTML("图片未完成校验")); return; }
      const im = entries.get(seg.id);
      if (!im) { parts.push(`<div class="img-missing">图片引用不可用</div>`); return; }
      if (used.has(seg.id)) {
        parts.push(`<div class="img-ref"><a class="chip fig" href="#fig-${esc(seg.id)}">🖼 ` +
          `${esc(figLabel(im.figure_number))} · p.${esc(im.page)}</a></div>`);
        return;
      }
      used.add(seg.id);
      parts.push(imageCardHTML(im));
    });
    const supplement = validated ? (m.images || []).filter(im => !used.has(im.image_occurrence_id)) : [];
    return { html: parts.join(""), supplement };
  }

  return { esc, figLabel, splitAnswerSegments, withholdTrailingPartial, renderAnswerBody, imageCardHTML };
});
