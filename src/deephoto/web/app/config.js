export const BRAND = '云寰 Agent';
export const DISCLAIMER = `${BRAND} 可能会出错，请核查重要信息。`;
export const GUIDE_TITLE = '您的专属云寰Agent助理';
export const GUIDE_SUBTITLE = '本地功能指南 · 未连接问答模型';
export const QUICK_PROMPTS = [
  { title: '器件结构解析', subtitle: '拆解 CMOS 器件层级', icon: 'file', prompt: '请结合文档与原图，解析器件结构、各部分作用及工作原理，并标注出处。' },
  { title: '工艺参数分析', subtitle: '整理工艺数据与趋势', icon: 'chart', prompt: '请梳理文档中的关键工艺参数，分析参数对性能的影响，并给出出处。' },
  { title: '编写仿真脚本', subtitle: '生成 SPICE 仿真代码', icon: 'code', prompt: '请根据文档编写仿真脚本，说明输入参数、假设与运行步骤；缺少的信息请先向我确认。' },
  { title: '生成实验报告', subtitle: '整理器件测试结论', icon: 'report', prompt: '请基于文档整理实验报告，包括目的、方法、参数、结果与结论，明确区分原文事实与推断。' },
];
export const PLACEHOLDERS = {
  schedule: { label: '定时任务', icon: 'calendar', placeholder: true },
  extensions: { label: '管理扩展', icon: 'extension', placeholder: true },
  team: { label: '团队', placeholder: true },
  agent: { label: '选择 Agent', icon: 'bot', placeholder: true },
  tools: { label: '选择扩展', icon: 'grid', placeholder: true },
  voice: { label: '语音输入', icon: 'mic', placeholder: true },
};
export const FAQ = [
  { question: '如何创建新对话？', keywords: ['新建', '对话', '会话', '快捷键'], answer: '点击左侧「新建对话」或按 Alt+N（Mac 为 ⌥N）。新建只清空当前视图，旧会话仍保存在左侧工作空间中。' },
  { question: '如何上传文档？', keywords: ['上传', '附件', '文档', '知识库', '格式'], answer: '进入左侧「RAG 知识库」上传，也可以点击对话输入框的附件按钮。附件会进入知识库，等待入库完成后即可提问；支持的格式以知识库页面说明为准。' },
  { question: '引用和原图怎么看？', keywords: ['引用', '出处', '原图', '图片', '来源'], answer: '回答完成且服务端校验通过后才会展示出处和原图。点击出处或图片的「原页」可在右侧打开来源预览；点击原图可放大。非 PDF 文档可能没有原页预览。' },
  { question: '什么是按需确认？', keywords: ['审批', '确认', '批准', '拒绝', '看图'], answer: '开启看图审批时，超出自动放行数量的看图请求需要逐项批准或拒绝。全部选择后提交，回答会在同一会话中继续。审批配置由服务端管理。' },
  { question: '检索降级怎么办？', keywords: ['降级', '关键词', '向量', '检索', '嵌入'], answer: '「仅关键词」表示当前未使用嵌入服务。若已配置嵌入服务，请检查服务连接与配置，修复后重启；未配置时使用关键词检索属于正常行为。' },
  { question: '为什么会话变为只读？', keywords: ['只读', '失败', '中断', '继续'], answer: '未完成的回答、配置变化或关联文档不可用可能使会话只读。可以查看历史，并点击「新建对话」重新提问。' },
];
export function matchFAQ(query) {
  const text = query.trim().toLowerCase();
  return text ? FAQ.filter(item => item.question.toLowerCase().includes(text) ||
    item.keywords.some(word => text.includes(word) || word.includes(text))) : FAQ;
}
