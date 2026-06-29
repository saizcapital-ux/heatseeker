#!/usr/bin/env python3
"""HEATSEEKER — minimal signal dashboard."""
import json, os, threading, time, logging, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("heatseeker")

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE    = os.path.join(DATA_DIR, "gex_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
VIX_UUID     = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"

# ── simple in-memory cache ───────────────────────────────────────────────────
_cache = {}
_lock  = threading.Lock()

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

# ── background ticker: just SPY + VIX every 60s ──────────────────────────────
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
                    rh.login(username=u, password=p,
                             mfa_code=os.getenv("RH_MFA_CODE") or None,
                             store_session=True, expiresIn=86400,
                             pickle_name="heatseeker")
                    logged_in = True
                    log.info("RH login OK")

            if logged_in:
                patch = {"market_updated": datetime.now(ET).strftime("%H:%M ET")}
                # SPY
                q = rh.stocks.get_quotes(["SPY"], info=None) or [{}]
                raw = q[0].get("last_trade_price") or q[0].get("adjusted_previous_close")
                if raw: patch["spot"] = round(float(raw), 2)
                # VIX via index endpoint
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
                # balance
                try:
                    ph = rh.account.load_phoenix_account() or {}
                    bal = float((ph.get("account_buying_power") or {}).get("amount", 0) or 0)
                    if not bal:
                        port = rh.account.load_portfolio_profile() or {}
                        bal = float(port.get("withdrawable_amount") or port.get("excess_margin") or 0)
                    if bal: patch["balance"] = round(bal, 2)
                except Exception: pass
                _write_state(patch)
                log.info(f"tick SPY={patch.get('spot')} VIX={patch.get('vix')}")
        except Exception as e:
            log.warning(f"ticker error: {e}")
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
        "spot":         g.get("spot"),
        "vix":          g.get("vix"),
        "vix3m":        g.get("vix3m"),
        "ts_label":     g.get("ts_label"),
        "ts_slope":     g.get("ts_slope"),
        "vanna_label":  g.get("vanna_label"),
        "direction":    g.get("direction"),
        "confidence":   g.get("confidence"),
        "last_action":  g.get("last_action", "Bot starting up..."),
        "market_updated": g.get("market_updated"),
        "balance":      round(float(bal), 2),
        "total_trades": len(closed),
        "total_pnl":    round(total_pnl, 2),
        "trades":       trades[-5:],
        "ts":           datetime.now(ET).strftime("%H:%M:%S ET"),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
