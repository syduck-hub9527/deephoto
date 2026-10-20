import { html } from '../ui.js';
import { PLACEHOLDERS } from '../config.js';
import { Icon } from './Icon.js';
export function Placeholder({ feature }) {
  const item = PLACEHOLDERS[feature];
  return html`<button type="button" class="placeholder" aria-disabled="true" title="即将上线" onClick=${e => e.preventDefault()}>
    ${item.icon && html`<${Icon} name=${item.icon}/>`}<span class=${feature === 'voice' ? 'sr-only' : ''}>${item.label}</span>${['agent', 'tools'].includes(feature) && html`<${Icon} name="up" size=${12}/>`}</button>`;
}
