/**
 * Heatseeker™ — Backend Server
 *
 * Data flow:
 *  1. POST /sessions          → Tastytrade REST → session-token
 *  2. GET  /api-quote-tokens  → Tastytrade REST → DXLink WS URL + auth token
 *  3. GET  /option-chains/…   → Tastytrade REST → list of streamer symbols
 *  4. GET  /market-data/…     → Tastytrade REST → Open Interest (EOD, per-strike)
 *  5. DXLink WS (dxfeed)      → subscribe Greeks + Quote + Summary events
 *  6. Compute GEX / Vanna     → push via SSE every 2s
 *
 * Key facts about the Tastytrade / DXFeed data layer (as of 2025):
 *  - /api-quote-tokens  → public API endpoint, use this (not /quote-streamer-tokens)
 *  - Greeks (delta,gamma,vega,theta,vol) stream fine for equity options
 *  - SPX index Quote can be spotty; we fall back to the SPY×10 proxy
 *  - Open Interest is EOD data — comes from Summary events or REST market-data
 *  - If OI is missing we still show gamma shape (GEX un-scaled), which is still useful
 */

require('dotenv').config();
// isomorphic-ws polyfills WebSocket for Node.js (required by @dxfeed/dxlink-api)
global.WebSocket = require('isomorphic-ws');

const express = require('express');
const cors    = require('cors');
const axios   = require('axios').default;
const path    = require('path');
const { DXLinkWebSocketClient, DXLinkFeed, FeedDataFormat } = require('@dxfeed/dxlink-api');

const app    = express();
const isProd = process.env.NODE_ENV === 'production';
const TT_API = 'https://api.tastytrade.com';

app.use(express.json());
if (!isProd) app.use(cors({ origin: 'http://localhost:5173', credentials: true }));

// ─── Session store ────────────────────────────────────────────────────────
//  { sessionToken, dxToken, dxWsUrl, greeksMap, oiMap, symbolMeta,
//    streamerSymbols, spotPrice, createdAt }
const sessions = new Map();
let sid = 0;

setInterval(() => {
  const cutoff = Date.now() - 4 * 60 * 60 * 1000;
  for (const [id, s] of sessions) {
    if (s.createdAt < cutoff) { cleanupSession(s); sessions.delete(id); }
  }
}, 30 * 60 * 1000);

function cleanupSession(s) {
  try { s.dxFeed?.close?.(); s.dxClient?.disconnect?.(); } catch (_) {}
}

// ─── GEX / Vanna ─────────────────────────────────────────────────────────
function computeNodes(greeksMap, oiMap, spotPrice) {
  const byStrike = {};

  for (const [sym, g] of greeksMap) {
    const { strike, type } = g;
    if (!strike) continue;

    const oi = oiMap.get(sym) ?? 0; // 0 = OI not yet received
    const gamma = g.gamma ?? 0;
    const delta = g.delta ?? 0;
    const vega  = g.vega  ?? 0;
    const vol   = g.volatility ?? 0;

    if (!byStrike[strike]) byStrike[strike] = { strike, cGex:0, pGex:0, cVanna:0, pVanna:0, hasOI: false };
    const n = byStrike[strike];

    // GEX ($): OI × Gamma × Spot × 100  (calls positive, puts negative net exposure)
    const gexRaw   = oi * gamma * spotPrice * 100;
    // Vanna proxy: Vega × |Delta| / (Spot × ImpliedVol)
    const vannaRaw = vol > 0 ? (vega * Math.abs(delta)) / (spotPrice * vol) : 0;

    if (oi > 0) n.hasOI = true;
    if (type === 'call') { n.cGex += gexRaw; n.cVanna += vannaRaw; }
    else                 { n.pGex += gexRaw; n.pVanna += vannaRaw; }
  }

  return Object.values(byStrike)
    .map(n => ({
      strike: n.strike,
      gex:   parseFloat(((n.cGex - n.pGex)    / 1e9).toFixed(3)),
      vanna: parseFloat(((n.cVanna - n.pVanna) * 1000).toFixed(3)),
      hasOI: n.hasOI,
    }))
    .sort((a, b) => a.strike - b.strike);
}

function identifyNodes(nodes, spot) {
  if (!nodes.length) return { kingNode: null, gatekeepers: [] };
  const king = nodes.reduce((b, n) => Math.abs(n.gex) > Math.abs(b.gex) ? n : b, nodes[0]);
  const lo = Math.min(spot, king.strike), hi = Math.max(spot, king.strike);
  const gates = nodes
    .filter(n => n.strike > lo && n.strike < hi && Math.abs(n.gex) >= 1.0)
    .sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))
    .slice(0, 3);
  return { kingNode: king, gatekeepers: gates };
}

// ─── Auth middleware ──────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  const id   = req.headers['x-session-id'] || req.query._sid;
  const sess = sessions.get(id);
  if (!sess) return res.status(401).json({ error: 'Not authenticated' });
  req.sess = sess; req.sid = id;
  next();
}

// ─── Health check ─────────────────────────────────────────────────────────
app.get('/health', (_, res) => res.json({ ok: true }));

// ─── Login ────────────────────────────────────────────────────────────────
app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;
  if (!username || !password)
    return res.status(400).json({ error: 'Username and password required' });

  try {
    // 1. Authenticate with Tastytrade REST API
    const loginResp = await axios.post(`${TT_API}/sessions`, {
      login: username,
      password,
      'remember-me': true,
    }, { headers: { 'Content-Type': 'application/json' } });

    const sessionToken = loginResp.data?.data?.['session-token'];
    if (!sessionToken) throw new Error('No session token in response');

    // 2. Fetch DXLink streaming credentials
    //    /api-quote-tokens is the public API endpoint (vs /quote-streamer-tokens which is internal)
    const tokenResp = await axios.get(`${TT_API}/api-quote-tokens`, {
      headers: { Authorization: sessionToken },
    });
    const dxData  = tokenResp.data?.data;
    const dxToken = dxData?.token;
    const dxWsUrl = dxData?.['dxlink-url'] ?? dxData?.websocket_url ?? 'wss://tasty-openapi-ws.dxfeed.com/realtime';

    if (!dxToken) throw new Error('Could not obtain DXLink token');

    const id = `hs_${++sid}_${Date.now()}`;
    sessions.set(id, {
      sessionToken,
      dxToken,
      dxWsUrl,
      greeksMap: new Map(),    // sym → { strike, type, delta, gamma, vega, theta, volatility }
      oiMap:     new Map(),    // sym → openInterest (from Summary events)
      symbolMeta: [],          // [{ sym, strike, type }]
      streamerSymbols: [],
      spotPrice: null,
      dxClient:  null,
      dxFeed:    null,
      createdAt: Date.now(),
    });

    res.json({ success: true, sessionId: id });
  } catch (err) {
    console.error('Login error:', err?.response?.data ?? err?.message);
    const msg = err?.response?.data?.error?.message ?? err.message ?? 'Authentication failed';
    res.status(401).json({ error: msg });
  }
});

// ─── Logout ───────────────────────────────────────────────────────────────
app.post('/api/auth/logout', requireAuth, (req, res) => {
  cleanupSession(req.sess);
  sessions.delete(req.sid);
  res.json({ success: true });
});

// ─── Load options chain ───────────────────────────────────────────────────
app.get('/api/chain/:symbol', requireAuth, async (req, res) => {
  const { symbol } = req.params;
  const targetDTE  = parseInt(req.query.dte ?? '0', 10);
  const { sessionToken, greeksMap, oiMap } = req.sess;

  try {
    // Fetch nested option chain from Tastytrade REST
    const chainResp = await axios.get(
      `${TT_API}/option-chains/${encodeURIComponent(symbol)}/nested`,
      { headers: { Authorization: sessionToken } }
    );

    const items     = chainResp.data?.data?.items ?? [];
    const underlying = items[0];
    if (!underlying) return res.status(404).json({ error: `No chain found for ${symbol}` });

    // Find the target expiration by DTE
    const expirations = (underlying.expirations ?? [])
      .sort((a, b) => (a['days-to-expiration'] ?? 999) - (b['days-to-expiration'] ?? 999));

    const expiration = expirations.find(e =>
      Math.abs((e['days-to-expiration'] ?? 999) - targetDTE) <= 1
    ) ?? expirations[0];

    if (!expiration) return res.status(404).json({ error: 'No expiration found' });

    const strikes = expiration.strikes ?? [];
    const newMeta = [], newSymbols = [];

    for (const s of strikes) {
      const price   = parseFloat(s['strike-price'] ?? s.strikePrice ?? 0);
      const callSym = s['call-streamer-symbol'] ?? s.callStreamerSymbol;
      const putSym  = s['put-streamer-symbol']  ?? s.putStreamerSymbol;

      for (const [sym, type] of [[callSym, 'call'], [putSym, 'put']]) {
        if (!sym) continue;
        newSymbols.push(sym);
        newMeta.push({ sym, strike: price, type });
        if (!greeksMap.has(sym))
          greeksMap.set(sym, { strike: price, type, delta: 0, gamma: 0, vega: 0, theta: 0, volatility: 0 });
        if (!oiMap.has(sym)) oiMap.set(sym, 0);
      }
    }

    req.sess.symbolMeta      = newMeta;
    req.sess.streamerSymbols = newSymbols;

    // Attempt to pre-fetch Open Interest from Tastytrade's market metrics endpoint.
    // This endpoint may not exist on all account types — we fail gracefully.
    try {
      // Tastytrade provides OI via GET /market-data/options endpoint (batch)
      // We batch-request in chunks of 100 to avoid URL length limits
      const CHUNK = 100;
      for (let i = 0; i < newSymbols.length; i += CHUNK) {
        const chunk = newSymbols.slice(i, i + CHUNK);
        // Build query string: symbols[]=X&symbols[]=Y...
        // Tastytrade OCC symbol format is needed — derive from streamer symbol
        // e.g. .SPXW250507C5600 → the chain `s.call` field has the OCC format
        const oiResp = await axios.get(`${TT_API}/market-data/options`, {
          headers: { Authorization: sessionToken },
          params: { 'symbols[]': chunk },
        }).catch(() => null);

        if (oiResp?.data?.data?.items) {
          for (const item of oiResp.data.data.items) {
            const streamerSym = item['streamer-symbol'] ?? item.streamerSymbol;
            const oi = item['open-interest'] ?? item.openInterest;
            if (streamerSym && oi != null) oiMap.set(streamerSym, oi);
          }
        }
      }
    } catch (oiErr) {
      // OI pre-fetch failed — DXLink Summary events will populate it later
      console.warn('OI pre-fetch skipped:', oiErr?.message);
    }

    res.json({
      symbol,
      expirationDate: expiration['expiration-date'] ?? expiration.expirationDate,
      dte:            expiration['days-to-expiration'] ?? expiration.daysToExpiration ?? 0,
      strikeCount:    strikes.length,
      contractCount:  newSymbols.length,
      oiLoaded:       [...oiMap.values()].some(v => v > 0),
    });
  } catch (err) {
    console.error('Chain error:', err?.response?.data ?? err?.message);
    res.status(500).json({ error: err.message });
  }
});

// ─── SSE stream ───────────────────────────────────────────────────────────
app.get('/api/stream/:symbol', requireAuth, (req, res) => {
  const { symbol } = req.params;
  const sess = req.sess;

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');
  res.flushHeaders();

  const send = (data) => { try { res.write(`data: ${JSON.stringify(data)}\n\n`); } catch (_) {} };

  send({ type: 'status', message: 'Connecting to DXFeed DXLink...' });

  try {
    const { dxToken, dxWsUrl, greeksMap, oiMap, symbolMeta, streamerSymbols } = sess;

    // Build DXLink client
    const dxClient = new DXLinkWebSocketClient();
    sess.dxClient = dxClient;

    dxClient.connect(dxWsUrl);
    dxClient.setAuthToken(dxToken);

    const feed = new DXLinkFeed(dxClient, 'AUTO');
    sess.dxFeed = feed;

    // Request only the fields we need — reduces bandwidth
    feed.configure({
      acceptAggregationPeriod: 2,
      acceptDataFormat: FeedDataFormat.COMPACT,
      acceptEventFields: {
        Greeks:  ['eventSymbol', 'delta', 'gamma', 'theta', 'vega', 'volatility', 'price'],
        Quote:   ['eventSymbol', 'bidPrice', 'askPrice'],
        Summary: ['eventSymbol', 'openInterest'],
      },
    });

    // Handle incoming events
    feed.addEventListener((events) => {
      for (const ev of events) {
        switch (ev.eventType) {
          case 'Greeks': {
            const entry = greeksMap.get(ev.eventSymbol);
            if (!entry) break;
            if (ev.delta      != null) entry.delta      = ev.delta;
            if (ev.gamma      != null) entry.gamma      = ev.gamma;
            if (ev.theta      != null) entry.theta      = ev.theta;
            if (ev.vega       != null) entry.vega       = ev.vega;
            if (ev.volatility != null) entry.volatility = ev.volatility;
            break;
          }
          case 'Quote': {
            // Spot price — try both SPX and $SPX symbols
            if (ev.eventSymbol === symbol || ev.eventSymbol === `$${symbol}`) {
              const mid = ((ev.bidPrice ?? 0) + (ev.askPrice ?? 0)) / 2;
              if (mid > 0) sess.spotPrice = mid;
            }
            // Also capture from SPY (×10) as fallback if SPX quote is missing
            if (ev.eventSymbol === 'SPY' && !sess.spotPrice) {
              const spyMid = ((ev.bidPrice ?? 0) + (ev.askPrice ?? 0)) / 2;
              if (spyMid > 0) sess.spotPrice = spyMid * 10;
            }
            break;
          }
          case 'Summary': {
            // Open Interest comes through Summary events (EOD data)
            if (oiMap.has(ev.eventSymbol) && ev.openInterest > 0)
              oiMap.set(ev.eventSymbol, ev.openInterest);
            break;
          }
        }
      }
    });

    // Subscribe to everything
    // Quote for underlying spot price — DXLink may need '$SPX' format for index
    const spotSubs = [
      { type: 'Quote', symbol },
      { type: 'Quote', symbol: `$${symbol}` },  // some DXFeed providers use $ prefix for indices
      { type: 'Quote', symbol: 'SPY' },          // fallback proxy (×10)
    ];
    feed.addSubscriptions(...spotSubs);

    // Greeks + Summary for each option contract
    const optionSubs = streamerSymbols.flatMap(sym => [
      { type: 'Greeks',  symbol: sym },
      { type: 'Summary', symbol: sym },
    ]);

    if (optionSubs.length > 0) {
      feed.addSubscriptions(...optionSubs);
      send({ type: 'status', message: `Subscribed — ${streamerSymbols.length} contracts · ${oiMap.size} OI entries loaded. Waiting for data...` });
    }

    // Push computed nodes every 2 seconds
    const interval = setInterval(() => {
      const spot = sess.spotPrice;
      if (!spot) {
        send({ type: 'status', message: 'Waiting for spot price data...' });
        return;
      }

      const nodes = computeNodes(greeksMap, oiMap, spot);
      const { kingNode, gatekeepers } = identifyNodes(nodes, spot);
      const dir = kingNode ? (kingNode.strike > spot ? 'BULL' : 'BEAR') : 'NEUT';

      // OI status — if none populated, note it so user knows GEX is gamma-shape only
      const oiPopulated = [...oiMap.values()].some(v => v > 0);

      send({
        type:      'update',
        timestamp: Date.now(),
        spot,
        symbol,
        nodes,
        kingNode,
        gatekeepers,
        oiPopulated,
        signals: {
          SPX: dir,
          SPY: dir,
          QQQ: 'NEUT', // needs independent QQQ chain subscription
        },
      });
    }, 2000);

    req.on('close', () => {
      clearInterval(interval);
      cleanupSession(sess);
    });

  } catch (err) {
    console.error('Stream setup error:', err?.message);
    send({ type: 'error', message: err.message });
    res.end();
  }
});

// ─── Serve built React app ────────────────────────────────────────────────
if (isProd) {
  const dist = path.join(__dirname, 'dist');
  app.use(express.static(dist));
  app.get('*', (_, res) => res.sendFile(path.join(dist, 'index.html')));
}

const PORT = process.env.PORT || 3001;
app.listen(PORT, () =>
  console.log(`\n🔥 Heatseeker [${isProd ? 'PROD' : 'DEV'}] → http://localhost:${PORT}\n`)
);
