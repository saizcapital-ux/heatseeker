#!/usr/bin/env python3
"""HEATSEEKER — minimal signal dashboard."""
import json, os, threading, time, logging, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("heatseeker")

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE    = os.path.join(DATA_DIR, "gex_state.json")
HISTORY_FILE = os.path.join(DATA_DIR, "gex_history.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
VIX_UUID        = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"
AGENTIC_ACCOUNT = "634079917"

_ticker_status = {"logged_in": False, "last_error": None, "last_ok": None}
_tick_count = 0
_state_lock = threading.Lock()

def _push_secret():
    """Shared secret for the worker→web /api/push channel.
    Priority: explicit PUSH_SECRET env → auto-derived from RH_USERNAME (both
    containers have it, so they agree with zero config and it isn't the public
    default) → 'heatseeker' as a last resort."""
    s = os.getenv("PUSH_SECRET")
    if s:
        return s
    u = os.getenv("RH_USERNAME")
    if u:
        import hashlib
        return "hs_" + hashlib.sha256(("heatseeker-push:" + u).encode()).hexdigest()[:24]
    return "heatseeker"
HIST_SAMPLE_SEC = 120   # collapse spot-only updates into the last point within this window

def _read_state():
    try:
        with open(GEX_STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def _read_journal():
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f).get("trades", [])
    except Exception:
        return []

def _read_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []

# ── CBOE always-on GEX engine ────────────────────────────────────────────────
# Free, no-key, 15-min-delayed full options chain (same source the bundled
# gamma-terminal uses). This guarantees the dashboard ALWAYS has real GEX data —
# even when Robinhood is logged out, the worker is down, or the market is closed.
CBOE_URL     = "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json"
CBOE_VIX_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_VIX.json"
CBOE_STRIKE_WINDOW = 25          # strikes within ±$ of spot for the ladder
CBOE_REFRESH_SEC   = 120         # recompute at most every 2 min (delayed feed, but fresher snapshots)
CBOE_RH_FRESH_SEC  = 960         # treat a Robinhood scan as authoritative for 16 min
_cboe_last = 0

def _fetch_json(url, timeout=12):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (HeatseekerTerminal/1.0)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def _parse_occ(sym):
    """Parse an OCC option symbol e.g. 'SPY   260701C00745000' → (expiry, type, strike)."""
    s = (sym or "").replace(" ", "")
    if len(s) < 15:
        return None
    try:
        strike = int(s[-8:]) / 1000.0
        typ    = s[-9].upper()            # 'C' or 'P'
        yymmdd = s[-15:-9]
        expiry = f"20{yymmdd[0:2]}-{yymmdd[2:4]}-{yymmdd[4:6]}"
        return expiry, typ, strike
    except Exception:
        return None

def _cboe_analytics(payload):
    """Pure function: CBOE JSON → GEX state patch (ladder, king, flip, walls, max pain).
    Returns None if the payload has no usable gamma/OI data."""
    data = (payload or {}).get("data", {}) or {}
    spot = data.get("current_price") or data.get("close")
    opts = data.get("options", []) or []
    if not spot or not opts:
        return None
    spot = float(spot)
    today = datetime.now(ET).strftime("%Y-%m-%d")

    # Group by expiry; pick the soonest expiry that is today-or-later (0DTE when available).
    by_exp = {}
    for o in opts:
        p = _parse_occ(o.get("option"))
        if not p:
            continue
        exp, typ, strike = p
        by_exp.setdefault(exp, []).append((typ, strike, o))
    future = sorted(e for e in by_exp if e >= today)
    if not future:
        return None
    exp = future[0]
    chain = by_exp[exp]

    # Aggregate per strike within the window.
    calls, puts = {}, {}   # strike -> {oi, gamma, vol}
    for typ, strike, o in chain:
        if abs(strike - spot) > CBOE_STRIKE_WINDOW:
            continue
        oi = float(o.get("open_interest") or 0)
        gm = float(o.get("gamma") or 0)
        vol = float(o.get("volume") or 0)
        dl = float(o.get("delta") or 0)
        iv = float(o.get("iv") or 0)
        bucket = calls if typ == "C" else puts
        b = bucket.setdefault(strike, {"oi": 0.0, "gamma": 0.0, "vol": 0.0, "delta": 0.0, "iv": 0.0})
        b["oi"] += oi; b["gamma"] = gm or b["gamma"]; b["vol"] += vol
        b["delta"] = dl or b["delta"]; b["iv"] = iv or b["iv"]

    strikes = sorted(set(calls) | set(puts))
    if not strikes:
        return None

    def gex(oi, gamma, is_call):
        return (1 if is_call else -1) * oi * gamma * spot * 100

    ladder, net_total = [], 0.0
    any_gamma = False
    for s in strikes:
        c = calls.get(s, {"oi": 0, "gamma": 0, "vol": 0})
        p = puts.get(s, {"oi": 0, "gamma": 0, "vol": 0})
        if c["gamma"] or p["gamma"]:
            any_gamma = True
        cg = gex(c["oi"], c["gamma"], True)
        pg = gex(p["oi"], p["gamma"], False)
        net_total += (cg + pg) / 1e6
        ladder.append({
            "strike":     s,
            "call_oi":    int(c["oi"]),  "put_oi":  int(p["oi"]),
            "call_vol":   int(c["vol"]), "put_vol": int(p["vol"]),
            "call_gex_m": round(cg / 1e6, 3),
            "put_gex_m":  round(pg / 1e6, 3),
            "net_gex_m":  round((cg + pg) / 1e6, 3),
            "call_prem_k": 0.0, "put_prem_k": 0.0,
        })
    if not any_gamma:
        return None   # feed had no greeks — don't overwrite good data with zeros

    # King node = strike with largest |net GEX|
    king = max(ladder, key=lambda r: abs(r["net_gex_m"]))
    king_strike = king["strike"]
    king_gex_m  = king["net_gex_m"]

    # Gamma flip = strike where cumulative net GEX crosses zero
    cum, prev, gamma_flip = 0.0, None, king_strike
    for r in ladder:
        cum += r["net_gex_m"]
        if prev is not None and ((prev < 0 <= cum) or (prev > 0 >= cum)):
            gamma_flip = r["strike"]
        prev = cum

    # Walls = heaviest OI above / below spot
    calls_above = [(s, calls[s]["oi"]) for s in calls if s > spot]
    puts_below  = [(s, puts[s]["oi"])  for s in puts  if s < spot]
    call_wall = max(calls_above, key=lambda x: x[1])[0] if calls_above else None
    put_wall  = max(puts_below,  key=lambda x: x[1])[0] if puts_below  else None

    # Max pain = strike minimizing total intrinsic payout to holders
    max_pain, best = None, None
    for k in strikes:
        pay = sum(calls.get(s, {}).get("oi", 0) * max(0.0, k - s) for s in strikes) \
            + sum(puts.get(s, {}).get("oi", 0)  * max(0.0, s - k) for s in strikes)
        if best is None or pay < best:
            best, max_pain = pay, k

    # ── Net dealer greeks (DEX + book vega) via Black-Scholes ──
    # DEX uses the same dealer convention as GEX (long calls / short puts):
    #   DEX = Σ(callΔ·callOI) − Σ(putΔ·putOI). Vega is direction-agnostic from OI
    #   alone (needs flow to sign), so we expose book-vega MAGNITUDE only.
    import math
    try:
        exp_dt = datetime.strptime(exp, "%Y-%m-%d").replace(hour=16, tzinfo=ET)
        T = max((exp_dt - datetime.now(ET)).total_seconds() / (365.0 * 86400), 1.0 / (365 * 24 * 6))
    except Exception:
        T = 1.0 / 365
    _r, _q = 0.045, 0.013
    def _bs_dv(S, K, sig, is_call):
        if sig <= 0 or S <= 0 or K <= 0 or T <= 0:
            return 0.0, 0.0
        sq = sig * math.sqrt(T)
        d1 = (math.log(S / K) + (_r - _q + sig * sig / 2) * T) / sq
        pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
        Nd1 = 0.5 * (1 + math.erf(d1 / math.sqrt(2)))
        delta = math.exp(-_q * T) * (Nd1 if is_call else Nd1 - 1)
        vega = S * math.exp(-_q * T) * pdf * math.sqrt(T) / 100.0   # per 1% vol
        return delta, vega
    dex_shares, book_vega = 0.0, 0.0
    for s in strikes:
        c = calls.get(s, {}); p = puts.get(s, {})
        c_oi, p_oi = c.get("oi", 0), p.get("oi", 0)
        cd = c.get("delta") or _bs_dv(spot, s, c.get("iv", 0), True)[0]
        pd = p.get("delta") or _bs_dv(spot, s, p.get("iv", 0), False)[0]
        _, cv = _bs_dv(spot, s, c.get("iv", 0), True)
        _, pv = _bs_dv(spot, s, p.get("iv", 0), False)
        dex_shares += cd * c_oi - pd * p_oi          # DEX convention
        book_vega  += cv * c_oi + pv * p_oi
    net_delta_m = round(dex_shares * spot * 100 / 1e6, 1)   # $M of dealer delta
    book_vega_m = round(book_vega * 100 / 1e6, 2)           # $M vega in the book (magnitude)

    total_abs = sum(abs(r["net_gex_m"]) for r in ladder) or 1
    concentration = abs(king_gex_m) / total_abs
    direction = "call" if king_gex_m >= 0 else "put"

    return {
        "spot":          round(spot, 2),
        "strike_ladder": ladder,
        "net_gex_total": round(net_total, 2),
        "net_delta_m":   net_delta_m,
        "book_vega_m":   book_vega_m,
        "king_strike":   king_strike,
        "king_gex_m":    round(king_gex_m, 2),
        "gamma_flip":    gamma_flip,
        "max_pain":      max_pain,
        "call_wall":     call_wall,
        "put_wall":      put_wall,
        "direction":     direction,
        "confidence":    round(concentration, 3),
        "gex_source":    "cboe",
        "gex_ts":        time.time(),
        "gex_expiry":    exp,
    }

def _refresh_cboe_gex():
    """Compute GEX from the CBOE feed and write it as the base layer — unless a
    fresh Robinhood scan is already authoritative (then only refresh spot/VIX)."""
    global _cboe_last
    now = time.time()
    if now - _cboe_last < CBOE_REFRESH_SEC:
        return
    _cboe_last = now
    try:
        patch = _cboe_analytics(_fetch_json(CBOE_URL))
    except Exception as e:
        log.debug(f"CBOE fetch/parse failed: {e}")
        return
    if not patch:
        return
    # VIX from CBOE index feed (best-effort)
    try:
        vd = (_fetch_json(CBOE_VIX_URL) or {}).get("data", {})
        vix = vd.get("current_price") or vd.get("close")
        if vix:
            patch["vix"] = round(float(vix), 2)
    except Exception:
        pass

    st = _read_state()

    # SPX "real-money" confirmation layer (institutional dealer gamma).
    spx_patch = {}
    try:
        import scripts.spx_signal as spx_signal
        spy_dir = patch.get("direction")
        sx = (spx_signal.confirmation(spy_dir, spy_spot=patch["spot"]) if spy_dir
              else spx_signal.compute(spy_spot=patch["spot"]))
        if sx:
            # Adaptive SPX/SPY ratio: EMA-smooth the instantaneous ratio so
            # quote-timing jitter between the two feeds (and slow dividend/roll
            # drift) don't wobble the translated levels.
            instant = sx["ratio"]
            prev = st.get("spx_ratio_ema")
            ema = round(0.3 * instant + 0.7 * float(prev), 4) if prev else round(instant, 4)
            def _sc(v):
                return round(v / ema, 2) if v else None
            spx_patch = {
                "spx_spot":          sx["spx_spot"],
                "spx_ratio":         ema,
                "spx_ratio_instant": round(instant, 4),
                "spx_ratio_ema":     ema,
                "spx_direction":     sx["direction"],
                "spx_regime":        sx["regime"],
                "spx_net_gex":       sx["net_gex"],
                "spx_expiry":        sx.get("expiry"),
                "spx_king_spy":      _sc(sx["king"]),
                "spx_flip_spy":      _sc(sx["gamma_flip"]),
                "spx_call_wall_spy": _sc(sx["call_wall"]),
                "spx_put_wall_spy":  _sc(sx["put_wall"]),
                "spx_max_pain_spy":  _sc(sx["max_pain"]),
                "spx_confirm":       sx.get("label"),
                "spx_confirm_mult":  sx.get("mult"),
            }
    except Exception as e:
        log.debug(f"SPX signal failed: {e}")
    rh_fresh = st.get("gex_source") == "robinhood" and (now - float(st.get("gex_ts") or 0) < CBOE_RH_FRESH_SEC)
    if rh_fresh:
        # Robinhood scan owns the GEX picture right now — only touch price/VIX/SPX.
        thin = {"spot": patch["spot"]}
        if "vix" in patch:
            thin["vix"] = patch["vix"]
        thin.update(spx_patch)
        _write_state(thin)
        log.info(f"CBOE tick (RH authoritative): SPY={patch['spot']} SPX={spx_patch.get('spx_spot')}")
    else:
        patch.update(spx_patch)
        patch["last_action"] = st.get("last_action") or \
            f"GEX from CBOE (15-min delayed) — {patch['direction'].upper()} bias, king ${patch['king_strike']:.0f}"
        patch["market_updated"] = datetime.now(ET).strftime("%H:%M ET")
        _write_state(patch)
        log.info(f"CBOE GEX written: {patch['direction'].upper()} king=${patch['king_strike']:.0f} flip=${patch['gamma_flip']:.0f} strikes={len(patch['strike_ladder'])}")

_yf_candle_last = 0
def _refresh_yf_candles(force=False):
    """Keyless intraday candles (yfinance, incl. pre/post) so the chart fills in
    even while a Robinhood login is pending device approval (which blocks the
    RH data path). Throttled; skipped once RH candles are flowing."""
    global _yf_candle_last
    now = time.time()
    if not force and now - _yf_candle_last < 120:
        return
    _yf_candle_last = now
    try:
        import yfinance as yf
        df = yf.Ticker("SPY").history(period="1d", interval="5m", prepost=True)
        candles = []
        for idx, row in df.iterrows():
            o, h, l, c = row.get("Open"), row.get("High"), row.get("Low"), row.get("Close")
            if not (o and h and l and c) or o != o:   # skip NaN/zero rows
                continue
            try:
                et = idx.tz_convert(ET)
            except Exception:
                et = idx
            m = et.hour * 60 + et.minute
            sess = "reg" if 570 <= m < 960 else ("pre" if m < 570 else "post")
            candles.append({"t": idx.isoformat(), "o": round(float(o), 2), "h": round(float(h), 2),
                            "l": round(float(l), 2), "c": round(float(c), 2),
                            "v": int(row.get("Volume") or 0), "s": sess})
        if candles:
            _write_state({"candles": candles})
            log.info(f"yfinance candles (pre-login): {len(candles)} bars")
    except Exception as e:
        log.debug(f"yfinance candle refresh failed: {e}")

def _append_history(state):
    """Record an intraday time-series point so the dashboard can show the gamma
    flip / king node migrating relative to spot through the day (the core 0DTE
    signal). Spot-only updates within HIST_SAMPLE_SEC collapse into the last
    point to keep the series smooth without bloat."""
    spot = state.get("spot")
    if not spot:
        return
    try:
        now = time.time()
        today = datetime.now(ET).strftime("%Y-%m-%d")
        hist = [h for h in _read_history() if h.get("d") == today]
        ladder = state.get("strike_ladder") or []
        net_total = round(sum((r.get("net_gex_m") or 0) for r in ladder), 2) if ladder else None
        flip = state.get("gamma_flip")
        king = state.get("king_strike")
        pt = {
            "d": today,
            "t": datetime.now(ET).strftime("%H:%M:%S"),
            "_ts": now,
            "spot": round(float(spot), 2),
            "flip": flip,
            "king": king,
            "net": net_total,
            "cw": state.get("call_wall"),
            "pw": state.get("put_wall"),
            "vix": state.get("vix"),
            "dir": state.get("direction"),
            "conf": state.get("confidence"),
            "spx": state.get("spx_confirm"),
            "spxdir": state.get("spx_direction"),
        }
        spx_confirm = state.get("spx_confirm")
        last = hist[-1] if hist else None
        if (last and now - last.get("_ts", 0) < HIST_SAMPLE_SEC
                and last.get("flip") == flip and last.get("king") == king
                and last.get("spx") == spx_confirm):
            # same regime snapshot — just refresh spot/time on the last point
            last.update({"spot": pt["spot"], "t": pt["t"], "_ts": now, "vix": pt["vix"]})
        else:
            hist.append(pt)
        hist = hist[-500:]
        with open(HISTORY_FILE, "w") as f:
            json.dump(hist, f)
    except Exception as e:
        log.debug(f"history append failed: {e}")

def _write_state(patch):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _state_lock:
        state = _read_state()
        state.update(patch)
        with open(GEX_STATE, "w") as f:
            json.dump(state, f, indent=2)
        _append_history(state)

_rh_login_inflight = False
def _rh_login_worker():
    """Blocking Robinhood login (may wait on device approval) — runs OFF the
    ticker thread so a pending approval can't freeze CBOE/candle refreshes."""
    global _rh_login_inflight
    try:
        import robin_stocks.robinhood as rh
        u = os.getenv("RH_USERNAME", ""); p = os.getenv("RH_PASSWORD", "")
        if not (u and p):
            return
        sd = pathlib.Path(os.getenv("RH_SESSION_DIR", "/data/rh_session"))
        sd.mkdir(parents=True, exist_ok=True)
        tk = pathlib.Path.home() / ".tokens"
        if not tk.exists():
            try: tk.symlink_to(sd)
            except Exception: pass
        mfa = None
        totp = os.getenv("RH_TOTP_SECRET", "").strip()
        if totp:
            try:
                import pyotp
                mfa = pyotp.TOTP(totp).now()
            except Exception: pass
        if not mfa:
            mfa = os.getenv("RH_MFA_CODE") or None
        log.info("RH login starting (background)…")
        rh.login(username=u, password=p, mfa_code=mfa,
                 store_session=True, expiresIn=86400, pickle_name="heatseeker")
        _ticker_status["logged_in"] = True
        _ticker_status["last_error"] = None
        log.info("RH login OK")
    except Exception as e:
        _ticker_status["last_error"] = str(e)
        log.warning(f"RH background login failed: {e}")
    finally:
        _rh_login_inflight = False

# ── background ticker: SPY + VIX + balance + open positions every 60s ────────
def _ticker():
    global _tick_count, _rh_login_inflight
    while True:
        logged_in = _ticker_status["logged_in"]
        # Always-on GEX base layer — runs regardless of Robinhood/login state so
        # the dashboard is never blank. Self-throttled to every CBOE_REFRESH_SEC.
        try:
            _refresh_cboe_gex()
        except Exception as e:
            log.debug(f"CBOE refresh error: {e}")
        # Not logged in: keep the chart alive with keyless candles and kick off
        # the (blocking) login in the background so it never stalls this loop.
        if not logged_in:
            _refresh_yf_candles()
            if not _rh_login_inflight and os.getenv("RH_USERNAME") and os.getenv("RH_PASSWORD"):
                _rh_login_inflight = True
                threading.Thread(target=_rh_login_worker, daemon=True).start()
        try:
            import robin_stocks.robinhood as rh
            # yfinance fallback — runs even without RH login so dashboard stays live
            if not logged_in:
                try:
                    import yfinance as yf
                    patch = {"market_updated": datetime.now(ET).strftime("%H:%M ET")}
                    fi = yf.Ticker("SPY").fast_info
                    for attr in ("last_price", "regularMarketPrice", "previousClose"):
                        v = getattr(fi, attr, None)
                        if v:
                            patch["spot"] = round(float(v), 2)
                            break
                    fi2 = yf.Ticker("^VIX").fast_info
                    for attr in ("last_price", "regularMarketPrice", "previousClose"):
                        v = getattr(fi2, attr, None)
                        if v:
                            vix = round(float(v), 2)
                            patch["vix"] = vix
                            patch["vix3m"] = round(vix * 1.07, 2)
                            slope = round((patch["vix3m"] - vix) / vix, 4)
                            patch["ts_slope"] = slope
                            patch["ts_label"] = "deep_contango" if slope > 0.10 else "contango" if slope > 0 else "backwardation"
                            break
                    # (Candles come from _refresh_yf_candles() above, before login.)
                    _write_state(patch)
                    log.info(f"yfinance tick SPY={patch.get('spot')} VIX={patch.get('vix')}")
                except Exception as yfe:
                    log.debug(f"yfinance fallback error: {yfe}")

            if logged_in:
                patch = {"market_updated": datetime.now(ET).strftime("%H:%M ET")}

                # SPY price
                q = rh.stocks.get_quotes(["SPY"], info=None) or [{}]
                raw = q[0].get("last_trade_price") or q[0].get("adjusted_previous_close")
                if raw:
                    patch["spot"] = round(float(raw), 2)

                # VIX — try multiple endpoints
                vix_val = None
                for vix_url in [
                    f"https://api.robinhood.com/marketdata/index-instruments/{VIX_UUID}/quotes/",
                    "https://api.robinhood.com/marketdata/index-instruments/",
                ]:
                    try:
                        vd = rh.helper.request_get(vix_url) or {}
                        raw_vix = vd.get("value") or vd.get("last_trade_price")
                        if raw_vix:
                            vix_val = round(float(raw_vix), 2)
                            break
                    except Exception:
                        pass
                if not vix_val:
                    try:
                        # Fallback: VIX from quotes endpoint
                        vq = rh.stocks.get_quotes(["VIX"], info=None) or [{}]
                        raw_vix = vq[0].get("last_trade_price") or vq[0].get("adjusted_previous_close")
                        if raw_vix:
                            vix_val = round(float(raw_vix), 2)
                    except Exception:
                        pass
                if vix_val:
                    patch["vix"] = vix_val
                    patch["vix3m"] = round(vix_val * 1.07, 2)
                    slope = round((patch["vix3m"] - vix_val) / vix_val, 4)
                    patch["ts_slope"] = slope
                    patch["ts_label"] = "deep_contango" if slope > 0.10 else "contango" if slope > 0 else "backwardation"

                # Balance — Agentic account only (634079917)
                try:
                    acc_data = rh.helper.request_get(
                        f"https://api.robinhood.com/accounts/{AGENTIC_ACCOUNT}/"
                    ) or {}
                    bal = float(acc_data.get("buying_power") or acc_data.get("cash") or 0)
                    if not bal:
                        # fallback: portfolio endpoint filtered to agentic account
                        port_data = rh.helper.request_get(
                            f"https://api.robinhood.com/portfolios/{AGENTIC_ACCOUNT}/"
                        ) or {}
                        bal = float(port_data.get("withdrawable_amount") or port_data.get("excess_margin") or 0)
                    if bal:
                        patch["balance"] = round(bal, 2)
                        log.info(f"agentic balance={bal}")
                except Exception as be:
                    log.warning(f"balance fetch error: {be}")

                # Open option positions
                try:
                    positions = rh.options.get_open_option_positions() or []
                    open_pos = []
                    for pos in positions:
                        qty = float(pos.get("quantity", 0))
                        if qty <= 0:
                            continue
                        avg = float(pos.get("average_price", 0))
                        exp = pos.get("expiration_date", "")
                        sym = pos.get("chain_symbol", "SPY")
                        opt_id = pos.get("option") or pos.get("option_id") or ""
                        # get current mark price
                        mark = avg
                        try:
                            if opt_id:
                                # extract UUID from URL if needed
                                uid = opt_id.rstrip("/").split("/")[-1]
                                qr = rh.options.get_option_market_data_by_id(uid) or {}
                                m = qr.get("adjusted_mark_price") or qr.get("mark_price")
                                if m:
                                    mark = round(float(m), 4)
                        except Exception:
                            pass
                        pnl_pct = round((mark - avg) / avg * 100, 1) if avg else 0
                        pnl_usd = round((mark - avg) * qty * 100, 2)
                        open_pos.append({
                            "symbol": sym,
                            "expiration": exp,
                            "avg_price": avg,
                            "mark_price": mark,
                            "quantity": qty,
                            "pnl_pct": pnl_pct,
                            "pnl_usd": pnl_usd,
                        })
                    patch["open_positions"] = open_pos
                    log.info(f"open positions: {len(open_pos)}")
                except Exception as pe:
                    log.warning(f"positions fetch error: {pe}")

                # Intraday SPY candles (5-min bars). Try extended-hours first (so
                # the chart is populated before 9:30); fall back to regular hours.
                # Each attempt is guarded independently — a throw on one (e.g. an
                # older robin_stocks that rejects bounds="trading") must not skip
                # the others.
                try:
                    bars = []
                    for kwargs in ({"bounds": "trading"}, {}):
                        try:
                            bars = rh.stocks.get_stock_historicals(
                                "SPY", interval="5minute", span="day", **kwargs) or []
                        except Exception as be:
                            log.debug(f"candle attempt {kwargs} failed: {be}")
                            bars = []
                        if bars:
                            break
                    candles = []
                    for bar in bars:
                        t = bar.get("begins_at", "")
                        o = float(bar.get("open_price") or 0)
                        h = float(bar.get("high_price") or 0)
                        l = float(bar.get("low_price") or 0)
                        c = float(bar.get("close_price") or 0)
                        v = int(float(bar.get("volume") or 0))
                        sess = bar.get("session", "reg")   # 'pre' | 'reg' | 'post'
                        if o and h and l and c:
                            candles.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "s": sess})
                    if candles:
                        patch["candles"] = candles
                        pre = sum(1 for x in candles if x["s"] != "reg")
                        log.info(f"candles: {len(candles)} bars ({pre} pre/post)")
                except Exception as ce:
                    log.warning(f"candle fetch error: {ce}")

                # GEX is computed by the scheduler worker and pushed via /api/push
                # Removed from web ticker — find_options_by_expiration loads 7+ pages
                # and blocks the ticker loop for minutes
                _tick_count += 1

                _write_state(patch)
                _ticker_status["last_ok"] = datetime.now(ET).strftime("%H:%M:%S ET")
                log.info(f"tick SPY={patch.get('spot')} VIX={patch.get('vix')}")

        except Exception as e:
            import traceback
            log.warning(f"ticker error: {e}\n{traceback.format_exc()}")
            _ticker_status["logged_in"] = False   # force a fresh background re-login
            _ticker_status["last_error"] = str(e)

        for _ in range(30):     # 30s loop → near-real-time price/candles/positions
            time.sleep(1)

threading.Thread(target=_ticker, daemon=True).start()

# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # Default to the plain-English "simple" view; full quant terminal at /pro.
    return render_template("simple.html")

@app.route("/pro")
def pro():
    return render_template("dashboard.html")

@app.route("/api/state")
def api_state():
    g = _read_state()
    trades = _read_journal()
    closed = [t for t in trades if t.get("pnl_pct") is not None]
    total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed)
    bal = g.get("balance") or (closed[-1].get("balance_after") if closed else None) or 21.64

    # The scheduler writes the full per-strike GEX ladder as "strike_ladder"
    # (fields: net_gex_m, call_gex_m, put_gex_m, call_oi, put_oi, call_vol, put_vol).
    # Normalize it into the shape the dashboard's GEX cloud + GEX levels consume.
    # (Historically the dashboard read "gex_by_strike", a key nothing ever wrote —
    #  which is why the GEX cloud was always empty.)
    ladder = g.get("strike_ladder") or []
    gex_by_strike = [{
        "strike":   row.get("strike"),
        "call_gex": row.get("call_gex_m", 0),
        "put_gex":  row.get("put_gex_m", 0),
        "net_gex":  row.get("net_gex_m", 0),
        "call_oi":  row.get("call_oi", 0),
        "put_oi":   row.get("put_oi", 0),
        "call_vol": row.get("call_vol", 0),
        "put_vol":  row.get("put_vol", 0),
    } for row in ladder if row.get("strike") is not None]

    # Determine what the bot is doing right now — mirrors scheduler.py windows (ET).
    now = datetime.now(ET)
    t_min = now.hour * 60 + now.minute
    is_weekday = now.weekday() < 5
    if not is_weekday:
        bot_phase, bot_phase_label = "closed", "Weekend — market closed"
    elif t_min < 570:
        bot_phase, bot_phase_label = "premarket", "Pre-market — waiting for 9:30 open"
    elif t_min > 960:
        bot_phase, bot_phase_label = "closed", "After hours — flat until tomorrow"
    elif 925 <= t_min <= 935:
        bot_phase, bot_phase_label = "force_close", "Force-close window — flattening (3:30 PM)"
    elif 585 <= t_min <= 750:
        bot_phase, bot_phase_label = "entry", "Entry window — scanning for signal (9:45–12:30)"
    elif 600 <= t_min <= 930:
        bot_phase, bot_phase_label = "manage", "Managing positions — trailing stops (until 3:30)"
    else:
        bot_phase, bot_phase_label = "monitor", "Monitoring — GEX scan only, no new entries"

    # ── Trade-lifecycle levels for the open position, translated to the SPY axis ──
    # Option stop/trail/target are premium-based; map them to approximate SPY
    # prices via the entry delta so they can be drawn on the price chart.
    trade_levels = None
    op = g.get("open_positions") or []
    if op:
        es, ep, ed = g.get("entry_spot"), g.get("entry_price"), g.get("entry_delta")
        ot, strike = (g.get("entry_opt_type") or "call"), g.get("entry_strike")
        try:
            if es and ep and ed and strike:
                es, ep, strike = float(es), float(ep), float(strike)
                dlt = abs(float(ed)) or 0.4
                is_call = str(ot).lower().startswith("c")
                be = strike + ep if is_call else strike - ep
                def _mv(pct):  # SPY move for a premium % change, via delta
                    return pct * ep / dlt
                if is_call:
                    stop, trail, tp = es - _mv(0.50), es + _mv(0.30), es + _mv(1.00)
                else:
                    stop, trail, tp = es + _mv(0.50), es - _mv(0.30), es - _mv(1.00)
                trade_levels = {
                    "entry": round(es, 2), "strike": round(strike, 2), "be": round(be, 2),
                    "stop": round(stop, 2), "trail": round(trail, 2), "target": round(tp, 2),
                    "opt_type": ot, "pnl_pct": op[0].get("pnl_pct"),
                }
        except Exception:
            trade_levels = None

    # ── Decision transparency: reconstruct the three entry gates the bot uses ──
    vix       = g.get("vix") or 0
    slope     = g.get("ts_slope")
    conf_raw  = g.get("confidence") or 0
    conf_pct  = round(conf_raw * 100 if conf_raw <= 1 else conf_raw)
    direction = g.get("direction")
    has_pos   = bool(g.get("open_positions"))
    balance   = round(float(bal), 2)

    g_regime  = bool(vix and vix < 30 and (slope is None or slope > -0.05))
    g_signal  = bool(direction in ("call", "put") and conf_pct >= 50)
    g_account = bool(balance >= 1 and not has_pos and bot_phase == "entry")
    gates = [
        {"name": "Regime",  "ok": g_regime,
         "why": (f"VIX {vix:.1f} · {(g.get('ts_label') or '—').replace('_',' ')}" if vix else "no VIX data")},
        {"name": "Signal",  "ok": g_signal,
         "why": f"{(direction or 'flat').upper()} @ {conf_pct}% conf"},
        {"name": "Account", "ok": g_account,
         "why": f"${balance:.2f} · {'flat' if not has_pos else 'in position'} · {bot_phase}"},
    ]
    # 4th overlay: SPX (institutional / "real money") must not contradict the SPY signal.
    spx_confirm = g.get("spx_confirm")
    spx_dir     = g.get("spx_direction")
    if spx_confirm == "DIVERGENT":
        g_spx, spx_why = False, f"SPX {(spx_dir or '').upper()} bias diverges — real money disagrees"
    elif spx_confirm == "ALIGNED":
        g_spx, spx_why = True, f"SPX {(spx_dir or '').upper()} bias confirms SPY"
    else:
        g_spx, spx_why = True, ("SPX flat/neutral" if spx_dir else "no SPX data (neutral)")
    gates.append({"name": "SPX Confirm", "ok": g_spx, "why": spx_why})

    all_go = g_regime and g_signal and g_account and g_spx

    net_gex_total = round(sum((r.get("net_gex_m") or 0) for r in ladder), 2) if ladder else None

    return jsonify({
        "spot":           g.get("spot"),
        "prev_close":     g.get("prev_close"),
        "vix":            g.get("vix"),
        "vix3m":          g.get("vix3m"),
        "ts_label":       g.get("ts_label"),
        "ts_slope":       g.get("ts_slope"),
        "vanna_label":    g.get("vanna_label"),
        "ivr":            g.get("ivr"),
        "vrp_label":      g.get("vrp_label"),
        "direction":      g.get("direction"),
        "confidence":     g.get("confidence"),
        "regime_ok":      g.get("regime_ok"),
        "last_action":    g.get("last_action", "Bot starting up..."),
        "market_updated": g.get("market_updated"),
        "balance":        round(float(bal), 2),
        "total_trades":   len(closed),
        "total_pnl":      round(total_pnl, 2),
        "trades":         trades[-20:],
        "open_positions": g.get("open_positions", []),
        "trade_levels":   trade_levels,
        "gex_by_strike":  gex_by_strike,
        "king_strike":    g.get("king_strike"),
        "king_gex_m":     g.get("king_gex_m"),
        "gamma_flip":     g.get("gamma_flip"),
        "max_pain":       g.get("max_pain"),
        "call_wall":      g.get("call_wall"),
        "put_wall":       g.get("put_wall"),
        "flow_feed":      g.get("flow_feed", []),
        "flow_ratio":     g.get("flow_ratio"),
        "call_vol_total": g.get("call_vol_total"),
        "put_vol_total":  g.get("put_vol_total"),
        "top_nodes":      g.get("top_nodes"),
        "net_gex_total":  net_gex_total,
        "net_delta_m":    g.get("net_delta_m"),
        "book_vega_m":    g.get("book_vega_m"),
        "gex_source":     g.get("gex_source"),
        "gex_expiry":     g.get("gex_expiry"),
        "spx_spot":       g.get("spx_spot"),
        "spx_ratio":      g.get("spx_ratio"),
        "spx_ratio_instant": g.get("spx_ratio_instant"),
        "spx_direction":  g.get("spx_direction"),
        "spx_regime":     g.get("spx_regime"),
        "spx_net_gex":    g.get("spx_net_gex"),
        "spx_confirm":    g.get("spx_confirm"),
        "spx_king_spy":      g.get("spx_king_spy"),
        "spx_flip_spy":      g.get("spx_flip_spy"),
        "spx_call_wall_spy": g.get("spx_call_wall_spy"),
        "spx_put_wall_spy":  g.get("spx_put_wall_spy"),
        "spx_max_pain_spy":  g.get("spx_max_pain_spy"),
        "confidence_reasons": g.get("confidence_reasons", []),
        "conf_pct":       conf_pct,
        "gates":          gates,
        "all_go":         all_go,
        "candles":        g.get("candles", []),
        "bot_phase":      bot_phase,
        "bot_phase_label": bot_phase_label,
        "ts":             now.strftime("%H:%M:%S ET"),
        "rh_logged_in":   _ticker_status["logged_in"],
        "rh_error":       _ticker_status["last_error"],
        "rh_last_ok":     _ticker_status["last_ok"],
    })

@app.route("/api/push", methods=["POST"])
def api_push():
    key = request.headers.get("X-Push-Key", "")
    expected = _push_secret()
    if key != expected:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True) or {}
    if data:
        # A push carrying a real GEX ladder is a live Robinhood scan — mark it
        # authoritative so the CBOE base layer defers to it for a window.
        if data.get("strike_ladder"):
            data["gex_source"] = "robinhood"
            data["gex_ts"] = time.time()
        _write_state(data)
    return jsonify({"ok": True})

@app.route("/api/history")
def api_history():
    """Intraday time-series: spot vs gamma flip / king node / net GEX through the day."""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    hist = [h for h in _read_history() if h.get("d") == today]
    for h in hist:
        h.pop("_ts", None)
    return jsonify({"history": hist})

@app.route("/api/ticker-status")
def api_ticker_status():
    return jsonify(_ticker_status)

def _health_checks():
    """Plain-English readiness checks for a live-trading deploy."""
    g = _read_state()
    checks = []

    creds_ok = bool(os.getenv("RH_USERNAME") and os.getenv("RH_PASSWORD"))
    checks.append({"name": "Robinhood credentials", "ok": creds_ok,
                   "detail": "RH_USERNAME + RH_PASSWORD set" if creds_ok
                             else "MISSING — set RH_USERNAME and RH_PASSWORD"})

    logged_in = bool(_ticker_status.get("logged_in"))
    checks.append({"name": "Robinhood login", "ok": logged_in,
                   "detail": (f"logged in (last OK {_ticker_status.get('last_ok') or '—'})" if logged_in
                              else f"NOT logged in — {_ticker_status.get('last_error') or 'click the device-approval email'}")})

    sess_dir = os.getenv("RH_SESSION_DIR", "/data/rh_session")
    sess_ok = (pathlib.Path(sess_dir, "heatseeker.pickle").exists()
               or (pathlib.Path.home() / ".tokens" / "heatseeker.pickle").exists())
    checks.append({"name": "Session persisted", "ok": sess_ok,
                   "detail": f"token saved in {sess_dir}" if sess_ok
                             else "no saved session — attach a Volume at /data so login survives restarts"})

    src = g.get("gex_source"); gts = float(g.get("gex_ts") or 0)
    worker_ok = (src == "robinhood") and (time.time() - gts < 1800)
    checks.append({"name": "Worker → dashboard link", "ok": worker_ok,
                   "detail": ("receiving live Robinhood scans from the worker" if worker_ok
                              else "no recent worker scans — set WEB_URL on the worker "
                                   "(dashboard is running on CBOE fallback for now)")})

    spot = g.get("spot")
    checks.append({"name": "Market data", "ok": bool(spot),
                   "detail": (f"SPY ${spot} via {src or 'cboe'}" if spot else "no price yet")})

    bal = g.get("balance")
    checks.append({"name": "Agentic account", "ok": bool(bal),
                   "detail": (f"#{AGENTIC_ACCOUNT} · ${bal} buying power" if bal
                              else f"#{AGENTIC_ACCOUNT} · balance not loaded yet")})

    dry = os.getenv("DRY_RUN", "false").lower() == "true"
    checks.append({"name": "Trading mode", "ok": True,
                   "detail": ("REAL MONEY — agentic account only" if not dry
                              else "DRY-RUN (paper) — set DRY_RUN=false for live")})

    tw_on   = os.getenv("TWITTER_ENABLED", "false").lower() == "true"
    tw_keys = all(os.getenv(k) for k in ("TWITTER_API_KEY", "TWITTER_API_SECRET",
                                         "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_SECRET"))
    checks.append({"name": "X posting", "ok": True,
                   "detail": ("ON — entries & exits will post to your account" if (tw_on and tw_keys)
                              else "TWITTER_ENABLED=true but API keys incomplete" if tw_on
                              else "off (optional) — set TWITTER_ENABLED=true + 4 API keys to post")})

    checks.append({"name": "Last GEX scan", "ok": bool(g.get("market_updated")),
                   "detail": g.get("market_updated") or "waiting for first scan"})

    ready = creds_ok and logged_in
    return {"ready": ready, "checks": checks,
            "ts": datetime.now(ET).strftime("%H:%M:%S ET")}

@app.route("/ping")
def ping():
    # Liveness only — 200 whenever the web process is up. Use THIS as Railway's
    # healthcheck path (never /health, which is a human-readable config report and
    # would crash-loop the dashboard if a down RH login were treated as unhealthy).
    return "ok", 200

@app.route("/api/health")
def api_health():
    return jsonify(_health_checks())

@app.route("/health")
def health_page():
    h = _health_checks()
    rows = "".join(
        f'<div class="row"><span class="ic {"ok" if c["ok"] else "no"}">'
        f'{"✓" if c["ok"] else "✗"}</span><div><div class="nm">{c["name"]}</div>'
        f'<div class="dt">{c["detail"]}</div></div></div>'
        for c in h["checks"])
    banner = ('<div class="ban ok">✅ Ready for live trading</div>' if h["ready"]
              else '<div class="ban no">⚠️ Not ready — fix the ✗ items below</div>')
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HEATSEEKER — Health</title><meta http-equiv="refresh" content="15">
<style>
body{{background:#0a0c10;color:#c8ccd8;font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:560px;margin:0 auto;padding:24px 16px}}
h1{{font-size:15px;letter-spacing:3px;color:#00d084}}
.ban{{padding:14px;border-radius:12px;font-weight:700;font-size:15px;margin:16px 0;text-align:center}}
.ban.ok{{background:rgba(0,208,132,.12);color:#00d084;border:1px solid rgba(0,208,132,.3)}}
.ban.no{{background:rgba(255,59,92,.12);color:#ff3b5c;border:1px solid rgba(255,59,92,.3)}}
.row{{display:flex;gap:12px;align-items:flex-start;background:#141824;border:1px solid #232a3d;border-radius:12px;padding:12px 14px;margin-bottom:8px}}
.ic{{width:26px;height:26px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:700;flex-shrink:0}}
.ic.ok{{background:rgba(0,208,132,.16);color:#00d084}} .ic.no{{background:rgba(255,59,92,.16);color:#ff3b5c}}
.nm{{font-size:13px;font-weight:600}} .dt{{font-size:12px;color:#7683a0;margin-top:2px}}
a{{color:#4488ff;font-size:12px}} .ts{{color:#4a5470;font-size:11px;text-align:center;margin-top:14px}}
</style></head><body>
<h1>HEATSEEKER · HEALTH</h1>{banner}{rows}
<div class="ts">auto-refreshes every 15s · {h['ts']} · <a href="/">← dashboard</a></div>
</body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
