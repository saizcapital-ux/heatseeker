/**
 * Heatseeker™ — Backend Server (OAuth2 edition)
 *
 * Auth: OAuth2 via env vars — no login screen needed.
 * Set these in Railway Variables:
 *   TT_CLIENT_ID      — from developer.tastytrade.com OAuth Applications
 *   TT_CLIENT_SECRET  — same
 *   TT_REFRESH_TOKEN  — from Create Grant on the same page
 *
 * The access token lasts 15 min; we refresh every 14 min automatically.
 */

require('dotenv').config();
global.WebSocket = require('isomorphic-ws');

const express  = require('express');
const cors     = require('cors');
const axios    = require('axios').default;
const path     = require('path');
const { DXLinkWebSocketClient, DXLinkFeed, FeedDataFormat } = require('@dxfeed/dxlink-api');

const app    = express();
const isProd = process.env.NODE_ENV === 'production';
const TT_API = 'https://api.tastytrade.com';
const USER_AGENT = 'heatseeker-pro/1.0';

app.use(express.json());
if (!isProd) app.use(cors({ origin: 'http://localhost:5173', credentials: true }));

// ─── OAuth2 token state ───────────────────────────────────────────────────
let accessToken  = null;
let tokenExpires = 0;

async function refreshAccessToken() {
  const { TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN } = process.env;
  if (!TT_CLIENT_ID || !TT_CLIENT_SECRET || !TT_REFRESH_TOKEN) {
    throw new Error('Missing env vars. Set TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN in Railway Variables.');
  }
  const resp = await axios.post(
    `${TT_API}/oauth/token`,
    new URLSearchParams({
      grant_type:    'refresh_token',
      refresh_token: TT_REFRESH_TOKEN,
      client_id:     TT_CLIENT_ID,
      client_secret: TT_CLIENT_SECRET,
    }),
    { headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': USER_AGENT } }
  );
  accessToken  = resp.data.access_token;
  tokenExpires = Date.now() + (resp.data.expires_in ?? 900) * 1000;
  console.log(`✓ OAuth token refreshed — expires in ${resp.data.expires_in}s`);
  return accessToken;
}

async function getToken() {
  if (!accessToken || Date.now() > tokenExpires - 60_000) await refreshAccessToken();
  return accessToken;
}

// Refresh every 14 minutes
setInterval(() => refreshAccessToken().catch(e => console.error('Token refresh failed:', e.message)), 14 * 60 * 1000);

// ─── GEX / Vanna ─────────────────────────────────────────────────────────
function computeNodes(greeksMap, oiMap, spotPrice) {
  const byStrike = {};
  for (const [sym, g] of greeksMap) {
    const { strike, type } = g;
    if (!strike) continue;
    const oi  = oiMap.get(sym) ?? 0;
    const gexRaw   = oi * (g.gamma ?? 0) * spotPrice * 100;
    const vannaRaw = (g.volatility ?? 0) > 0
      ? ((g.vega ?? 0) * Math.abs(g.delta ?? 0)) / (spotPrice * g.volatility)
      : 0;
    if (!byStrike[strike]) byStrike[strike] = { strike, cGex:0, pGex:0, cVanna:0, pVanna:0 };
    if (type === 'call') { byStrike[strike].cGex += gexRaw; byStrike[strike].cVanna += vannaRaw; }
    else                 { byStrike[strike].pGex += gexRaw; byStrike[strike].pVanna += vannaRaw; }
  }
  return Object.values(byStrike)
    .map(n => ({
      strike: n.strike,
      gex:   parseFloat(((n.cGex - n.pGex)    / 1e9).toFixed(3)),
      vanna: parseFloat(((n.cVanna - n.pVanna) * 1000).toFixed(3)),
    }))
    .sort((a, b) => a.strike - b.strike);
}

function identifyNodes(nodes, spot) {
  if (!nodes.length) return { kingNode: null, gatekeepers: [] };
  const king  = nodes.reduce((b, n) => Math.abs(n.gex) > Math.abs(b.gex) ? n : b, nodes[0]);
  const lo    = Math.min(spot, king.strike), hi = Math.max(spot, king.strike);
  const gates = nodes
    .filter(n => n.strike > lo && n.strike < hi && Math.abs(n.gex) >= 1.0)
    .sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex)).slice(0, 3);
  return { kingNode: king, gatekeepers: gates };
}

// ─── Session store ────────────────────────────────────────────────────────
const sessions = new Map();
let sid = 0;

function cleanupSession(s) { try { s.dxFeed?.close?.(); s.dxClient?.disconnect?.(); } catch (_) {} }
setInterval(() => {
  const cut = Date.now() - 4 * 60 * 60 * 1000;
  for (const [id, s] of sessions) { if (s.createdAt < cut) { cleanupSession(s); sessions.delete(id); } }
}, 30 * 60 * 1000);

function requireSession(req, res, next) {
  const sess = sessions.get(req.headers['x-session-id'] || req.query._sid);
  if (!sess) return res.status(401).json({ error: 'No session. Reload the page.' });
  req.sess = sess; next();
}

// ─── Health ───────────────────────────────────────────────────────────────
app.get('/health', (_, res) => res.json({ ok: true, tokenReady: !!accessToken }));

// ─── Session init — no username/password, OAuth token is in env vars ──────
app.post('/api/session/start', async (req, res) => {
  try {
    const token     = await getToken();
    const tokenResp = await axios.get(`${TT_API}/api-quote-tokens`, {
      headers: { Authorization: `Bearer ${token}`, 'User-Agent': USER_AGENT },
    });
    const dxData  = tokenResp.data?.data ?? {};
    const dxToken = dxData.token;
    const dxWsUrl = dxData['dxlink-url'] ?? 'wss://tasty-openapi-ws.dxfeed.com/realtime';
    if (!dxToken) throw new Error('Could not obtain DXLink token');

    const id = `hs_${++sid}_${Date.now()}`;
    sessions.set(id, {
      dxToken, dxWsUrl,
      greeksMap: new Map(), oiMap: new Map(),
      symbolMeta: [], streamerSymbols: [],
      spotPrice: null, dxClient: null, dxFeed: null,
      createdAt: Date.now(),
    });
    res.json({ success: true, sessionId: id });
  } catch (err) {
    const msg = err?.response?.data?.errors?.[0]?.detail ?? err?.response?.data?.error?.message ?? err.message;
    console.error('Session start error:', msg);
    res.status(500).json({ error: msg });
  }
});

app.post('/api/session/end', requireSession, (req, res) => {
  cleanupSession(req.sess); sessions.delete(req.headers['x-session-id'] || req.query._sid);
  res.json({ success: true });
});

// ─── Load options chain ───────────────────────────────────────────────────
app.get('/api/chain/:symbol', requireSession, async (req, res) => {
  const { symbol }  = req.params;
  const targetDTE   = parseInt(req.query.dte ?? '0', 10);
  const { greeksMap, oiMap } = req.sess;

  try {
    const token = await getToken();
    const chainResp = await axios.get(
      `${TT_API}/option-chains/${encodeURIComponent(symbol)}/nested`,
      { headers: { Authorization: `Bearer ${token}`, 'User-Agent': USER_AGENT } }
    );
    const underlying = (chainResp.data?.data?.items ?? [])[0];
    if (!underlying) return res.status(404).json({ error: `No chain for ${symbol}` });

    const expirations = (underlying.expirations ?? [])
      .sort((a, b) => (a['days-to-expiration'] ?? 999) - (b['days-to-expiration'] ?? 999));
    const expiration = expirations.find(e => Math.abs((e['days-to-expiration'] ?? 999) - targetDTE) <= 1) ?? expirations[0];
    if (!expiration) return res.status(404).json({ error: 'No expiration found' });

    const strikes = expiration.strikes ?? [];
    const newMeta = [], newSymbols = [];

    for (const s of strikes) {
      const price = parseFloat(s['strike-price'] ?? 0);
      for (const [sym, type] of [[s['call-streamer-symbol'], 'call'], [s['put-streamer-symbol'], 'put']]) {
        if (!sym) continue;
        newSymbols.push(sym);
        newMeta.push({ sym, strike: price, type });
        if (!greeksMap.has(sym)) greeksMap.set(sym, { strike: price, type, delta:0, gamma:0, vega:0, theta:0, volatility:0 });
        if (!oiMap.has(sym)) oiMap.set(sym, 0);
      }
    }
    req.sess.symbolMeta = newMeta;
    req.sess.streamerSymbols = newSymbols;

    res.json({
      symbol,
      expirationDate: expiration['expiration-date'],
      dte:            expiration['days-to-expiration'] ?? 0,
      strikeCount:    strikes.length,
      contractCount:  newSymbols.length,
    });
  } catch (err) {
    console.error('Chain error:', err?.response?.data ?? err?.message);
    res.status(500).json({ error: err.message });
  }
});

// ─── SSE stream ───────────────────────────────────────────────────────────
app.get('/api/stream/:symbol', requireSession, (req, res) => {
  const { symbol } = req.params;
  const sess = req.sess;

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders();

  const send = (data) => { try { res.write(`data: ${JSON.stringify(data)}\n\n`); } catch (_) {} };
  send({ type: 'status', message: 'Connecting to DXLink streamer...' });

  try {
    const { dxToken, dxWsUrl, greeksMap, oiMap, streamerSymbols } = sess;
    const dxClient = new DXLinkWebSocketClient();
    sess.dxClient  = dxClient;
    dxClient.connect(dxWsUrl);
    dxClient.setAuthToken(dxToken);

    const feed = new DXLinkFeed(dxClient, 'AUTO');
    sess.dxFeed = feed;

    feed.configure({
      acceptAggregationPeriod: 2,
      acceptDataFormat: FeedDataFormat.COMPACT,
      acceptEventFields: {
        Greeks:  ['eventSymbol', 'delta', 'gamma', 'theta', 'vega', 'volatility'],
        Quote:   ['eventSymbol', 'bidPrice', 'askPrice'],
        Summary: ['eventSymbol', 'openInterest'],
      },
    });

    feed.addEventListener((events) => {
      for (const ev of events) {
        if (ev.eventType === 'Greeks') {
          const e = greeksMap.get(ev.eventSymbol); if (!e) continue;
          if (ev.delta      != null) e.delta      = ev.delta;
          if (ev.gamma      != null) e.gamma      = ev.gamma;
          if (ev.theta      != null) e.theta      = ev.theta;
          if (ev.vega       != null) e.vega       = ev.vega;
          if (ev.volatility != null) e.volatility = ev.volatility;
        } else if (ev.eventType === 'Quote') {
          if ([symbol, `$${symbol}`, `${symbol}.X`].includes(ev.eventSymbol)) {
            const mid = ((ev.bidPrice ?? 0) + (ev.askPrice ?? 0)) / 2;
            if (mid > 0) sess.spotPrice = mid;
          }
          if (ev.eventSymbol === 'SPY' && !sess.spotPrice) {
            const m = ((ev.bidPrice ?? 0) + (ev.askPrice ?? 0)) / 2;
            if (m > 0) sess.spotPrice = m * 10;
          }
        } else if (ev.eventType === 'Summary') {
          if (oiMap.has(ev.eventSymbol) && (ev.openInterest ?? 0) > 0)
            oiMap.set(ev.eventSymbol, ev.openInterest);
        }
      }
    });

    feed.addSubscriptions({ type:'Quote', symbol }, { type:'Quote', symbol:`$${symbol}` }, { type:'Quote', symbol:'SPY' });
    if (streamerSymbols.length > 0) {
      feed.addSubscriptions(
        ...streamerSymbols.map(s => ({ type:'Greeks',  symbol:s })),
        ...streamerSymbols.map(s => ({ type:'Summary', symbol:s })),
      );
      send({ type: 'status', message: `Subscribed to ${streamerSymbols.length} contracts. Waiting for data...` });
    }

    const interval = setInterval(() => {
      const spot = sess.spotPrice;
      if (!spot) return;
      const nodes = computeNodes(greeksMap, oiMap, spot);
      const { kingNode, gatekeepers } = identifyNodes(nodes, spot);
      const dir = kingNode ? (kingNode.strike > spot ? 'BULL' : 'BEAR') : 'NEUT';
      send({ type:'update', timestamp:Date.now(), spot, symbol, nodes, kingNode, gatekeepers,
             oiPopulated: [...oiMap.values()].some(v => v > 0),
             signals:{ SPX:dir, SPY:dir, QQQ:'NEUT' } });
    }, 2000);

    req.on('close', () => { clearInterval(interval); cleanupSession(sess); });
  } catch (err) {
    send({ type:'error', message:err.message }); res.end();
  }
});

// ─── Serve built React app ────────────────────────────────────────────────
if (isProd) {
  const dist = path.join(__dirname, 'dist');
  app.use(express.static(dist));
  app.get('*', (_, res) => res.sendFile(path.join(dist, 'index.html')));
}

const PORT = process.env.PORT || 3001;
app.listen(PORT, async () => {
  console.log(`\n🔥 Heatseeker [${isProd ? 'PROD' : 'DEV'}] → http://localhost:${PORT}`);
  try { await refreshAccessToken(); console.log('✓ Tastytrade OAuth2 ready\n'); }
  catch (err) { console.error('⚠ OAuth init failed:', err.message, '\n  → Set TT_CLIENT_ID, TT_CLIENT_SECRET, TT_REFRESH_TOKEN in Railway Variables\n'); }
});
