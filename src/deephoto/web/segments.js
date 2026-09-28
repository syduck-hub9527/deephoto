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

  // ---- 入库进度展示辅助(纯函数;阶段标识与展示名分离)----

  const PROG_STAGE_NAMES = {
    queued: "排队中", dedup_lookup: "检查已有处理结果", reuse: "复用已有结果",
    parsing: "云端解析", persist_figures: "保存图片", chunks_and_links: "整理正文与图文关系",
    describing: "生成图片描述", indexing: "准备检索", finalizing: "完成保存",
    mineru_split: "读取与拆分", mineru_merge: "合并解析结果", index_prepare: "整理检索内容",
    embed_batches: "生成语义向量",
  };

  // 已等待时长显示(不猜测"还需多久");未知为 —
  function formatElapsed(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return "—";
    const s = Math.max(0, Math.round(ms / 1000));
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60), rs = s % 60;
    if (m < 60) return rs ? `${m} 分 ${rs} 秒` : `${m} 分`;
    return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
  }

  // 列表一行的进度描述;无观测记录返回 null(调用方回退到原状态文案)
  function progressLine(doc) {
    const p = doc.progress;
    if (!p) return null;
    if (p.state === "queued") return `排队中 · 已等待 ${formatElapsed(p.total_elapsed_ms)}`;
    if (p.state === "running") {
      let text = PROG_STAGE_NAMES[p.stage] || "处理中";
      if (p.total) text += ` · 已处理 ${p.completed || 0}/${p.total}`;
      if (p.current_item && p.current_item.label) {
        text += ` · 当前:${p.current_item.label}`;
        if (p.current_item.elapsed_ms != null) text += `,已等待 ${formatElapsed(p.current_item.elapsed_ms)}`;
      } else if (p.stage_elapsed_ms != null) {
        text += ` · 已等待 ${formatElapsed(p.stage_elapsed_ms)}`;
      }
      return text;
    }
    if (p.state === "succeeded") return `就绪 · 总耗时 ${formatElapsed(p.total_elapsed_ms)}`;
    if (p.state === "interrupted") return "服务曾重启,未确认自动恢复";
    if (p.state === "failed") return "处理失败";
    return null;
  }

  const PROG_STAGE_STATE = { running: "进行中", succeeded: "已完成", skipped: "跳过",
    partial: "部分降级", failed: "失败", interrupted: "中断" };

  /* 文档列表轮询器:单一定时器 + 在途标志,连续上传/切换不会叠加循环;
   * load() 返回 false(全部终态)即停止;失败走 onError,保留上次数据。 */
  function createDocPoller(opts) {
    const interval = opts.interval || 3000;
    const setT = opts.setTimeout || setTimeout;
    const clearT = opts.clearTimeout || clearTimeout;
    let timer = null, inflight = false, running = false;
    async function tick() {
      if (!running || inflight) return;
      if (timer !== null) { clearT(timer); timer = null; }   // 立即刷新前先取消待触发定时器:调度始终只有一条
      inflight = true;
      let cont = true;
      try { cont = await opts.load(); }
      catch (e) { if (opts.onError) opts.onError(e); }
      inflight = false;
      if (!running) return;
      if (cont === false) { stop(); return; }
      timer = setT(tick, interval);
    }
    function start() { if (running) return; running = true; tick(); }
    function stop() { running = false; if (timer !== null) { clearT(timer); timer = null; } }
    return { start, stop, tick, get running() { return running; } };
  }

  /* 详情页细分表。runActive(任务是否运行中)决定是否现算在途耗时;
   * 中断/终态任务的在途项耗时显示 —,不随时间虚涨。 */
  function renderChunks(items) {
    const chunks = items.filter(i => i.kind === "mineru_chunk");
    if (!chunks.length) return "";
    const rows = chunks.map(i => {
      const d = i.detail || {};
      return `<tr><td>第 ${i.seq} 部分</td><td>${esc(PROG_STAGE_STATE[i.result] || i.result)}</td>` +
        `<td>申请 ${formatElapsed(d.request_ms)} · 上传 ${formatElapsed(d.upload_ms)}<br>` +
        `等待 ${formatElapsed(d.wait_ms)} · 下载 ${formatElapsed(d.download_ms)}</td></tr>`;
    }).join("");
    return `<div class="prog-sub">云端解析逐块往返:</div><table class="prog-table">${rows}</table>`;
  }

  function renderImageItems(items, runActive) {
    const images = items.filter(i => i.kind === "image");
    if (!images.length) return "";
    const rows = images.map(i => {
      // 仅任务运行中且该张仍在进行时才现算;中断的显示 —
      const live = runActive && i.result === "running" && i.started_at;
      const elapsed = i.duration_ms != null ? i.duration_ms
        : (live ? Date.now() - Date.parse(i.started_at) : null);
      return `<tr><td>${esc(i.label || "第 " + i.seq + " 张")}</td>` +
        `<td>${i.page != null ? "p." + i.page : ""}</td>` +
        `<td>${esc(PROG_STAGE_STATE[i.result] || i.result)}</td>` +
        `<td>${formatElapsed(elapsed)}</td></tr>`;
    }).join("");
    return `<div class="prog-sub">图片描述逐张结果:</div><table class="prog-table">${rows}</table>`;
  }

  function renderBatchItems(items) {
    const batches = items.filter(i => i.kind === "embed_batch");
    if (!batches.length) return "";
    const rows = batches.map(i => `<tr><td>第 ${i.seq} 批</td><td>${i.count != null ? i.count + " 条" : ""}</td>` +
      `<td>${esc(PROG_STAGE_STATE[i.result] || i.result)}</td>` +
      `<td>${formatElapsed(i.duration_ms)}</td></tr>`).join("");
    return `<div class="prog-sub">向量逐批结果:</div><table class="prog-table">${rows}</table>`;
  }

  /* 文档耗时详情加载器:
   * - 同一文档只允许一个请求在途,期间 refresh 不再重复发送(慢于轮询周期也不会堆积);
   * - 最近一次成功内容入缓存,列表重建时直接复用,不再每轮回退到"加载中…";
   * - 失败状态按文档持久保存:列表重建、收起再展开、重试在途都不提前清除,
   *   只有成功获得新详情后才清除(否则用户会把旧内容误认为最新结果);
   * - 响应回来后由调用方写入当前节点;切换展开文档后,旧请求结果只进缓存不渲染。 */
  function createProgDetailLoader(opts) {
    let expandedId = null;
    const inflight = new Set();
    const cache = new Map();
    const errors = new Set();
    function expanded(id) { expandedId = id; }      // null 表示收起
    function cachedHtml(id) { return cache.get(id) || null; }
    function hasError(id) { return errors.has(id); }

    // 错误回调与列表重建共用的渲染出口(三态:失败+缓存 / 失败无缓存 / 加载中)
    function innerHtml(id) {
      const cached = cache.get(id);
      if (errors.has(id)) {
        return cached
          ? '<div class="doc-err">详情刷新失败,显示上次内容</div>' + cached
          : '<div class="doc-err">详情加载失败</div>';
      }
      return cached || '<div class="doc-err" style="color:var(--muted)">加载中…</div>';
    }

    async function refresh(id) {
      if (expandedId !== id || inflight.has(id)) return;
      inflight.add(id);
      try {
        const html = await opts.fetchDetail(id);
        cache.set(id, html);
        errors.delete(id);                       // 只有成功获得新详情才清除失败状态
        if (expandedId === id) opts.onRender(id, html);
      } catch (e) {
        errors.add(id);                          // 失败状态与旧缓存共存
        if (expandedId === id && opts.onError) opts.onError(id, e);   // 文档 ID 与异常一起回传
      } finally {
        inflight.delete(id);
      }
    }
    return { expanded, refresh, cachedHtml, hasError, innerHtml,
             get inflightCount() { return inflight.size; } };
  }

  return { esc, figLabel, splitAnswerSegments, withholdTrailingPartial, renderAnswerBody,
           imageCardHTML, figureDomId, newMessageId, createAssistantMessage,
           applyStreamEvent, finishStream, restoreMessages,
           PROG_STAGE_NAMES, PROG_STAGE_STATE, formatElapsed, progressLine, createDocPoller,
           renderChunks, renderImageItems, renderBatchItems, createProgDetailLoader };
});