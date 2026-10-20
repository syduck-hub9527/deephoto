import { html, useApp } from '../ui.js';
import { QUICK_PROMPTS } from '../config.js';
import { Icon } from './Icon.js';
export function Home() {
  const { state, actions } = useApp();
  return html`<section class="home"><h1>你好，<span>${state.nickname}</span><br/>我能为你做些什么？</h1>
    <div class="quick-prompts">${QUICK_PROMPTS.map(item => html`<button key=${item.title} onClick=${() => { actions.patch({ draft: item.prompt }); document.getElementById('input')?.focus(); }}><span class=${`quick-icon ${item.icon}`}><${Icon} name=${item.icon}/></span><span class="quick-copy"><b>${item.title}</b><small>${item.subtitle}</small></span><${Icon} name="arrow" size=${17} className="quick-arrow"/></button>`)}</div></section>`;
}
