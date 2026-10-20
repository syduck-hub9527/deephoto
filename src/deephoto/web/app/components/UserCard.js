import { useState, useRef } from 'preact/hooks';
import { html, useApp } from '../ui.js';
import { request, jsonOptions } from '../api.js';
import { BRAND } from '../config.js';
export function UserCard() {
  const { state, actions } = useApp();
  const [prefs, setPrefs] = useState(null), [busy, setBusy] = useState(false);
  const menu = useRef(null);
  async function load() {
    setBusy(true);
    try { setPrefs((await request('/api/qa/memory')).preferences); }
    catch (error) { actions.toast(`读取回答偏好失败：${error.message}`); }
    finally { setBusy(false); }
  }
  async function save(key, value) {
    setBusy(true);
    try { setPrefs((await request('/api/qa/memory', jsonOptions('PUT', { ...prefs, [key]: value }))).preferences); actions.toast('回答偏好已保存'); }
    catch (error) { actions.toast(`保存偏好失败：${error.message}`); }
    finally { setBusy(false); }
  }
  function keys(event) {
    if (event.key === 'Escape') { menu.current.open = false; menu.current.querySelector('summary').focus(); }
    if (['ArrowDown', 'ArrowUp'].includes(event.key) && event.target.tagName !== 'SELECT') {
      const items = [...menu.current.querySelectorAll('button,select')];
      const index = items.indexOf(document.activeElement);
      event.preventDefault(); items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    }
  }
  return html`<details class="user-menu" ref=${menu} onKeyDown=${keys}>
    <summary title="本地用户菜单"><span class="user-avatar">${state.nickname.slice(0, 1)}</span><span><b>${state.nickname}</b><small>本地工作空间</small></span><span class="muted">⌃</span></summary>
    <div class="user-popover"><button onClick=${actions.nickname}>修改本地昵称</button>
      <button onClick=${actions.theme}>切换为${state.theme === 'light' ? '暗色' : '浅色'}主题</button>
      <button disabled=${busy} onClick=${load}>回答偏好${busy ? ' · 加载中' : ''}</button>
      ${prefs && html`<label>语言<select aria-label="回答语言" disabled=${busy} value=${prefs.language} onChange=${e => save('language', e.target.value)}><option value="zh">中文</option><option value="en">English</option></select></label>
        <label>详略<select aria-label="回答详略" disabled=${busy} value=${prefs.detail} onChange=${e => save('detail', e.target.value)}><option value="balanced">均衡</option><option value="concise">简洁</option><option value="detailed">详细</option></select></label>`}
      <button onClick=${() => actions.toast(`${BRAND} · 本地图文知识库问答。历史保存在服务端，本地指南无需连接问答模型。`)}>关于</button>
    </div></details>`;
}
