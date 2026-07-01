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
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
VIX_UUID        = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"
AGENTIC_ACCOUNT = "634079917"

_ticker_status = {"logged_in": False, "last_error": None, "last_ok": None}
_tick_count = 0

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

def _write_state(patch):
    os.makedirs(DATA_DIR, exist_ok=True)
    state = _read_state()
    state.update(patch)
    with open(GEX_STATE, "w") as f:
        json.dump(state, f, indent=2)

# ── background ticker: SPY + VIX + balance + open positions every 60s ────────
def _ticker():
    global _tick_count
    logged_in = False
    while True:
        try:
            import robin_stocks.robinhood as rh
            if not logged_in:
                u = os.getenv("RH_USERNAME", "")
                p = os.getenv("RH_PASSWORD", "")
                if u and p:
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
                    rh.login(username=u, password=p, mfa_code=mfa,
                             store_session=True, expiresIn=86400,
                             pickle_name="heatseeker")
                    logged_in = True
                    _ticker_status["logged_in"] = True
                    _ticker_status["last_error"] = None
                    log.info("RH login OK")

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

                # Intraday SPY candles (5-min bars, today only)
                try:
                    bars = rh.stocks.get_stock_historicals(
                        "SPY", interval="5minute", span="day"
                    ) or []
                    candles = []
                    for bar in bars:
                        t = bar.get("begins_at", "")
                        o = float(bar.get("open_price") or 0)
                        h = float(bar.get("high_price") or 0)
                        l = float(bar.get("low_price") or 0)
                        c = float(bar.get("close_price") or 0)
                        v = int(float(bar.get("volume") or 0))
                        if o and h and l and c:
                            candles.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
                    if candles:
                        patch["candles"] = candles
                        log.info(f"candles: {len(candles)} bars")
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
            _ticker_status["logged_in"] = False
            _ticker_status["last_error"] = str(e)
            logged_in = False

        for _ in range(60):
            time.sleep(1)

threading.Thread(target=_ticker, daemon=True).start()

# ── routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
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
    expected = os.getenv("PUSH_SECRET", "heatseeker")
    if key != expected:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True) or {}
    if data:
        _write_state(data)
    return jsonify({"ok": True})

@app.route("/api/ticker-status")
def api_ticker_status():
    return jsonify(_ticker_status)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
