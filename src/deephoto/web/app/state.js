export const initialState = {
  messages: [], sessions: [], docs: [], nextOffset: null, sessionId: null,
  scope: '', view: 'chat', sending: false, opening: false, ready: false,
  readonlyReason: null, status: null, docsStale: false, docsUpdatedAt: null,
  nickname: '本地用户', theme: 'light', rightOpen: false, sidebarOpen: false,
  notifications: [], readNotices: [], tabs: [], activeTab: 'guide', draft: '',
  attachments: [], accept: '', uploadHelp: '', uploading: false, toast: '',
};

export function reducer(state, action) {
  switch (action.type) {
    case 'PATCH': return { ...state, ...action.value };
    case 'NEW_CHAT': return { ...state, messages: [], sessionId: null,
      readonlyReason: null, view: 'chat', sidebarOpen: false, draft: '' };
    case 'OPEN_SESSION': return { ...state, messages: action.messages,
      sessionId: action.id, scope: action.scope || '', readonlyReason: action.reason || null,
      view: 'chat', sidebarOpen: false };
    case 'SEND_START': return { ...state, sending: true, draft: '',
      messages: [...state.messages, action.user, action.message] };
    case 'MESSAGE': return { ...state, messages: state.messages.map(m =>
      m.message_id === action.message.message_id ? { ...action.message } : m) };
    case 'SEND_END': return { ...state, sending: false };
    case 'READONLY': return { ...state, readonlyReason: action.reason };
    case 'NOTICE': return state.notifications.some(n => n.id === action.notice.id) ? state :
      { ...state, notifications: [action.notice, ...state.notifications].slice(0, 100) };
    case 'SOURCE': {
      const existing = state.tabs.find(tab => tab.url === action.tab.url);
      return { ...state, rightOpen: true, activeTab: existing?.id || action.tab.id,
        tabs: existing ? state.tabs : [...state.tabs, action.tab].slice(-5) };
    }
    default: return state;
  }
}
