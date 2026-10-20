import { html, useApp } from '../ui.js';
import { Icon } from './Icon.js';
export function SessionList() {
  const { state, actions } = useApp();
  const groups = window.DeepphotoSegments.groupSessions(state.sessions);
  const labels = { running: '回答生成中', awaiting_approval: '等待审批', failed: '失败' };
  return html`<div class="session-list" aria-label="历史会话">
    ${!groups.length && html`<p class="muted empty-list">${state.ready ? '暂无历史会话' : '正在加载会话…'}</p>`}
    ${groups.map(group => html`<details key=${group.label} class="session-group" open>
      <summary><${Icon} name="folder" size=${19}/><span class="group-title">${group.label}</span><span class="group-count">${group.items.length}</span><${Icon} name="chevron" size=${14} className="group-chevron"/></summary>
      ${group.items.map(s => html`<div key=${s.session_id} class=${`session-row ${state.sessionId === s.session_id ? 'active' : ''}`}>
        <button class="session-open" onClick=${() => actions.openSession(s.session_id)} title=${s.title}>
          ${labels[s.status] && html`<span class=${`session-status ${s.status}`} role="img" aria-label=${labels[s.status]} title=${labels[s.status]}><${Icon} size=${15} name=${s.status === 'running' ? 'loader' : s.status === 'awaiting_approval' ? 'help' : 'alert'}/></span>`}
          <span class="truncate">${s.title}</span></button>
        <span class="session-actions"><button aria-label="重命名会话" title="重命名" onClick=${() => actions.rename(s.session_id)}><${Icon} name="pencil" size=${14}/></button>
          <button aria-label="删除会话" title="删除" onClick=${() => actions.removeSession(s.session_id)}><${Icon} name="close" size=${14}/></button></span>
      </div>`)}
    </details>`)}
    ${state.nextOffset !== null && html`<button class="load-more" onClick=${() => actions.refreshSessions(true)}>加载更多</button>`}
  </div>`;
}
