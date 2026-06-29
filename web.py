#!/usr/bin/env python3
"""HEATSEEKER Vol Signal Dashboard — all signals computed in-process, no worker dependency."""
import json, os, threading, time, logging, pathlib
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import Flask, render_template, jsonify

app = Flask(__name__)
ET  = ZoneInfo("America/New_York")
log = logging.getLogger("heatseeker.web")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

DATA_DIR     = os.path.join(os.path.dirname(__file__), "data")
GEX_STATE    = os.path.join(DATA_DIR, "gex_state.json")
JOURNAL_FILE = os.path.join(DATA_DIR, "trades.json")
GOAL         = 10_000.0
VIX_UUID     = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"

_state = {
    "spot": None, "vix": None, "vix_prev": None, "vix3m": None,
    "ts_slope": None, "ts_label": None,
    "vanna_label": None, "vanna_mult": None,
    "balance": None, "last_updated": None,
    "last_action": "Starting up...",
}
_lock = threading.Lock()
_rh_ok = False


# ── Robinhood auth ────────────────────────────────────────────────────────────

def _rh_login():
    global _rh_ok
    u = os.getenv("RH_USERNAME", "")
    p = os.getenv("RH_PASSWORD", "")
    if not u or not p:
        log.warning("RH_USERNAME/RH_PASSWORD not set")
        return False
    try:
        import robin_stocks.robinhood as rh
        session_dir = pathlib.Path(os.getenv("RH_SESSION_DIR", "/data/rh_session"))
        session_dir.mkdir(parents=True, exist_ok=True)
        tokens = pathlib.Path.home() / ".tokens"
        if not tokens.exists():
            tokens.symlink_to(session_dir)
        rh.login(username=u, password=p,
                 mfa_code=os.getenv("RH_MFA_CODE") or None,
                 store_session=True, expiresIn=86400, pickle_name="heatseeker")
        _rh_ok = True
        log.info("Robinhood login OK")
        return True
    except Exception as e:
        log.warning(f"Robinhood login failed: {e}")
        _rh_ok = False
        return False


# ── Signal computation ────────────────────────────────────────────────────────

def _vix_regime(vix):
    if vix < 13:   return "ultra_low",  "Ultra-Low / Complacency", "var(--green)"
    if vix < 18:   return "normal",     "Normal / Bull",           "var(--green)"
    if vix < 25:   return "elevated",   "Elevated — Caution",      "var(--amber)"
    if vix < 35:   return "high_vol",   "High Vol / Fear",         "var(--red)"
    return            "crisis",     "CRISIS — Stay Flat",      "var(--red)"

def _vanna_flow(vix_now, vix_prev):
    if not vix_prev or vix_prev <= 0:
        return "neutral", 1.0
    pct = (vix_now - vix_prev) / vix_prev
    if pct < -0.07: return "strong_vanna_bid",  1.25
    if pct < -0.03: return "vanna_bid",         1.12
    if pct >  0.07: return "vanna_sell",        0.65
    if pct >  0.03: return "vanna_headwind",    0.80
    return "neutral", 1.0

def _ts_label(slope):
    if slope is None: return None
    if slope > 0.10:  return "deep_contango"
    if slope > 0:     return "contango"
    return "backwardation"


# ── Live fetch thread ─────────────────────────────────────────────────────────

def _fetch_once():
    import robin_stocks.robinhood as rh
    patch = {"last_updated": datetime.now(ET).isoformat()}

    # SPY price
    try:
        q = rh.stocks.get_quotes(["SPY"], info=None) or [{}]
        raw = q[0].get("last_trade_price") or q[0].get("adjusted_previous_close")
        if raw:
            patch["spot"] = round(float(raw), 2)
    except Exception as e:
        log.debug(f"SPY fetch: {e}")

    # VIX via index-instruments endpoint
    try:
        vd = rh.helper.request_get(
            f"https://api.robinhood.com/marketdata/index-instruments/{VIX_UUID}/quotes/"
        )
        if vd and vd.get("value"):
            patch["vix"] = round(float(vd["value"]), 2)
    except Exception as e:
        log.debug(f"VIX fetch: {e}")

    # VIX3M — estimate as vix*1.07 if unavailable (typical contango)
    vix_now = patch.get("vix") or _state.get("vix")
    if vix_now:
        patch.setdefault("vix3m", round(vix_now * 1.07, 2))
        vix3m = patch["vix3m"]
        slope = round((vix3m - vix_now) / vix_now, 4)
        patch["ts_slope"] = slope
        patch["ts_label"]  = _ts_label(slope)

    # Vanna flow
    vix_prev = _state.get("vix_prev")
    if vix_now:
        vl, vm = _vanna_flow(vix_now, vix_prev)
        patch["vanna_label"] = vl
        patch["vanna_mult"]  = vm
        # Roll prev at EOD (store today's VIX as tomorrow's prev)
        patch["vix_prev"] = vix_now

    # Account balance
    try:
        ph = rh.account.load_phoenix_account()
        bal = None
        if ph:
            abp = ph.get("account_buying_power") or {}
            bal = float(abp.get("amount", 0) or 0) or None
        if not bal:
            port = rh.account.load_portfolio_profile()
            if port:
                bal = float(port.get("withdrawable_amount") or port.get("excess_margin") or 0) or None
        if bal:
            patch["balance"] = round(bal, 2)
    except Exception as e:
        log.debug(f"Balance fetch: {e}")

    patch["last_action"] = f"Live — SPY ${patch.get('spot','?')}  VIX {patch.get('vix','?')}"

    with _lock:
        _state.update(patch)

    # Persist to disk
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        try:
            with open(GEX_STATE) as f:
                disk = json.load(f)
        except Exception:
            disk = {}
        disk.update(patch)
        with open(GEX_STATE, "w") as f:
            json.dump(disk, f, indent=2)
    except Exception as e:
        log.debug(f"State write: {e}")


def _live_thread():
    # Load persisted state first
    try:
        with open(GEX_STATE) as f:
            saved = json.load(f)
        with _lock:
            _state.update({k: v for k, v in saved.items() if v is not None})
    except Exception:
        pass

    # Login with retry
    while not _rh_login():
        with _lock:
            _state["last_action"] = "Waiting for Robinhood auth — retrying in 5 min"
        for _ in range(300):
            time.sleep(1)

    # Fetch loop
    while True:
        try:
            _fetch_once()
            log.info(f"Tick: SPY={_state.get('spot')} VIX={_state.get('vix')} vanna={_state.get('vanna_label')}")
        except Exception as e:
            log.warning(f"Fetch error: {e}")
            with _lock:
                _state["last_action"] = f"Fetch error: {e}"
        for _ in range(60):
            time.sleep(1)


def start_live_thread():
    t = threading.Thread(target=_live_thread, daemon=True)
    t.start()


# ── Journal helpers ───────────────────────────────────────────────────────────

def _load_journal():
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f).get("trades", [])
    except Exception:
        return []

def _journal_stats(trades):
    closed  = [t for t in trades if t.get("pnl_pct") is not None]
    recent  = closed[-20:]
    wins    = [t for t in recent if t["pnl_pct"] > 0]
    losses  = [t for t in recent if t["pnl_pct"] <= 0]
    wr      = len(wins) / len(recent) if recent else 0.5
    total   = sum(t.get("pnl_usd", 0) or 0 for t in closed)
    streak, stype = 0, None
    for t in reversed(recent):
        w = t["pnl_pct"] > 0
        if stype is None:
            stype = "win" if w else "loss"; streak = 1
        elif (stype == "win") == w:
            streak += 1
        else:
            break
    bal = None
    with _lock:
        bal = _state.get("balance")
    if not bal and closed:
        bal = closed[-1].get("balance_after")
    bal = bal or 21.64
    return {
        "total_trades": len(closed),
        "win_rate":     round(wr, 3),
        "total_pnl_usd": round(total, 2),
        "streak":       streak,
        "streak_type":  stype,
        "balance":      round(bal, 2),
        "goal_pct":     round(bal / GOAL * 100, 2),
    }


# ── Market session ────────────────────────────────────────────────────────────

def _session():
    now = datetime.now(ET)
    t = now.hour * 60 + now.minute
    if now.weekday() >= 5: return "closed"
    if t < 570:            return "pre"
    if t < 960:            return "open"
    return "after"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/state")
def api_state():
    with _lock:
        g = dict(_state)
    trades  = _load_journal()
    stats   = _journal_stats(trades)
    return jsonify({
        "gex":     g,
        "stats":   stats,
        "journal": trades[-10:],
        "session": _session(),
        "ts":      datetime.now(ET).strftime("%H:%M:%S ET"),
    })

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    start_live_thread()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

# Gunicorn entry point
start_live_thread()
