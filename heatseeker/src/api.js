// src/api.js — no username/password needed. Server handles OAuth via env vars.

const KEY = 'hs_sid';
export const getSessionId = () => sessionStorage.getItem(KEY);
const set   = (id) => sessionStorage.setItem(KEY, id);
const clear = () => sessionStorage.removeItem(KEY);

function headers() {
  return { 'Content-Type': 'application/json', 'x-session-id': getSessionId() || '' };
}

async function req(method, url, body) {
  const r = await fetch(url, { method, headers: headers(), body: body ? JSON.stringify(body) : undefined });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

// No credentials — server uses OAuth env vars
export async function startSession() {
  const data = await req('POST', '/api/session/start');
  set(data.sessionId);
  return data;
}

export async function endSession() {
  try { await req('POST', '/api/session/end'); } finally { clear(); }
}

export const hasSession = () => Boolean(getSessionId());

export async function loadChain(symbol, dte = 0) {
  return req('GET', `/api/chain/${encodeURIComponent(symbol)}?dte=${dte}`);
}

export function openStream(symbol, { onUpdate, onStatus, onError }) {
  const sid = getSessionId();
  const es  = new EventSource(`/api/stream/${encodeURIComponent(symbol)}?_sid=${encodeURIComponent(sid)}`);
  es.onmessage = (ev) => {
    try {
      const p = JSON.parse(ev.data);
      if (p.type === 'update') onUpdate?.(p);
      else if (p.type === 'status') onStatus?.(p.message);
      else if (p.type === 'error') onError?.(p.message);
    } catch (_) {}
  };
  es.onerror = () => onError?.('Stream disconnected — refresh to reconnect.');
  return () => es.close();
}
