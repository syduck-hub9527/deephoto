/* segments.js 的 node 断言脚本:由 tests/test_segments_js.py 调用,失败时非零退出。
   覆盖场景(见 md文档/deephoto_inline_images_dev.md §8.1 前端表)
   与四项修复回归(md文档/deephoto_inline_images_fixes.md):
   F1 行内代码不拆块、F2 跨消息 DOM ID 唯一、F3 异常结束不露内部 ID、F4 done 立即终态渲染。 */
"use strict";
const SEG = require("../src/deephoto/web/segments.js");

let failures = 0;
function check(name, cond) {
  if (cond) { console.log("ok - " + name); return; }
  failures++;
  console.error("FAIL - " + name);
}

const E1 = { image_occurrence_id: "occ_a", document_id: "doc1", figure_number: "1.1",
             caption: "容量与尺寸", page: 2,
             image_url: "/api/documents/doc1/images/occ_a",
             source_page_url: "/api/documents/doc1/pages/2" };
const E2 = { image_occurrence_id: "occ_b", document_id: "doc1", figure_number: "1.5",
             caption: "光刻步骤", page: 5,
             image_url: "/api/documents/doc1/images/occ_b",
             source_page_url: "/api/documents/doc1/pages/5" };
const done = (content, images) => ({ role: "assistant", content, images, citations: [], validated: true });

for (const flag of ["broken", "error", "warning", "sessionFailed"]) {
  check(`${flag} 触发会话复查`, SEG.needsSessionRecheck({ [flag]: true }) === true);
}
check("正常完成无需复查", SEG.needsSessionRecheck(done("answer", [])) === false);
check("待审批无需复查", SEG.needsSessionRecheck({ approvals: [] }) === false);
check("空消息无需复查", SEG.needsSessionRecheck(null) === false);

// ---- 既有行为 ----

// 模型调用前的过程说明：按 mid 移段，不能留在最终正文或加载其中的图片。
{
  const message = SEG.createAssistantMessage();
  SEG.applyStreamEvent(message, { type: "token", mid: "call-1", text: "先检索" });
  SEG.applyStreamEvent(message, { type: "token", mid: "call-1", text: "原理。[image:occ_a]" });
  check("S1: 同一 mid 的 token 逐块追加", message.content === "先检索原理。[image:occ_a]");
  SEG.applyStreamEvent(message, { type: "step", mid: "call-1", text: "先检索原理。[image:occ_a]" });
  check("S2: step 将该次调用移出正文", message.content === "" && message.segments[0].step);
  check("S3: step 保存为过程说明", message.steps.join("") === "先检索原理。[image:occ_a]");
  check("S4: 过程说明不触发图片加载", !SEG.renderAnswerBody(message).html.includes("<img"));
  SEG.applyStreamEvent(message, { type: "token", mid: "call-2", text: "再核对参数。" });
  SEG.applyStreamEvent(message, { type: "step", mid: "call-2", text: "再核对参数。" });
  SEG.applyStreamEvent(message, { type: "token", mid: "call-3", text: "最终答" });
  SEG.applyStreamEvent(message, { type: "token", mid: "call-3", text: "案。" });
  check("S5: 多次工具调用都移出正文", message.steps.length === 2 && message.content === "最终答案。");
  SEG.applyStreamEvent(message, { type: "thinking", text: "独立的推理内容" });
  check("S6: thinking 与过程说明独立", message.thinking === "独立的推理内容" && message.steps.length === 2);
  SEG.applyStreamEvent(message, { type: "done", answer: "服务端校验后的正文", citations: [], images: [] });
  check("S7: done 正文是权威值", message.content === "服务端校验后的正文" && !message.streaming && message.validated);
  check("S8: 完成后仍可折叠查看过程说明", message.steps.length === 2);
  SEG.applyStreamEvent(message, { type: "done", answer: "", citations: [], images: [] });
  check("S9: 空的 done.answer 也覆盖临时文字", message.content === "");
}
{
  const message = SEG.createAssistantMessage();
  SEG.applyStreamEvent(message, { type: "token", text: "旧版" });
  SEG.applyStreamEvent(message, { type: "token", text: "正文" });
  check("S10: 没有 mid 的旧事件仍逐块追加", message.content === "旧版正文" && message.steps.length === 0);
  const resumed = SEG.fromHistoryMessage({ role: "assistant", answer: "历史回答", status: "complete" });
  SEG.applyStreamEvent(resumed, { type: "step", mid: "resume-1", text: "查看原图。" });
  SEG.applyStreamEvent(resumed, { type: "token", mid: "resume-2", text: "恢复后的正文" });
  check("S11: 没有分段字段的历史消息可接收恢复流", resumed.content === "恢复后的正文" && resumed.steps[0] === "查看原图。");
}

// 1. 文字 A + 图 A + 文字 B:DOM 顺序严格为文字、figure、文字
{
  const segs = SEG.splitAnswerSegments("先看。\n\n[image:occ_a]\n\n再解释。");
  check("分段为 文字/图/文字", segs.map(s => s.type).join(",") === "text,image,text");
  const { html } = SEG.renderAnswerBody(done("先看。\n\n[image:occ_a]\n\n再解释。", [E1]));
  const iText1 = html.indexOf("先看"), iFig = html.indexOf("<figure"), iText2 = html.indexOf("再解释");
  check("渲染顺序 文字<figure<文字", iText1 >= 0 && iText1 < iFig && iFig < iText2);
}

// 2. 两张图分布在两段之间,末尾不重复(补充区为空)
{
  const { html, supplement } = SEG.renderAnswerBody(
    done("甲。\n\n[image:occ_a]\n\n乙。\n\n[image:occ_b]\n\n丙。", [E1, E2]));
  check("两图各自在对应位置", html.indexOf('id="fig-') > html.indexOf("甲。")
    && html.indexOf('id="fig-') < html.indexOf("乙。") && html.lastIndexOf('id="fig-') > html.indexOf("乙。"));
  check("末尾不重复", supplement.length === 0);
  check("只有两张卡片", (html.match(/<figure class="img-card inline"/g) || []).length === 2);
}

// 3. 同一 occurrence 两次引用:一张卡片,第二次为徽标
{
  const m = done("一[image:occ_a]二[image:occ_a]三", [E1]);
  const { html } = SEG.renderAnswerBody(m);
  check("只渲染一张卡片", (html.match(/<figure class="img-card inline"/g) || []).length === 1);
  check("第二次为引用徽标", html.includes('class="chip fig"')
    && html.includes(`href="#${SEG.figureDomId(m.message_id, "occ_a")}"`));
}

// 4. images 数组顺序与正文相反:展示顺序仍按正文
{
  const { html } = SEG.renderAnswerBody(done("先[image:occ_a]后[image:occ_b]", [E2, E1]));
  check("顺序跟随正文", html.indexOf("images/occ_a") < html.indexOf("images/occ_b"));
}

// 5. 无效 ID:不发起图片请求,不显示裸 ID
{
  const { html } = SEG.renderAnswerBody(done("看[image:occ_bad]这", [E1]));
  check("无 <img>", !html.includes("<img"));
  check("不显示裸 ID", !html.includes("occ_bad"));
  check("显示不可用提示", html.includes("图片引用不可用"));
}

// 6. 标记跨多个 token:不闪现内部 ID,不丢字(原文未动)
{
  const streaming = SEG.createAssistantMessage();
  streaming.content = "文字[ima";
  const html1 = SEG.renderAnswerBody(streaming).html;
  check("末尾未闭合标记暂缓显示", !html1.includes("[ima"));
  streaming.content = "文字[image:occ_a]后续";
  const html2 = SEG.renderAnswerBody(streaming).html;
  check("流式中完整标记显示占位", html2.includes("图片将在回答完成后显示"));
  check("流式中不加载图片、不露 ID", !html2.includes("<img") && !html2.includes("occ_a"));
  check("原始正文未被修改", streaming.content === "文字[image:occ_a]后续");
  check("withhold 只截协议候选尾", SEG.withholdTrailingPartial("前文[image:occ") === "前文"
    && SEG.withholdTrailingPartial("完整[image:occ_a]保留") === "完整[image:occ_a]保留");
}

// 7. 代码中的标记:作为代码示例,不插图
{
  const content = "示例:`[image:occ_a]` 以及围栏:\n```\n[image:occ_b]\n```\n真图:\n\n[image:occ_a]";
  const segs = SEG.splitAnswerSegments(content);
  const imgSegs = segs.filter(s => s.type === "image");
  check("代码区不产生图片片段", imgSegs.length === 1 && imgSegs[0].id === "occ_a");
  const { html } = SEG.renderAnswerBody(done(content, [E1]));
  check("代码示例标记按文字展示", html.includes("[image:occ_b]"));
}

// 8. 旧消息没有锚点:进补充图片区,不丢失
{
  const legacy = { role: "assistant", content: "纯文字旧回答", images: [E1, E2], citations: [] };
  const { html, supplement } = SEG.renderAnswerBody(legacy);   // 无 validated 字段:完成态视为已校验
  check("旧消息正文无图", !html.includes("<figure"));
  check("旧消息图片进补充区", supplement.length === 2);
}

// 9. HTML/引号等恶意内容:作为文字展示,不执行脚本
{
  const evil = { ...E1, caption: '<script>alert(1)</script><img src=x onerror="alert(2)">' };
  const { html } = SEG.renderAnswerBody(done('正文 <img src=x onerror="alert(3)"> [image:occ_a]', [evil]));
  check("正文脚本被转义", !html.includes('<img src=x onerror="alert(3)">'));
  check("图注脚本被转义", !html.includes("<script>") && html.includes("&lt;script&gt;"));
}

// 10. 网络中断/未完成校验:保留正文,图片位置提示清晰,补充区为空
{
  const m = { role: "assistant", content: "写到一半[image:occ_a]", broken: true };
  const { html, supplement } = SEG.renderAnswerBody(m);
  check("broken 保留正文", html.includes("写到一半"));
  check("broken 显示未完成校验", html.includes("图片未完成校验"));
  check("broken 无补充图片、不发请求", supplement.length === 0 && !html.includes("<img"));
}

// ---- F1:行内代码不拆块 ----
{
  const segs = SEG.splitAnswerSegments("电阻用 `R` 表示,单位为欧姆。");
  check("F1: 行内代码不拆块", segs.length === 1 && segs[0].type === "text");
  const { html } = SEG.renderAnswerBody(done("电阻用 `R` 表示,单位为欧姆。", []));
  check("F1: 单个文字块", (html.match(/<div class="text">/g) || []).length === 1);
  check("F1: 行内代码保留", html.includes("<code>R</code>"));
  const multi = SEG.renderAnswerBody(done("用 `R` 和 `C` 两个符号。", [])).html;
  check("F1: 多个行内代码不新增块", (multi.match(/<div class="text">/g) || []).length === 1);
  const segs2 = SEG.splitAnswerSegments("图前 `[image:occ_x]` 文字\n\n[image:occ_a]\n\n图后");
  check("F1: 锚点仍切开文字", segs2.map(s => s.type).join(",") === "text,image,text");
  check("F1: 原始换行保留", segs2[0].text.includes("\n\n"));
  const chips = SEG.renderAnswerBody({ role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "chk_1", page: 3 }], content: "见 [chunk:chk_1],示例 `[chunk:chk_9]`" }).html;
  check("F1: 代码内的 chunk 标记不换徽标", chips.includes("出处 p.3") && chips.includes("[chunk:chk_9]"));
}

// ---- F2:跨消息 DOM ID 唯一 ----
{
  const m1 = SEG.createAssistantMessage();
  m1.content = "一[image:occ_a]"; m1.images = [E1]; m1.validated = true; m1.streaming = false;
  const m2 = SEG.createAssistantMessage();
  m2.content = "二[image:occ_a] 再提[image:occ_a]"; m2.images = [E1]; m2.validated = true; m2.streaming = false;
  const html = SEG.renderAnswerBody(m1).html + SEG.renderAnswerBody(m2).html;
  const ids = html.match(/id="[^"]+"/g) || [];
  check("F2: 跨消息 DOM ID 唯一", new Set(ids).size === ids.length);
  check("F2: 第二轮徽标指向本轮卡片",
    SEG.renderAnswerBody(m2).html.includes(`href="#${SEG.figureDomId(m2.message_id, "occ_a")}"`));
  check("F2: 业务图片身份不变", html.includes('src="/api/documents/doc1/images/occ_a"'));
  check("F2: 两条消息各一张卡片", (html.match(/<figure class="img-card inline"/g) || []).length === 2);
}

// ---- F3:异常结束不露内部 ID ----
{
  const m1 = { role: "assistant", content: "前文[ima", broken: true };
  check("F3: 断流隐藏 [ima", !SEG.renderAnswerBody(m1).html.includes("[ima"));
  const m2 = { role: "assistant", content: "前文[image:occ_a", broken: true };
  const h2 = SEG.renderAnswerBody(m2).html;
  check("F3: 断流不露图片 ID、不发请求", !h2.includes("occ_a") && !h2.includes("<img"));
  const m3 = { role: "assistant", content: "前文[chunk:chk_a", broken: true };
  check("F3: 断流不露块 ID", !SEG.renderAnswerBody(m3).html.includes("chk_a"));
  const m4 = { role: "assistant", content: "正常 [ABC] 文字", broken: true };
  check("F3: 普通方括号保留", SEG.renderAnswerBody(m4).html.includes("[ABC]"));
  const m5 = { role: "assistant", content: "文[image:occ_a]", images: [E1], validated: false, streaming: false };
  const h5 = SEG.renderAnswerBody(m5).html;
  check("F3: 明确未校验不当作已验证(刷新恢复)", !h5.includes("<img") && h5.includes("未完成校验"));
  // 事件路径:error 不拼正文、EOF 无 done 标 broken、自然语言保留
  const m6 = SEG.createAssistantMessage();
  SEG.applyStreamEvent(m6, { type: "token", text: "半截[image:occ_a" });
  SEG.applyStreamEvent(m6, { type: "error", detail: "boom" });
  check("F3: error 不拼进正文", m6.content === "半截[image:occ_a" && m6.error === "boom");
  SEG.finishStream(m6);
  check("F3: error 后渲染不露 ID", !SEG.renderAnswerBody(m6).html.includes("occ_a"));
  const m7 = SEG.createAssistantMessage();
  SEG.applyStreamEvent(m7, { type: "token", text: "只有文字" });
  SEG.finishStream(m7);
  check("F3: 无 done 的 EOF 标记 broken 并提示", m7.broken && !!m7.warning);
  check("F3: 已生成文字保留", SEG.renderAnswerBody(m7).html.includes("只有文字"));
  const m8 = { role: "assistant", content: "示例 `[image:occ_a]` 结束", broken: true };
  const h8 = SEG.renderAnswerBody(m8).html;
  check("F3: 代码区标记保持示例文本、不插图", h8.includes("[image:occ_a]") && !h8.includes("<img"));
}

// ---- F4:done 到达立即终态渲染 ----
{
  const m = { role: "assistant", content: "看[image:occ_a]图", images: [E1], citations: [],
              streaming: true, validated: true };
  const { html } = SEG.renderAnswerBody(m);
  check("F4: streaming+validated 直接显示卡片", html.includes("<img") && !html.includes("完成后显示"));
  // 完整事件序列:token -> done(暂不 EOF)-> EOF
  const m2 = SEG.createAssistantMessage();
  SEG.applyStreamEvent(m2, { type: "token", text: "看" });
  SEG.applyStreamEvent(m2, { type: "token", text: "[image:occ_a]" });
  check("F4: 完成前仍是占位", SEG.renderAnswerBody(m2).html.includes("完成后显示"));
  SEG.applyStreamEvent(m2, { type: "done", answer: "看[image:occ_a]", citations: [], images: [E1] });
  check("F4: done 立即终态(未 EOF)", !m2.streaming && m2.validated
    && SEG.renderAnswerBody(m2).html.includes("<img"));
  check("F4: done 后补充区立即可用", SEG.renderAnswerBody({ ...m2, images: [E1, E2] }).supplement.length === 1);
  SEG.finishStream(m2);
  check("F4: EOF 不撤销有效答案", !m2.broken && SEG.renderAnswerBody(m2).html.includes("<img"));
  // 恢复:流式态被清除、消息 ID 补齐且不重复
  const restored = SEG.restoreMessages([
    { role: "assistant", content: "旧[image:occ_a]", images: [E1], streaming: true },
    { role: "assistant", content: "更旧", images: [] },
  ]);
  check("F2/F4: 恢复清除流式态并补消息 ID",
    restored.every(m => m.streaming === false) && !!restored[0].message_id
    && restored[0].message_id !== restored[1].message_id);
  check("F4: 恢复的旧消息按兼容规则渲染", SEG.renderAnswerBody(restored[0]).html.includes("<img"));
}

// ---- R:回答渲染(Markdown 子集 / 徽标 / 页码范围)----
{
  const h = SEG.renderAnswerBody(done("## 标题\n\n正文一行\n第二行\n\n- 甲\n- 乙\n\n1. 一\n2. 二", [])).html;
  check("R1: 标题不露 ##", !h.includes("##") && h.includes('class="md-h'));
  check("R1: 无序列表渲染为 ul,不露行首 -", h.includes("<ul") && (h.match(/<li>/g) || []).length === 4 && !/>\s*- /.test(h));
  check("R1: 有序列表渲染为 ol", h.includes("<ol"));
  check("R1: 段内换行用 br", h.includes("正文一行<br>第二行"));
  check("R1: 空行不产生空段落", !h.includes("<p class=\"md-p\"></p>"));
  check("R1: 仍是单个文字块 div", (h.match(/<div class="text">/g) || []).length === 1);

  const bold = SEG.renderAnswerBody(done("**核心规律:**\n- 每三年**增加四倍**", [])).html;
  check("R2: 粗体在标题行与列表项内都生效", (bold.match(/<strong>/g) || []).length === 2);

  const fence = SEG.renderAnswerBody(done("示例:\n```\n## 不是标题\n- 不是列表\n```\n后文", [])).html;
  check("R3: 围栏代码内的 ## 与 - 不被解释", fence.includes("md-code") && !fence.includes("<li>") && !fence.includes("md-h"));

  const xss = SEG.renderAnswerBody(done("- <img src=x onerror=alert(1)>\n## <b>x</b>", [])).html;
  check("R4: 列表/标题内 HTML 仍被转义", !xss.includes("<img") && !xss.includes("<b>"));

  check("R5: pageLabel 单页/跨页/缺省", SEG.pageLabel({ page: 3 }) === "3"
    && SEG.pageLabel({ page: 1, page_end: 2 }) === "1\u20132"
    && SEG.pageLabel({ page: 4, page_end: 4 }) === "4");
  const cite = SEG.renderAnswerBody({ role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "chk_1", page: 1, page_end: 2 }, { chunk_id: "chk_2", page: 7 }],
    content: "甲[chunk:chk_1]乙[chunk:chk_2]" }).html;
  check("R5: 跨页块徽标显示 p.1–2,单页显示 p.7", cite.includes("出处 p.1\u20132") && cite.includes("出处 p.7"));
  check("R6: 徽标不含 emoji 图标", !/[\u{1F300}-\u{1FAFF}]/u.test(cite));

  const m = { role: "assistant", validated: true, message_id: "m1", images: [E1], citations: [],
              content: "看图\n\n[image:occ_a]\n\n再看\n\n[image:occ_a]\n\n结束" };
  const rep = SEG.renderAnswerBody(m).html;
  check("R7: 重复锚点的引用徽标不含 emoji", rep.includes('class="chip fig"') && !/[\u{1F300}-\u{1FAFF}]/u.test(rep.split('class="chip fig"')[1].split("</a>")[0]));
}

// ---- T:表格渲染(GFM 管道表格;见 md文档/deephoto_table_render_fix.md)----
{
  const render = (content, extra) => SEG.renderAnswerBody(Object.assign(done(content, []), extra || {})).html;
  const SHOT = "在 MQTT 通信中,主题用于分类:\n\n| 分类 | Topic 主题 | 设计说明 |\n|------|-----------|---------|\n" +
    "| 设备心跳上报 | alarm/device/ping | 用于判断设备是否离线 |\n| 温湿度数据上报 | alarm/device/temp | 供后端解析 |\n\n合理的主题划分。";
  const h = render(SHOT);
  check("T1 截图同款表格渲染为 <table>", h.includes("<table") && h.includes("</table>"));
  check("T1 表头 3 列 / 数据 2 行", (h.match(/<th[ >]/g) || []).length === 3 && (h.match(/<tr>/g) || []).length === 3);
  check("T1 不再出现裸管道符分隔行", !h.includes("|------|") && !h.includes("<br>|"));
  check("T1 表格前后段落保留", h.indexOf("主题用于分类") < h.indexOf("<table") && h.indexOf("</table>") < h.indexOf("合理的主题划分"));

  // 无外侧管道符、对齐、转义管道
  const h2 = render("a | b | c\n:--- | :---: | ---:\n1 | 2 | x\\|y");
  check("T2 无首尾管道符可识别", h2.includes("<table"));
  check("T2 对齐 class", h2.includes('class="al-center"') && h2.includes('class="al-right"'));
  check("T2 \\| 还原为字面竖线且不拆列", h2.includes("x|y") && (h2.match(/<td/g) || []).length === 3);

  // 不是表格的情况
  check("T3 分隔行缺失 → 不是表格", !render("| a | b |\n| c | d |").includes("<table"));
  check("T3 列数不等 → 不是表格", !render("| a | b |\n|---|---|---|\n| 1 | 2 |").includes("<table"));
  check("T3 水平线 --- 不误判", !render("标题行 | 含竖线\n---\n正文").includes("<table"));

  // 数据行:补齐 / 截断 / 遇空行结束
  const h4 = render("| a | b |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |\n\n后文");
  check("T4 缺列补空、超列截断", (h4.match(/<td/g) || []).length === 4 && !h4.includes(">3<"));
  check("T4 空行后回到段落", h4.indexOf("</table>") < h4.indexOf("后文"));

  // 表格可直接打断上一段(无空行)
  const h5 = render("先说明\n| a | b |\n|---|---|\n| 1 | 2 |");
  check("T5 表格打断段落", h5.indexOf("先说明") < h5.indexOf("<table") && h5.includes("<td>1</td>"));

  // 单元格内的行内 Markdown / 转义 / 引用徽标
  const cm = { role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "c1", page: 11, page_end: 12 }],
    content: "| k | v |\n|---|---|\n| **粗** | `code` <b>x</b> [chunk:c1] |" };
  const h6 = SEG.renderAnswerBody(cm).html;
  check("T6 单元格支持粗体", h6.includes("<td><strong>粗</strong></td>"));
  check("T6 单元格 HTML 被转义", !h6.includes("<b>x</b>") && h6.includes("&lt;b&gt;x&lt;/b&gt;"));
  check("T6 单元格内出处徽标", h6.includes('class="chip cite"') && h6.includes("p.11"));

  // 与图片锚点共存;流式中不崩、半张表也能显示表头
  const h7 = render("| a | b |\n|---|---|\n| 1 | 2 |\n\n[image:occ_a]\n\n结尾", { images: [E1] });
  check("T7 表格与图片锚点共存", h7.includes("<table") && h7.includes("<figure") && h7.indexOf("</table>") < h7.indexOf("<figure"));
  const h8 = SEG.renderAnswerBody({ role: "assistant", streaming: true, content: "| a | b |\n|---|---|\n| 1" }).html;
  check("T8 流式半张表:表头+补空行", h8.includes("<th") && (h8.match(/<td/g) || []).length === 2);
  const h9 = SEG.renderAnswerBody({ role: "assistant", streaming: true, content: "| a | b |\n|---|---|" }).html;
  check("T8 流式仅表头+分隔行:只有表头", h9.includes("<th") && !h9.includes("<td"));

  // 围栏代码里的表格原样展示
  check("T9 围栏代码内不渲染表格", !render("```\n| a | b |\n|---|---|\n| 1 | 2 |\n```").includes("<table"));
}

// ---- L:数学公式(LaTeX;见 md文档/deephoto_math_render_fix.md)----
{
  const render = (content, extra) => SEG.renderAnswerBody(Object.assign(done(content, []), extra || {})).html;
  const texOf = html => [...html.matchAll(/data-tex="([^"]*)"/g)].map(m => m[1]);

  // 截图里的真实公式
  const inl = render("平均速率 $\\bar{c}=\\sqrt{8kT/\\pi m}$;均方根 $v_{rms}=\\sqrt{3kT/m}$。");
  check("L1 行内公式变占位节点", texOf(inl).length === 2 && inl.includes('data-display="0"'));
  check("L1 tex 原样保留(反斜杠/下划线/花括号不被吃)", texOf(inl)[0] === "\\bar{c}=\\sqrt{8kT/\\pi m}"
    && texOf(inl)[1] === "v_{rms}=\\sqrt{3kT/m}");
  check("L1 占位节点内是原文(KaTeX 未加载时的回退)", inl.includes(">$\\bar{c}=\\sqrt{8kT/\\pi m}$</span>"));

  const blk = render("速度分布:\n\n$$p(\\mathbf{v}) = 4\\pi\\left[\\frac{m}{2\\pi kT}\\right]^{3/2}v^2 e^{-mv^2/2kT}$$\n\n式中 m 是质量。");
  check("L2 独立公式渲染为 math-block", blk.includes('class="math-block"') && blk.includes('data-display="1"'));
  check("L2 独立公式前后文字保留", blk.indexOf("速度分布") < blk.indexOf("math-block") && blk.indexOf("math-block") < blk.indexOf("式中 m"));
  check("L2 公式里的 [ ] 不被当成引用标记", texOf(blk)[0].includes("\\left[\\frac{m}{2\\pi kT}\\right]^{3/2}"));

  const multi = render("$$\n\\lambda = \\frac{1}{\\sqrt{2}\\pi d^2 n}\n$$");
  check("L3 多行 $$ 块", multi.includes("math-block") && texOf(multi)[0] === "\\lambda = \\frac{1}{\\sqrt{2}\\pi d^2 n}");

  check("L4 \\( \\) 行内与 \\[ \\] 独立",
    texOf(render("式 \\(a+b\\) 成立")).join() === "a+b"
    && render("\\[x^2\\]").includes("math-block"));

  // 不误判
  check("L5 金额不当公式", texOf(render("价格 $5 和 $10 都可以")).length === 0);
  check("L5 \\$ 是字面美元", texOf(render("花了 \\$5 和 \\$10")).length === 0 && render("花了 \\$5").includes("花了 $5"));
  check("L5 空白紧贴的 $ 不识别", texOf(render("a $ b $ c")).length === 0);
  check("L5 未闭合的 $$(流式中)不生成公式块", !render("$$\n\\frac{a}{b}").includes("math-block"));
  check("L5 单个 $ 不吞后文", render("只有一个 $ 符号,后文照常").includes("后文照常"));

  // 代码里不解释
  check("L6 行内代码里的 $x$ 保持代码", texOf(render("写作 `$x$` 即可")).length === 0 && render("写作 `$x$` 即可").includes("<code>$x$</code>"));
  check("L6 围栏代码里不解释", texOf(render("```\n$$x^2$$\n```")).length === 0);

  // 与其他规则共存
  const bold = render("**结论** $a**b**c$ 与 $x_1$");
  check("L7 公式内的 ** 不被粗体规则破坏", texOf(bold).join("|") === "a**b**c|x_1" && bold.includes("<strong>结论</strong>"));
  const evil = render("$<img src=x onerror=alert(1)>$");
  check("L7 公式源码被转义,无法注入标签", !evil.includes("<img") && texOf(evil)[0].includes("&lt;img"));
  const cm = SEG.renderAnswerBody({ role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "c1", page: 11, page_end: 12 }], content: "速度 $v=\\sqrt{2}$ [chunk:c1]" }).html;
  check("L7 公式后的出处徽标仍在", cm.includes('class="chip cite"') && texOf(cm).length === 1);
  const tbl = render("| 量 | 式 |\n|---|---|\n| 平均 | $\\bar{c}$ |");
  check("L7 表格单元格内公式", tbl.includes("<table") && texOf(tbl)[0] === "\\bar{c}");
  check("L7 标题与列表里的公式", texOf(render("## 式 $E=mc^2$\n\n- 项 $a_1$")).join("|") === "E=mc^2|a_1");
  const stream = SEG.renderAnswerBody({ role: "assistant", streaming: true, content: "速度 $v=\\sqrt{2}$ 和 $未闭合" }).html;
  check("L8 流式:已闭合公式即生成,未闭合保持文字", texOf(stream).length === 1 && stream.includes("$未闭合"));

  // 真实 KaTeX:截图里的公式必须能被引擎无错排版(throwOnError: true)
  const katex = require("../src/deephoto/web/vendor/katex/katex.min.js");
  const real = [
    "p(\\mathbf{v}) = 4\\pi\\left[\\frac{m}{2\\pi kT}\\right]^{3/2}v^2 e^{-mv^2/2kT}",
    "\\bar{c}=\\sqrt{8kT/\\pi m}", "\\bar{v}_x=\\bar{v}_y=\\bar{v}_z=\\sqrt{2kT/\\pi m}", "v_{rms}=\\sqrt{3kT/m}",
    "\\lambda = \\frac{1}{\\sqrt{2}\\pi d^2 n}", "\\lambda = \\frac{kT}{\\sqrt{2}\\pi d^2 p}",
    "\\lambda \\ll L", "J_n = \\frac{n\\bar{v}_x}{2} = \\sqrt{\\frac{p^2}{2\\pi kTm}}",
    "6\\times10^{29}\\ \\mathrm{cm}^{-2}\\mathrm{s}^{-1}",
  ];
  let bad = [];
  for (const tex of real) {
    try { katex.renderToString(tex, { throwOnError: true, strict: "ignore", trust: false }); }
    catch (e) { bad.push(tex + " → " + e.message); }
  }
  check("L9 截图中的公式全部可被 KaTeX 排版" + (bad.length ? ":" + bad.join(";") : ""), bad.length === 0);
  check("L9 排版结果含 katex 节点", katex.renderToString("x^2", { displayMode: true }).includes("katex-display"));
}

// ---- M:多格式(位置文案 / 可空页预览 / 长章节胶囊)----
{
  // 服务端给 label:章节路径直接显示(渲染层 esc,">" 转义为 &gt;);长路径带 title 供悬停看全文
  const longLabel = "第1章 引论 > 1.2 存储器容量进展";
  const escLabel = longLabel.replace(/>/g, "&gt;");
  const labeled = SEG.renderAnswerBody({ role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "chk_1", page: 2, label: longLabel }],
    content: "结论[chunk:chk_1]" }).html;
  check("M1: 胶囊显示服务端 label(章节路径)", labeled.includes("出处 " + escLabel));
  check("M1: 长路径胶囊带 title", labeled.includes(`title="出处 ${escLabel}"`));

  // 旧消息没有 label:回退页码,本地缓存的历史消息不会坏
  const legacy = SEG.renderAnswerBody({ role: "assistant", validated: true, images: [],
    citations: [{ chunk_id: "chk_2", page: 7 }], content: "旧[chunk:chk_2]" }).html;
  check("M2: 无 label 的旧 citation 仍显示页码", legacy.includes("出处 p.7"));

  // source_page_url 为 null(md/txt 等无页预览):不出现"查看原页"链接,位置用 locator_label
  const mdImg = Object.assign({}, E1, { source_page_url: null, locator_label: "第2章 > 2.1 结构" });
  const card = SEG.imageCardHTML(mdImg, "m9");
  check("M3: 无页预览时不渲染查看链接", !card.includes("查看原页"));
  check("M3: 图片卡片位置用 locator_label", card.includes("第2章 &gt; 2.1 结构"));
  // 有页预览(PDF)时保留链接
  check("M4: PDF 仍渲染查看原页链接", SEG.imageCardHTML(E1, "m9").includes("查看原页 ↗"));
}


// ---- H:历史会话消息映射、日历分组与相对时间 ----
{
  const user = SEG.fromHistoryMessage({ role: "user", content: "问题", message_id: "u1" });
  check("H1: user 历史消息只保留角色与正文", JSON.stringify(user) === JSON.stringify({ role: "user", content: "问题" }));
  const complete = SEG.fromHistoryMessage({ role: "assistant", answer: "回答", status: "complete",
    message_id: "server-ai", citations: [{ chunk_id: "c1" }], images: [E1] });
  check("H2: complete 恢复已校验正文、出处、图片和消息 ID",
    complete.content === "回答" && complete.validated && !complete.streaming && !complete.broken
    && complete.message_id === "server-ai" && complete.citations.length === 1 && complete.images.length === 1
    && complete.thinking === "" && complete.warning === undefined);
  const interrupted = SEG.fromHistoryMessage({ role: "assistant", answer: "", status: "interrupted" });
  check("H3: interrupted 映射为 broken 和本轮未完成", interrupted.broken && !interrupted.validated
    && interrupted.warning === "本轮未完成" && !!interrupted.message_id);
  const running = SEG.fromHistoryMessage({ role: "assistant", answer: "", status: "running" });
  check("H4: running 提示其他窗口生成中但不标 broken", !running.broken && !running.validated
    && running.warning === "回答生成中(其他窗口)");
  const awaiting = SEG.fromHistoryMessage({ role: "assistant", answer: "", status: "awaiting_approval" });
  check("H5: awaiting 保持非流式、未校验且不误报中断", !awaiting.streaming && !awaiting.validated
    && !awaiting.broken && awaiting.warning === undefined);

  const groupNow = new Date(2026, 0, 2, 12, 0, 0);
  const grouped = SEG.groupSessions([
    { id: "today", updated_at: "2026-01-02T00:00:00" },
    { id: "yesterday", updated_at: "2026-01-01T23:59:59" },
    { id: "seven", updated_at: "2025-12-26T12:00:00" },
    { id: "older", updated_at: "2025-12-25T12:00:00" },
  ], groupNow);
  check("H6: 今天 00:00 / 昨天 23:59 / 恰好 7 天 / 更早分组",
    grouped.map(g => g.label).join(",") === "今天,昨天,近 7 天,更早"
    && grouped.every((g, i) => g.items[0].id === ["today", "yesterday", "seven", "older"][i]));
  const crossYear = SEG.groupSessions([{ id: "ny", updated_at: "2025-12-31T23:59:59" }],
    new Date(2026, 0, 1, 0, 0, 0));
  check("H7: 跨年昨天且空组不输出", crossYear.length === 1 && crossYear[0].label === "昨天");

  const relativeNow = new Date(2026, 4, 15, 12, 0, 0);
  check("H8: relativeTime 刚刚与各分钟档", SEG.relativeTime(new Date(2026, 4, 15, 11, 59, 30), relativeNow) === "刚刚"
    && SEG.relativeTime(new Date(2026, 4, 15, 11, 59, 0), relativeNow) === "1 分钟前"
    && SEG.relativeTime(new Date(2026, 4, 15, 11, 55, 0), relativeNow) === "5 分钟前");
  check("H9: relativeTime 小时、昨天与 MM/DD", SEG.relativeTime(new Date(2026, 4, 15, 11, 0, 0), relativeNow) === "1 小时前"
    && SEG.relativeTime(new Date(2026, 4, 15, 9, 0, 0), relativeNow) === "3 小时前"
    && SEG.relativeTime(new Date(2026, 4, 14, 11, 0, 0), relativeNow) === "昨天"
    && SEG.relativeTime(new Date(2026, 4, 2, 12, 0, 0), relativeNow) === "05/02");
}

// ---- U:解析阶段计数单位(docx/md 的"页"是虚拟分段)----
{
  check("U1: locatorUnit 按位置类型给单位", SEG.locatorUnit("section") === "段" && SEG.locatorUnit("slide") === "张幻灯片"
    && SEG.locatorUnit("sheet") === "个工作表" && SEG.locatorUnit("page") === "页");
  check("U2: 缺省/未知类型回退为 页(旧记录无 unit)", SEG.locatorUnit(undefined) === "页" && SEG.locatorUnit("x") === "页");
}

// ---- 入库进度:格式化与轮询器(md文档/deephoto_ingestion_progress_plan.md §11/§14.12)----
{
  check("formatElapsed 秒/分/小时", SEG.formatElapsed(42000) === "42 秒"
    && SEG.formatElapsed(125000) === "2 分 5 秒"
    && SEG.formatElapsed(3720000) === "1 小时 2 分"
    && SEG.formatElapsed(null) === "—");
  check("formatElapsed 负数防护", SEG.formatElapsed(-5) === "0 秒");
  check("formatElapsed 亚秒按 1 秒显示,不显示 0 秒", SEG.formatElapsed(0) === "1 秒"
    && SEG.formatElapsed(999) === "1 秒" && SEG.formatElapsed(1500) === "2 秒");

  const running = { status: "describing", progress: { state: "running", stage: "describing",
    completed: 3, total: 7, stage_elapsed_ms: 120000, total_elapsed_ms: 241200,
    current_item: { index: 4, label: "图 1.4", elapsed_ms: 42000 } } };
  check("进度行:阶段+计数+当前项+累计耗时", SEG.progressLine(running) ===
    "生成图片描述 · 已处理 3/7 · 当前:图 1.4(本项已等待 42 秒) · 本阶段已耗时 2 分");
  {
    const at = (elapsed) => ({ progress: { state: "running", stage: "describing", total: 7, completed: 3,
      stage_elapsed_ms: 100000 + elapsed, processing_elapsed_ms: 400000 + elapsed,
      current_item: { label: "图 1.4", elapsed_ms: 3000 } } });
    const a = SEG.progressLine(at(0)), b = SEG.progressLine(at(60000));
    check("进度行:当前项等待重置时,阶段/总耗时仍单调累加", a !== b
      && a.includes("本阶段已耗时 1 分 40 秒") && a.includes("总耗时 6 分 40 秒")
      && b.includes("本阶段已耗时 2 分 40 秒") && b.includes("总耗时 7 分 40 秒"));
    const conc = { progress: { state: "running", stage: "describing", total: 715, completed: 564,
      stage_elapsed_ms: 723000, processing_elapsed_ms: 900000,
      current_item: { label: "8 张并发处理中", elapsed_ms: 6000 } } };
    check("进度行:并发窗口口径", SEG.progressLine(conc) ===
      "生成图片描述 · 已处理 564/715 · 8 张并发处理中(最久一张已等待 6 秒) · 本阶段已耗时 12 分 3 秒 · 总耗时 15 分");
    const embed = { progress: { state: "running", stage: "embed_batches", total: 9, completed: 2,
      stage_elapsed_ms: 30000, processing_elapsed_ms: 800000,
      current_item: { label: "第 3/9 批", elapsed_ms: 2000 } } };
    check("进度行:向量批次也显示累计", SEG.progressLine(embed).includes("本阶段已耗时 30 秒")
      && SEG.progressLine(embed).includes("总耗时 13 分 20 秒"));
    const mineru = { progress: { state: "running", stage: "parsing", total: 4, completed: 1,
      stage_elapsed_ms: 200000, processing_elapsed_ms: 200500,
      current_item: { label: "第 2 部分 · 云端解析中", elapsed_ms: 20000 } } };
    check("进度行:MinerU 第 2 部分不再让累计归零", SEG.progressLine(mineru) ===
      "解析文档 · 已处理 1/4 · 当前:第 2 部分 · 云端解析中(本项已等待 20 秒) · 本阶段已耗时 3 分 20 秒");
    const noItem = { progress: { state: "running", stage: "chunks_and_links",
      stage_elapsed_ms: 5000, processing_elapsed_ms: 60000 } };
    check("进度行:无当前项时仍显示阶段与总耗时", SEG.progressLine(noItem) ===
      "整理正文与图文关系 · 本阶段已耗时 5 秒 · 总耗时 1 分");
  }
  check("进度行:排队", SEG.progressLine({ progress: { state: "queued", total_elapsed_ms: 30000 } }) === "排队中 · 已等待 30 秒");
  check("进度行:无观测记录为 null", SEG.progressLine({ progress: null }) === null);
  check("进度行:中断", SEG.progressLine({ progress: { state: "interrupted" } }) === "服务曾重启,未确认自动恢复");

  // 轮询器:连续 start 不叠加定时器;load 返回 false 即停止;失败走 onError 继续
  const timers = [];
  const fakeSetTimeout = (fn) => { timers.push(fn); return timers.length; };
  const fakeClearTimeout = () => { timers.pop(); };
  (async () => {
    let loads = 0;
    const p1 = SEG.createDocPoller({ interval: 10, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
      load: async () => { loads++; return true; } });
    p1.start(); p1.start(); p1.start();
    await new Promise(r => setImmediate(r));
    check("轮询:重复 start 不叠加", timers.length === 1 && loads === 1);
    timers.shift()();   // 手动触发下一拍
    await new Promise(r => setImmediate(r));
    check("轮询:按间隔继续", loads === 2 && timers.length === 1);
    p1.stop();
    check("轮询:stop 后不再调度", timers.length === 0);

    let errors = 0, runs2 = 0;
    const p2 = SEG.createDocPoller({ interval: 10, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
      load: async () => { runs2++; if (runs2 === 1) throw new Error("网络抖动"); return runs2 < 3; },
      onError: () => { errors++; } });
    p2.start();
    for (let i = 0; i < 3; i++) { await new Promise(r => setImmediate(r)); if (timers.length) timers.shift()(); }
    await new Promise(r => setImmediate(r));
    check("轮询:失败提示后继续,终态停止", errors === 1 && runs2 === 3 && !p2.running);

    // 在途时不并发:load 未返回前 tick 不再触发第二次
    let resolveBlock, concurrent = 0, maxConcurrent = 0;
    const p3 = SEG.createDocPoller({ interval: 10, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
      load: async () => { concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
        await new Promise(r => { resolveBlock = r; }); concurrent--; return true; } });
    p3.start();
    await new Promise(r => setImmediate(r));
    p3.tick(); p3.tick();
    check("轮询:在途请求不并发", maxConcurrent === 1);
    resolveBlock(); p3.stop();

    // 手动立即刷新:已有待触发定时器时先取消,调度仍只有一条
    let loads4 = 0;
    const p4 = SEG.createDocPoller({ interval: 10, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout,
      load: async () => { loads4++; return true; } });
    p4.start();
    await new Promise(r => setImmediate(r));
    check("轮询:一拍后有一个待触发定时器", timers.length === 1);
    p4.tick();                                   // 手动刷新
    await new Promise(r => setImmediate(r));
    check("轮询:手动刷新后仍只有一个定时器", timers.length === 1 && loads4 === 2);
    p4.stop();

    // P1 回归:docPoller 必须先定义后启动(暂时性死区曾中断整个页面初始化)
    const fs = require("fs");
    const html = fs.readFileSync(__dirname + "/../src/deephoto/web/app/controller.js", "utf-8").split('async function start()')[1];
    const defAt = html.indexOf("poller = SEG.createDocPoller");
    const startAt = html.indexOf("poller.start()");
    check("P1: docPoller 定义在首次启动之前", defAt > 0 && startAt > defAt);

    // 中断任务的逐图渲染:在途项耗时显示 —,不虚涨;运行中才现算
    const oldStart = new Date(Date.now() - 8 * 3600 * 1000).toISOString();
    const runningItem = { kind: "image", seq: 1, label: "图 1.1", page: 2,
                          result: "running", started_at: oldStart, duration_ms: null };
    const interruptedItem = { ...runningItem, seq: 2, result: "interrupted" };
    const htmlStopped = SEG.renderImageItems([runningItem, interruptedItem], false);
    check("中断任务:在途图片显示 —", (htmlStopped.match(/—/g) || []).length === 2
      && !htmlStopped.includes("小时"));
    const htmlActive = SEG.renderImageItems([runningItem], true);
    check("运行中任务:在途图片现算耗时", htmlActive.includes("小时"));

    // 详情响应慢于轮询周期:在途期间重复 refresh 不发新请求;
    // 响应到达后写入一次;缓存保留供列表重建复用
    {
      let fetchCalls = 0, resolveSlow;
      const renders = [];
      const loader = SEG.createProgDetailLoader({
        fetchDetail: (id) => { fetchCalls++; return new Promise(r => { resolveSlow = r; }); },
        onRender: (id, html) => renders.push([id, html]),
      });
      loader.expanded("doc1");
      loader.refresh("doc1");                       // 展开时发起
      // 模拟 3 次列表轮询(响应始终未回):不应叠加请求,也不应重置内容
      loader.refresh("doc1"); loader.refresh("doc1"); loader.refresh("doc1");
      check("慢响应:在途期间不重复发送", fetchCalls === 1 && loader.inflightCount === 1);
      check("慢响应:尚未渲染", renders.length === 0);
      resolveSlow("<table>阶段表</table>");
      await new Promise(r => setImmediate(r));
      check("慢响应:到达后写入一次", renders.length === 1 && renders[0][1].includes("阶段表"));
      check("慢响应:缓存供列表重建复用", loader.cachedHtml("doc1").includes("阶段表"));
      // 新一轮轮询(上一请求已完成):允许再发一次
      loader.refresh("doc1");
      check("完成后允许再次刷新", fetchCalls === 2);
      // 切换展开文档:旧请求结果只进缓存,不渲染
      loader.expanded("doc2");
      resolveSlow("<table>迟到的doc1</table>");
      await new Promise(r => setImmediate(r));
      check("切换后旧请求不渲染", renders.length === 1 && loader.cachedHtml("doc1").includes("迟到"));

      // 失败路径:onError 收到 (文档 ID, 异常);缓存不被失败清除
      const errors = [];
      const failing = SEG.createProgDetailLoader({
        fetchDetail: () => Promise.reject(new Error("HTTP 500")),
        onRender: (id, html) => renders.push([id, html]),
        onError: (id, err) => errors.push([id, err && err.message]),
      });
      failing.expanded("docX");
      await failing.refresh("docX");
      check("失败:onError 收到文档 ID 与异常",
        errors.length === 1 && errors[0][0] === "docX" && errors[0][1] === "HTTP 500");
      // 先成功一次拿到缓存,再失败:缓存保留(页面可显示上次内容)
      let flip = false;
      const flaky = SEG.createProgDetailLoader({
        fetchDetail: () => flip ? Promise.reject(new Error("boom")) : Promise.resolve("<table>旧内容</table>"),
        onRender: () => {}, onError: () => {},
      });
      flaky.expanded("docY");
      await flaky.refresh("docY");
      flip = true;
      await flaky.refresh("docY");
      check("失败后缓存保留", flaky.cachedHtml("docY").includes("旧内容"));

      // 失败状态持久化矩阵(详情刷新失败提示不被列表重建清除)
      // 1) 无缓存失败后:innerHtml 显示失败提示,不再是"加载中"
      check("无缓存失败:innerHtml 显示失败提示", failing.innerHtml("docX").includes("详情加载失败")
        && !failing.innerHtml("docX").includes("加载中"));
      // 2) 有缓存失败后:显示提示及旧内容
      check("有缓存失败:提示及旧内容", flaky.innerHtml("docY").includes("详情刷新失败")
        && flaky.innerHtml("docY").includes("旧内容"));
      // 3) 收起再展开:失败状态不丢
      flaky.expanded(null);
      check("收起后失败状态仍在", flaky.hasError("docY"));
      flaky.expanded("docY");
      check("再展开仍显示提示及旧内容", flaky.innerHtml("docY").includes("详情刷新失败"));
      // 4) 成功重试后:错误提示消失并显示新内容
      flip = false;
      await flaky.refresh("docY");
      check("成功重试后错误清除", !flaky.hasError("docY")
        && !flaky.innerHtml("docY").includes("详情刷新失败")
        && flaky.innerHtml("docY").includes("旧内容"));
      // 5) 不同文档的失败状态互不影响
      check("失败状态按文档隔离", !flaky.hasError("docX") && failing.hasError("docX")
        && !failing.hasError("docY"));
    }

    console.log(failures ? `\n${failures} 个失败` : "\n全部通过");
    process.exit(failures ? 1 : 0);
  })();
}
