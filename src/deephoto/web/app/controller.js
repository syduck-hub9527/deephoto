import { request, jsonOptions, consumeSSE, safeSourcePath } from './api.js';

// The graph, persistence, evidence validation and approval resume stay on the
// existing server endpoints. This layer owns only browser navigation and I/O.
export function createController(get, dispatch) {
  const SEG = window.DeepphotoSegments;
  let disposed = false, routeVersion = 0, listBusy = false, toastTimer;
  let poller, previousDocs = null;
  const uploaded = new Map();
  const patch = value => dispatch({ type: 'PATCH', value });
  const updateMessage = message => dispatch({ type: 'MESSAGE', message });
  const store = (key, value) => { try { localStorage.setItem(key, value); } catch { /* private browsing/storage full */ } };
  function toast(text) {
    patch({ toast: String(text) }); clearTimeout(toastTimer);
    toastTimer = setTimeout(() => patch({ toast: '' }), 4500);
  }
  function notice(id, text) { dispatch({ type: 'NOTICE', notice: { id, text, time: new Date().toLocaleTimeString() } }); }
  function hash(value) { history.replaceState(null, '', value); }
  const sessionHash = id => id ? `#/s/${encodeURIComponent(id)}` : '#/';
  function guard() {
    if (get().sending || get().opening) { toast('请等待当前回答结束或会话加载完成'); return true; }
    return false;
  }
  function newChat() {
    if (guard()) return;
    ++routeVersion;
    dispatch({ type: 'NEW_CHAT' }); hash('#/');
  }
  function scope(id) {
    if (guard() || id === get().scope) return;
    store('deephoto_scope', id);
    newChat(); patch({ scope: id });
  }
  async function refreshSessions(append = false) {
    if (listBusy || (append && get().nextOffset === null)) return;
    listBusy = true;
    try {
      const data = await request(`/api/qa/sessions?limit=30&offset=${append ? get().nextOffset : 0}`);
      const old = new Map(get().sessions.map(s => [s.session_id, s]));
      for (const item of data.items || []) {
        if (old.has(item.session_id) && item.status === 'awaiting_approval' && old.get(item.session_id)?.status !== item.status &&
            !(get().sessionId === item.session_id && get().messages.some(m => m.approvals?.length)))
          notice(`approval:${item.session_id}:${item.updated_at}`, `${item.title}：等待看图审批`);
      }
      const items = append ? [...get().sessions, ...(data.items || [])] : data.items || [];
      patch({ sessions: [...new Map(items.map(s => [s.session_id, s])).values()], nextOffset: data.next_offset ?? null });
    } catch (error) { toast(`会话列表加载失败：${error.message}`); }
    finally { listBusy = false; }
  }
  function attachApproval(message, event) {
    message.approvals = event.approvals || [];
    message.approvalExpires = event.expires_at || null;
    message.apChoices = {}; message.apError = null;
    message.streaming = false;
    if (event.session_id) { patch({ sessionId: event.session_id }); hash(sessionHash(event.session_id)); }
    notice(`approval:${get().sessionId}:${message.approvalExpires}`, '会话等待看图审批，请逐项选择批准或拒绝。');
  }
  async function openSession(id) {
    if (guard()) return;
    const version = ++routeVersion;
    patch({ opening: true });
    try {
      const data = await request('/api/qa/sessions/' + encodeURIComponent(id));
      if (disposed || version !== routeVersion) return;
      const messages = (data.messages || []).map(SEG.fromHistoryMessage);
      if (data.pending_approval) {
        const index = (data.messages || []).findLastIndex(m => m.role === 'assistant' && m.status === 'awaiting_approval');
        if (index >= 0) attachApproval(messages[index], { ...data.pending_approval, session_id: id });
      }
      dispatch({ type: 'OPEN_SESSION', id, messages, scope: data.session.document_id,
        reason: data.continuable ? null : data.readonly_reason || '该会话只读，请新建对话' });
      store('deephoto_scope', get().scope); hash(sessionHash(id));
      patch({ sessions: get().sessions.map(s => s.session_id === id ? data.session : s) });
    } catch (error) {
      if (version !== routeVersion) return;
      toast(`会话加载失败：${error.message}`);
      dispatch({ type: 'NEW_CHAT' }); hash('#/');
      if (error.status === 404) patch({ sessions: get().sessions.filter(s => s.session_id !== id) });
    } finally { if (version === routeVersion) patch({ opening: false }); }
  }
  async function recheck() {
    const id = get().sessionId;
    if (!id) return;
    try {
      const data = await request('/api/qa/sessions/' + encodeURIComponent(id));
      if (get().sessionId !== id) return;
      dispatch({ type: 'READONLY', reason: data.continuable ? null : data.readonly_reason || '该会话只读，请新建对话' });
      patch({ sessions: get().sessions.map(s => s.session_id === id ? data.session : s) });
    } catch (error) {
      dispatch({ type: 'READONLY', reason: '会话状态暂时无法确认，请新建对话或刷新后重试' });
      toast(`会话状态复查失败：${error.message}`);
    }
  }
  function beforeUnload(event) { event.preventDefault(); event.returnValue = '正在回答，离开页面会中断本轮'; }
  async function runStream(message, path, body, resume = false) {
    patch({ sending: true });
    window.addEventListener('beforeunload', beforeUnload);
    let accepted = false;
    try {
      const response = await fetch(path, jsonOptions('POST', body));
      if (!response.ok || !response.body) {
        const data = await response.json().catch(() => ({}));
        if (response.status === 409 && data.code === 'approval_pending') attachApproval(message, data);
        else {
          const error = typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
          if (resume) message.apError = `提交失败：${error}`;
          else { message.error = `请求失败：${error}`; message.broken = true; }
        }
      } else {
        accepted = true;
        if (resume) { message.approvals = null; message.apChoices = {}; message.streaming = true; }
        await consumeSSE(response.body, event => {
          if (event.type === 'session') {
            patch({ sessionId: event.session_id }); hash(sessionHash(event.session_id));
            const current = get().sessions.find(s => s.session_id === event.session_id);
            const running = { ...current, session_id: event.session_id,
              title: current?.title || body.question || '对话', updated_at: new Date().toISOString(), status: 'running' };
            patch({ sessions: [running, ...get().sessions.filter(s => s.session_id !== event.session_id)] });
          } else if (event.type === 'approval_required') attachApproval(message, event);
          else {
            if (event.type === 'error') { message.errorCode = event.code; message.recoverable = event.recoverable; }
            if (event.type === 'done') {
              message.sessionFailed = event.session_status === 'failed';
              message.tool_trace = event.tool_trace;
              patch({ sessionId: event.session_id || null }); hash(sessionHash(event.session_id));
              if (message.sessionFailed) dispatch({ type: 'READONLY', reason: '本轮失败，该会话只读，请新建对话' });
            }
            SEG.applyStreamEvent(message, event);
          }
          updateMessage(message);
        });
        if (!message.approvals?.length) SEG.finishStream(message);
      }
    } catch (error) {
      message.broken = true;
      if (resume) message.apError = `网络错误：${error.message}；请刷新会话确认审批状态`;
      else message.error = `网络错误：${error.message}`;
    } finally {
      message.streaming = false; message.apSubmitting = false;
      updateMessage(message);
      window.removeEventListener('beforeunload', beforeUnload);
      // Keep navigation locked until the authoritative recheck has finished.
      if (SEG.needsSessionRecheck(message) || (resume && !accepted)) await recheck();
      await refreshSessions();
      dispatch({ type: 'SEND_END' });
    }
  }
  async function send() {
    const state = get(); const question = state.draft.trim();
    if (!question || !state.ready || state.sending || state.opening || state.readonlyReason) return;
    if (Array.from(question).length > 4000) return toast('问题不能超过 4000 个字符');
    const message = SEG.createAssistantMessage();
    dispatch({ type: 'SEND_START', user: { role: 'user', content: question }, message });
    await runStream(message, '/api/qa/stream', { question, document_id: state.scope || null, session_id: state.sessionId });
  }
  function choose(message, id, choice) {
    if (get().sending) return;
    const choices = { ...message.apChoices };
    if (id === '*') for (const item of message.approvals) choices[item.approval_id] = choice;
    else choices[id] = choices[id] === choice ? undefined : choice;
    updateMessage({ ...message, apChoices: choices });
  }
  async function approve(message) {
    if (get().sending || !message.approvals?.length || !(Date.parse(message.approvalExpires) > Date.now())) return;
    if (!message.approvals.every(a => message.apChoices?.[a.approval_id])) return;
    const current = { ...message, apSubmitting: true, apError: null };
    updateMessage(current);
    await runStream(current, '/api/qa/approvals', { session_id: get().sessionId,
      decisions: message.approvals.map(a => ({ approval_id: a.approval_id, type: message.apChoices[a.approval_id] })) }, true);
  }
  async function rename(id) {
    if (guard()) return;
    const title = prompt('重命名会话', get().sessions.find(s => s.session_id === id)?.title || '');
    if (title === null) return;
    if (!title.trim()) return toast('会话标题不能为空');
    if ([...title.trim()].length > 60) return toast('会话标题不能超过 60 个字符');
    try { await request('/api/qa/sessions/' + id, jsonOptions('PATCH', { title: title.trim() })); await refreshSessions(); }
    catch (error) { toast(`改名失败：${error.message}`); }
  }
  async function removeSession(id) {
    if (guard() || !confirm('删除该会话及其历史记录？')) return;
    try {
      await request('/api/qa/sessions/' + id, { method: 'DELETE' });
      if (get().sessionId === id) newChat();
      await refreshSessions();
    } catch (error) { toast(`删除失败：${error.message}`); }
  }
  async function loadDocs() {
    const data = await request('/api/documents');
    if (disposed) return false;
    const docs = data.documents || [];
    for (const doc of docs) {
      const prior = previousDocs?.get(doc.id);
      if ((prior && prior !== doc.status || uploaded.has(doc.id)) && ['ready', 'failed'].includes(doc.status)) {
        notice(`document:${doc.id}:${doc.status}`, `${doc.filename}：${doc.status === 'ready' ? '入库完成，已可提问' : '入库失败'}`);
        if (uploaded.has(doc.id)) {
          uploaded.delete(doc.id);
          if (doc.status === 'ready') patch({ attachments: [...get().attachments.filter(a => a.id !== doc.id), { id: doc.id, name: doc.filename, ready: true }] });
          else patch({ attachments: get().attachments.map(a => a.id === doc.id ? { ...a, failed: true } : a) });
        }
      }
    }
    previousDocs = new Map(docs.map(d => [d.id, d.status]));
    patch({ docs, docsStale: false, docsUpdatedAt: new Date().toLocaleTimeString() });
    return docs.some(d => !['ready', 'failed'].includes(d.status));
  }
  async function upload(files) {
    if (!files.length || get().uploading) return;
    patch({ uploading: true });
    toast('附件会进入知识库，入库需要一段时间。');
    try {
      for (const file of files) {
        const form = new FormData(); form.append('file', file);
        try {
          const data = await request('/api/documents', { method: 'POST', body: form });
          uploaded.set(data.document_id, file.name);
          patch({ attachments: [...get().attachments, { id: data.document_id, name: file.name }] });
        } catch (error) { toast(`${file.name} 上传失败：${error.message}`); }
      }
    } finally { patch({ uploading: false }); poller.start(); poller.tick(); }
  }
  async function removeDoc(doc) {
    if (get().opening || (get().sending && get().scope === doc.id)) return toast('请等待当前回答结束后再删除');
    if (!confirm('删除该文档及其索引与图片？')) return;
    try {
      await request('/api/documents/' + doc.id, { method: 'DELETE' });
      if (get().scope === doc.id) { store('deephoto_scope', ''); newChat(); patch({ scope: '' }); }
      patch({ attachments: get().attachments.filter(a => a.id !== doc.id) });
      poller.start(); poller.tick(); await refreshSessions();
    } catch (error) { toast(`删除失败：${error.message}`); }
  }
  function source(url, title = '来源预览') {
    const path = safeSourcePath(url);
    if (!path) return toast('该来源没有可用的原页预览');
    dispatch({ type: 'SOURCE', tab: { id: path, url: path, title } }); store('deephoto_right_open', 'true');
  }
  function toggleRight() { const value = !get().rightOpen; patch({ rightOpen: value }); store('deephoto_right_open', String(value)); }
  function markRead() {
    const ids = [...new Set([...get().readNotices, ...get().notifications.map(n => n.id)])].slice(-200);
    patch({ readNotices: ids }); store('deephoto_notices_read', JSON.stringify(ids));
  }
  function theme() {
    const value = get().theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = value; patch({ theme: value }); store('deephoto_theme', value);
  }
  function nickname() {
    const value = prompt('设置本地昵称（1–30 字）', get().nickname);
    if (value === null) return;
    if (!value.trim() || [...value.trim()].length > 30) return toast('昵称需为 1–30 个字符');
    patch({ nickname: value.trim() }); store('deephoto_nickname', value.trim());
  }
  function navigate(view) {
    if (guard()) return;
    patch({ view, sidebarOpen: false }); hash(view === 'kb' ? '#/kb' : sessionHash(get().sessionId));
    if (view === 'kb') { poller.start(); poller.tick(); }
  }
  function route() {
    if (get().sending || get().opening) { hash(get().view === 'kb' ? '#/kb' : sessionHash(get().sessionId)); return toast('请等待当前回答结束或会话加载完成'); }
    const match = /^#\/s\/(sess_[0-9a-f]{32})$/.exec(location.hash);
    if (match) openSession(match[1]);
    else if (location.hash === '#/kb') navigate('kb');
    else newChat();
  }
  function keydown(event) {
    if (event.altKey && !event.ctrlKey && !event.metaKey && event.key.toLowerCase() === 'n') { event.preventDefault(); newChat(); }
    if (event.key === 'Escape') {
      patch({ sidebarOpen: false });
      if (get().rightOpen) toggleRight();
    }
  }
  async function start() {
    localStorage.removeItem('deephoto_chat'); localStorage.removeItem('deephoto_session');
    patch({ nickname: localStorage.getItem('deephoto_nickname') || '本地用户',
      scope: localStorage.getItem('deephoto_scope') || '',
      rightOpen: innerWidth >= 1280 && localStorage.getItem('deephoto_right_open') !== 'false',
      theme: localStorage.getItem('deephoto_theme') || 'light' });
    try { const ids = JSON.parse(localStorage.getItem('deephoto_notices_read') || '[]'); if (Array.isArray(ids)) patch({ readNotices: ids }); } catch { /* legacy data */ }
    document.documentElement.dataset.theme = get().theme;
    poller = SEG.createDocPoller({ interval: 3000, load: loadDocs, onError: () => patch({ docsStale: true }) });
    poller.start();
    const work = [refreshSessions(), request('/api/system/status').then(status => patch({ status })).catch(() => patch({ status: null })),
      request('/api/documents/upload-config').then(cfg => patch({ accept: (cfg.accept || []).join(','), uploadHelp: cfg.accept?.join(' / ') || '' })).catch(() => {})];
    await Promise.allSettled(work);
    if (disposed) return;
    patch({ ready: true }); route();
    window.addEventListener('hashchange', route); window.addEventListener('keydown', keydown);
  }
  function stop() { disposed = true; poller?.stop(); clearTimeout(toastTimer);
    window.removeEventListener('hashchange', route); window.removeEventListener('keydown', keydown); }
  return { start, stop, patch, toast, newChat, scope, openSession, send, choose, approve,
    rename, removeSession, refreshSessions, upload, removeDoc, source, toggleRight,
    markRead, theme, nickname, navigate };
}
