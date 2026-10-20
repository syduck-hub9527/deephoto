import { html, useApp } from '../ui.js';
import { Notifications } from './Notifications.js';
import { Icon } from './Icon.js';
export function TopBar() {
  const { state, actions } = useApp();
  return html`<header class="topbar"><button class="icon-button mobile-menu" aria-label="打开导航" title="打开导航" onClick=${() => actions.patch({ sidebarOpen: !state.sidebarOpen })}><${Icon} name="menu"/></button>
    <span class=${`breadcrumb ${state.view === 'chat' && !state.opening ? 'home-breadcrumb' : ''}`}>${state.view === 'kb' ? '工作空间 / 知识库' : '个人空间'}${state.opening ? ' · 正在打开会话…' : ''}</span><div class="topbar-actions"><${Notifications}/>
      <button class=${`icon-button assistant-toggle ${state.rightOpen && state.activeTab === 'guide' ? 'selected' : ''}`} title="助理指南" aria-label="打开助理指南" onClick=${() => { actions.patch({ activeTab: 'guide' }); if (!state.rightOpen) actions.toggleRight(); }}><${Icon} name="bot"/></button>
      <button class="icon-button" title="切换右侧面板" aria-label="切换右侧面板" aria-expanded=${state.rightOpen} onClick=${actions.toggleRight}><${Icon} name="panel"/></button></div></header>`;
}
