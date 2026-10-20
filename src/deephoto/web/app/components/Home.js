import { html, useApp } from '../ui.js';
import { QUICK_PROMPTS } from '../config.js';
export function Home() {
  const { state, actions } = useApp();
  return html`<section class="home"><div class="home-emblem" aria-hidden="true">✳</div><h1>你好，<span>${state.nickname}</span><br/>我能为你做些什么？</h1>
    <p class="home-subtitle">让知识触手可及，让探索更进一步</p>
    <div class="quick-prompts">${QUICK_PROMPTS.map(item => html`<button key=${item.title} onClick=${() => { actions.patch({ draft: item.prompt }); document.getElementById('input')?.focus(); }}><span class="quick-icon">${item.icon}</span><b>${item.title}</b><small>${item.subtitle}</small><span class="quick-arrow">↗</span></button>`)}</div></section>`;
}
