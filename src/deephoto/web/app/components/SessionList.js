import { html, useApp } from '../ui.js';
export function SessionList() {
  const { state, actions } = useApp();
  const groups = window.DeepphotoSegments.groupSessions(state.sessions);
  const labels = { running: '回答生成中', awaiting_approval: '等待审批', failed: '失败' };
  return html`<div class="session-list" aria-label="历史会话">
    ${!groups.length && html`<p class="muted empty-list">${state.ready ? '暂无历史会话' : '正在加载会话…'}</p>`}
    ${groups.map(group => html`<details key=${group.label} class="session-group" open>
      <summary>▱ ${group.label}<span>${group.items.length}</span></summary>
      ${group.items.map(s => html`<div key=${s.session_id} class=${`session-row ${state.sessionId === s.session_id ? 'active' : ''}`}>
        <button class="session-open" onClick=${() => actions.openSession(s.session_id)} title=${s.title}>
          ${labels[s.status] && html`<span class=${`session-status ${s.status}`} role="img" aria-label=${labels[s.status]} title=${labels[s.status]}>${s.status === 'running' ? '◌' : s.status === 'awaiting_approval' ? '?' : '!'}</span>`}
          <span class="truncate">${s.title}</span></button>
        <span class="session-actions"><button aria-label="重命名会话" title="重命名" onClick=${() => actions.rename(s.session_id)}>✎</button>
          <button aria-label="删除会话" title="删除" onClick=${() => actions.removeSession(s.session_id)}>×</button></span>
      </div>`)}
    </details>`)}
    ${state.nextOffset !== null && html`<button class="load-more" onClick=${() => actions.refreshSessions(true)}>加载更多</button>`}
  </div>`;
}
