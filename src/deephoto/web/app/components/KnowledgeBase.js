import { useState, useEffect, useRef } from 'preact/hooks';
import { html, useApp } from '../ui.js';
import { request } from '../api.js';
import { AttachButton } from './Composer.js';
const LABELS = { queued: '排队中', parsing: '解析中', describing: '描述图片', indexing: '索引中', ready: '已就绪', failed: '失败' };
function DocumentDetail({ id }) {
  const [data, setData] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false), inflight = useRef(false), alive = useRef(true), poll = useRef(null);
  async function refresh() {
    if (inflight.current) return true; inflight.current = true; setBusy(true);
    try {
      const next = await request('/api/documents/' + id);
      if (alive.current) { setData(next); setError(''); }
      return !['ready', 'failed'].includes(next.status);
    } catch (e) { if (alive.current) setError(`详情刷新失败，保留上次内容：${e.message}`); return true; }
    finally { inflight.current = false; if (alive.current) setBusy(false); }
  }
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (!expanded) return;
    poll.current = window.DeepphotoSegments.createDocPoller({ interval: 3000, load: refresh });
    poll.current.start(); return () => poll.current.stop();
  }, [expanded, id]);
  const SEG = window.DeepphotoSegments, p = data?.progress;
  const active = p?.summary?.state === 'running';
  const detailHTML = p ? SEG.renderChunks(p.items || []) + SEG.renderImageItems(p.items || [], active) + SEG.renderBatchItems(p.items || []) : '';
  return html`<details class="document-detail" onToggle=${e => setExpanded(e.currentTarget.open)}><summary>展开耗时</summary>
    ${error && html`<p class="error">${error}</p>`}${!data ? html`<p>${busy ? '加载中…' : '暂无数据'}</p>` : html`<div><table><tbody>${(p?.stages || []).map(s => html`<tr key=${s.stage}><td>${s.display_name || s.stage}</td><td>${SEG.PROG_STAGE_STATE[s.result] || s.result}${s.counts?.total ? ` (${s.counts.completed || 0}/${s.counts.total})` : ''}</td><td>${SEG.formatElapsed(s.duration_ms ?? (active && s.result === 'running' && s.started_at ? Date.now() - Date.parse(s.started_at) : null))}</td></tr>`)}</tbody></table><div dangerouslySetInnerHTML=${{ __html: detailHTML }}></div>${(p?.summary?.warnings || []).map(w => html`<p class="warning">${w}</p>`)}</div>`}
    <button disabled=${busy} onClick=${refresh}>刷新详情</button></details>`;
}
export function KnowledgeBase() {
  const { state, actions } = useApp();
  return html`<section class="knowledge-base"><div class="kb-heading"><div><span class="eyebrow">KNOWLEDGE BASE</span><h1>我的知识库</h1><p class="muted">上传资料，构建你的知识空间</p></div><${AttachButton} label="上传文档" className="primary"/></div>
    <p class="upload-help">支持格式：${state.uploadHelp || '由服务端校验'}。文件上传后进入知识库，入库完成后可提问。</p>
    ${state.docsStale && html`<p class="warning" role="status">进度暂时无法刷新${state.docsUpdatedAt ? `（上次更新 ${state.docsUpdatedAt}）` : ''}，显示上次数据。</p>`}
    <div class="document-table-wrap"><table class="document-table"><thead><tr><th>文件名</th><th>格式 / 解析引擎</th><th>状态 / 进度</th><th>页数</th><th>操作</th></tr></thead><tbody>
      ${state.docs.map(d => html`<tr key=${d.id}><td><b>${d.filename}</b></td><td><span class="format-tag">${(d.source_format || 'pdf').toUpperCase()}</span><small>${d.parse_engine || '—'}</small></td><td><span class=${d.status === 'failed' ? 'error' : d.status === 'ready' ? 'success' : ''}>${LABELS[d.status] || d.status}</span><small>${window.DeepphotoSegments.progressLine(d)}</small>${d.error && html`<p class="error">${d.error}</p>`}${d.progress && html`<${DocumentDetail} id=${d.id}/>`}</td><td>${d.page_count ?? d.pages ?? '—'}</td><td><div class="doc-actions"><button disabled=${d.status !== 'ready' || state.sending || state.opening} onClick=${() => actions.scope(d.id)}>${d.id === state.scope ? '当前范围' : '设为范围'}</button><button class="danger-link" onClick=${() => actions.removeDoc(d)}>删除</button></div></td></tr>`)}
      ${!state.docs.length && html`<tr><td colSpan="5" class="kb-empty">知识库还是空的，上传第一份文档开始探索。</td></tr>`}
    </tbody></table></div></section>`;
}
