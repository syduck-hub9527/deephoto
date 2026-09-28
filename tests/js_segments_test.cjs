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

// ---- 既有行为 ----

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

console.log(failures ? `\n${failures} 个失败` : "\n全部通过");
process.exit(failures ? 1 : 0);
