/**
 * GammaEngine — Dealer Gamma Flow Analytics
 *
 * CONTRACT SCHEMA
 * ───────────────
 * Input rawData (CBOE delayed quotes JSON):
 *   rawData.data.current_price  {number}  — underlying spot price
 *   rawData.data.close          {number}  — fallback spot price
 *   rawData.data.options        {Array}   — array of option objects:
 *     option.option            {string}  — OCC symbol e.g. "SPX   240119C04500000"
 *     option.iv                {number}  — implied volatility (decimal, e.g. 0.18)
 *     option.open_interest     {number}  — open interest in contracts
 *     option.gamma             {number}  — gamma (optional, from feed)
 *     option.delta             {number}  — delta (optional, from feed)
 *
 * Output of analyze():
 *   {
 *     spot          {number}    — underlying spot price
 *     symbol        {string}    — ticker symbol
 *     regime        {string}    — "POSITIVE GAMMA" | "NEGATIVE GAMMA"
 *     vixBand       {string}    — "ultra-low" | "normal" | "elevated" | "high" | "crisis"
 *     netGEX        {number}    — net dealer gamma exposure in dollars
 *     gexByStrike   {Object}    — { [strike]: gexDollars }
 *     gexByExpiry   {Object}    — { [expiryDateStr]: gexDollars }
 *     gexSurface    {Array}     — [{strike, expiry, gex}]
 *     gammaFlip     {number}    — interpolated strike where cumulative GEX crosses zero
 *     maxPain       {number}    — strike minimizing total option dollar pain
 *     callWall      {number}    — highest OI call strike above spot
 *     putWall       {number}    — highest OI put strike below spot
 *     totalCharmFlow {number}   — aggregate charm exposure (delta decay)
 *     totalVannaFlow {number}   — aggregate vanna exposure (vol-delta sensitivity)
 *   }
 */

'use strict';

/* ─── Math helpers ───────────────────────────────────────────────────── */

/**
 * normCDF — Abramowitz & Stegun §26.2.17 approximation (max error 7.5e-8)
 * @param {number} x
 * @returns {number}
 */
function normCDF(x) {
  if (x < -8) return 0;
  if (x >  8) return 1;
  const b1 =  0.319381530;
  const b2 = -0.356563782;
  const b3 =  1.781477937;
  const b4 = -1.821255978;
  const b5 =  1.330274429;
  const p  =  0.2316419;
  const t  = 1 / (1 + p * Math.abs(x));
  const poly = t * (b1 + t * (b2 + t * (b3 + t * (b4 + t * b5))));
  const pdf  = Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
  const cdf  = 1 - pdf * poly;
  return x >= 0 ? cdf : 1 - cdf;
}

/**
 * normPDF — standard normal probability density
 * @param {number} x
 * @returns {number}
 */
function normPDF(x) {
  return Math.exp(-0.5 * x * x) / Math.sqrt(2 * Math.PI);
}

/**
 * bsGreeks — Black-Scholes greeks (continuous dividend yield model)
 * @param {number} S     spot
 * @param {number} K     strike
 * @param {number} T     time to expiry in years
 * @param {number} r     risk-free rate (e.g. 0.05)
 * @param {number} q     continuous dividend yield (e.g. 0.013 for SPX)
 * @param {number} sigma implied volatility
 * @param {boolean} isCall
 * @returns {{ delta: number, gamma: number, charm: number, vanna: number }}
 */
function bsGreeks(S, K, T, r, q, sigma, isCall) {
  const zero = { delta: 0, gamma: 0, charm: 0, vanna: 0 };
  if (T <= 0 || sigma <= 0 || S <= 0 || K <= 0) return zero;

  const sqrtT = Math.sqrt(T);
  const d1 = (Math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrtT);
  const d2 = d1 - sigma * sqrtT;

  const pdf1 = normPDF(d1);
  const eqT  = Math.exp(-q * T);

  const gamma = eqT * pdf1 / (S * sigma * sqrtT);
  const delta = isCall
    ? eqT * normCDF(d1)
    : -eqT * normCDF(-d1);

  // Charm = dDelta/dT (rate of delta decay per calendar day)
  // charm = -e^{-qT} [ pdf(d1) * (2*(r-q)*T - d2*sigma*sqrtT) / (2*T*sigma*sqrtT) - q * N(±d1) ]
  const charmSign = isCall ? 1 : -1;
  const Nd1 = isCall ? normCDF(d1) : normCDF(-d1);
  const charm = -eqT * (
    pdf1 * (2 * (r - q) * T - d2 * sigma * sqrtT) / (2 * T * sigma * sqrtT)
    - charmSign * q * Nd1
  );

  // Vanna = dDelta/dSigma = -e^{-qT} * pdf(d1) * d2 / sigma
  const vanna = -eqT * pdf1 * d2 / sigma;

  return { delta, gamma, charm, vanna };
}

/* ─── OCC symbol parser ──────────────────────────────────────────────── */

/**
 * parseOCC — parse an OCC option symbol
 * Format: ROOT(variable) + YYMMDD(6) + (C|P)(1) + STRIKE_x1000_zero-padded(8)
 * @param {string} occSymbol   e.g. "SPX   240119C04500000"
 * @returns {{ root: string, expiry: string, type: string, strike: number }}
 */
function parseOCC(occSymbol) {
  // Strip spaces; the last 15 chars are YYMMDD+type+strike
  const sym = occSymbol.replace(/\s+/g, '');
  if (sym.length < 15) return null;

  const strikeRaw = sym.slice(-8);          // 8 digits, strike * 1000
  const typeChar  = sym.slice(-9, -8);      // C or P
  const yymmdd    = sym.slice(-15, -9);     // YYMMDD
  const root      = sym.slice(0, sym.length - 15);

  const strike = parseInt(strikeRaw, 10) / 1000;
  const yy = yymmdd.slice(0, 2);
  const mm = yymmdd.slice(2, 4);
  const dd = yymmdd.slice(4, 6);
  const expiry = `20${yy}-${mm}-${dd}`;

  return { root, expiry, type: typeChar.toUpperCase(), strike };
}

/* ─── Core analytics ─────────────────────────────────────────────────── */

const R = 0.05;   // risk-free rate
const Q = 0.013;  // SPX continuous dividend yield

/**
 * computeAnalytics — main GEX computation
 * @param {Array}   options         array of option objects from CBOE feed
 * @param {number}  spot            underlying spot price
 * @param {boolean} dealerLongCalls true = dealer bought calls (standard assumption)
 * @returns {Object}
 */
function computeAnalytics(options, spot, dealerLongCalls) {
  const now = Date.now();
  const gexByStrike = {};
  const gexByExpiry = {};
  const gexSurface  = [];

  let netGEX         = 0;
  let totalCharmFlow = 0;
  let totalVannaFlow = 0;

  // For walls and max-pain
  const callOI   = {};  // { strike: totalOI }
  const putOI    = {};

  for (const opt of options) {
    const sym = opt.option;
    if (!sym) continue;

    const parsed = parseOCC(sym);
    if (!parsed) continue;

    const { expiry, type, strike } = parsed;
    const isCall = type === 'C';

    // Time to expiry in years
    const expiryMs = Date.parse(expiry + 'T16:00:00-05:00'); // 4pm ET close
    const T = Math.max(0, (expiryMs - now) / (365.25 * 24 * 3600 * 1000));

    const oi    = Number(opt.open_interest) || 0;
    const iv    = Number(opt.iv)            || 0;

    // Greeks: prefer feed values for gamma/delta, always compute charm & vanna from BS
    let gamma, charm, vanna;
    const feedGamma = Number(opt.gamma);
    if (feedGamma && isFinite(feedGamma) && iv > 0) {
      gamma = feedGamma;
      const bs = bsGreeks(spot, strike, T, R, Q, iv, isCall);
      charm = bs.charm;
      vanna = bs.vanna;
    } else if (iv > 0 && T > 0) {
      const bs = bsGreeks(spot, strike, T, R, Q, iv, isCall);
      gamma = bs.gamma;
      charm = bs.charm;
      vanna = bs.vanna;
    } else {
      gamma = 0; charm = 0; vanna = 0;
    }

    // Dealer sign convention
    // dealerLongCalls=true: dealer long calls (+gamma) short puts (-gamma)
    // dealerLongCalls=false: dealer short calls (-gamma) long puts (+gamma)
    const sign = dealerLongCalls
      ? (isCall ? 1 : -1)
      : (isCall ? -1 : 1);

    // GEX in dollars = S^2 * 0.01 * 100 * sign * gamma * OI
    const gex = spot * spot * 0.01 * 100 * sign * gamma * oi;

    netGEX         += gex;
    totalCharmFlow += sign * charm * oi * 100;
    totalVannaFlow += sign * vanna * oi * 100;

    // Aggregate by strike
    gexByStrike[strike] = (gexByStrike[strike] || 0) + gex;

    // Aggregate by expiry
    gexByExpiry[expiry] = (gexByExpiry[expiry] || 0) + gex;

    // Surface point
    gexSurface.push({ strike, expiry, gex });

    // Track OI for walls & max-pain
    if (isCall) {
      callOI[strike] = (callOI[strike] || 0) + oi;
    } else {
      putOI[strike]  = (putOI[strike]  || 0) + oi;
    }
  }

  /* ── Gamma flip: cumulative GEX ascending strikes, first sign change ── */
  const strikes = Object.keys(gexByStrike).map(Number).sort((a, b) => a - b);
  let cumGEX = 0;
  let prevCum = 0;
  let prevStrike = null;
  let gammaFlip = null;

  for (const s of strikes) {
    cumGEX += gexByStrike[s];
    if (prevStrike !== null && prevCum * cumGEX < 0) {
      // Linear interpolation between prevStrike and s
      gammaFlip = prevStrike + (s - prevStrike) * (-prevCum) / (cumGEX - prevCum);
      break;
    }
    prevCum    = cumGEX;
    prevStrike = s;
  }
  if (gammaFlip === null) gammaFlip = spot; // fallback

  /* ── Max pain ─────────────────────────────────────────────────────── */
  const allStrikes = Array.from(
    new Set([...Object.keys(callOI), ...Object.keys(putOI)].map(Number))
  ).sort((a, b) => a - b);

  let maxPain = spot;
  let minPain = Infinity;
  for (const candidate of allStrikes) {
    let pain = 0;
    for (const s of allStrikes) {
      const cOI = callOI[s] || 0;
      const pOI = putOI[s]  || 0;
      pain += cOI * Math.max(0, s - candidate);   // call intrinsic at candidate
      pain += pOI * Math.max(0, candidate - s);   // put intrinsic at candidate
    }
    if (pain < minPain) { minPain = pain; maxPain = candidate; }
  }

  /* ── Walls ────────────────────────────────────────────────────────── */
  // Call wall: highest OI call strike ABOVE spot
  let callWall = null;
  let maxCallOI = 0;
  for (const s of allStrikes) {
    if (s > spot && (callOI[s] || 0) > maxCallOI) {
      maxCallOI = callOI[s];
      callWall  = s;
    }
  }

  // Put wall: highest OI put strike BELOW spot
  let putWall = null;
  let maxPutOI = 0;
  for (const s of allStrikes) {
    if (s < spot && (putOI[s] || 0) > maxPutOI) {
      maxPutOI = putOI[s];
      putWall  = s;
    }
  }

  return {
    netGEX,
    gexByStrike,
    gexByExpiry,
    gexSurface,
    gammaFlip,
    maxPain,
    callWall,
    putWall,
    totalCharmFlow,
    totalVannaFlow,
  };
}

/* ─── Regime classifier ─────────────────────────────────────────────── */

/**
 * classifyRegime
 * @param {number} netGEX
 * @param {number} [vix]
 * @returns {{ regime: string, vixBand: string }}
 */
function classifyRegime(netGEX, vix) {
  const regime = netGEX >= 0 ? 'POSITIVE GAMMA' : 'NEGATIVE GAMMA';

  let vixBand = 'unknown';
  if (typeof vix === 'number' && isFinite(vix)) {
    if      (vix < 13) vixBand = 'ultra-low';
    else if (vix < 18) vixBand = 'normal';
    else if (vix < 25) vixBand = 'elevated';
    else if (vix < 35) vixBand = 'high';
    else               vixBand = 'crisis';
  }

  return { regime, vixBand };
}

/* ─── Main entry point ──────────────────────────────────────────────── */

/**
 * analyze — full pipeline: parse CBOE feed → compute GEX → classify regime
 * @param {Object}  rawData         CBOE delayed quotes JSON
 * @param {string}  symbol          ticker symbol for labeling
 * @param {boolean} dealerLongCalls dealer position assumption
 * @returns {Object}                full result for UI rendering
 */
function analyze(rawData, symbol, dealerLongCalls) {
  const data = rawData.data || {};

  const spot = Number(data.current_price) || Number(data.close) || 0;
  const options = Array.isArray(data.options) ? data.options : [];

  const analytics = computeAnalytics(options, spot, dealerLongCalls);
  const { regime, vixBand } = classifyRegime(analytics.netGEX);

  return {
    spot,
    symbol,
    regime,
    vixBand,
    ...analytics,
  };
}

/* ─── Public API ────────────────────────────────────────────────────── */

if (typeof window !== 'undefined') {
  window.GammaEngine = { analyze, parseOCC, classifyRegime };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = { analyze, parseOCC, classifyRegime, bsGreeks, normCDF, normPDF };
}
