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

  // ---- 文字片段渲染(受限 Markdown:标题 / 列表 / 表格 / 公式 / 段落 / 粗体 / 代码)----

  const INLINE_CODE_RE = /(`[^`\n]*`)/g;
  const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*)$/;
  const LIST_RE = /^\s*([-*\u2022]|\d+[.)])\s+(.*)$/;

  // 引用页码标签:跨页块显示 "1–2",单页显示 "1"
  function pageLabel(c) {
    const s = c && c.page, e = c && c.page_end;
    return e && s && e !== s ? `${s}\u2013${e}` : String(s ?? "");
  }

  // 行内:代码区剔出后再做粗体/徽标替换,示例里的标记保持文字
  function renderInline(text, citeMap, opts) {
    return text.split(INLINE_CODE_RE).map((part, i) => {
      if (i % 2 === 1) return `<code>${esc(part.slice(1, -1))}</code>`;
      return renderPlain(part, citeMap, opts);
    }).join("");
  }

  // ---- 数学公式(LaTeX)----
  // 纯函数只负责"识别 + 输出带 data-tex 的占位节点";真正的排版由页面加载的 KaTeX 完成
  // (index.html 的 renderMath)。KaTeX 未加载时占位节点显示原文,与旧行为一致。
  // 支持 $...$ / \(...\) 行内,$$...$$ / \[...\] 独立公式;识别规则(避免把金额 "$5 和 $10" 当公式):
  //   - 开头 $ 后不能是空白,结尾 $ 前不能是空白、后不能紧跟数字;
  //   - \$ 是字面美元符;行内公式不跨行;独立公式未闭合(流式中)不识别,按普通文字显示。

  const MATH_OPEN = "\uE000", MATH_CLOSE = "\uE001";
  const MATH_PLACEHOLDER_RE = /\uE000(\d+)\uE001/g;
  const isSpace = ch => ch === undefined || /\s/.test(ch);

  function mathNode(tex, display, source) {
    return `<span class="math${display ? " math-display" : ""}" data-tex="${esc(tex)}" ` +
      `data-display="${display ? 1 : 0}">${esc(source)}</span>`;
  }

  // 把一行文字里的公式换成占位符;返回 { text, maths }
  function extractMath(text) {
    const maths = [];
    let out = "";
    const put = (tex, display, source) => {
      maths.push({ tex, display, source });
      out += MATH_OPEN + (maths.length - 1) + MATH_CLOSE;
    };
    for (let i = 0; i < text.length;) {
      const ch = text[i], nx = text[i + 1];
      if (ch === "\\" && nx === "$") { out += "$"; i += 2; continue; }             // \$ 字面美元符
      if (ch === "\\" && (nx === "(" || nx === "[")) {
        const close = nx === "(" ? "\\)" : "\\]";
        const end = text.indexOf(close, i + 2);
        if (end > i + 2 && text.slice(i + 2, end).trim()) {
          put(text.slice(i + 2, end), nx === "[", text.slice(i, end + 2));
          i = end + 2; continue;
        }
      }
      if (ch === "$" && nx === "$") {
        const end = text.indexOf("$$", i + 2);
        if (end > i + 2 && text.slice(i + 2, end).trim()) {
          put(text.slice(i + 2, end), true, text.slice(i, end + 2));
          i = end + 2; continue;
        }
      } else if (ch === "$" && !isSpace(nx)) {
        let j = i + 1, found = -1;
        while (j < text.length) {
          if (text[j] === "\\") { j += 2; continue; }
          if (text[j] === "$") { found = j; break; }
          j++;
        }
        if (found > i + 1 && !isSpace(text[found - 1]) && !/\d/.test(text[found + 1] || "")) {
          put(text.slice(i + 1, found), false, text.slice(i, found + 1));
          i = found + 1; continue;
        }
      }
      out += ch; i++;
    }
    return { text: out, maths };
  }

  function restoreMath(html, maths) {
    return html.replace(MATH_PLACEHOLDER_RE, (_, k) => {
      const m = maths[Number(k)];
      return m ? mathNode(m.tex, m.display, m.source) : "";
    });
  }

  // 独立公式块:从 lines[i] 起尝试匹配 $$...$$ 或 \[...\](可跨多行,闭合行后不得有其他文字)。
  // 成功返回 { html, next };未闭合/有夹杂文字返回 null(交给行内规则或当普通文字)
  function tryParseMathBlock(lines, i) {
    const first = lines[i].trim();
    const kinds = [["$$", "$$"], ["\\[", "\\]"]];
    for (const [open, close] of kinds) {
      if (!first.startsWith(open)) continue;
      const rest = first.slice(open.length);
      let body = null, end = i;
      const same = rest.indexOf(close);
      if (same >= 0) {
        if (rest.slice(same + close.length).trim() !== "") return null;
        body = rest.slice(0, same);
      } else {
        const buf = [rest];
        for (let k = i + 1; k < lines.length; k++) {
          const at = lines[k].indexOf(close);
          if (at >= 0) {
            if (lines[k].slice(at + close.length).trim() !== "") return null;
            buf.push(lines[k].slice(0, at));
            body = buf.join("\n"); end = k; break;
          }
          if (!lines[k].trim() && k > i + 1 && !buf[buf.length - 1].trim()) return null;   // 连续空行:不是公式块
          buf.push(lines[k]);
        }
        if (body === null) return null;
      }
      if (!body.trim()) return null;
      const source = lines.slice(i, end + 1).join("\n").trim();
      return { html: `<div class="math-block">${mathNode(body.trim(), true, source)}</div>`, next: end + 1 };
    }
    return null;
  }

  // ---- 表格(GFM 管道表格:表头行 + 分隔行 + 若干数据行)----

  // 按未转义的 | 切分一行;首尾的管道符不产生空列;\| 还原为字面 |
  function splitTableRow(line) {
    let s = String(line).trim();
    if (s.startsWith("|")) s = s.slice(1);
    const cells = [];
    let cur = "";
    for (let i = 0; i < s.length; i++) {
      const ch = s[i];
      if (ch === "\\" && s[i + 1] === "|") { cur += "|"; i++; }
      else if (ch === "|") { cells.push(cur.trim()); cur = ""; }
      else cur += ch;
    }
    if (cur.trim() !== "" || cells.length === 0) cells.push(cur.trim());   // 末尾管道符之后无内容则不补空列
    return cells;
  }

  // 分隔行:每列形如 --- / :--- / ---: / :---:;整行必须含管道符(否则是水平线,不是表格)
  const TABLE_SEP_CELL_RE = /^:?-+:?$/;
  function parseTableSeparator(line) {
    if (!String(line).includes("|")) return null;
    const cells = splitTableRow(line);
    if (!cells.length || !cells.every(c => TABLE_SEP_CELL_RE.test(c))) return null;
    return cells.map(c => c.startsWith(":") && c.endsWith(":") ? "center"
      : c.endsWith(":") ? "right" : c.startsWith(":") ? "left" : "");
  }

  // 在 lines[i] 处尝试解析表格;成功返回 { html, next }(next 为表格之后第一行的下标),否则 null。
  // 表头列数必须等于分隔行列数(GFM 规则);数据行在空行、含不到管道符的行、标题/围栏处结束,
  // 列数不足补空、超出截断。流式中只到表头+分隔行时也会渲染出表头。
  function tryParseTable(lines, i, citeMap, opts) {
    const header = lines[i];
    if (!header.includes("|") || i + 1 >= lines.length) return null;
    const aligns = parseTableSeparator(lines[i + 1]);
    if (!aligns) return null;
    const heads = splitTableRow(header);
    if (heads.length !== aligns.length) return null;
    const cell = (tag, text, k) => {
      const al = aligns[k] ? ` class="al-${aligns[k]}"` : "";
      return `<${tag}${al}>${renderInline(text, citeMap, opts)}</${tag}>`;
    };
    const rows = [];
    let j = i + 2;
    for (; j < lines.length; j++) {
      const ln = lines[j];
      if (!ln.trim() || !ln.includes("|")) break;
      if (HEADING_RE.test(ln) || ln.trimStart().startsWith("```")) break;
      const cells = splitTableRow(ln);
      while (cells.length < heads.length) cells.push("");
      rows.push(cells.slice(0, heads.length));
    }
    const html = `<div class="md-table-wrap"><table class="md-table"><thead><tr>` +
      heads.map((h, k) => cell("th", h, k)).join("") + `</tr></thead>` +
      (rows.length ? `<tbody>${rows.map(r => `<tr>${r.map((c, k) => cell("td", c, k)).join("")}</tr>`).join("")}</tbody>` : "") +
      `</table></div>`;
    return { html, next: j };
  }

  function renderTextFragment(text, citeMap, opts) {
    const lines = String(text).split("\n");
    const out = [];
    let para = [];          // 连续普通行:段内换行用 <br>
    let list = null;        // { tag, items }
    const flushPara = () => {
      if (para.length) out.push(`<p class="md-p">${para.join("<br>")}</p>`);
      para = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag} class="md-list">${list.items.map(x => `<li>${x}</li>`).join("")}</${list.tag}>`);
      list = null;
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (line.trimStart().startsWith("```")) {           // 围栏代码:原样转义展示,到闭合围栏或文末
        flushPara(); flushList();
        const buf = [line];
        while (++i < lines.length) {
          buf.push(lines[i]);
          if (lines[i].trimStart().startsWith("```")) break;
        }
        out.push(`<pre class="md-code">${esc(buf.join("\n"))}</pre>`);
        continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      const mb = tryParseMathBlock(lines, i);
      if (mb) {                                           // 独立公式块(可打断段落)
        flushPara(); flushList();
        out.push(mb.html);
        i = mb.next - 1;
        continue;
      }
      const h = HEADING_RE.exec(line);
      if (h) {
        flushPara(); flushList();
        out.push(`<div class="md-h md-h${Math.min(h[1].length, 3)}">${renderInline(h[2], citeMap, opts)}</div>`);
        continue;
      }
      const tbl = tryParseTable(lines, i, citeMap, opts);
      if (tbl) {                                          // 表格可直接打断上一段(无需空行)
        flushPara(); flushList();
        out.push(tbl.html);
        i = tbl.next - 1;
        continue;
      }
      const li = LIST_RE.exec(line);
      if (li) {
        flushPara();
        const tag = /\d/.test(li[1]) ? "ol" : "ul";
        if (list && list.tag !== tag) flushList();
        if (!list) list = { tag, items: [] };
        list.items.push(renderInline(li[2], citeMap, opts));
        continue;
      }
      flushList();
      para.push(renderInline(line.trim(), citeMap, opts));
    }
    flushPara(); flushList();
    return out.join("");
  }

  function renderPlain(text, citeMap, opts) {
    // 公式先换成占位符,避免其中的 * _ [ ] 被粗体/出处规则误伤;最后再还原
    const { text: plain, maths } = extractMath(text);
    let html = esc(plain).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    if (opts.streaming) {
      // 流式期间 chunk 标记只显示中性徽标,避免裸 ID 闪烁
      html = html.replace(/\[chunk:[A-Za-z0-9_]+\]/g, '<span class="chip cite">出处…</span>');
    } else {
      html = html.replace(/\[chunk:([A-Za-z0-9_]+)\]/g, (_, id) =>
        citeMap.has(id)
          ? `<span class="chip cite" title="出处 ${esc(citeMap.get(id))}">出处 ${esc(citeMap.get(id))}</span>`
          : "");
    }
    return maths.length ? restoreMath(html, maths) : html;
  }

  // ---- 图片卡片(正文插图与补充图片区共用)----

  // URL 只接受服务端校验条目里的路径;放大走事件委托(data-zoomable),不把 URL 放进内联 JS
  // locator_label 由服务端按文档类型算好(p.N / 幻灯片 N / 章节路径);旧消息回退"第 N 页"
  function imageCardHTML(im, messageId) {
    const where = im.locator_label || `第 ${im.page} 页`;
    // source_page_url 可空:非 PDF 没有页预览,不渲染链接
    const link = im.source_page_url
      ? ` <a href="${esc(im.source_page_url)}" target="_blank" rel="noopener">查看原页 ↗</a>` : "";
    return `<figure class="img-card inline" id="${figureDomId(messageId, im.image_occurrence_id)}">` +
      `<img src="${esc(im.image_url)}" alt="${esc(im.caption || "文档配图")}" loading="lazy" data-zoomable="1">` +
      `<div class="img-err" hidden>图片加载失败,可打开来源页查看</div>` +
      `<figcaption><b>${esc(figLabel(im.figure_number))}</b> · ${esc(where)}` +
      `${im.caption ? "<br>" + esc(im.caption) : ""}` +
      link +
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
    // 位置文案由服务端给 label(含 "p." 前缀/章节路径);旧消息没有 label 时回退页码
    const citeMap = new Map((m.citations || []).map(c => [c.chunk_id, c.label || "p." + pageLabel(c)]));
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
          parts.push(`<div class="img-ref"><a class="chip fig" href="#${figureDomId(m.message_id, seg.id)}">` +
            `${esc(figLabel(im.figure_number))} · ${esc(im.locator_label || "p." + im.page)}</a></div>`);
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
    parsing: "解析文档", persist_figures: "保存图片", chunks_and_links: "整理正文与图文关系",
    describing: "生成图片描述", indexing: "准备检索", finalizing: "完成保存",
    mineru_split: "读取与拆分", mineru_merge: "合并解析结果", index_prepare: "整理检索内容",
    embed_batches: "生成语义向量",
  };

  // 解析阶段计数的单位:docx/md 的"页"是虚拟分段,不是 Word 的真实页码
  function locatorUnit(kind) {
    return { slide: "张幻灯片", sheet: "个工作表", section: "段" }[kind] || "页";
  }

  // 已等待时长显示(不猜测"还需多久");未知为 —
  function formatElapsed(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return "—";
    // 亚秒按 1 秒显示("0 秒"读起来像没干活);负数只来自异常数据,防护为 0 秒
    const s = ms < 0 ? 0 : Math.max(1, Math.round(ms / 1000));
    if (s < 60) return `${s} 秒`;
    const m = Math.floor(s / 60), rs = s % 60;
    if (m < 60) return rs ? `${m} 分 ${rs} 秒` : `${m} 分`;
    return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
  }

  // 与 ingest._describe_concurrent 的聚合文案对应("N 张并发处理中")
  const CONCURRENT_LABEL_RE = /^\d+ 张并发处理中$/;

  // 列表一行的进度描述;无观测记录返回 null(调用方回退到原状态文案)
  function progressLine(doc) {
    const p = doc.progress;
    if (!p) return null;
    if (p.state === "queued") return `排队中 · 已等待 ${formatElapsed(p.total_elapsed_ms)}`;
    if (p.state === "running") {
      let text = PROG_STAGE_NAMES[p.stage] || "处理中";
      if (p.total) text += ` · 已处理 ${p.completed || 0}/${p.total}`;
      // current_item 只有一格,且随"当前项"切换(下一张图/下一批/下一部分/并发窗口里最久在途的一张换人)
      // 而重新计时:它是"这一项等了多久",不是累计。所以累计耗时必须另外显示,
      // 不能在有当前项时把阶段/总耗时藏起来
      const cur = p.current_item;
      if (cur && cur.label) {
        const concurrent = CONCURRENT_LABEL_RE.test(cur.label);
        text += concurrent ? ` · ${cur.label}` : ` · 当前:${cur.label}`;
        if (cur.elapsed_ms != null) {
          text += `(${concurrent ? "最久一张" : "本项"}已等待 ${formatElapsed(cur.elapsed_ms)})`;
        }
      }
      if (p.stage_elapsed_ms != null) text += ` · 本阶段已耗时 ${formatElapsed(p.stage_elapsed_ms)}`;
      // 总耗时(自开始处理起)与阶段耗时相差不足 1 秒时不重复显示(如第一个阶段)
      if (p.processing_elapsed_ms != null &&
          Math.abs(p.processing_elapsed_ms - (p.stage_elapsed_ms ?? 0)) >= 1000) {
        text += ` · 总耗时 ${formatElapsed(p.processing_elapsed_ms)}`;
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
    return `<div class="prog-sub">图片描述逐张结果(并发处理时各图耗时会重叠):</div><table class="prog-table">${rows}</table>`;
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

  return { esc, extractMath, figLabel, pageLabel, splitAnswerSegments, withholdTrailingPartial, renderAnswerBody,
           imageCardHTML, figureDomId, newMessageId, createAssistantMessage,
           applyStreamEvent, finishStream, restoreMessages,
           PROG_STAGE_NAMES, PROG_STAGE_STATE, locatorUnit, formatElapsed, progressLine, createDocPoller,
           renderChunks, renderImageItems, renderBatchItems, createProgDetailLoader };
});