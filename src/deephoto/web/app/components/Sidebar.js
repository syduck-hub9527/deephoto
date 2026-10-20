import { html, useApp } from '../ui.js';
import { BRAND } from '../config.js';
import { Placeholder } from './Placeholder.js';
import { SessionList } from './SessionList.js';
import { UserCard } from './UserCard.js';
export function Sidebar() {
  const { state, actions } = useApp();
  return html`<aside id="sidebar" class=${state.sidebarOpen ? 'open' : ''}>
    <div class="brand"><span class="brand-mark">✳</span>${BRAND}<span role="img" class=${`status-dot ${state.status ? 'online' : ''}`} aria-label=${state.status ? '服务在线' : '服务不可达'} title=${state.status ? '服务在线' : '服务不可达'}></span></div>
    <nav class="side-nav" aria-label="主导航"><button class="new-chat" onClick=${actions.newChat}><span>＋ 新建对话</span><kbd>${/Mac/i.test(navigator.platform) ? '⌥N' : 'Alt N'}</kbd></button>
      <${Placeholder} feature="schedule"/>
      <button class=${state.view === 'kb' ? 'selected' : ''} onClick=${() => actions.navigate('kb')}>▤ <span>RAG 知识库</span><span class="count">${state.docs.length}</span></button>
      <${Placeholder} feature="extensions"/>
    </nav>
    <div class="workspace-label">工作空间</div><div class="workspace-tabs"><button class="selected" onClick=${() => actions.navigate('chat')}>个人</button><${Placeholder} feature="team"/></div>
    <${SessionList}/><${UserCard}/>
  </aside>`;
}
