import MarkdownIt from 'markdown-it';

// segments.js still owns splitting, withholding incomplete markers, validation,
// image cards and formula extraction. Only ordinary Markdown uses the M0 parser.
const SEG = window.DeepphotoSegments;
const md = new MarkdownIt({ html: false, linkify: false, breaks: false });
md.disable('image'); // Only server-validated protocol image cards may load images.
const originalText = md.renderer.rules.text;
md.renderer.rules.text = (tokens, index, options, env, self) => {
  const text = originalText ? originalText(tokens, index, options, env, self) : SEG.esc(tokens[index].content);
  return text.replace(/\[chunk:([A-Za-z0-9_]+)\]/g, (_, id) => env.streaming
    ? '<span class="chip cite">出处…</span>'
    : env.citeMap.has(id) ? `<button type="button" class="chip cite" data-citation="${id}" title="查看出处 ${SEG.esc(env.citeMap.get(id))}">出处 ${SEG.esc(env.citeMap.get(id))}</button>` : '');
};
export function renderMarkdown(text, citeMap, options) {
  // Math nodes are emitted by the existing renderer, so formula escaping and
  // fenced-code behavior remain identical to the established protocol.
  if (/(?:\$|\\\[|\\\()/.test(text)) {
    const message = { content: text, images: [], citations: [...citeMap].map(([chunk_id, label]) => ({ chunk_id, label })),
      streaming: options.streaming, validated: !options.streaming };
    return SEG.renderAnswerBody(message).html;
  }
  return md.render(text, { citeMap, streaming: options.streaming });
}
