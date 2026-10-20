import { useEffect, useRef, useState } from 'preact/hooks';
import { html, useApp } from '../ui.js';
import { safeSourcePath } from '../api.js';
import { renderMarkdown } from '../markdown.js';
import { Icon } from './Icon.js';
export function ReadonlyBar() {
  const { state, actions } = useApp();
  return state.readonlyReason && html`<div class="readonly-bar" role="status"><span>${state.readonlyReason}</span><button onClick=${actions.newChat}>新建对话</button></div>`;
}
export function ApprovalCard({ message: m }) {
  const { state, actions } = useApp(); const [now, setNow] = useState(Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  const seconds = Math.max(0, Math.floor((Date.parse(m.approvalExpires || 0) - now) / 1000));
  const disabled = !seconds || state.sending;
  return html`<section class="approval-card"><b>需要审批：模型想查看 ${m.approvals.length} 张原图</b>
    ${m.approvals.map(item => html`<div key=${item.approval_id} class="approval-item"><span>${item.description || item.tool}</span><div>
      <button class=${m.apChoices?.[item.approval_id] === 'approve' ? 'chosen approve' : ''} disabled=${disabled} onClick=${() => actions.choose(m, item.approval_id, 'approve')}>批准</button>
      <button class=${m.apChoices?.[item.approval_id] === 'reject' ? 'chosen reject' : ''} disabled=${disabled} onClick=${() => actions.choose(m, item.approval_id, 'reject')}>拒绝</button></div></div>`)}
    <p class="muted">${seconds ? `剩 ${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}，未审批将视为拒绝` : '已超时，将视为拒绝；可直接继续提问'}</p>
    <div class="approval-footer"><button disabled=${disabled} onClick=${() => actions.choose(m, '*', 'approve')}>全部批准</button><button disabled=${disabled} onClick=${() => actions.choose(m, '*', 'reject')}>全部拒绝</button>
      <button class="primary" disabled=${disabled || !m.approvals.every(a => m.apChoices?.[a.approval_id])} onClick=${() => actions.approve(m)}>${m.apSubmitting ? '提交中…' : '提交决定'}</button></div>
    ${m.apError && html`<p class="error">${m.apError}</p>`}</section>`;
}
export function UserMessage({ message }) {
  return html`<article class="message user"><div class="user-bubble">${message.content}</div></article>`;
}
export function AssistantMessage({ message: m }) {
  const { state, actions } = useApp(); const root = useRef(null); const [zoom, setZoom] = useState(null);
  const SEG = window.DeepphotoSegments; const rendered = SEG.renderAnswerBody(m, renderMarkdown);
  const body = rendered.html + (rendered.supplement.length ? '<div class="supplement"><p>补充图片</p>' + rendered.supplement.map(im => SEG.imageCardHTML(im, m.message_id)).join('') + '</div>' : '');
  useEffect(() => {
    const el = root.current;
    el?.querySelectorAll('.math:not(.math-done)').forEach(node => {
      if (!window.katex) return;
      window.katex.render(node.dataset.tex, node, { displayMode: node.dataset.display === '1', throwOnError: false, strict: 'ignore', trust: false });
      node.classList.add('math-done');
    });
    const error = event => {
      if (event.target.tagName === 'IMG') { const card = event.target.closest('.img-card'); if (card) { const hint = card.querySelector('.img-err'); if (hint) hint.hidden = false; event.target.hidden = true; } }
    };
    el?.addEventListener('error', error, true); return () => el?.removeEventListener('error', error, true);
  }, [body]);
  function click(event) {
    const citation = event.target.closest('[data-citation]');
    if (citation && m.validated) {
      const entry = m.citations?.find(c => c.chunk_id === citation.dataset.citation);
      if (entry) preview(entry);
    }
    const link = event.target.closest('a');
    if (link) {
      const href = link.getAttribute('href');
      if (safeSourcePath(href)) { event.preventDefault(); actions.source(href); }
      else if (href?.startsWith('#fig-')) { event.preventDefault(); document.getElementById(href.slice(1))?.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    }
    const image = event.target.closest('img[data-zoomable]');
    if (image && safeSourcePath(image.getAttribute('src'))) setZoom(image.getAttribute('src'));
  }
  function preview(citation) {
    const doc = state.docs.find(d => d.id === citation.document_id);
    if (doc && !['pdf', 'image'].includes(doc.source_format || 'pdf')) return actions.toast('该格式不提供原页预览，请参考出处中的章节位置。');
    actions.source(`/api/documents/${citation.document_id}/pages/${citation.page}`, citation.label || citation.filename || '出处');
  }
  const trace = m.tool_trace;
  return html`<article class="message assistant"><div class="assistant-avatar" aria-hidden="true"><${Icon} name="sprout" size=${20}/></div><div class="assistant-body">
    ${m.thinking && html`<details class="thinking" open=${m.streaming}><summary>思考过程</summary><pre>${m.thinking}</pre></details>`}
    ${m.steps?.length > 0 && html`<details class="thinking execution-steps" open=${m.streaming}><summary>执行过程</summary>${m.steps.map((step, index) => html`<pre key=${index}>${step}</pre>`)}</details>`}
    <div ref=${root} onClick=${click} dangerouslySetInnerHTML=${{ __html: body }}></div>
    ${m.streaming && html`<span class="streaming" role="status">${m.content ? '●' : '正在思考…'}</span>`}
    ${m.warning && html`<p class="warning">⚠ ${m.warning}</p>`}${m.error && html`<p class="error">⚠ ${m.error}</p>`}
    ${m.validated && m.citations?.length > 0 && html`<div class="citations">${m.citations.map(c => {
      return html`<button key=${c.chunk_id} class="chip" title=${c.filename || c.label} onClick=${() => preview(c)}>▤ ${c.label || `p.${c.page}`}${c.filename ? ' · ' + c.filename : ''}</button>`;
    })}</div>`}
    ${trace && html`<details class="tool-trace"><summary>工具调用过程</summary><${ToolTrace} trace=${trace}/></details>`}
    ${m.approvals?.length > 0 && html`<${ApprovalCard} message=${m}/>`}
    ${zoom && html`<div class="lightbox" role="dialog" aria-label="原图放大" tabIndex="0" onClick=${() => setZoom(null)} onKeyDown=${e => { if (e.key === 'Escape') setZoom(null); }}><button aria-label="关闭大图" title="关闭" onClick=${() => setZoom(null)}>×</button><img src=${zoom} alt="文档原图"/></div>`}
  </div></article>`;
}
function ToolTrace({ trace }) {
  // Explicit allowlist: never display tool arguments, payloads or raw messages.
  const items = Array.isArray(trace) ? trace : trace.items || trace.calls || trace.events || [];
  return html`<div class="trace-items">${items.length ? items.map((item, index) => html`<p key=${index}>${item.agent || 'agent'} / ${item.tool || item.name || '工具调用'} · ${item.outcome || '已记录'}${item.elapsed_ms != null ? ` · ${item.elapsed_ms} ms` : ''}${item.error_type ? ` · ${item.error_type}` : ''}</p>`) : html`<p>本轮没有工具调用</p>`}${trace.dropped > 0 && html`<p>另有 ${trace.dropped} 项超出轨迹记录上限</p>`}</div>`;
}
export function Thread() {
  const { state } = useApp(); const root = useRef(null); const following = useRef(true);
  useEffect(() => { if (following.current && root.current) root.current.scrollTop = root.current.scrollHeight; }, [state.messages]);
  return html`<div id="thread" ref=${root} onScroll=${e => { const el = e.currentTarget; following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}><div class="thread-inner"><${ReadonlyBar}/>${state.messages.map((message, i) => message.role === 'user' ? html`<${UserMessage} key=${'u' + i} message=${message}/>` : html`<${AssistantMessage} key=${message.message_id} message=${message}/>` )}</div></div>`;
}
