import { render } from 'preact';
import { useReducer, useRef, useEffect } from 'preact/hooks';
import { html, AppContext } from './ui.js';
import { initialState, reducer } from './state.js';
import { createController } from './controller.js';
import { Sidebar } from './components/Sidebar.js';
import { TopBar } from './components/TopBar.js';
import { Home } from './components/Home.js';
import { Thread, ReadonlyBar } from './components/Thread.js';
import { Composer } from './components/Composer.js';
import { KnowledgeBase } from './components/KnowledgeBase.js';
import { RightPanel } from './components/RightPanel.js';
function App() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const current = useRef(state), runtime = useRef(null);
  if (!runtime.current) runtime.current = createController(() => current.current, action => {
    current.current = reducer(current.current, action); dispatch(action);
  });
  useEffect(() => { runtime.current.start(); return () => runtime.current.stop(); }, []);
  const actions = runtime.current;
  return html`<${AppContext.Provider} value=${{ state, actions }}><div class="app-shell">
    ${state.sidebarOpen && html`<button class="sidebar-scrim" aria-label="关闭导航" title="关闭导航" onClick=${() => actions.patch({ sidebarOpen: false })}></button>`}
    <${Sidebar}/><main id="main"><${TopBar}/>${state.view === 'kb' ? html`<${KnowledgeBase}/>` : html`<div class=${`chat-view ${!state.messages.length ? 'is-home' : ''}`}>${state.messages.length ? html`<${Thread}/>` : html`<${ReadonlyBar}/><${Home}/>`}<${Composer}/></div>`}</main><${RightPanel}/>
    ${state.toast && html`<div class="toast" role="status">${state.toast}</div>`}</div></${AppContext.Provider}>`;
}
render(html`<${App}/>`, document.getElementById('app'));
