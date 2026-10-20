// Transport only. Malformed frames are ignored; EOF and application failures are
// interpreted by the conversation controller, never silently turned into done.
export function parseSSEFrame(raw) {
  const data = raw.split(/\r?\n/).filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).replace(/^ /, '')).join('\n');
  if (!data) return null;
  try { const event = JSON.parse(data); return event && typeof event.type === 'string' ? event : null; }
  catch { return null; }
}

export async function consumeSSE(body, onEvent) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const drain = () => {
    let match;
    while ((match = /\r?\n\r?\n/.exec(buffer))) {
      const frame = buffer.slice(0, match.index);
      buffer = buffer.slice(match.index + match[0].length);
      const event = parseSSEFrame(frame);
      if (event) onEvent(event);
    }
  };
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }); drain();
    }
    buffer += decoder.decode(); drain();
    // SSE dispatches only delimited frames; a partial final frame is not done.
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

export async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : `HTTP ${response.status}`;
    throw Object.assign(new Error(detail), { status: response.status, data });
  }
  return data;
}
export const jsonOptions = (method, body) => ({ method,
  headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// Only same-origin document page/image endpoints may become source tabs.
export function safeSourcePath(value) {
  if (typeof value !== 'string' || !/^\/api\/documents\/[A-Za-z0-9_-]+\/(?:pages\/[1-9]\d*|images\/[A-Za-z0-9_-]+)$/.test(value)) return null;
  return value;
}
