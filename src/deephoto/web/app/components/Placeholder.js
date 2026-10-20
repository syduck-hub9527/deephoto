import { html } from '../ui.js';
import { PLACEHOLDERS } from '../config.js';
export function Placeholder({ feature }) {
  const item = PLACEHOLDERS[feature];
  return html`<button type="button" class="placeholder" aria-disabled="true" title="即将上线" onClick=${e => e.preventDefault()}>
    ${item.icon && html`<span aria-hidden="true">${item.icon}</span>`}${item.label}</button>`;
}
