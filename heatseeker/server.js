require('dotenv').config();
const express = require('express');
const cors = require('cors');
const path = require('path');

const app = express();
const isProd = process.env.NODE_ENV === 'production';

app.use(express.json());

// In production, frontend is served from same origin — no CORS needed.
// In dev, allow Vite dev server on 5173.
if (!isProd) {
  app.use(cors({ origin: 'http://localhost:5173', credentials: true }));
}

// ─── In-memory session store ──────────────────────────────────────────────
// Maps sessionId → { client, greeksMap, symbolMeta, streamerSymbols, spotPrice }
const sessions = new Map();
let sessionCounter = 0;

// Clean up stale sessions older than 4 hours
setInterval(() => {
  const cutoff = Date.now() - 4 * 60 * 60 * 1000;
  for (const [id, sess] of sessions.entries()) {
    if (sess.createdAt < cutoff) {
      try { sess.client?.quoteStreamer?.disconnect(); } catch (_) {}
      sessions.delete(id);
    }
  }
}, 30 * 60 * 1000);

// ─── GEX / Vanna computation ──────────────────────────────────────────────
function computeNodes(greeksMap, spotPrice) {
  const byStrike = {};

  for (const [, data] of greeksMap.entries()) {
    const { strike, type, gamma = 0, delta = 0, vega = 0, volatility = 0, oi = 0 } = data;
    if (!strike) continue;

    if (!byStrike[strike]) byStrike[strike] = { strike, callGex: 0, putGex: 0, callVanna: 0, putVanna: 0 };
    const node = byStrike[strike];

    const gexRaw = oi * gamma * spotPrice * 100;
    const vannaRaw = volatility > 0 ? (vega * Math.abs(delta)) / (spotPrice * volatility) : 0;

    if (type === 'call') { node.callGex += gexRaw; node.callVanna += vannaRaw; }
    else                 { node.putGex  += gexRaw; node.putVanna  += vannaRaw; }
  }

  return Object.values(byStrike)
    .map(n => ({
      strike: n.strike,
      gex:   parseFloat(((n.callGex - n.putGex)   / 1e9).toFixed(3)),
      vanna: parseFloat(((n.callVanna - n.putVanna) * 1000).toFixed(3)),
    }))
    .sort((a, b) => a.strike - b.strike);
}

function identifyNodes(nodes, spot) {
  if (!nodes.length) return { kingNode: null, gatekeepers: [] };
  const kingNode = nodes.reduce((best, n) => Math.abs(n.gex) > Math.abs(best.gex) ? n : best, nodes[0]);
  const lo = Math.min(spot, kingNode.strike);
  const hi = Math.max(spot, kingNode.strike);
  const gatekeepers = nodes
    .filter(n => n.strike > lo && n.strike < hi && Math.abs(n.gex) >= 1.5)
    .sort((a, b) => Math.abs(b.gex) - Math.abs(a.gex))
    .slice(0, 3);
  return { kingNode, gatekeepers };
}

// ─── Auth middleware ──────────────────────────────────────────────────────
function requireAuth(req, res, next) {
  // Session ID can come from header (REST) or query string (SSE — EventSource can't set headers)
  const sessionId = req.headers['x-session-id'] || req.query._sid;
  const sess = sessions.get(sessionId);
  if (!sess) return res.status(401).json({ error: 'Not authenticated' });
  req.sess = sess;
  req.sessionId = sessionId;
  next();
}

// ─── Health check (Railway uses this) ────────────────────────────────────
app.get('/health', (_, res) => res.json({ ok: true }));

// ─── Login ────────────────────────────────────────────────────────────────
app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;
  if (!username || !password)
    return res.status(400).json({ error: 'Username and password required' });

  try {
    // Dynamically import ESM package
    const { default: TastytradeClient } = await import('@tastytrade/api');
    const client = new TastytradeClient(TastytradeClient.ProdConfig);
    await client.sessionService.login(username, password);

    const sessionId = `hs_${++sessionCounter}_${Date.now()}`;
    sessions.set(sessionId, {
      client,
      greeksMap: new Map(),
      symbolMeta: [],
      streamerSymbols: [],
      spotPrice: null,
      createdAt: Date.now(),
    });

    res.json({ success: true, sessionId });
  } catch (err) {
    console.error('Login failed:', err?.message);
    res.status(401).json({ error: 'Authentication failed. Check your Tastytrade credentials.' });
  }
});

// ─── Logout ───────────────────────────────────────────────────────────────
app.post('/api/auth/logout', requireAuth, (req, res) => {
  try { req.sess.client?.quoteStreamer?.disconnect(); } catch (_) {}
  sessions.delete(req.sessionId);
  res.json({ success: true });
});

// ─── Load options chain ───────────────────────────────────────────────────
app.get('/api/chain/:symbol', requireAuth, async (req, res) => {
  const { symbol } = req.params;
  const targetDTE = parseInt(req.query.dte ?? '0', 10);

  try {
    const { client, greeksMap, symbolMeta } = req.sess;
    const chainResp = await client.instrumentService.getOptionChain(symbol);
    const items = chainResp?.data?.items || chainResp?.items || [];
    const underlying = items[0];
    if (!underlying) return res.status(404).json({ error: `No chain found for ${symbol}` });

    // Find the matching expiration
    const expirations = underlying.expirations || [];
    const sorted = [...expirations].sort((a, b) =>
      (a['days-to-expiration'] ?? 999) - (b['days-to-expiration'] ?? 999)
    );
    const expiration = sorted.find(e => {
      const d = e['days-to-expiration'] ?? e.daysToExpiration ?? 999;
      return Math.abs(d - targetDTE) <= 1;
    }) || sorted[0];

    if (!expiration) return res.status(404).json({ error: 'No expiration found' });

    const strikes = expiration.strikes || [];
    const newMeta = [];
    const newSymbols = [];

    for (const strike of strikes) {
      const strikePrice = parseFloat(strike['strike-price'] ?? strike.strikePrice);
      const callSym = strike['call-streamer-symbol'] ?? strike.callStreamerSymbol;
      const putSym  = strike['put-streamer-symbol']  ?? strike.putStreamerSymbol;

      if (callSym) {
        newSymbols.push(callSym);
        newMeta.push({ sym: callSym, strike: strikePrice, type: 'call' });
        if (!greeksMap.has(callSym))
          greeksMap.set(callSym, { strike: strikePrice, type: 'call', delta: 0, gamma: 0, theta: 0, vega: 0, volatility: 0, oi: 0 });
      }
      if (putSym) {
        newSymbols.push(putSym);
        newMeta.push({ sym: putSym, strike: strikePrice, type: 'put' });
        if (!greeksMap.has(putSym))
          greeksMap.set(putSym, { strike: strikePrice, type: 'put', delta: 0, gamma: 0, theta: 0, vega: 0, volatility: 0, oi: 0 });
      }
    }

    req.sess.symbolMeta = newMeta;
    req.sess.streamerSymbols = newSymbols;

    res.json({
      symbol,
      expirationDate: expiration['expiration-date'] ?? expiration.expirationDate,
      dte: expiration['days-to-expiration'] ?? expiration.daysToExpiration,
      strikeCount: strikes.length,
      contractCount: newSymbols.length,
    });
  } catch (err) {
    console.error('Chain error:', err?.message);
    res.status(500).json({ error: err.message });
  }
});

// ─── SSE stream ───────────────────────────────────────────────────────────
app.get('/api/stream/:symbol', requireAuth, async (req, res) => {
  const { symbol } = req.params;
  const sess = req.sess;

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no'); // disable nginx buffering
  res.flushHeaders();

  const send = (data) => {
    try { res.write(`data: ${JSON.stringify(data)}\n\n`); } catch (_) {}
  };

  send({ type: 'status', message: 'Connecting to Tastytrade DXLink streamer...' });

  try {
    const { client, greeksMap, symbolMeta, streamerSymbols } = sess;

    client.quoteStreamer.addEventListener((events) => {
      for (const ev of events) {
        // Greeks event
        if (ev.delta !== undefined || ev.gamma !== undefined || ev.eventType === 'Greeks') {
          const entry = greeksMap.get(ev.eventSymbol);
          if (!entry) continue;
          if (ev.delta     !== undefined) entry.delta     = ev.delta;
          if (ev.gamma     !== undefined) entry.gamma     = ev.gamma;
          if (ev.theta     !== undefined) entry.theta     = ev.theta;
          if (ev.vega      !== undefined) entry.vega      = ev.vega;
          if (ev.volatility !== undefined) entry.volatility = ev.volatility;
        }

        // Quote event — spot price
        if (ev.eventType === 'Quote' && ev.eventSymbol === symbol) {
          const mid = ((ev.bidPrice ?? 0) + (ev.askPrice ?? 0)) / 2;
          if (mid > 0) sess.spotPrice = mid;
        }

        // Summary event — open interest
        if (ev.eventType === 'Summary' && greeksMap.has(ev.eventSymbol)) {
          if (ev.openInterest > 0)
            greeksMap.get(ev.eventSymbol).oi = ev.openInterest;
        }
      }
    });

    await client.quoteStreamer.connect();
    client.quoteStreamer.subscribe([symbol]); // spot price
    if (streamerSymbols.length > 0) {
      client.quoteStreamer.subscribe(streamerSymbols);
      send({ type: 'status', message: `Subscribed to ${streamerSymbols.length} contracts. Waiting for market data...` });
    } else {
      send({ type: 'status', message: 'No contracts found. Try loading the chain first.' });
    }

    const interval = setInterval(() => {
      const spot = sess.spotPrice;
      if (!spot) return;

      const nodes = computeNodes(greeksMap, spot);
      const { kingNode, gatekeepers } = identifyNodes(nodes, spot);
      const dir = kingNode ? (kingNode.strike > spot ? 'BULL' : 'BEAR') : 'NEUT';

      send({
        type: 'update',
        timestamp: Date.now(),
        spot,
        symbol,
        nodes,
        kingNode,
        gatekeepers,
        signals: { SPX: dir, SPY: dir, QQQ: 'NEUT' },
      });
    }, 2000);

    req.on('close', () => {
      clearInterval(interval);
      try { client.quoteStreamer.disconnect(); } catch (_) {}
    });

  } catch (err) {
    console.error('Stream error:', err?.message);
    send({ type: 'error', message: err.message });
    res.end();
  }
});

// ─── Serve built React app (production) ──────────────────────────────────
if (isProd) {
  const distPath = path.join(__dirname, 'dist');
  app.use(express.static(distPath));
  app.get('*', (_, res) => res.sendFile(path.join(distPath, 'index.html')));
}

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`\n🔥 Heatseeker ${isProd ? 'PRODUCTION' : 'DEV'} → http://localhost:${PORT}\n`);
});
