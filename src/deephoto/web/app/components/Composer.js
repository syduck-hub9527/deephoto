import { html, useApp } from '../ui.js';
import { DISCLAIMER, BRAND } from '../config.js';
import { Placeholder } from './Placeholder.js';
import { Icon } from './Icon.js';
export function AttachButton({ label = '添加附件', className = '', iconOnly = false }) {
  const { state, actions } = useApp();
  return html`<label class=${`attach-button ${iconOnly ? 'icon-only' : ''} ${className}`} title="附件会上传到知识库，入库完成后可提问"><${Icon} name=${state.uploading ? 'loader' : iconOnly ? 'paperclip' : 'plus'} className=${state.uploading ? 'rotating' : ''}/><span class=${iconOnly ? 'sr-only' : ''}>${state.uploading ? '上传中…' : label}</span><input type="file" aria-label=${label} multiple accept=${state.accept || undefined} disabled=${state.uploading} onChange=${e => { const files = [...e.target.files]; e.target.value = ''; actions.upload(files); }}/></label>`;
}
export function ScopePicker() {
  const { state, actions } = useApp();
  return html`<div class="scope-control"><${Icon} name="folder" size=${18}/><select id="scope" style=${{ width: state.scope ? 'min(220px, 23vw)' : '5em' }} title="文档范围：切换范围将新建对话" aria-label="文档范围" value=${state.scope} disabled=${state.sending || state.opening} onChange=${e => actions.scope(e.target.value)}><option value="">全部文档</option>
    ${state.scope && !state.docs.some(d => d.id === state.scope && d.status === 'ready') && html`<option value=${state.scope}>当前会话文档（不可用）</option>`}
    ${state.docs.filter(d => d.status === 'ready').map(d => html`<option key=${d.id} value=${d.id}>${d.filename}</option>`)}</select><${Icon} name="up" size=${12}/></div>`;
}
export function ModeBadges() {
  const { state } = useApp(); const hitl = state.status?.hitl;
  return html`<details class="mode-info" onKeyDown=${e => { if (e.key === 'Escape') e.currentTarget.open = false; }}><summary title="审批模式由服务端配置"><${Icon} name="shield" size=${18}/><span>${!state.status ? '审批状态未知' : hitl?.enabled ? `按需确认（前 ${hitl.auto_approve_images} 张自动）` : '自动执行'}</span><${Icon} name="up" size=${12}/></summary><p>开启审批时，超出自动放行数量的看图请求需要逐项确认。此设置由服务端配置，变更后需重启。</p></details>`;
}
export function Composer() {
  const { state, actions } = useApp(); const hybrid = state.status?.retrieval.mode === 'hybrid';
  const disabled = state.sending || state.opening || !!state.readonlyReason || !state.ready;
  return html`<section class="composer-area"><div class="attachments" aria-live="polite">${state.attachments.map(a => html`<div key=${a.id} class="attachment"><span>${a.name} · ${a.ready ? '已可提问，是否将范围切到该文档？' : a.failed ? '入库失败，请查看知识库' : '入库中…'}</span>
    ${!a.ready && !a.failed && html`<progress aria-label="文档入库中"></progress>`}${a.ready && html`<button disabled=${state.sending || state.opening} onClick=${() => { actions.scope(a.id); actions.patch({ attachments: state.attachments.filter(item => item.id !== a.id) }); }}>切换范围</button>`}<button title="关闭提示" aria-label="关闭附件提示" onClick=${() => actions.patch({ attachments: state.attachments.filter(item => item.id !== a.id) })}>×</button></div>`)}</div>
    <div class="composer"><div class="composer-top"><${ScopePicker}/><${ModeBadges}/></div>
    <textarea id="input" rows="3" aria-label="问题" maxLength="4000" placeholder=${`发送给${BRAND}…`} value=${state.draft} disabled=${disabled} onInput=${e => { actions.patch({ draft: e.target.value }); e.target.style.height = 'auto'; e.target.style.height = Math.min(160, e.target.scrollHeight) + 'px'; }} onKeyDown=${e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); actions.send(); } }}></textarea>
    <div class="composer-bottom"><div class="composer-tools"><${AttachButton} label="添加附件" iconOnly/><${Placeholder} feature="agent"/><${Placeholder} feature="tools"/>
      <button class="model-badge" aria-disabled="true" title="模型由服务端配置"><${Icon} name="bot" size=${17}/><span>${state.status?.chat_model || '模型状态未知'}</span><${Icon} name="down" size=${13}/></button></div>
      <div class="send-tools"><span class=${`retrieval ${state.status ? hybrid ? 'hybrid' : 'keyword' : ''}`} title=${!state.status ? '服务状态不可达' : hybrid ? '使用语义向量与关键词检索' : '未配置嵌入服务或嵌入服务不可用，已降级为关键词检索；修复后需重启服务'}><i aria-hidden="true"></i>${!state.status ? '检索状态未知' : hybrid ? '向量 + 关键词' : '仅关键词'}</span><${Placeholder} feature="voice"/>
      <button id="sendBtn" class="send-button" title="发送" aria-label="发送" disabled=${disabled || !state.draft.trim()} onClick=${actions.send}><${Icon} name=${state.sending ? 'loader' : 'send'} size=${23} className=${state.sending ? 'rotating' : ''}/></button></div></div></div><p class="disclaimer">${DISCLAIMER}</p></section>`;
}
