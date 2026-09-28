/* segments.js 的 node 断言脚本:由 tests/test_segments_js.py 调用,失败时非零退出。
   覆盖场景(见 md文档/deephoto_inline_images_dev.md §8.1 前端表):
   分段顺序、双图位置、重复锚点徽标、entries 乱序、无效 ID、跨 token 标记、
   代码区标记、旧消息无锚点、XSS 转义、broken/占位状态。 */
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
  check("两图各自在对应位置", html.indexOf('id="fig-occ_a"') > html.indexOf("甲。")
    && html.indexOf('id="fig-occ_a"') < html.indexOf("乙。")
    && html.indexOf('id="fig-occ_b"') > html.indexOf("乙。"));
  check("末尾不重复", supplement.length === 0);
  check("只有两张卡片", (html.match(/<figure class="img-card inline"/g) || []).length === 2);
}

// 3. 同一 occurrence 两次引用:一张卡片,第二次为徽标
{
  const { html } = SEG.renderAnswerBody(done("一[image:occ_a]二[image:occ_a]三", [E1]));
  check("只渲染一张卡片", (html.match(/<figure class="img-card inline"/g) || []).length === 1);
  check("第二次为引用徽标", html.includes('class="chip fig"') && html.includes('href="#fig-occ_a"'));
}

// 4. images 数组顺序与正文相反:展示顺序仍按正文
{
  const { html } = SEG.renderAnswerBody(done("先[image:occ_a]后[image:occ_b]", [E2, E1]));
  check("顺序跟随正文", html.indexOf('id="fig-occ_a"') < html.indexOf('id="fig-occ_b"'));
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
  const streaming = { role: "assistant", content: "文字[ima", streaming: true };
  const html1 = SEG.renderAnswerBody(streaming).html;
  check("末尾未闭合标记暂缓显示", !html1.includes("[ima"));
  streaming.content = "文字[image:occ_a]后续";
  const html2 = SEG.renderAnswerBody(streaming).html;
  check("流式中完整标记显示占位", html2.includes("图片将在回答完成后显示"));
  check("流式中不加载图片、不露 ID", !html2.includes("<img") && !html2.includes("occ_a"));
  check("原始正文未被修改", streaming.content === "文字[image:occ_a]后续");
  check("withhold 只截尾", SEG.withholdTrailingPartial("前文[image:occ") === "前文"
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

console.log(failures ? `\n${failures} 个失败` : "\n全部通过");
process.exit(failures ? 1 : 0);
