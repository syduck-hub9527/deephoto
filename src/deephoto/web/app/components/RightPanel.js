import { useState } from 'preact/hooks';
import { html, useApp } from '../ui.js';
import { matchFAQ, GUIDE_TITLE, GUIDE_SUBTITLE } from '../config.js';
import { Icon, AssistantAvatar } from './Icon.js';
export function AssistantGuide() {
  const [query, setQuery] = useState(''), [search, setSearch] = useState('');
  const items = matchFAQ(search);
  const question = item => html`<details class="faq-card" key=${item.question}><summary><span>${item.question}</span><${Icon} name="chevron" size=${17}/></summary><p>${item.answer}</p></details>`;
  return html`<div class="assistant-guide"><div class="guide-intro"><div class="robot"><${AssistantAvatar}/></div><h2>${GUIDE_TITLE}</h2><p>${GUIDE_SUBTITLE}</p></div>
    <div class="faq-list"><p class="muted">您可以问我：</p>${(search ? items : items.slice(0, 4)).map(question)}
    ${!search && items.length > 4 && html`<details class="guide-more"><summary>更多使用指南<${Icon} name="down" size=${14}/></summary>${items.slice(4).map(question)}</details>`}
    ${!items.length && html`<p class="no-match">未找到匹配的指南，可以在左侧对话中直接提问。</p>`}</div>
    <form class="guide-search" onSubmit=${e => { e.preventDefault(); setSearch(query); }}><input aria-label="搜索本地指南" placeholder="请输入你的问题…" value=${query} onInput=${e => { setQuery(e.target.value); if (!e.target.value) setSearch(''); }}/><button title="搜索指南" aria-label="搜索指南"><${Icon} name="send" size=${22}/></button></form>
  </div>`;
}
export function SourcePreview({ tab }) {
  const [failed, setFailed] = useState(false);
  return html`<section class="source-preview"><p>${tab.title}</p>${failed ? html`<p class="error">来源无法加载，文档可能已删除，或没有原页预览。</p>` : html`<img src=${tab.url} alt=${tab.title} onError=${() => setFailed(true)}/>`}<a href=${tab.url} target="_blank" rel="noopener">在新窗口打开原始来源 ↗</a></section>`;
}
export function RightPanel() {
  const { state, actions } = useApp(); const active = state.tabs.find(t => t.id === state.activeTab);
  function close(id) { actions.patch({ tabs: state.tabs.filter(t => t.id !== id), activeTab: state.activeTab === id ? 'guide' : state.activeTab }); }
  return html`<aside class=${`right-panel ${state.rightOpen ? 'open' : ''}`} aria-label="助理与来源预览"><div class="right-tabs" role="tablist" aria-label="助理与来源标签">
    <div class=${`guide-tab ${state.activeTab === 'guide' ? 'active' : ''}`}><button role="tab" aria-selected=${state.activeTab === 'guide'} class=${state.activeTab === 'guide' ? 'active' : ''} onClick=${() => actions.patch({ activeTab: 'guide' })}><${Icon} name="bot" size=${16}/>助理Agent</button><button class="close-panel" title="收起右栏" aria-label="收起右栏" onClick=${actions.toggleRight}><${Icon} name="close" size=${14}/></button></div>
    ${state.tabs.map(tab => html`<div class="source-tab" key=${tab.id}><button role="tab" aria-selected=${tab.id === state.activeTab} class=${tab.id === state.activeTab ? 'active' : ''} title=${tab.title} onClick=${() => actions.patch({ activeTab: tab.id })}>${tab.title}</button><button title="关闭来源" aria-label="关闭来源" onClick=${() => close(tab.id)}><${Icon} name="close" size=${14}/></button></div>`)}
    <button class="add-source-tab" title="点击回答出处或原页可添加来源标签" aria-label="来源标签帮助" onClick=${() => actions.toast('点击回答中的出处或图片的「原页」，即可打开来源预览；最多保留 5 个来源标签。')}><${Icon} name="plus" size=${18}/></button></div>
    <div class="right-content" role="tabpanel">${active ? html`<${SourcePreview} key=${active.id} tab=${active}/>` : html`<${AssistantGuide}/>`}</div></aside>`;
}
