// src/api.js

const SESSION_KEY = 'hs_session_id';

export const getSessionId = () => sessionStorage.getItem(SESSION_KEY);
const setSessionId = (id) => sessionStorage.setItem(SESSION_KEY, id);
const clearSessionId = () => sessionStorage.removeItem(SESSION_KEY);

function headers() {
  return { 'Content-Type': 'application/json', 'x-session-id': getSessionId() || '' };
}

async function req(method, url, body) {
  const r = await fetch(url, { method, headers: headers(), body: body ? JSON.stringify(body) : undefined });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

export async function login(username, password) {
  const data = await req('POST', '/api/auth/login', { username, password });
  setSessionId(data.sessionId);
  return data;
}

export async function logout() {
  try { await req('POST', '/api/auth/logout'); } finally { clearSessionId(); }
}

export const isLoggedIn = () => Boolean(getSessionId());

export async function loadChain(symbol, dte = 0) {
  return req('GET', `/api/chain/${encodeURIComponent(symbol)}?dte=${dte}`);
}

export function openStream(symbol, { onUpdate, onStatus, onError }) {
  const sid = getSessionId();
  // Pass session ID via query string — EventSource doesn't support custom headers
  const es = new EventSource(`/api/stream/${encodeURIComponent(symbol)}?_sid=${encodeURIComponent(sid)}`);

  es.onmessage = (ev) => {
    try {
      const p = JSON.parse(ev.data);
      if (p.type === 'update') onUpdate?.(p);
      else if (p.type === 'status') onStatus?.(p.message);
      else if (p.type === 'error') onError?.(p.message);
    } catch (_) {}
  };

  es.onerror = () => onError?.('Stream disconnected. Refresh to reconnect.');
  return () => es.close();
}
