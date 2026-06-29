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
VIX_UUID     = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"

_ticker_status = {"logged_in": False, "last_error": None, "last_ok": None}

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

            if logged_in:
                patch = {"market_updated": datetime.now(ET).strftime("%H:%M ET")}

                # SPY price
                q = rh.stocks.get_quotes(["SPY"], info=None) or [{}]
                raw = q[0].get("last_trade_price") or q[0].get("adjusted_previous_close")
                if raw:
                    patch["spot"] = round(float(raw), 2)

                # VIX via index UUID endpoint
                vd = rh.helper.request_get(
                    f"https://api.robinhood.com/marketdata/index-instruments/{VIX_UUID}/quotes/"
                )
                if vd and vd.get("value"):
                    vix = round(float(vd["value"]), 2)
                    patch["vix"] = vix
                    patch["vix3m"] = round(vix * 1.07, 2)
                    slope = round((patch["vix3m"] - vix) / vix, 4)
                    patch["ts_slope"] = slope
                    patch["ts_label"] = "deep_contango" if slope > 0.10 else "contango" if slope > 0 else "backwardation"

                # Balance — try multiple endpoints
                try:
                    port = rh.profiles.load_portfolio_profile() or {}
                    bal = float(port.get("withdrawable_amount") or port.get("excess_margin") or 0)
                    if not bal:
                        acc = rh.profiles.load_account_profile() or {}
                        bal = float(acc.get("cash") or acc.get("buying_power") or 0)
                    if not bal:
                        ph = rh.account.load_phoenix_account() or {}
                        bal = float((ph.get("account_buying_power") or {}).get("amount", 0) or 0)
                    if bal:
                        patch["balance"] = round(bal, 2)
                        log.info(f"balance={bal}")
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
    return jsonify({
        "spot":           g.get("spot"),
        "vix":            g.get("vix"),
        "vix3m":          g.get("vix3m"),
        "ts_label":       g.get("ts_label"),
        "ts_slope":       g.get("ts_slope"),
        "vanna_label":    g.get("vanna_label"),
        "direction":      g.get("direction"),
        "confidence":     g.get("confidence"),
        "last_action":    g.get("last_action", "Bot starting up..."),
        "market_updated": g.get("market_updated"),
        "balance":        round(float(bal), 2),
        "total_trades":   len(closed),
        "total_pnl":      round(total_pnl, 2),
        "trades":         trades[-5:],
        "open_positions": g.get("open_positions", []),
        "ts":             datetime.now(ET).strftime("%H:%M:%S ET"),
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
