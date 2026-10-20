import { useState } from 'preact/hooks';
import { html, useApp } from '../ui.js';
import { Icon } from './Icon.js';
export function Notifications() {
  const { state, actions } = useApp(); const [open, setOpen] = useState(false);
  const unread = state.notifications.some(n => !state.readNotices.includes(n.id));
  return html`<div class="notification-wrap" onKeyDown=${e => { if (e.key === 'Escape') setOpen(false); }}>
    <button class="icon-button" aria-label="通知中心" title="通知中心" aria-expanded=${open} onClick=${() => { setOpen(!open); if (!open) actions.markRead(); }}><${Icon} name="bell"/>${unread && html`<span class="unread-dot"></span>`}</button>
    ${open && html`<section class="notification-popover"><div class="popover-heading"><b>通知中心</b><button aria-label="关闭通知" title="关闭" onClick=${() => setOpen(false)}><${Icon} name="close" size=${16}/></button></div>
      ${state.notifications.length ? state.notifications.map(n => html`<p key=${n.id}>${n.text}<small>${n.time}</small></p>`) : html`<p class="muted">暂无通知。文档入库与看图审批动态会显示在这里。</p>`}</section>`}</div>`;
}
