import { html } from '../ui.js';

// Local SVGs share a stroke and grid; no icon font, images or network dependency.
const PATHS = {
  sprout: ['M12 20V10m0 5H8a5 5 0 0 1-5-5V7h3a6 6 0 0 1 6 6', 'M12 10V8a5 5 0 0 1 5-5h4v3a5 5 0 0 1-5 5h-4M7 21h10'],
  chat: ['M21 4H3v16l4-4h14V4M12 7v6m-3-3h6'],
  calendar: ['M8 2v4m8-4v4M3 9h18M5 4h14a2 2 0 0 1 2 2v6M3 15V6a2 2 0 0 1 2-2M5 20a2 2 0 0 1-2-2', 'M20 16a4 4 0 1 1-8 0 4 4 0 0 1 8 0m-4-2v2l1.5 1'],
  book: ['M12 5v15m0-15C9 3 5 3 2 4v15c3-1 7-1 10 1 3-2 7-2 10-1V4c-3-1-7-1-10 1'],
  extension: ['M9 3v3H6v4H3v4h3v4h4v3h4v-3h4v-4h3v-4h-3V6h-4V3zM10 10h4v4h-4z'],
  folder: ['M3 19V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v2M3 19l3-9h16l-3 9H3'],
  chevron: ['m9 5 7 7-7 7'], down: ['m6 9 6 6 6-6'], up: ['m6 15 6-6 6 6'],
  arrow: ['M4 12h16m-6-6 6 6-6 6'],
  file: ['M14 3H5v18h14V8zM14 3v6h5M8 13h8m-8 4h6'],
  chart: ['M4 3v17h17M9 15V9m5 6V5m5 10v-4'],
  code: ['m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18'],
  report: ['M14 3H5v18h14V8zM14 3v5h5M8 12h3m-3 4h8m-3-4h3'],
  bot: ['M8 4h8m-4-3v3M5 7h14v13H5zM2 11v5m20-5v5M8 11v2m8-2v2m-7 3h6'],
  grid: ['M3 3h18v18H3zM3 12h18M12 3v18'],
  mic: ['M8 5a4 4 0 0 1 8 0v7a4 4 0 0 1-8 0V5M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8'],
  paperclip: ['m8 13 7-7a3 3 0 0 1 4 4l-9 9a5 5 0 0 1-7-7l10-10a3 3 0 0 1 4 4l-10 10a1 1 0 0 1-2-2l9-9'],
  send: ['m21 3-7 18-4-8-8-4 19-6zM10 13 21 3'],
  shield: ['m12 2 8 3v6c0 5-4 8-8 11-4-3-8-6-8-11V5zM8 12l3 3 5-6'],
  bell: ['M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4'],
  panel: ['M3 3h18v18H3zM15 3v18m-8-13 4 4-4 4'],
  menu: ['M3 6h18M3 12h18M3 18h18'], plus: ['M12 4v16M4 12h16'],
  close: ['m6 6 12 12M6 18 18 6'], pencil: ['m4 16 11-11 4 4L8 20H4v-4m9-9 4 4'],
  loader: ['M20 12a8 8 0 1 1-8-8'],
  alert: ['M12 8v5m0 4v.01', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'],
  help: ['M9 8a3 3 0 1 1 5 2c-1 1-2 1-2 3m0 4v.01', 'M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'],
};
export function Icon({ name, size = 20, className = '' }) {
  return html`<svg class=${`ui-icon ${className}`} width=${size} height=${size} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${(PATHS[name] || []).map((d, index) => html`<path key=${index} d=${d}/>` )}</svg>`;
}
export function AssistantAvatar() {
  return html`<svg class="assistant-illustration" viewBox="0 0 120 120" aria-hidden="true" focusable="false">
    <circle cx="60" cy="60" r="59" fill="var(--brand-soft)"/><circle cx="60" cy="60" r="47" fill="var(--brand)" opacity=".16"/><circle cx="60" cy="60" r="41" fill="#66af7e"/>
    <path d="M60 28v10" stroke="#e6f4eb" stroke-width="4" stroke-linecap="round"/><circle cx="60" cy="27" r="4" fill="#f1fbf4"/>
    <rect x="27" y="51" width="8" height="18" rx="4" fill="#e5f4e8"/><rect x="85" y="51" width="8" height="18" rx="4" fill="#e5f4e8"/>
    <rect x="33" y="39" width="54" height="44" rx="19" fill="#edf7ed"/><rect x="38" y="47" width="44" height="27" rx="12" fill="#354b47"/>
    <ellipse cx="49" cy="59" rx="3.5" ry="4.5" fill="#e7f9ec"/><ellipse cx="71" cy="59" rx="3.5" ry="4.5" fill="#e7f9ec"/>
    <path d="M55 67q5 4 10 0" stroke="#8dd8a7" stroke-width="2" fill="none" stroke-linecap="round"/>
    <path d="M45 86q15-7 30 0l5 11H40z" fill="#edf7ed"/><circle cx="60" cy="89" r="3" fill="#62af7e"/>
  </svg>`;
}
